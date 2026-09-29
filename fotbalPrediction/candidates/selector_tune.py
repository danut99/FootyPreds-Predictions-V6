"""Tuning driver for the selector candidate (TUNE seasons only: choose on 2223, check 2324).

Runs ``fotbalPrediction.benchmark.run_benchmark`` for several parameter sets on rows loaded
once and prints, per market key and group, the select / select_high figures (accuracy,
coverage, mean fair odds, ROI at real prices when they exist) next to the baseline.

    python -m fotbalPrediction.candidates.selector_tune --seasons 2223 \
        --config "{}" --config "{'z': 1.5}" [--odds avg] [--out footypreds/data/fotbal/bench]

The locked test season is refused by the benchmark itself; this driver also refuses 2425 so
that the single CONFIRM run goes through the benchmark CLI.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from pathlib import Path

import numpy as np

from fotbalPrediction import benchmark as bm
from fotbalPrediction import data
from fotbalPrediction import markets as mk

REPORT_KEYS = (
    "1",
    "2",
    "1X",
    "X2",
    "12",
    "dnb_1",
    "dnb_2",
    "over05",
    "over15",
    "under25",
    "under35",
    "under45",
    "home_over05",
    "away_over05",
    "home_under_2.5",
    "away_under_2.5",
    "btts",
    "no_btts",
    "ah_1_-0.5",
    "ah_1_+0.5",
    "ah_2_+0.5",
    "ah_1_+1.5",
    "ah_2_+1.5",
    "ah_2_+1",
    "ht_over05",
    "ht_under15",
    "ht_under25",
    "corners_over_7.5",
    "corners_over_8.5",
    "corners_under_12.5",
    "corners_ah_1_+1.5",
    "corners_ah_2_+1.5",
    "cards_over_1.5",
    "cards_over_2.5",
    "cards_under_5.5",
    "cards_under_6.5",
    "bookings_under_6.5",
    "sot_over_5.5",
    "sot_over_6.5",
    "home_sot_over_1.5",
)


def _cell(stats: dict | None) -> str:
    if not stats:
        return "-"
    acc = stats.get("accuracy")
    text = f"{'-' if acc is None else f'{100 * acc:.1f}'}/{100 * stats['coverage']:.1f}"
    if stats.get("fair_odds"):
        text += f"/{stats['fair_odds']:.2f}"
    if stats.get("roi") is not None and stats.get("n_priced", 0) >= 30:
        text += f"/{100 * stats['roi']:+.1f}@{stats['n_priced']}"
    return text


def summary(metrics: dict, seasons: list[str], keys=REPORT_KEYS) -> str:
    lines = []
    for season in seasons:
        lines.append(f"== {season}: groups (select | select_high) acc/cov/fair[/roi@n]")
        for group, per_season in metrics["groups"].items():
            m = per_season.get(season)
            if not m:
                continue
            ppm = m["select"].get("picks_per_match", 0.0)
            lines.append(
                f"  {group:<13}{_cell(m['select']):>28}{_cell(m['select_high']):>28}"
                f"  picks/match {ppm:.2f}"
            )
        lines.append(f"== {season}: keys  logloss ece | select | select_high")
        for key in keys:
            m = (metrics["keys"].get(key) or {}).get(season)
            if not m or not m.get("n"):
                continue
            lines.append(
                f"  {key:<20}{m['log_loss']:>8.4f}{m['ece']:>7.3f}"
                f"{_cell(m['select']):>28}{_cell(m['select_high']):>28}"
            )
        worst = []
        for key, per_season in metrics["keys"].items():
            m = per_season.get(season)
            if not m or not m.get("n"):
                continue
            for mode in ("select", "select_high"):
                s = m[mode]
                if s["n_selected"] >= 50 and s["accuracy"] is not None:
                    worst.append((s["accuracy"], key, mode, s["n_selected"]))
        worst.sort()
        for target, mode in ((0.80, "select"), (0.85, "select_high")):
            bad = [w for w in worst if w[2] == mode and w[0] < target]
            picks = sum(w[3] for w in worst if w[2] == mode)
            lines.append(
                f"  {mode}: keys with >=50 picks {sum(w[2] == mode for w in worst)}, "
                f"below {target:.2f}: {len(bad)} ({sum(w[3] for w in bad)} of {picks} picks)"
            )
        by_group: dict[str, list] = {}
        for key, per_season in metrics["keys"].items():
            m = per_season.get(season)
            if m and m.get("n") and mk.CATALOGUE[key].question != "cs":
                by_group.setdefault(mk.CATALOGUE[key].group, []).append(m)
        lines.append(
            "  mean logloss/ece per group: "
            + ", ".join(
                f"{g} {np.mean([m['log_loss'] for m in ms]):.4f}/"
                f"{np.mean([m['ece'] for m in ms]):.3f}"
                for g, ms in by_group.items()
            )
        )
        lines.append(
            "  weakest keys (>=50 picks): "
            + ", ".join(f"{k}:{mode[-4:]} {100 * a:.1f}%@{n}" for a, k, mode, n in worst[:8])
        )
    return "\n".join(lines)


def disable_power_throttling() -> bool:
    """Windows 11 throttles (EcoQoS) processes started from a background console; long tuning
    runs then crawl. Opt this process out (no effect elsewhere)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class State(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetProcessInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetProcessInformation.restype = wintypes.BOOL
    state = State(1, 1, 0)  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED off
    process_power_throttling = 4
    return bool(
        kernel.SetProcessInformation(
            kernel.GetCurrentProcess(),
            process_power_throttling,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
    )


# --------------------------------------------------------------------------- stream replay


def dump_stream(seasons, params: dict, path: Path, odds: str = "none", rows=None) -> dict:
    """One benchmark run with ``log_stream=True``; saves the issued stream to ``path`` (.npz)."""
    from fotbalPrediction.candidates import selector as sel

    holder = []

    def factory():
        model = sel.factory(**{**params, "log_stream": True})
        holder.append(model)
        return model

    result = bm.run_benchmark(factory, seasons, rows=rows, odds=odds)
    model = holder[0]
    if model.buffer:
        model._flush()
    log = model.stream_log
    np.savez_compressed(
        path,
        day=np.array([e[0] for e in log], dtype=np.int32),
        season=np.array([e[1] for e in log]),
        league=np.array([e[2] for e in log], dtype=np.int16),
        bucket=np.array([e[3] for e in log], dtype=np.int8),
        evaluated=np.array([e[4] for e in log], dtype=bool),
        issued=np.stack([e[5] for e in log]),
        y=np.stack([e[6] for e in log]),
    )
    return result


def replay(stream: dict, **params) -> dict:
    """Re-runs the selection rule of ``SelectorModel`` on a logged issued stream.

    Returns per evaluated season: {key: [predicted, selected, hits, sum 1/p]} for both modes.
    Calibration is NOT refitted (the stream already holds the issued probabilities).
    """
    from fotbalPrediction.candidates import selector as sel

    model = sel.factory(**{**params, "calibrate": False})
    day, league, bucket = stream["day"], stream["league"], stream["bucket"]
    issued, y, evaluated = stream["issued"].astype(float), stream["y"], stream["evaluated"]
    season = stream["season"]
    nan_raw = np.full(sel.K, np.nan)
    output: dict = {}
    boundaries = np.nonzero(np.diff(day))[0] + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(day)]])
    for start, end in zip(starts, ends):
        today = int(day[start])
        for i in range(start, end):
            if not evaluated[i]:
                continue
            p, yy = issued[i], y[i]
            known = ~np.isnan(p) & (yy >= 0)
            b, lg = int(bucket[i]), int(league[i])
            if not model.buckets and b == 1:
                b = 0
            block = output.setdefault(season[i], np.zeros((2, sel.K, 4)))
            block[:, known, 0] += 1
            matches = output.setdefault(("matches", season[i]), np.zeros(1))
            matches[0] += 1
            for mode in (0, 1):
                chosen = known & model.choose(mode, p, b, lg)
                block[mode, chosen, 1] += 1
                block[mode, chosen, 2] += yy[chosen]
                block[mode, chosen, 3] += 1.0 / np.maximum(p[chosen], 1e-9)
                if chosen.any():  # one pick per match: the longest fair odds among the picks
                    k = int(np.nonzero(chosen)[0][np.argmin(p[chosen])])
                    one = output.setdefault(("one", season[i]), np.zeros((2, sel.K, 4)))
                    one[mode, k, 1] += 1
                    one[mode, k, 2] += yy[k]
                    one[mode, k, 3] += 1.0 / max(p[k], 1e-9)
        weight = model._weight(today)
        for i in range(start, end):
            b = int(bucket[i])
            if not model.buckets and b == 1:
                b = 0
            model._add(y[i], nan_raw, issued[i], b, int(league[i]), weight)
        if model.last_refit is None or today - model.last_refit >= model.refit_days:
            model._fit_thresholds(1.0 / model._weight(today))
            model.last_refit = today
    return output


