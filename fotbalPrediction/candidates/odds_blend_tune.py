"""Tuning helpers for the odds_blend candidate (TUNE seasons 2223/2324 only).

    python -m fotbalPrediction.candidates.odds_blend_tune demargin [--sources avg,avg_closing]
    python -m fotbalPrediction.candidates.odds_blend_tune bench --odds avg --label blend-avg
        [--param mode=market] [--seasons 2223,2324] [--markets goals,ht] [--dump]

``bench`` runs the shared walk-forward benchmark on the candidate, keeps the model object to
print the walk-forward blend weights, adds the listed-AH-line metrics (from the records) and
writes ``footypreds/data/fotbal/bench/odds_blend-<label>.json`` and ``.txt``.

``demargin`` compares margin-removal methods on the market alone (no model): 1X2 multi-class
log loss, over/under 2.5 and the listed Asian half line (binary log loss), per season. It reads
only prices and results of the seasons it prints (1920..2324 by default); it never touches
2425 (confirm), 2526 (locked test) or 2627 (running season).
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from fotbalPrediction import data
from fotbalPrediction import markets as mk
from fotbalPrediction.candidates.odds_blend import DEMARGIN_METHODS, demargin

TUNE_LIMIT = "2324"


def demargin_report(sources: list[str], first: str, last: str) -> str:
    if data.season_start(last) > data.season_start(TUNE_LIMIT):
        raise SystemExit("Doar sezoanele de tuning (<= 2324).")
    rows = data.load_rows(data.MAIN_LEAGUES, first, last)
    lines = []
    for source in sources:
        acc = defaultdict(lambda: defaultdict(list))
        for row in rows:
            prices = row.odds.get(source)
            if not prices:
                continue
            h, a = row.home_goals, row.away_goals
            season = row.season
            if all(prices.get(k) for k in ("1", "X", "2")):
                y = 0 if h > a else (1 if h == a else 2)
                for method in DEMARGIN_METHODS:
                    p = demargin([prices["1"], prices["X"], prices["2"]], method)
                    if p is not None:
                        acc[(season, "1x2")][method].append(-math.log(max(p[y], 1e-12)))
            if prices.get("over25") and prices.get("under25"):
                y = 1 if h + a > 2 else 0
                for method in DEMARGIN_METHODS:
                    p = demargin([prices["over25"], prices["under25"]], method)
                    if p is not None:
                        acc[(season, "ou25")][method].append(
                            -math.log(max(p[0] if y else p[1], 1e-12))
                        )
            home_key = next((k for k in prices if k.startswith("ah_1_")), None)
            away_key = next((k for k in prices if k.startswith("ah_2_")), None)
            if home_key and away_key:
                line = float(home_key[5:])
                if abs(line * 2 - round(line * 2)) < 1e-9 and round(line * 2) % 2:
                    win, _ = mk.asian(h - a, line)
                    y = 1 if win > 0 else 0
                    for method in DEMARGIN_METHODS:
                        p = demargin([prices[home_key], prices[away_key]], method)
                        if p is not None:
                            acc[(season, "ah_half")][method].append(
                                -math.log(max(p[0] if y else p[1], 1e-12))
                            )
        lines.append(f"sursa {source} ({'ÎNCHIDERE' if data.is_closing(source) else 'pre'})")
        lines.append(
            f"{'sezon':<6}{'piață':<9}{'n':>7}" + "".join(f"{m:>14}" for m in DEMARGIN_METHODS)
        )
        for (season, market), per in sorted(acc.items()):
            n = len(per["proportional"])
            cells = "".join(f"{np.mean(per[m]):>14.5f}" for m in DEMARGIN_METHODS)
            lines.append(f"{season:<6}{market:<9}{n:>7}{cells}")
    return "\n".join(lines)


KEYS = ("1", "X", "2", "1X", "X2", "12", "over15", "over25", "under25", "under35", "btts")
GROUPS = ("1x2", "dc", "goals", "team_goals", "btts", "dnb", "ah", "ht", "all")


def _pct(x):
    return "-" if x is None else f"{100 * x:.1f}"


def _pick(stats):
    if not stats:
        return "-"
    text = f"{_pct(stats.get('accuracy'))}/{_pct(stats.get('coverage'))}"
    fair = stats.get("fair_odds")
    text += "/-" if fair is None else f"/{fair:.2f}"
    if stats.get("roi") is not None:
        text += f"/{100 * stats['roi']:+.1f}@{stats.get('n_priced', 0)}"
    return text


def listed_ah(records) -> dict:
    """Metrics of the priced (listed) Asian line: log loss on half lines and all lines."""
    arr = records.arrays()
    keys = records.keys
    is_ah = np.array([k.startswith("ah_") for k in keys])[arr["key"]]
    priced = is_ah & ~np.isnan(arr["price"]) & (arr["y"] >= 0)
    half = np.array([k.startswith("ah_") and abs(float(k[5:]) * 2 % 2 - 1) < 1e-9 for k in keys])[
        arr["key"]
    ]
    out = {}
    for s_index, season in enumerate(arr["seasons"] + ["all"]):
        mask = priced & ((arr["season"] == s_index) if season != "all" else True)
        block = {}
        for name, extra in (("all_lines", np.ones_like(mask)), ("half_lines", half)):
            m = mask & extra
            p = np.clip(arr["p"][m], 1e-6, 1 - 1e-6)
            y = arr["y"][m]
            block[name] = {
                "n": int(m.sum()),
                "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
                if m.any()
                else None,
            }
        sel = mask & arr["sel"]
        gains = arr["win"][sel] * (arr["price"][sel] - 1) - arr["loss"][sel]
        block["select"] = {
            "n": int(sel.sum()),
            "accuracy": float(arr["y"][sel].mean()) if sel.any() else None,
            "roi": float(gains.mean()) if sel.any() else None,
        }
        out[season] = block
    return out


def bench(args) -> None:
    import json
    from pathlib import Path

    from fotbalPrediction import benchmark as bm
    from fotbalPrediction.candidates import odds_blend

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    if any(data.season_start(s) > data.season_start(TUNE_LIMIT) for s in seasons):
        if seasons != [bm.CONFIRM_SEASON] or not args.confirm:
            raise SystemExit("Tuning doar pe 2223/2324; 2425 doar cu --confirm, o dată.")
    params = bm._parse_params(args.param)
    holder = {}

    def factory():
        holder["model"] = odds_blend.factory(**params)
        return holder["model"]

    result = bm.run_benchmark(
        factory,
        seasons,
        args.leagues,
        odds=args.odds,
        markets=args.markets,
        return_records=True,
    )
    metrics = result["metrics"]
    model = holder["model"]
    lines = [f"odds_blend {args.label}: odds={args.odds} params={params}"]
    q = metrics["questions"].get("1x2", {})
    for season, m in q.items():
        lines.append(
            f"  1x2 {season}: ll {m['log_loss']:.4f} brier {m['brier']:.4f} "
            f"acc {_pct(m['accuracy'])} ece {m['ece']:.3f} n {m['n']}"
        )
    for key in KEYS:
        per = metrics["keys"].get(key, {})
        for season, m in per.items():
            if not m.get("n"):
                continue
            lines.append(
                f"  {key:<8}{season}: ll {m['log_loss']:.4f} acc {_pct(m['accuracy'])} "
                f"ece {m['ece']:.3f} cov80 {_pct(m['cov_80']['coverage'])} "
                f"sel {_pick(m['select'])} high {_pick(m['select_high'])}"
            )
    ah = listed_ah(result["records"])
    for season, block in ah.items():
        lines.append(
            f"  AH listat {season}: ll(all) {block['all_lines']['log_loss']} "
            f"n {block['all_lines']['n']}, ll(half) {block['half_lines']['log_loss']} "
            f"n {block['half_lines']['n']}; select {block['select']}"
        )
    for group in GROUPS:
        per = metrics["groups"].get(group, {})
        for season, m in per.items():
            sel, high = m.get("select", {}), m.get("select_high", {})
            lines.append(
                f"  grup {group:<11}{season}: sel {_pick(sel)} "
                f"ppm {sel.get('picks_per_match', 0):.3f}"
                f" | high {_pick(high)} ppm {high.get('picks_per_match', 0):.3f}"
            )
    history = model.history
    lines.append(f"  refit-uri: {len(history)}")
    for season in seasons:
        start = data.season_start(season)
        from datetime import date

        day = date(start, 8, 1).toordinal()
        before = [h for h in history if h["day"] <= day]
        if before:
            lines.append(f"  ponderi la 1 aug {start}: {before[-1]}")
    if history:
        lines.append(f"  ultimele ponderi: {history[-1]}")
    text = "\n".join(lines)
    print(text)
    out_dir = Path(data.DATA_DIR) / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metrics": metrics,
        "meta": {**result["meta"], "model": "odds_blend", "params": params, "label": args.label},
        "listed_ah": ah,
        "weights": history,
    }
    (out_dir / f"odds_blend-{args.label}.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False, default=float), encoding="utf-8"
    )
    (out_dir / f"odds_blend-{args.label}.txt").write_text(text, encoding="utf-8")
    if args.dump:
        shadow = model.shadow
        arrays = shadow.arrays(0, len(shadow))
        np.savez_compressed(
            out_dir / f"odds_blend-{args.label}-shadow.npz",
            **arrays,
            final_odds=np.stack(shadow.final_odds),
            final_none=np.stack(shadow.final_none),
            outcomes=np.stack(shadow.outcomes),
        )


def _season_of(day: np.ndarray) -> np.ndarray:
    from datetime import date

    years = np.array([date.fromordinal(int(d)).year for d in day])
    months = np.array([date.fromordinal(int(d)).month for d in day])
    start = np.where(months >= 7, years, years - 1)
    return np.array([f"{y % 100:02d}{(y + 1) % 100:02d}" for y in start])


def _ll_multi(z: np.ndarray, y: np.ndarray) -> float:
    z = z - z.max(axis=1, keepdims=True)
    logp = z - np.log(np.exp(z).sum(axis=1, keepdims=True))
    return float(-logp[np.arange(len(y)), y].mean())


def _ll_binary(z: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean(np.logaddexp(0.0, z) - y * z))


def weights_report(label: str, noises: list[float], seeds: int = 3) -> str:
    """Fixed-weight pools vs the walk-forward learned blend on the tune seasons, with log-normal
    noise on the prices seen at prediction time (noise on log implied probabilities, then
    renormalised). Learned weights: fitted on shadow records before 2223 only (frozen)."""
    from fotbalPrediction.candidates.odds_blend import _fit_binary, _fit_multinomial

    path = Path(data.DATA_DIR) / "bench" / f"odds_blend-{label}-shadow.npz"
    arr = dict(np.load(path))
    season = _season_of(arr["day"])
    lpm = np.stack([arr["lpm1"], arr["lpmX"], arr["lpm2"]], axis=1)
    lpq = np.stack([arr["lpq1"], arr["lpqX"], arr["lpq2"]], axis=1)
    y = arr["y1x2"].astype(int)
    ok = np.isfinite(lpq).all(axis=1) & np.isfinite(arr["loq"])
    train = ok & (season < "2223")
    tune = ok & np.isin(season, ["2223", "2324"])
    theta = _fit_multinomial([lpm[train], lpq[train]], y[train], np.array([0.2, 0.8, 0, 0]), 1e-4)
    theta_o = _fit_binary(
        [arr["lom"][train], arr["loq"][train]], arr["yover"][train], np.array([0.2, 0.8, 0]), 1e-4
    )
    lines = [
        f"{label}: învățat pe {int(train.sum())} meciuri < 2223; evaluat pe {int(tune.sum())}",
        f"  1X2 învățat a(model)={theta[0]:.3f} b(piață)={theta[1]:.3f} "
        f"c=({theta[2]:.3f},{theta[3]:.3f}); O/U a={theta_o[0]:.3f} b={theta_o[1]:.3f} "
        f"c={theta_o[2]:.3f}",
        f"{'zgomot':>7}{'pondere':>9}{'ll 1X2':>10}{'ll O/U2.5':>11}",
    ]
    lm, lq, yy = lpm[tune], lpq[tune], y[tune]
    om, oq, yo = arr["lom"][tune], arr["loq"][tune], arr["yover"][tune]
    for noise in noises:
        rows = {}
        for seed in range(seeds if noise > 0 else 1):
            rng = np.random.default_rng(seed)
            nq = lq + rng.normal(0, noise, lq.shape)
            nq = nq - np.log(np.exp(nq).sum(axis=1, keepdims=True))
            # Two-way: noise on both implied probabilities, renormalised (logit shift).
            e1, e2 = rng.normal(0, noise, len(oq)), rng.normal(0, noise, len(oq))
            p_over = 1 / (1 + np.exp(-oq))
            a, b = np.log(p_over) + e1, np.log(1 - p_over) + e2
            noq = a - b
            for w in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                rows.setdefault(f"{w:.1f}", []).append(
                    (
                        _ll_multi((1 - w) * lm + w * nq, yy),
                        _ll_binary((1 - w) * om + w * noq, yo),
                    )
                )
            z = theta[0] * lm + theta[1] * nq
            z[:, 0] += theta[2]
            z[:, 1] += theta[3]
            rows.setdefault("învățat", []).append(
                (_ll_multi(z, yy), _ll_binary(theta_o[0] * om + theta_o[1] * noq + theta_o[2], yo))
            )
        for name, values in rows.items():
            v = np.mean(values, axis=0)
            lines.append(f"{noise:>7.2f}{name:>9}{v[0]:>10.4f}{v[1]:>11.4f}")
    return chr(10).join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fotbalPrediction.candidates.odds_blend_tune")
    sub = parser.add_subparsers(dest="command", required=True)
    dm = sub.add_parser("demargin")
    dm.add_argument("--sources", default="avg,avg_closing,max,b365")
    dm.add_argument("--first", default="1920")
    dm.add_argument("--last", default="2324")
    wp = sub.add_parser("weights")
    wp.add_argument("--label", default="blend-avg")
    wp.add_argument("--noise", default="0,0.02,0.04,0.06")
    bp = sub.add_parser("bench")
    bp.add_argument("--label", required=True)
    bp.add_argument("--odds", default="avg")
    bp.add_argument("--seasons", default="2223,2324")
    bp.add_argument("--leagues", default="main")
    bp.add_argument("--markets", default="all")
    bp.add_argument("--param", action="append", default=[])
    bp.add_argument("--confirm", action="store_true", help="rulează confirmarea 2425 (o dată)")
    bp.add_argument("--dump", action="store_true", help="salvează înregistrările shadow (.npz)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "demargin":
        print(demargin_report(args.sources.split(","), args.first, args.last))
    elif args.command == "weights":
        print(weights_report(args.label, [float(x) for x in args.noise.split(",")]))
    elif args.command == "bench":
        bench(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
