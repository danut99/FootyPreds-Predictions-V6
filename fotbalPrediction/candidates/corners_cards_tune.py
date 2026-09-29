"""Tuning driver for the corners_cards candidate (TUNE seasons 2223 + 2324 only).

    python -m fotbalPrediction.candidates.corners_cards_tune --grid default
    python -m fotbalPrediction.candidates.corners_cards_tune --set prior=4 --set team_half_life=365

Prints, per stat family, the mean log loss / Brier over a fixed set of representative lines
(the objective) for each season, next to the shared baseline's league-average and
team-count references read from footypreds/data/fotbal/bench/. It never touches 2425/2526.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from fotbalPrediction import benchmark, data

SEASONS = ("2223", "2324")
MARKETS = "corners,cards,bookings,sot"
BENCH = data.DATA_DIR / "bench"
OBJECTIVE = {
    "corners": [f"corners_over_{x}" for x in ("8.5", "9.5", "10.5", "11.5")],
    "team_corners": [
        f"{s}_corners_over_{x}" for s in ("home", "away") for x in ("3.5", "4.5", "5.5")
    ],
    "corners_ah": [f"corners_ah_1_{x}" for x in ("-2.5", "-1.5", "-0.5", "+0.5", "+1.5")],
    "cards": [f"cards_over_{x}" for x in ("2.5", "3.5", "4.5", "5.5")],
    "team_cards": [f"{s}_cards_over_{x}" for s in ("home", "away") for x in ("0.5", "1.5", "2.5")],
    "bookings": [f"bookings_over_{x}" for x in ("3.5", "4.5", "5.5", "6.5")],
    "sot": [f"sot_over_{x}" for x in ("6.5", "7.5", "8.5", "9.5")],
    "team_sot": [f"{s}_sot_over_{x}" for s in ("home", "away") for x in ("2.5", "3.5", "4.5")],
}
_ROWS = None


def _rows():
    global _ROWS
    if _ROWS is None:
        _ROWS = data.load_rows(list(data.MAIN_LEAGUES), data.FIRST_SEASON, SEASONS[-1])
    return _ROWS


def objective(metrics: dict) -> dict:
    """{family: {season: (mean log loss, mean brier, mean ece)}} over OBJECTIVE lines."""
    output = {}
    for family, keys in OBJECTIVE.items():
        per = {}
        for season in SEASONS + ("all",):
            found = [metrics["keys"][k][season] for k in keys if k in metrics["keys"]]
            found = [m for m in found if m.get("n")]
            if len(found) != len(keys):
                continue
            n = len(found)
            per[season] = (
                sum(m["log_loss"] for m in found) / n,
                sum(m["brier"] for m in found) / n,
                sum(m["ece"] for m in found) / n,
            )
        output[family] = per
    return output


def run(params: dict, markets: str = MARKETS, referee: bool = True, odds=None) -> dict:
    from fotbalPrediction.candidates.corners_cards import factory

    result = benchmark.run_benchmark(
        lambda: factory(**params),
        SEASONS,
        rows=_rows(),
        markets=markets,
        referee=referee,
        odds=odds,
    )
    return result["metrics"]


def _job(args):
    params, markets, referee, odds = args
    metrics = run(params, markets, referee, odds)
    return params, objective(metrics), metrics


def reference() -> dict[str, dict]:
    refs = {}
    for name in ("baseline-none", "baseline-teamcounts"):
        path = BENCH / f"{name}.json"
        if path.exists():
            refs[name] = objective(json.loads(path.read_text(encoding="utf-8"))["metrics"])
    return refs


def fmt(obj: dict) -> str:
    parts = []
    for family, per in obj.items():
        if "all" in per:
            parts.append(f"{family}={per['all'][0]:.4f}")
    return " ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", action="append", default=[], help="name=value (base params)")
    parser.add_argument("--vary", action="append", default=[], help="name=v1,v2,... (grid)")
    parser.add_argument("--markets", default=MARKETS)
    parser.add_argument("--no-referee", action="store_true")
    parser.add_argument("--odds", default=None, help="odds source passed in ctx (e.g. avg)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--save", type=Path, help="write metrics of the FIRST config to JSON")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    base = {}
    for item in args.set:
        name, value = item.split("=", 1)
        try:
            base[name] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            base[name] = value
    axes = []
    for item in args.vary:
        name, values = item.split("=", 1)
        parsed = []
        for v in values.split("|" if "|" in values else ","):
            try:
                parsed.append(ast.literal_eval(v))
            except (ValueError, SyntaxError):
                parsed.append(v)
        axes.append([(name, v) for v in parsed])
    configs = [dict(base, **dict(combo)) for combo in itertools.product(*axes)] or [base]
    for name, obj in reference().items():
        print(f"REF {name:22s} {fmt(obj)}")
    jobs = [(c, args.markets, not args.no_referee, args.odds) for c in configs]
    if len(jobs) == 1 or args.workers <= 1:
        results = map(_job, jobs)
    else:
        pool = ProcessPoolExecutor(max_workers=min(args.workers, len(jobs)))
        results = pool.map(_job, jobs)
    for index, (params, obj, metrics) in enumerate(results):
        print(f"RUN {json.dumps(params, sort_keys=True)}\n    {fmt(obj)}", flush=True)
        for family, per in obj.items():
            seasons = " ".join(
                f"{s}:{v[0]:.4f}/{v[1]:.4f}/ece{v[2]:.3f}" for s, v in per.items() if s != "all"
            )
            print(f"      {family:13s} {seasons}")
        if index == 0 and args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            args.save.write_text(
                json.dumps({"params": params, "metrics": metrics}, ensure_ascii=False),
                encoding="utf-8",
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
