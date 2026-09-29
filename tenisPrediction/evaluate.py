"""Benchmark walk-forward: python -m tenisPrediction.evaluate --test-year 2025."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path

from .model import CompactTennisModel, _number


def evaluate(data_dir: Path, train_years: list[int], test_year: int, threshold: float = 0.72):
    model = CompactTennisModel.train_recent(data_dir, max(train_years), len(train_years))
    total = correct = selected = selected_correct = 0
    log_loss = 0.0
    with (data_dir / f"{test_year}.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("tourney_date") and row.get("match_num")
            and row.get("winner_name") and row.get("loser_name")
        ]
    rows.sort(key=lambda row: (row["tourney_date"], int(row["match_num"])))
    for index, row in enumerate(rows):
        # Orientarea este alternată pentru a împiedica învățarea coloanei „winner”.
        winner_first = index % 2 == 0
        first = row["winner_name"] if winner_first else row["loser_name"]
        second = row["loser_name"] if winner_first else row["winner_name"]
        rank_1 = _number(row.get("winner_rank" if winner_first else "loser_rank"))
        rank_2 = _number(row.get("loser_rank" if winner_first else "winner_rank"))
        prediction = model.predict(
            first, second, row.get("surface") or "Hard", rank_1, rank_2, threshold
        )
        actual = 1.0 if winner_first else 0.0
        probability = min(1 - 1e-12, max(1e-12, prediction.probability_1))
        total += 1
        correct += (probability >= 0.5) == bool(actual)
        log_loss -= actual * math.log(probability) + (1 - actual) * math.log(1 - probability)
        if prediction.decision == "selectează":
            selected += 1
            selected_correct += (probability >= 0.5) == bool(actual)
        model.update(row["winner_name"], row["loser_name"], row.get("surface") or "Hard",
                     datetime.strptime(row["tourney_date"], "%Y%m%d").date(),
                     _number(row.get("winner_rank")), _number(row.get("loser_rank")))
    return {
        "train_years": train_years, "test_year": test_year, "matches": total,
        "accuracy": round(correct / total, 4), "log_loss": round(log_loss / total, 4),
        "threshold": threshold, "selected": selected,
        "coverage": round(selected / total, 4),
        "selected_accuracy": round(selected_correct / selected, 4) if selected else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "tml-data")
    parser.add_argument("--test-year", type=int, default=2025)
    parser.add_argument("--seasons", type=int, default=4)
    parser.add_argument("--threshold", type=float, default=0.72)
    args = parser.parse_args()
    years = list(range(args.test_year - args.seasons, args.test_year))
    print(json.dumps(evaluate(args.data_dir, years, args.test_year, args.threshold), indent=2))


if __name__ == "__main__":
    main()
