"""Experimente offline: oboseală și Serve/Return Elo ajustat după adversar.

Nu este importat de aplicație. 2024 alege parametrii, 2025 confirmă rezultatul.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from footypreds.evaluation.tennis_data import RAW_DIR, load_tennis_matches
from footypreds.evaluation.tennis_eval import games_total
from footypreds.sports import common, tennis

from .model import CompactTennisModel, _number, serve_record


def logistic(value):
    return 1 / (1 + math.exp(-value))


def logit(value):
    value = min(1 - 1e-9, max(1e-9, value))
    return math.log(value / (1 - value))


def csv_rows(data_dir, first_year, last_year):
    rows = []
    for year in range(first_year, last_year + 1):
        for suffix, tour in (("", "atp"), ("_wta", "wta"), ("_challenger", "challenger")):
            path = Path(data_dir) / f"{year}{suffix}.csv"
            if not path.exists():
                continue
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        day = datetime.strptime(row["tourney_date"], "%Y%m%d").date()
                    except (KeyError, ValueError):
                        continue
                    if row.get("winner_name") and row.get("loser_name"):
                        rows.append((day, int(row.get("match_num") or 0), tour, row))
    return sorted(rows, key=lambda item: (item[0], item[1], item[2]))


def game_count(score):
    total = 0
    for token in str(score or "").split():
        token = token.split("(", 1)[0]
        try:
            home, away = token.split("-")[:2]
            total += int(home) + int(away)
        except (ValueError, IndexError):
            continue
    return total


def fatigue_records(data_dir):
    model = CompactTennisModel()
    workload = defaultdict(list)
    records = {2024: [], 2025: []}
    for index, (day, _, _, row) in enumerate(csv_rows(data_dir, 2020, 2025)):
        winner, loser = row["winner_name"], row["loser_name"]
        surface = row.get("surface") or "Hard"
        winner_first = index % 2 == 0
        first, second = (winner, loser) if winner_first else (loser, winner)
        rank_1 = _number(row.get("winner_rank" if winner_first else "loser_rank"))
        rank_2 = _number(row.get("loser_rank" if winner_first else "winner_rank"))
        probability = model.probability(first, second, surface, rank_1, rank_2)
        keys = [model.resolve_player(first), model.resolve_player(second)]

        def load(player):
            recent = [
                (date, minutes) for date, minutes in workload[player] if (day - date).days <= 14
            ]
            minutes = sum(value for _, value in recent)
            rest = min(14, (day - workload[player][-1][0]).days) if workload[player] else 14
            return rest, minutes

        rest_1, load_1 = load(keys[0])
        rest_2, load_2 = load(keys[1])
        if day.year in records:
            records[day.year].append(
                {
                    "p": probability,
                    "won": winner_first,
                    "rest": (rest_1 - rest_2) / 7,
                    "load": (load_2 - load_1) / 600,
                }
            )
        minutes = _number(row.get("minutes")) or max(45, game_count(row.get("score")) * 4)
        winner_key, loser_key = model.resolve_player(winner), model.resolve_player(loser)
        workload[winner_key].append((day, minutes))
        workload[loser_key].append((day, minutes))
        model.update(
            winner,
            loser,
            surface,
            day,
            _number(row.get("winner_rank")),
            _number(row.get("loser_rank")),
            serve_record(row, "w"),
            serve_record(row, "l"),
        )
    return records


def winner_metrics(records, rest_weight=0, load_weight=0):
    loss = correct = 0.0
    for record in records:
        adjustment = rest_weight * record["rest"] + load_weight * record["load"]
        p = logistic(logit(record["p"]) + adjustment)
        loss -= math.log(p if record["won"] else 1 - p)
        correct += (p >= 0.5) == record["won"]
    return {
        "matches": len(records),
        "log_loss": loss / len(records),
        "accuracy": correct / len(records),
    }


def fatigue_experiment(data_dir):
    records = fatigue_records(data_dir)
    grid = (0, 0.05, 0.1, 0.2, 0.3, 0.5)
    choices = []
    for rest in grid:
        for load in grid:
            choices.append((winner_metrics(records[2024], rest, load)["log_loss"], rest, load))
    _, rest, load = min(choices)
    return {
        "weights_chosen_on_2024": {"rest": rest, "load": load},
        "validation_2024": {
            "baseline": winner_metrics(records[2024]),
            "candidate": winner_metrics(records[2024], rest, load),
        },
        "test_2025": {
            "baseline": winner_metrics(records[2025]),
            "candidate": winner_metrics(records[2025], rest, load),
        },
    }


class ServeReturnBook:
    def __init__(self, k=0.12):
        self.k = k
        self.serve = defaultdict(float)
        self.return_rating = defaultdict(float)
        self.played = defaultdict(int)

    def update(self, player, opponent, surface, observed, tour_base):
        key, other = (player.casefold(), surface), (opponent.casefold(), surface)
        expected = logistic(logit(tour_base) + self.serve[key] - self.return_rating[other])
        change = self.k * (observed - expected)
        self.serve[key] += change
        self.return_rating[other] -= change
        self.played[key] += 1

    def resolve(self, name, surface):
        direct = name.casefold()
        if (direct, surface) in self.played:
            return direct
        parts = direct.replace(".", " ").split()
        if len(parts) < 2:
            return direct
        surname, initial = parts[0], parts[1][:1]
        candidates = [
            player
            for player, own_surface in self.played
            if own_surface == surface and player.split()[-1] == surname and player[:1] == initial
        ]
        if not candidates:
            return direct
        return max(candidates, key=lambda player: self.played[player, surface])

    def base(self, home, away, surface, model_default, raw_default):
        home_key = (self.resolve(home, surface), surface)
        away_key = (self.resolve(away, surface), surface)
        if min(self.played[home_key], self.played[away_key]) < 5:
            return model_default
        pa = logistic(logit(raw_default) + self.serve[home_key] - self.return_rating[away_key])
        pb = logistic(logit(raw_default) + self.serve[away_key] - self.return_rating[home_key])
        adjustment = (pa + pb) / 2 - raw_default
        return min(model_default + 0.05, max(model_default - 0.05, model_default + adjustment))


def train_serve_return(data_dir, k, through=2024):
    book = ServeReturnBook(k)
    for _, _, tour, row in csv_rows(data_dir, 2020, through):
        winner_serve, loser_serve = serve_record(row, "w"), serve_record(row, "l")
        if not winner_serve or not loser_serve:
            continue
        surface = (row.get("surface") or "Hard").casefold()
        raw_default = 0.58 if tour == "wta" else 0.62
        book.update(
            row["winner_name"],
            row["loser_name"],
            surface,
            winner_serve[0] / winner_serve[1],
            raw_default,
        )
        book.update(
            row["loser_name"],
            row["winner_name"],
            surface,
            loser_serve[0] / loser_serve[1],
            raw_default,
        )
    return book


def markov_metrics(matches, book=None, step=5):
    loss = error = bias = 0.0
    used = 0
    for match in matches[::step]:
        actual, priced = games_total(match), common.two_way(match.odds, "1", "2")
        if actual is None or priced is None or match.finish_type or match.status != "finished":
            continue
        women = tennis.is_women(match)
        default = tennis.PARAMS.serve_women if women else tennis.PARAMS.serve_men
        surface = tennis.surface_of(match) or "hard"
        base = (
            book.base(match.home, match.away, surface, default, 0.58 if women else 0.62)
            if book
            else default
        )
        totals, _ = tennis.games_totals(
            priced[0], tennis.best_of(match), base, tennis.PARAMS.set_spread
        )
        mean = sum(games * probability for games, probability in totals.items())
        loss -= math.log(max(1e-12, totals.get(actual, 0.0)))
        error += abs(mean - actual)
        bias += mean - actual
        used += 1
    return {"matches": used, "log_loss": loss / used, "mae": error / used, "bias": bias / used}


def serve_return_experiment(data_dir, step=5):
    validation = load_tennis_matches([2024], directory=RAW_DIR)
    test = load_tennis_matches([2025], directory=RAW_DIR)
    candidates = []
    for k in (0.04, 0.08, 0.12, 0.2):
        book = train_serve_return(data_dir, k, through=2023)
        candidates.append((markov_metrics(validation, book, step)["log_loss"], k))
    _, chosen = min(candidates)
    validation_book = train_serve_return(data_dir, chosen, through=2023)
    test_book = train_serve_return(data_dir, chosen, through=2024)
    return {
        "k_chosen_on_2024": chosen,
        "validation_2024": {
            "baseline": markov_metrics(validation, None, step),
            "candidate": markov_metrics(validation, validation_book, step),
        },
        "test_2025": {
            "baseline": markov_metrics(test, None, step),
            "candidate": markov_metrics(test, test_book, step),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "tml-data")
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--only", choices=("fatigue", "serve-return", "all"), default="all")
    args = parser.parse_args()
    report = {}
    if args.only in ("fatigue", "all"):
        report["fatigue"] = fatigue_experiment(args.data_dir)
    if args.only in ("serve-return", "all"):
        report["serve_return_markov"] = serve_return_experiment(args.data_dir, args.step)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
