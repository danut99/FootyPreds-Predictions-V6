"""Tuning helper for ``goals_model`` (TUNE seasons 2223/2324 only; refuses anything else).

Objective: mean of the multi-class log losses of 1X2 and HT 1X2 plus the binary log losses of
over 2.5 and BTTS, over both tune seasons (lower is better). Runs configurations in parallel
through the shared benchmark (``run_benchmark`` with pre-loaded rows).

    python -m fotbalPrediction.candidates.goals_model_tune --grid w_sot=0,0.2,0.4 \
        --grid half_life=360,540 [--set prior=6] [--first-season 1718] [--workers 6]
    python -m fotbalPrediction.candidates.goals_model_tune --descent half_life,prior,rho
"""

from __future__ import annotations

import argparse
import ast
import inspect
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor

from fotbalPrediction import benchmark as bm
from fotbalPrediction import data
from fotbalPrediction.candidates import goals_model

TUNE = ("2223", "2324")
KEYS = "1,X,2,over25,under25,btts,no_btts,ht_1,ht_X,ht_2,over15,under35,1X,X2"
STEPS = {
    "half_life": (0.8, 1.25),
    "max_days": (0.75, 1.33),
    "prior": (0.7, 1.4),
    "rho": (-0.03, 0.03),
    "draw_boost": (-0.03, 0.03),
    "w_sot": (-0.1, 0.1),
    "w_shots": (-0.05, 0.05),
    "c_att_up": (-0.05, 0.05),
    "c_def_up": (-0.05, 0.05),
    "c_att_down": (-0.05, 0.05),
    "c_def_down": (-0.05, 0.05),
    "beta": (-0.15, 0.15),
    "ht_rho": (-0.05, 0.05),
    "ht_draw_boost": (-0.03, 0.03),
    "total_half_life": (0.8, 1.25),
    "total_prior": (0.7, 1.4),
    "total_w_sot": (-0.1, 0.1),
    "total_w_shots": (-0.05, 0.05),
    "market_weight": (-0.1, 0.1),
    "totals_market_weight": (-0.1, 0.1),
}
MULTIPLICATIVE = {"half_life", "max_days", "prior"}

_ROWS = None


def _rows(first_season: str):
    global _ROWS
    if _ROWS is None:
        leagues = sorted(data.MAIN_LEAGUES, key=data.ALL_LEAGUES.index)
        _ROWS = data.load_rows(leagues, first_season, TUNE[-1])
    return _ROWS


def evaluate(params: dict, first_season: str = "1718", odds: str | None = None) -> dict:
    rows = _rows(first_season)
    result = bm.run_benchmark(
        lambda: goals_model.factory(**params),
        TUNE,
        first_season=first_season,
        rows=rows,
        markets=KEYS,
        odds=odds,
    )
    metrics = result["metrics"]
    q, k = metrics["questions"], metrics["keys"]
    out = {
        "1x2": q["1x2"]["all"]["log_loss"],
        "ht": q["ht_1x2"]["all"]["log_loss"] if "ht_1x2" in q else None,
        "o25": k["over25"]["all"]["log_loss"],
        "btts": k["btts"]["all"]["log_loss"],
        "acc1x2": q["1x2"]["all"]["accuracy"],
        "ece_x": k["X"]["all"]["ece"],
        "o15_sel": k["over15"]["all"]["select"],
        "1X_sel": k["1X"]["all"]["select"],
    }
    parts = [out["1x2"], out["o25"], out["btts"]] + ([out["ht"]] if out["ht"] else [])
    out["score"] = sum(parts) / len(parts)
    return out


def _run(args):
    params, first_season, odds = args
    return params, evaluate(params, first_season, odds)


def _line(params: dict, out: dict) -> str:
    return (
        f"{out['score']:.5f}  1x2 {out['1x2']:.5f} ht {out['ht'] or 0:.5f} "
        f"o25 {out['o25']:.5f} btts {out['btts']:.5f} acc {100 * out['acc1x2']:.1f} "
        f"eceX {out['ece_x']:.3f}  {json.dumps(params)}"
    )


def _value(text: str):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def run_many(configs, first_season, odds, workers):
    results = []
    with ProcessPoolExecutor(workers) as pool:
        for params, out in pool.map(_run, [(c, first_season, odds) for c in configs]):
            print(_line(params, out), flush=True)
            results.append((out["score"], params, out))
    return sorted(results, key=lambda item: item[0])


def descent(base: dict, names: list[str], first_season, odds, workers, rounds: int = 8):
    """Parallel coordinate search: every +/- step of every parameter per round, keep the best
    single move, stop when no move improves the objective."""
    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(goals_model.GoalsModel).parameters.items()
    }
    best_score = run_many([base], first_season, odds, 1)[0][0]
    for _ in range(rounds):
        configs = []
        for name in names:
            value = base.get(name)
            if value is None:
                value = defaults.get(name)
            if value is None and name.startswith("total_"):
                value = base.get(name[6:], defaults[name[6:]])
            low, high = STEPS[name]
            if name in MULTIPLICATIVE or name.endswith(("half_life", "prior", "max_days")):
                candidates = [round(value * low, 3), round(value * high, 3)]
            else:
                candidates = [round(value + low, 4), round(value + high, 4)]
            configs += [{**base, name: c} for c in candidates]
        score, params, _ = run_many(configs, first_season, odds, workers)[0]
        if score >= best_score - 1e-5:
            break
        best_score, base = score, params
        print(f"-> {best_score:.5f} {json.dumps(base)}", flush=True)
    print("BEST", best_score, json.dumps(base))
    return base


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid", action="append", default=[])
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--descent")
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--first-season", default="1718")
    parser.add_argument("--odds", default=None)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)
    base = {}
    for item in args.set:
        name, _, text = item.partition("=")
        base[name] = _value(text)
    if args.descent:
        descent(
            base, args.descent.split(","), args.first_season, args.odds, args.workers, args.rounds
        )
        return 0
    axes = []
    for item in args.grid:
        name, _, text = item.partition("=")
        axes.append([(name, _value(v)) for v in text.split(",")])
    configs = [{**base, **dict(combo)} for combo in itertools.product(*axes)] or [base]
    ranked = run_many(configs, args.first_season, args.odds, args.workers)
    print("BEST", _line(ranked[0][1], ranked[0][2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