def replay_report(output: dict, targets=(0.80, 0.85), min_picks: int = 50) -> str:
    from fotbalPrediction.candidates import selector as sel

    lines = []
    for season, block in output.items():
        if isinstance(season, tuple):
            continue
        one = output.get(("one", season))
        n_matches = output.get(("matches", season), [0])[0]
        if one is not None:
            for mode in (0, 1):
                _, chosen, hits, inv = one[mode].T
                n = chosen.sum()
                groups: dict[str, list] = {}
                for k in np.nonzero(chosen)[0]:
                    item = groups.setdefault(mk.CATALOGUE[sel.KEYS[k]].group, [0, 0])
                    item[0] += chosen[k]
                    item[1] += hits[k]
                lines.append(
                    f"{season} one-per-match {'select' if mode == 0 else 'high  '}: "
                    f"{int(n)} of {int(n_matches)} matches acc {100 * hits.sum() / max(n, 1):.1f}"
                    f" fair {inv.sum() / max(n, 1):.3f} | "
                    + " ".join(
                        f"{g}:{int(c)}/{100 * h / c:.0f}%"
                        for g, (c, h) in sorted(groups.items(), key=lambda t: -t[1][0])
                    )
                )
        for mode, target in enumerate(targets):
            pred, chosen, hits, inv = block[mode].T
            ok = chosen >= min_picks
            acc = np.where(chosen > 0, hits / np.maximum(chosen, 1), np.nan)
            bad = ok & (acc < target)
            groups = {}
            for k in range(sel.K):
                if chosen[k]:
                    g = mk.CATALOGUE[sel.KEYS[k]].group
                    item = groups.setdefault(g, [0, 0, 0.0])
                    item[0] += chosen[k]
                    item[1] += hits[k]
                    item[2] += inv[k]
            lines.append(
                f"{season} {'select' if mode == 0 else 'high  '}: picks {int(chosen.sum())} "
                f"acc {100 * hits.sum() / max(chosen.sum(), 1):.1f} "
                f"fair {inv.sum() / max(chosen.sum(), 1):.3f} | keys>={min_picks}: {int(ok.sum())}"
                f" below {target:.2f}: {int(bad.sum())} ({int(chosen[bad].sum())} picks; worst "
                + ", ".join(
                    f"{sel.KEYS[k]} {100 * acc[k]:.1f}@{int(chosen[k])}"
                    for k in np.argsort(np.where(ok, acc, 9))[:4]
                    if ok[k]
                )
                + ")"
            )
            lines.append(
                "    "
                + " ".join(
                    f"{g}:{100 * h / n:.1f}/{int(n)}/{f / n:.2f}" for g, (n, h, f) in groups.items()
                )
            )
    return "\n".join(lines)


