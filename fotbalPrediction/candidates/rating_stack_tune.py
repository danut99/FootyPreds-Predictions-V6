"""Offline tuning for rating_stack (TUNE seasons 2223/2324 only).

Step 1 (``--collect``) walks every main-league row up to ``--last`` (default 2324) through
``RatingStackModel``'s own deferred feature builder, so each row's features are frozen before
its date is applied (identical to what the benchmark walk produces), and caches them in
``footypreds/data/fotbal/bench/rating_stack_features.npz``.

Step 2 (default) replays the per-season refit protocol offline: for every evaluated season S
the stack is trained only on samples dated before S's first match and scored on S. It
prints log loss / accuracy for 1X2, over/under 2.5, BTTS and the double-chance keys against
the harness baseline's own Dixon-Coles rates (the ``dc_log_*`` features) for every
configuration in the grid. The locked test and running season are refused.

    python -m fotbalPrediction.candidates.rating_stack_tune --collect
    python -m fotbalPrediction.candidates.rating_stack_tune --grid c=0.01,0.05,0.2
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys
import time

import numpy as np

from fotbalPrediction import data
from fotbalPrediction.candidates import rating_stack as rs

FEATURES = data.DATA_DIR / "bench" / "rating_stack_features.npz"
ALLOWED = ("2223", "2324", "2425")


def collect(last: str, first: str = data.FIRST_SEASON, path=FEATURES, **params) -> None:
    if data.season_start(last) >= data.season_start(data.LOCKED_TEST_SEASON):
        raise SystemExit("REFUZAT: sezonul de test blocat / sezonul curent nu se colectează.")
    started = time.perf_counter()
    rows = data.load_rows(list(data.MAIN_LEAGUES), first, last)
    rows.sort(key=data.sort_key)
    model = rs.RatingStackModel(passthrough=False, **params)
    for row in rows:
        model.update(row)
    model._flush()
    x = np.vstack(model.x)
    meta = np.array([(r.season, r.league, r.home, r.away) for r in model.rows], dtype=object)
    goals = np.array([(r.home_goals, r.away_goals) for r in model.rows], dtype=float)
    days = np.asarray(model.days)
    odds = np.array([rs.market_features(r.odds.get("avg")) or [math.nan] * 3 for r in model.rows])
    np.savez_compressed(
        path, x=x, meta=meta, goals=goals, days=days, odds=odds, names=np.array(rs.FEATURE_NAMES)
    )
    print(f"{len(x)} rânduri, {x.shape[1]} trăsături, {time.perf_counter() - started:.1f}s")


def _ll(p, y):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def score(cfg: dict, blob, seasons, use_odds=False, verbose=False) -> dict:
    x, meta, goals, days = blob["x"], blob["meta"], blob["goals"], blob["days"]
    names = list(blob["names"])
    drop = cfg.get("drop", ())
    cols = [i for i, n in enumerate(names) if not any(n.startswith(d) for d in drop)]
    x = x[:, cols]
    names = [names[i] for i in cols]
    if use_odds:
        x = np.hstack([x, blob["odds"]])
    season = meta[:, 0]
    output = {}
    for s in seasons:
        test = season == s
        start = days[test].min()
        train = days < start
        if use_odds:
            train &= ~np.isnan(blob["odds"][:, 0])
            test &= ~np.isnan(blob["odds"][:, 0])
        w = np.exp(-math.log(2) * (start - days[train]) / cfg.get("half_life", 1460))
        hgb = {
            "max_iter": int(cfg.get("hgb_max_iter", 200)),
            "learning_rate": cfg.get("hgb_lr", 0.05),
            "max_leaf_nodes": int(cfg.get("hgb_leaf", 15)),
            "min_samples_leaf": int(cfg.get("hgb_min_leaf", 200)),
            "l2_regularization": 1.0,
        }
        stack = rs.Stack(
            cfg.get("learner", "logit"),
            cfg.get("c", 0.1),
            hgb,
            names + (rs.ODDS_FEATURES if use_odds else []),
            bool(cfg.get("expand", 1)),
        )
        stack.fit(x[train], goals[train, 0], goals[train, 1], w)
        probs, over, rh, ra = stack.predict(x[test])
        hg, ag = goals[test, 0], goals[test, 1]
        outcome = np.where(hg > ag, 0, np.where(hg == ag, 1, 2))
        n = len(outcome)
        dc_h = np.exp(x[test][:, names.index("dc_log_home")]) if "dc_log_home" in names else None
        m = {"n": n}
        m["ll_1x2"] = float(-np.mean(np.log(np.clip(probs[np.arange(n), outcome], 1e-12, 1))))
        m["acc_1x2"] = float(np.mean(probs.argmax(1) == outcome))
        y_over = (hg + ag > 2.5).astype(float)
        m["ll_over25_clf"] = _ll(over, y_over)
        # Full market set through the matrix.
        keys = ("over15", "over25", "over35", "btts", "1X", "X2", "12")
        pk = {k: np.empty(n) for k in keys}
        tw = cfg.get("totals_weight", 1.0)
        for i in range(n):
            mk_ = rs.goal_markets(
                probs[i], over[i] if tw > 0 else None, rh[i], ra[i], cfg.get("rho", -0.12), tw
            )
            for k in keys:
                pk[k][i] = mk_[k]
        ys = {
            "over15": (hg + ag > 1.5),
            "over25": (hg + ag > 2.5),
            "over35": (hg + ag > 3.5),
            "btts": (hg > 0) & (ag > 0),
            "1X": hg >= ag,
            "X2": ag >= hg,
            "12": hg != ag,
        }
        for k in keys:
            m[f"ll_{k}"] = _ll(pk[k], ys[k].astype(float))
            sel = pk[k] >= 0.8
            m[f"sel80_{k}"] = (int(sel.sum()), float(ys[k][sel].mean()) if sel.any() else None)
        m["glm_mean_home"] = float(rh.mean())
        m["obs_home"] = float(hg.mean())
        if dc_h is not None and verbose:
            m["dc_mean_home"] = float(dc_h.mean())
        output[s] = m
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--last", default="2324")
    parser.add_argument("--seasons", default="2223,2324")
    parser.add_argument("--grid", action="append", default=[])
    parser.add_argument("--odds", action="store_true")
    parser.add_argument("--param", action="append", default=[])
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    params = {}
    for item in args.param:
        k, _, v = item.partition("=")
        params[k] = float(v)
    if args.collect:
        collect(args.last, **params)
        return 0
    seasons = [s for s in args.seasons.split(",") if s]
    if any(s not in ALLOWED for s in seasons):
        print("REFUZAT: doar 2223/2324 (tuning) și 2425 (confirmare).", file=sys.stderr)
        return 2
    blob = dict(np.load(FEATURES, allow_pickle=True))
    grid = {}
    for item in args.grid:
        k, _, v = item.partition("=")
        values = []
        for part in v.split(","):
            try:
                values.append(float(part))
            except ValueError:
                values.append(part)
        grid[k] = values
    if "drop" in grid:
        grid["drop"] = [tuple(d.split("+")) if d else () for d in grid["drop"]]
    names = list(grid)
    for combo in itertools.product(*grid.values()) if grid else [()]:
        cfg = dict(zip(names, combo))
        started = time.perf_counter()
        result = score(cfg, blob, seasons, use_odds=args.odds)
        line = [f"{cfg}"]
        for s, m in result.items():
            line.append(
                f"  {s}: 1x2 {m['ll_1x2']:.4f}/{100 * m['acc_1x2']:.1f}% "
                f"o25clf {m['ll_over25_clf']:.4f} o15 {m['ll_over15']:.4f} "
                f"o25 {m['ll_over25']:.4f} o35 {m['ll_over35']:.4f} btts {m['ll_btts']:.4f} "
                f"1X {m['ll_1X']:.4f} X2 {m['ll_X2']:.4f} 12 {m['ll_12']:.4f} "
                f"| sel80 1X {m['sel80_1X']} o15 {m['sel80_over15']} "
                f"glm_h {m['glm_mean_home']:.3f}/{m['obs_home']:.3f}"
            )
        print("\n".join(line), f"({time.perf_counter() - started:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
