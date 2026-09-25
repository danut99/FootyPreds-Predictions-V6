"""Parameter search on the VALIDATION season only.

python -m footypreds.evaluation.tune            # ratings/form/H2H coordinate search + 1X2 blend
python -m footypreds.evaluation.tune --totals   # goals calibration (Platt map, O/U pool weight)
                                                # and recommend.MAX_VALUE, 16 leagues when present
"""

import argparse
import json
from dataclasses import replace

from footypreds.engine import PARAMS
from footypreds.evaluation.dataset import load
from footypreds.evaluation.run import PROTOCOL, evaluate

GRID = {
    "half_life": [90, 120, 180, 270, 365],
    "prior": [2.0, 4.0, 8.0],
    "rho": [0.0, -0.04, -0.08, -0.12],
    "form_weight": [0.0, 0.15, 0.3, 0.5],
    "form_window": [4, 6, 10],
    "form_prior": [2.0, 3.0, 6.0],
    "h2h_weight": [0.0, 0.1, 0.2],
}


def score(report):
    goals = report["goals"]["model"]
    return (
        report["one_x_two"]["model"]["log_loss"]
        + goals["over25"]["log_loss"]
        + goals["btts"]["log_loss"]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--totals", action="store_true", help="fit the goals calibration")
    parser.add_argument("--top5", action="store_true", help="--totals on the 5 benchmark leagues")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.totals:
        from footypreds.evaluation.calibration import tune_totals

        result = tune_totals(protocol, top5=args.top5)
        print(json.dumps({k: v for k, v in result.items() if k != "value_table"}, default=str))
        for row in result["value_table"]:
            print("value", json.dumps(row), flush=True)
        keys = ("totals_intercept", "totals_slope", "totals_market_weight", "max_value")
        print("FINAL", json.dumps({k: result[k] for k in keys}, default=str), flush=True)
        return
    holdout = protocol["validation_seasons"][-1]
    records, _ = load()

    def run(params, market=False):
        report, _ = evaluate(
            records, protocol, holdout=holdout, params=params, baseline=False, market=market
        )
        return report

    best = PARAMS
    best_score = score(run(best))
    print("start", round(best_score, 5), flush=True)
    for _ in range(2):
        for name, values in GRID.items():
            for value in values:
                if getattr(best, name) == value:
                    continue
                candidate = replace(best, **{name: value})
                current = score(run(candidate))
                print(name, value, round(current, 5), flush=True)
                if current < best_score - 1e-5:
                    best, best_score = candidate, current
            print("best", name, getattr(best, name), round(best_score, 5), flush=True)
    market = {}
    for weight in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        report = run(replace(best, market_weight=weight), market=True)
        market[weight] = report["one_x_two"]["model_market"]["log_loss"]
        print("market_weight", weight, round(market[weight], 5), flush=True)
    best = replace(best, market_weight=min(market, key=market.get))
    print("FINAL", json.dumps(best.__dict__), flush=True)


if __name__ == "__main__":
    main()
