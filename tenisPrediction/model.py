"""Predicții ATP fără leakage dintr-un set mic de fișiere TML.

Modelul combină Elo general, Elo pe suprafață și ranking ATP. Fiecare predicție este
calculată înainte ca rezultatul rândului curent să fie introdus în stare. Statisticile din
meci (ași, puncte la serviciu, scor) sunt intenționat ignorate: nu există înainte de meci.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

START_RATING = 1500.0
SURFACE_WEIGHT = 0.35
RANK_WEIGHT = 0.28
ELO_SCALE = 400.0
FORM_ALPHA = 0.08


def _key(value: str) -> str:
    return " ".join((value or "").casefold().replace(".", " ").split())


def _number(value: str | None) -> float | None:
    try:
        result = float(value or "")
    except ValueError:
        return None
    return result if math.isfinite(result) and result > 0 else None


def _day(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def serve_record(row: dict, prefix: str) -> tuple[float, float] | None:
    """(puncte câștigate la serviciu, puncte jucate), fără a folosi scorul ca feature."""
    total = _number(row.get(f"{prefix}_svpt"))
    first_won = _number(row.get(f"{prefix}_1stWon"))
    second_won = _number(row.get(f"{prefix}_2ndWon"))
    if total is None or first_won is None or second_won is None:
        return None
    won = first_won + second_won
    return (won, total) if 0 <= won <= total else None


@dataclass(frozen=True)
class Prediction:
    player_1: str
    player_2: str
    winner: str
    probability_1: float
    confidence: int
    decision: str
    surface: str
    experience_1: int
    experience_2: int

    def as_dict(self) -> dict:
        return {
            "player_1": self.player_1,
            "player_2": self.player_2,
            "winner": self.winner,
            "probability_1": round(self.probability_1, 4),
            "probability_2": round(1 - self.probability_1, 4),
            "confidence": self.confidence,
            "decision": self.decision,
            "surface": self.surface,
            "experience_1": self.experience_1,
            "experience_2": self.experience_2,
        }


class CompactTennisModel:
    """Model online mic; implicit folosește numai ultimele patru sezoane ATP."""

    def __init__(self, k_factor: float = 28.0, stats_scale: float = 0.0):
        self.k_factor = k_factor
        self.stats_scale = stats_scale
        self.ratings: dict[str, float] = {}
        self.surface_ratings: dict[tuple[str, str], float] = {}
        self.played: dict[str, int] = {}
        self.rank: dict[str, float] = {}
        self.point_form: dict[tuple[str, str], float] = {}
        self.point_samples: dict[tuple[str, str], int] = {}
        self.serve_form: dict[tuple[str, str], float] = {}
        self.last_day: date | None = None

    def _rating(self, player: str, surface: str) -> float:
        overall = self.ratings.get(player, START_RATING)
        on_surface = self.surface_ratings.get((player, surface), overall)
        return (1 - SURFACE_WEIGHT) * overall + SURFACE_WEIGHT * on_surface

    def resolve_player(self, name: str) -> str:
        """Rezolvă forma FlashScore `Nume I.` către numele complet din TML."""
        direct = _key(name)
        if direct in self.played:
            return direct
        parts = direct.split()
        if len(parts) < 2:
            return direct
        surname, initial = parts[0], parts[1][:1]
        candidates = [
            player for player in self.played
            if player.split()[-1] == surname and player[:1] == initial
        ]
        if not candidates:
            return direct
        return max(candidates, key=lambda player: self.played[player])

    def probability(self, player_1: str, player_2: str, surface: str = "Hard",
                    rank_1: float | None = None, rank_2: float | None = None) -> float:
        p1, p2 = self.resolve_player(player_1), self.resolve_player(player_2)
        surface = (surface or "Hard").casefold()
        rating_delta = self._rating(p1, surface) - self._rating(p2, surface)
        elo_logit = rating_delta * math.log(10) / ELO_SCALE
        r1 = rank_1 or self.rank.get(p1)
        r2 = rank_2 or self.rank.get(p2)
        rank_logit = math.log((r2 + 5) / (r1 + 5)) if r1 and r2 else 0.0
        experience = min(self.played.get(p1, 0), self.played.get(p2, 0))
        rank_share = RANK_WEIGHT if r1 and r2 else 0.0
        # Jucătorii noi se bazează mai mult pe clasamentul publicat decât pe Elo instabil.
        if experience < 10 and rank_share:
            rank_share = 0.55
        stats_logit = 0.0
        if self.stats_scale and min(
            self.point_samples.get((p1, surface), 0), self.point_samples.get((p2, surface), 0)
        ) >= 5:
            strength_1 = self.point_form.get((p1, surface), 0.5)
            strength_2 = self.point_form.get((p2, surface), 0.5)
            stats_logit = self.stats_scale * (strength_1 - strength_2)
        return _sigmoid((1 - rank_share) * elo_logit + rank_share * rank_logit + stats_logit)

    def predict(self, player_1: str, player_2: str, surface: str = "Hard",
                rank_1: float | None = None, rank_2: float | None = None,
                min_probability: float = 0.72) -> Prediction:
        probability = self.probability(player_1, player_2, surface, rank_1, rank_2)
        chosen = max(probability, 1 - probability)
        experience_1 = self.played.get(_key(player_1), 0)
        experience_2 = self.played.get(_key(player_2), 0)
        enough_history = min(experience_1, experience_2) >= 5
        decision = "selectează" if chosen >= min_probability and enough_history else "fără pariu"
        return Prediction(
            player_1, player_2, player_1 if probability >= 0.5 else player_2,
            probability, round(chosen * 100), decision, surface,
            experience_1, experience_2,
        )

    def update(self, winner: str, loser: str, surface: str, when: date,
               winner_rank: float | None = None, loser_rank: float | None = None,
               winner_serve: tuple[float, float] | None = None,
               loser_serve: tuple[float, float] | None = None) -> None:
        winner, loser, surface = _key(winner), _key(loser), surface.casefold()
        # Actualizarea Elo nu include rankingul: ratingul rămâne o sursă independentă.
        delta = self._rating(loser, surface) - self._rating(winner, surface)
        raw_expected = 1 / (1 + 10 ** (delta / ELO_SCALE))
        change = self.k_factor * (1 - raw_expected)
        self.ratings[winner] = self.ratings.get(winner, START_RATING) + change
        self.ratings[loser] = self.ratings.get(loser, START_RATING) - change
        self.surface_ratings[winner, surface] = self.surface_ratings.get(
            (winner, surface), self.ratings[winner] - change
        ) + change
        self.surface_ratings[loser, surface] = self.surface_ratings.get(
            (loser, surface), self.ratings[loser] + change
        ) - change
        self.played[winner] = self.played.get(winner, 0) + 1
        self.played[loser] = self.played.get(loser, 0) + 1
        if winner_rank:
            self.rank[winner] = winner_rank
        if loser_rank:
            self.rank[loser] = loser_rank
        if winner_serve and loser_serve and min(winner_serve[1], loser_serve[1]) > 0:
            winner_dominance = (
                winner_serve[0] / winner_serve[1] + 1 - loser_serve[0] / loser_serve[1]
            ) / 2
            loser_dominance = 1 - winner_dominance
            for player, value in ((winner, winner_dominance), (loser, loser_dominance)):
                key = (player, surface)
                prior = self.point_form.get(key, 0.5)
                self.point_form[key] = prior + FORM_ALPHA * (value - prior)
                self.point_samples[key] = self.point_samples.get(key, 0) + 1
            for player, record in ((winner, winner_serve), (loser, loser_serve)):
                key = (player, surface)
                value = record[0] / record[1]
                prior = self.serve_form.get(key, value)
                self.serve_form[key] = prior + FORM_ALPHA * (value - prior)
        self.last_day = when

    def serve_base(self, player_1: str, player_2: str, surface: str, default: float) -> float:
        """Media serviciului celor doi, limitată prudent în jurul mediei circuitului."""
        p1, p2 = self.resolve_player(player_1), self.resolve_player(player_2)
        surface = surface.casefold()
        if min(
            self.point_samples.get((p1, surface), 0), self.point_samples.get((p2, surface), 0)
        ) < 5:
            return default
        observed = (self.serve_form[(p1, surface)] + self.serve_form[(p2, surface)]) / 2
        return min(default + 0.05, max(default - 0.05, observed))

    def fit_files(self, files: list[str | Path], *, before: date | None = None) -> int:
        rows = []
        for file in files:
            with Path(file).open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        when = _day(row["tourney_date"])
                    except (KeyError, ValueError):
                        continue
                    if before is None or when < before:
                        rows.append((when, int(row.get("match_num") or 0), row))
        rows.sort(key=lambda item: (item[0], item[1]))
        for when, _, row in rows:
            self.update(
                row["winner_name"], row["loser_name"], row.get("surface") or "Hard", when,
                _number(row.get("winner_rank")), _number(row.get("loser_rank")),
                serve_record(row, "w"), serve_record(row, "l"),
            )
        return len(rows)

    @classmethod
    def train_recent(cls, data_dir: str | Path, end_year: int = 2025, seasons: int = 4):
        model = cls()
        directory = Path(data_dir)
        files = [directory / f"{year}.csv" for year in range(end_year - seasons + 1, end_year + 1)]
        model.fit_files([file for file in files if file.exists()])
        return model

    @classmethod
    def train_dataset(
        cls, data_dir: str | Path, end_year: int = 2025, seasons: int = 4,
        stats_scale: float = 0.0,
    ):
        """ATP, WTA și Challenger recente, păstrând ordinea cronologică între fișiere."""
        directory = Path(data_dir)
        files = []
        for year in range(end_year - seasons + 1, end_year + 1):
            files.extend(
                path for path in (
                    directory / f"{year}.csv",
                    directory / f"{year}_wta.csv",
                    directory / f"{year}_challenger.csv",
                ) if path.exists()
            )
        model = cls(stats_scale=stats_scale)
        model.fit_files(files)
        return model

    def predict_api_match(self, match, min_probability: float = 0.72) -> dict:
        """Adaptor pentru `footypreds.domain.Match` primit deja de la FlashScore API."""
        league = str(getattr(match, "league", ""))
        surface = next((name for name in ("Hard", "Clay", "Grass", "Carpet")
                        if name.casefold() in league.casefold()), "Hard")
        result = self.predict(match.home, match.away, surface, min_probability=min_probability)
        payload = result.as_dict()
        payload["match_id"] = match.id
        return payload
