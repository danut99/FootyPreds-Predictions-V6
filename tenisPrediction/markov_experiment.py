"""Experiment izolat: bază de serviciu specifică jucătorilor în modelul Markov.

Nu este importat de aplicație. Rulează:
    python -m tenisPrediction.markov_experiment --test-year 2025
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from footypreds.evaluation.tennis_data import RAW_DIR, load_tennis_matches
from footypreds.evaluation.tennis_eval import games_total
from footypreds.sports import common, tennis

from .model import CompactTennisModel


def metrics(rows, model, personalized, step=5):
    loss = error = bias = 0.0
    used = 0
    for match in rows[::step]:
        actual = games_total(match)
        priced = common.two_way(match.odds, "1", "2")
        if actual is None or priced is None or match.finish_type or match.status != "finished":
            continue
        sets = tennis.best_of(match)
        default = tennis.PARAMS.serve_women if tennis.is_women(match) else tennis.PARAMS.serve_men
        surface = tennis.surface_of(match) or "hard"
        base = (
            model.serve_base(match.home, match.away, surface, default) if personalized else default
        )
        totals, _ = tennis.games_totals(priced[0], sets, base, tennis.PARAMS.set_spread)
        mean = sum(games * probability for games, probability in totals.items())
        loss -= math.log(max(1e-12, totals.get(actual, 0.0)))
        error += abs(mean - actual)
        bias += mean - actual
        used += 1
    return {
        "matches": used,
        "log_loss": round(loss / used, 6),
        "mae": round(error / used, 6),
        "bias": round(bias / used, 6),
    }


def experiment(data_dir: Path, test_year: int, step=5):
    model = CompactTennisModel.train_dataset(data_dir, test_year - 1, seasons=4)
    rows = load_tennis_matches([test_year], directory=RAW_DIR)
    baseline = metrics(rows, model, personalized=False, step=step)
    personalized = metrics(rows, model, personalized=True, step=step)
    return {
        "test_year": test_year,
        "sample_step": step,
        "baseline": baseline,
        "player_service_markov": personalized,
        "delta": {
            key: round(personalized[key] - baseline[key], 6) for key in ("log_loss", "mae", "bias")
        },
        "accepted": (
            personalized["log_loss"] < baseline["log_loss"]
            and personalized["mae"] < baseline["mae"]
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "tml-data")
    parser.add_argument("--test-year", type=int, default=2025)
    parser.add_argument("--step", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(experiment(args.data_dir, args.test_year, args.step), indent=2))


if __name__ == "__main__":
    main()