def leak_check(leagues=("E0", "E1"), first_season: str = "1920", season: str = "2223") -> bool:
    """Altering every result of one date must not change any prediction or pick made on or
    before that date (and must change later ones). Uses daily refits to be strict."""
    import dataclasses

    from fotbalPrediction.candidates import selector as sel

    rows = data.load_rows(list(leagues), first_season, season)
    dates = sorted({r.date for r in rows if r.season == season})
    target = dates[len(dates) // 8]

    def altered(items):
        output = []
        for row in items:
            if row.date == target:
                row = dataclasses.replace(
                    row, home_goals=row.away_goals + 3, away_goals=0, home_corners=30, home_yellow=9
                )
            output.append(row)
        return output

    def run(items):
        captured = {}

        class Spy(sel.SelectorModel):
            def predict(self, ctx):
                output = super().predict(ctx)
                picks = frozenset(k for k, p in output.items() if self.select(ctx, k, p))
                captured[ctx.match_id] = (ctx.date, output, picks)
                return output

        bm.run_benchmark(
            lambda: Spy(refit_days=1), [season], list(leagues), first_season, rows=items
        )
        return captured

    before, after = run(rows), run(altered(rows))
    same = all(before[m] == after[m] for m in before if before[m][0] <= target)
    changed = any(before[m] != after[m] for m in before if before[m][0] > target)
    print(f"leak check {target}: unchanged on/before: {same}, changed after: {changed}")
    return same and changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", default="2223")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--names", default="")
    parser.add_argument("--odds", default="none")
    parser.add_argument("--markets", default="all")
    parser.add_argument("--leagues", default="main")
    parser.add_argument("--out", type=Path, default=data.DATA_DIR / "bench" / "selector")
    parser.add_argument("--model", default="fotbalPrediction.candidates.selector:factory")
    parser.add_argument("--dump", action="store_true", help="also save the issued stream (.npz)")
    parser.add_argument("--replay", type=Path, help="replay the selection rule on a saved stream")
    parser.add_argument("--leak-check", action="store_true")
    args = parser.parse_args(argv)
    disable_power_throttling()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if args.leak_check:
        return 0 if leak_check() else 1
    if args.replay:
        saved = dict(np.load(args.replay))
        for text in args.config or ["{}"]:
            print(f"\n##### replay {text}")
            print(replay_report(replay(saved, **ast.literal_eval(text))))
        return 0
    seasons = bm.check_seasons(args.seasons.split(","), locked_test=False)
    if data.CONFIRM_SEASON in seasons:
        print("REFUZAT: confirmarea 2425 se rulează o singură dată, după înghețarea regulii.")
        return 2
    leagues = bm.resolve_leagues(args.leagues)
    rows = data.load_rows(
        sorted(set(leagues) | set(data.MAIN_LEAGUES), key=data.ALL_LEAGUES.index),
        data.FIRST_SEASON,
        seasons[-1],
    )
    factory = bm.load_factory(args.model)
    names = args.names.split(",") if args.names else []
    args.out.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(args.config or ["{}"]):
        params = ast.literal_eval(text)
        name = names[i] if i < len(names) else f"cfg{i}"
        path = args.out / f"{name}-{'-'.join(seasons)}-{args.odds}.json"
        started = time.perf_counter()
        if args.dump:
            result = dump_stream(seasons, params, path.with_suffix(".npz"), args.odds, rows)
        else:
            result = bm.run_benchmark(
                lambda params=params: factory(**params),
                seasons,
                leagues,
                rows=rows,
                markets=args.markets,
                odds=args.odds,
            )
        took = time.perf_counter() - started
        print(f"\n##### {name}: {params}  odds={args.odds}  ({took:.0f}s)")
        print(summary(result["metrics"], seasons))
        payload = {"metrics": result["metrics"], "meta": {**result["meta"], "params": params}}
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
