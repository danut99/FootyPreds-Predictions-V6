"""Benchmark walk-forward comun, fără leakage, pentru modelele de tenis (TML / Sackmann).

Every tennis candidate is measured with this module, so the rules below are binding.

Data
----
``load_rows(first_year, last_year, tours)`` returns the TML rows of the requested seasons in
chronological order. A season is the year in the file name (``2025.csv`` also holds events that
start on 2024-12-29; their ``season`` is 2025). Order: event start date, then qualifying
before the main draw of the same week, then tour, tournament and, inside a tournament, round
(Q1<Q2<Q3<R256<R128<R64<R32<R16<RR<QF<SF<BR<F) and ``match_num``. Round goes before
``match_num`` because some TML events number matches out of chronological order; split and late
dates are repaired too (see ``_order_key_factory``). Rows with an empty ``match_num`` are kept
(last inside their round).

Each row is a read-only ``Row`` mapping (``row["x"]``, ``row.get("x")``, ``dict(row)``) with all
CSV columns plus ``tour`` ("atp", "challenger", "quali", "wta"), ``season`` (int), ``date``
(``datetime.date`` of ``tourney_date``), ``winner_key``/``loser_key`` (stable player keys: the
TML id, prefixed ``"wta:"`` on the WTA tour, or ``"name:<name>"`` when the id is missing) and
``is_walkover``. Empty cells are ``None``; numeric columns are ``int`` (``*_age`` is ``float``).
Parsed files are cached as pickles in ``footypreds/data/tennis_bench_cache/`` (git-ignored),
keyed by file size and mtime.

Model protocol
--------------
A candidate is a zero-argument ``factory()`` returning a fresh model object::

    class Model:
        def predict(self, ctx) -> float:      # P(ctx.first wins), in [0, 1]
        def update(self, row) -> None:        # full finished Row (score and stats allowed)
        def select(self, ctx, p) -> bool:     # optional; default rule: max(p, 1 - p) >= 0.72

The benchmark walks every loaded row once, in order. For an evaluated row it calls
``predict(ctx)``, then ``select(ctx, p)`` if the model defines it, then ``update(row)``. Every
other row (other tours, earlier seasons, walkovers) only goes to ``update(row)``. Nothing is
trained in advance: models learn online through ``update``.

``ctx`` is a frozen ``MatchContext`` holding ONLY pre-match-safe fields, oriented first/second:

    first_id, first_key, first_name, first_hand, first_ht, first_ioc, first_age, first_rank,
    first_rank_points, first_seed, first_entry          (same with ``second_``)
    tour, season, date, tourney_id, tourney_name, tourney_level, draw_size, surface, indoor,
    best_of, round, match_num
    first_odds, second_odds    (only with ``odds``: pre-match closing prices, else None)

Attributes are read as ``ctx.first_rank``; ``ctx["first_rank"]``, ``ctx.get(name)`` and
``ctx.as_dict()`` also work. Orientation is a hash of (tour, tourney_id, match_num, round, the
two player keys in sorted order, salt): deterministic, ~50/50 and independent of who won, so
"the winner is first" cannot be learned. Walkovers (score empty, "W/O", or no games played) are
never evaluated but are still passed to ``update``; retirements are evaluated.

Metrics (per "tour/season" and pooled "tour/all")
-------------------------------------------------
``n``, ``log_loss``, ``brier``, ``accuracy``, ``ece`` (10 equal-width bins of p), accuracy and
coverage at confidence thresholds 0.60/0.65/0.70/0.72/0.75/0.80/0.85, ``coverage_at_80`` (sort
by confidence max(p, 1-p) descending; the largest prefix with accuracy >= 0.80 that ends on a
confidence change and holds at least 50 matches; else 0) with its threshold in
``coverage_at_80_detail``, ``transfer_80`` (the previous evaluated season's coverage_at_80
threshold applied to this season: an honest out-of-sample check) and ``select`` (accuracy and
coverage of the model's own ``select`` rule) when the model defines ``select``, and
``select_high`` likewise when it defines an optional stricter ``select_high(ctx, p)``.

Odds (opt-in, ``odds="avg"|"ps"|"b365"|"max"``, CLI ``--odds``): ATP/WTA rows joined to a
tennis-data.co.uk closing price (see ``tenisPrediction/odds.py``) are passed as ``OddsRow``
(``row.get("winner_odds")``/``row.get("loser_odds")``, None on plain rows) and their context
carries ``first_odds``/``second_odds``. Without ``odds`` nothing changes.

Protocol: tune on 2021-2023 only, confirm once on 2024. 2025 is the locked test year
(``--locked-test``).

CLI::

    python -m tenisPrediction.benchmark --model tenisPrediction.candidates.baseline:factory \
        --years 2023,2024 --tours atp,wta [--first-year 1990] [--json out.json] \
        [--odds avg --odds-dir footypreds/data/benchmark/tennis/raw]
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import importlib
import json
import math
import os
import pickle
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "tml-data"
DEFAULT_CACHE_DIR = (
    Path(__file__).resolve().parent.parent / "footypreds" / "data" / ("tennis_bench_cache")
)
TOURS = ("atp", "challenger", "quali", "wta")
LOCKED_TEST_YEAR = 2025
DEFAULT_THRESHOLD = 0.72
THRESHOLDS = (0.60, 0.65, 0.70, 0.72, 0.75, 0.80, 0.85)
TARGET_ACCURACY = 0.80
MIN_SELECTED = 50
PARSER_VERSION = 1
EPS = 1e-12

CSV_COLUMNS = (
    "tourney_id",
    "tourney_name",
    "surface",
    "draw_size",
    "tourney_level",
    "indoor",
    "tourney_date",
    "match_num",
    "winner_id",
    "winner_seed",
    "winner_entry",
    "winner_name",
    "winner_hand",
    "winner_ht",
    "winner_ioc",
    "winner_age",
    "winner_rank",
    "winner_rank_points",
    "loser_id",
    "loser_seed",
    "loser_entry",
    "loser_name",
    "loser_hand",
    "loser_ht",
    "loser_ioc",
    "loser_age",
    "loser_rank",
    "loser_rank_points",
    "score",
    "best_of",
    "round",
    "minutes",
    "w_ace",
    "w_df",
    "w_svpt",
    "w_1stIn",
    "w_1stWon",
    "w_2ndWon",
    "w_SvGms",
    "w_bpSaved",
    "w_bpFaced",
    "l_ace",
    "l_df",
    "l_svpt",
    "l_1stIn",
    "l_1stWon",
    "l_2ndWon",
    "l_SvGms",
    "l_bpSaved",
    "l_bpFaced",
)
EXTRA_COLUMNS = ("tour", "season", "date", "winner_key", "loser_key", "is_walkover")
FIELDS = CSV_COLUMNS + EXTRA_COLUMNS
_INDEX = {name: position for position, name in enumerate(FIELDS)}

_INT_COLUMNS = frozenset(
    ["draw_size", "match_num", "best_of", "minutes"]
    + [
        f"{side}_{name}"
        for side in ("winner", "loser")
        for name in ("seed", "ht", "rank", "rank_points")
    ]
    + [name for name in CSV_COLUMNS if name.startswith(("w_", "l_"))]
)
_FLOAT_COLUMNS = frozenset(("winner_age", "loser_age"))
_UPPER_COLUMNS = frozenset(("winner_entry", "loser_entry", "winner_hand", "loser_hand"))

ROUND_ORDER = {
    "Q1": 0,
    "Q2": 1,
    "Q3": 2,
    "Q4": 3,
    "R256": 4,
    "R128": 5,
    "R64": 6,
    "R32": 7,
    "R16": 8,
    "RR": 9,
    "QF": 10,
    "SF": 11,
    "BR": 12,
    "3P": 12,
    "3rd/4th": 12,
    "F": 13,
    "Fs": 13,
}
_UNKNOWN_ROUND = ROUND_ORDER["RR"]
_TOUR_ORDER = {tour: position for position, tour in enumerate(TOURS)}
_FILE_NAME = {
    "atp": "{year}.csv",
    "challenger": "{year}_challenger.csv",
    "wta": "{year}_wta.csv",
    "quali": "atp_quali/{year}_atp_quali.csv",
}
_SIDE_FIELDS = (
    "id",
    "key",
    "name",
    "hand",
    "ht",
    "ioc",
    "age",
    "rank",
    "rank_points",
    "seed",
    "entry",
)
_MATCH_FIELDS = (
    "tour",
    "season",
    "date",
    "tourney_id",
    "tourney_name",
    "tourney_level",
    "draw_size",
    "surface",
    "indoor",
    "best_of",
    "round",
    "match_num",
)


class LockedTestError(ValueError):
    """Raised when the locked test season is requested without ``locked_test=True``."""


class Row(Mapping):
    """Read-only, tuple-backed row (about 4x smaller than a dict of the same columns)."""

    __slots__ = ("_values",)

    def __init__(self, values: tuple):
        self._values = values

    def __getitem__(self, key: str) -> Any:
        try:
            return self._values[_INDEX[key]]
        except KeyError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        position = _INDEX.get(key)
        return default if position is None else self._values[position]

    def __contains__(self, key: object) -> bool:
        return key in _INDEX

    def __iter__(self):
        return iter(FIELDS)

    def __len__(self) -> int:
        return len(FIELDS)

    def __reduce__(self):
        return (Row, (self._values,))

    def __repr__(self) -> str:
        return (
            f"Row({self['tour']} {self['tourney_date']} {self['tourney_id']} "
            f"{self['round']} {self['winner_name']} d. {self['loser_name']} {self['score']})"
        )


class OddsRow(Row):
    """A ``Row`` with the pre-match prices of its winner and loser (``--odds``)."""

    __slots__ = ("winner_odds", "loser_odds")
    _ODDS = frozenset(("winner_odds", "loser_odds"))

    def __init__(self, values: tuple, winner_odds: float, loser_odds: float):
        super().__init__(values)
        self.winner_odds = winner_odds
        self.loser_odds = loser_odds

    def __getitem__(self, key: str) -> Any:
        if key in OddsRow._ODDS:
            return getattr(self, key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in OddsRow._ODDS:
            return getattr(self, key)
        return super().get(key, default)

    def __reduce__(self):
        return (OddsRow, (self._values, self.winner_odds, self.loser_odds))


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Pre-match view of one match, oriented first/second (see module docstring)."""

    first_id: str | None
    first_key: str
    first_name: str | None
    first_hand: str | None
    first_ht: int | None
    first_ioc: str | None
    first_age: float | None
    first_rank: int | None
    first_rank_points: int | None
    first_seed: int | None
    first_entry: str | None
    second_id: str | None
    second_key: str
    second_name: str | None
    second_hand: str | None
    second_ht: int | None
    second_ioc: str | None
    second_age: float | None
    second_rank: int | None
    second_rank_points: int | None
    second_seed: int | None
    second_entry: str | None
    tour: str
    season: int
    date: date
    tourney_id: str | None
    tourney_name: str | None
    tourney_level: str | None
    draw_size: int | None
    surface: str | None
    indoor: str | None
    best_of: int | None
    round: str | None
    match_num: int | None
    first_odds: float | None = None
    second_odds: float | None = None

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(name) from None

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    def as_dict(self) -> dict:
        return asdict(self)


CONTEXT_FIELDS = tuple(field.name for field in fields(MatchContext))


# --------------------------------------------------------------------------- loading


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if math.isfinite(number) else None


def _parse_float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _repair_date(raw: str | None, season: int) -> str | None:
    """Fix the few impossible dates (e.g. 20071231 inside 2024_challenger.csv)."""
    if not raw or len(raw) != 8 or not raw.isdigit():
        return None
    year, month = int(raw[:4]), int(raw[4:6])
    if abs(year - season) <= 1:
        return raw
    fixed_year = season - 1 if month == 12 else season
    return f"{fixed_year}{raw[4:]}"


def is_walkover(score: str | None) -> bool:
    """True when no tennis was played: empty score, W/O, or no game digits at all."""
    if not score:
        return True
    text = score.upper()
    if "W/O" in text or text.strip() in {"WO", "W.O.", "WALKOVER"}:
        return True
    return not any(character.isdigit() for character in text)


def _player_key(tour: str, player_id: str | None, name: str | None) -> str:
    prefix = "wta:" if tour == "wta" else ""
    if player_id:
        return f"{prefix}{player_id}"
    if name:
        return f"{prefix}name:{' '.join(name.casefold().split())}"
    return ""


def _parse_file(path: Path, tour: str, season: int) -> list[tuple]:
    interned: dict[str, str] = {}
    dates: dict[str, date] = {}
    rows: list[tuple] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            values: list[Any] = []
            for column in CSV_COLUMNS:
                text = (raw.get(column) or "").strip()
                if not text:
                    values.append(None)
                elif column in _INT_COLUMNS:
                    values.append(_parse_int(text))
                elif column in _FLOAT_COLUMNS:
                    values.append(_parse_float(text))
                else:
                    if column in _UPPER_COLUMNS:
                        text = text.upper()
                    values.append(interned.setdefault(text, text))
            record = dict(zip(CSV_COLUMNS, values))
            tourney_date = _repair_date(record["tourney_date"], season)
            if tourney_date is None:
                continue
            values[_INDEX["tourney_date"]] = interned.setdefault(tourney_date, tourney_date)
            indoor = record["indoor"]
            values[_INDEX["indoor"]] = indoor if indoor in ("I", "O") else None
            when = dates.get(tourney_date)
            if when is None:
                try:
                    when = date(
                        int(tourney_date[:4]), int(tourney_date[4:6]), int(tourney_date[6:])
                    )
                except ValueError:
                    continue
                dates[tourney_date] = when
            values.extend(
                (
                    tour,
                    season,
                    when,
                    _player_key(tour, record["winner_id"], record["winner_name"]),
                    _player_key(tour, record["loser_id"], record["loser_name"]),
                    is_walkover(record["score"]),
                )
            )
            rows.append(tuple(values))
    return rows


def data_file(data_dir: Path, tour: str, year: int) -> Path:
    return Path(data_dir) / _FILE_NAME[tour].format(year=year)


def _load_file(path: Path, tour: str, season: int, cache_dir: Path | None) -> list[tuple]:
    if cache_dir is None:
        return _parse_file(path, tour, season)
    stat = path.stat()
    stamp = (PARSER_VERSION, str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cache_file = Path(cache_dir) / f"{tour}_{season}.pkl"
    try:
        with cache_file.open("rb") as handle:
            cached = pickle.load(handle)
        if cached.get("stamp") == stamp:
            return cached["rows"]
    except (OSError, pickle.PickleError, EOFError, AttributeError, KeyError, TypeError):
        pass
    rows = _parse_file(path, tour, season)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            pickle.dump({"stamp": stamp, "rows": rows}, handle, pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, cache_file)
    except OSError:
        pass  # the cache is only an optimisation
    return rows


EVENT_WINDOW_DAYS = 3  # rows of one tourney_id dated this close belong to the same event
QUALI_WINDOW_DAYS = 7  # qualifying dated up to this long after its main draw start


def _order_key_factory(rows: list[tuple]) -> Callable[[tuple[int, tuple]], tuple]:
    """Sort key: (event date, quali first, tour, tournament, round, match_num, file line).

    TML data has three ordering traps, all found in the real files:

    * one event split over two dates (ATP Winston-Salem 2024: 20240818 and 20240819), which
      would feed a semi-final before a quarter-final. Dates of the same (tour, tourney_id)
      within ``EVENT_WINDOW_DAYS`` collapse to the event's first date. Different events that
      reuse an id a week apart (WTA Adelaide 1/2) stay separate.
    * qualifying dated a few days after its main draw start (2021). A qualifying row uses the
      main draw's date when that is at most ``QUALI_WINDOW_DAYS`` earlier, and sorts first.
    * ``match_num`` out of chronological order (Dubai 2023 numbers R16 below R32). Inside an
      event the round goes first, then ``match_num``; with a clean ``match_num`` both agree.
    """
    tour_at, id_at, num_at = _INDEX["tour"], _INDEX["tourney_id"], _INDEX["match_num"]
    round_at, day_at = _INDEX["round"], _INDEX["date"]
    days: dict[tuple[str, str], set[date]] = {}
    for values in rows:
        days.setdefault((values[tour_at], values[id_at] or ""), set()).add(values[day_at])
    event_start: dict[tuple[str, str, date], date] = {}
    for (tour, tourney), seen in days.items():
        start = None
        for day in sorted(seen):
            if start is None or (day - start).days > EVENT_WINDOW_DAYS:
                start = day
            event_start[tour, tourney, day] = start
    main_starts: dict[str, list[date]] = {}
    for (tour, tourney, _), start in event_start.items():
        if tour == "atp":
            main_starts.setdefault(tourney, []).append(start)

    def key(item: tuple[int, tuple]) -> tuple:
        sequence, values = item
        tour, tourney = values[tour_at], values[id_at] or ""
        when = event_start[tour, tourney, values[day_at]]
        if tour == "quali":
            earlier = [
                start
                for start in main_starts.get(tourney, ())
                if 0 <= (when - start).days <= QUALI_WINDOW_DAYS
            ]
            if earlier:
                when = min(earlier)
        number = values[num_at]
        return (
            when,
            0 if tour == "quali" else 1,
            _TOUR_ORDER[tour],
            tourney,
            ROUND_ORDER.get(values[round_at], _UNKNOWN_ROUND),
            number if number is not None else 1 << 30,
            sequence,
        )

    return key


def load_rows(
    first_year: int,
    last_year: int,
    tours: Iterable[str] = TOURS,
    *,
    data_dir: str | Path = DATA_DIR,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
) -> list[Row]:
    """Chronologically ordered rows of seasons first_year..last_year (missing files skipped)."""
    wanted = tuple(dict.fromkeys(tours))
    unknown = set(wanted) - set(TOURS)
    if unknown:
        raise ValueError(f"Circuite necunoscute: {sorted(unknown)}; permise: {TOURS}")
    raw: list[tuple] = []
    for season in range(first_year, last_year + 1):
        for tour in wanted:
            path = data_file(Path(data_dir), tour, season)
            if path.exists():
                raw.extend(_load_file(path, tour, season, cache_dir))
    order = _order_key_factory(raw)
    ordered = sorted(enumerate(raw), key=order)
    return [Row(values) for _, values in ordered]


# --------------------------------------------------------------------------- context


def first_is_winner(row: Mapping, salt: str = "") -> bool:
    """Deterministic ~50/50 orientation that does not depend on who won."""
    low, high = sorted((row["winner_key"], row["loser_key"]))
    text = "|".join(
        (
            str(row["tour"]),
            str(row["tourney_id"]),
            str(row["match_num"]),
            str(row["round"]),
            low,
            high,
            salt,
        )
    )
    bit = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()[0] & 1
    first = low if bit else high
    return first == row["winner_key"]


def make_context(row: Mapping, salt: str = "") -> tuple[MatchContext, bool]:
    """(pre-match context, True when ctx.first is the actual winner)."""
    winner_first = first_is_winner(row, salt)
    first, second = ("winner", "loser") if winner_first else ("loser", "winner")
    values: dict[str, Any] = {}
    for name in _SIDE_FIELDS:
        values[f"first_{name}"] = row[f"{first}_{name}"]
        values[f"second_{name}"] = row[f"{second}_{name}"]
    for name in _MATCH_FIELDS:
        values[name] = row[name]
    first_odds, second_odds = row.get(f"{first}_odds"), row.get(f"{second}_odds")
    if first_odds is not None and second_odds is not None:
        values["first_odds"], values["second_odds"] = first_odds, second_odds
    return MatchContext(**values), winner_first


def is_evaluable(row: Mapping) -> bool:
    return (
        not row["is_walkover"]
        and bool(row["winner_key"])
        and bool(row["loser_key"])
        and row["winner_key"] != row["loser_key"]
    )


# --------------------------------------------------------------------------- metrics


def _clip(p: float) -> float:
    return min(1.0 - EPS, max(EPS, p))


def _correct(p: float, y: int) -> float:
    if p == 0.5:
        return 0.5
    return 1.0 if (p > 0.5) == bool(y) else 0.0


def expected_calibration_error(probs: list[float], outcomes: list[int], bins: int = 10) -> float:
    """ECE over equal-width bins of p (probability that first wins)."""
    if not probs:
        return 0.0
    totals = [[0, 0.0, 0.0] for _ in range(bins)]
    for p, y in zip(probs, outcomes):
        bucket = totals[min(bins - 1, int(p * bins))]
        bucket[0] += 1
        bucket[1] += p
        bucket[2] += y
    n = len(probs)
    return sum(abs(s_p - s_y) / n for count, s_p, s_y in totals if count)


def coverage_at_accuracy(
    confidences: list[float],
    correct: list[float],
    target: float = TARGET_ACCURACY,
    min_selected: int = MIN_SELECTED,
) -> dict:
    """Largest confidence-sorted prefix whose accuracy is >= target.

    Cuts are allowed only where confidence changes (so the prefix equals a threshold rule
    ``confidence >= threshold``) and the prefix must hold at least ``min_selected`` matches.
    """
    n = len(confidences)
    pairs = sorted(zip(confidences, correct), key=lambda item: -item[0])
    best = {"coverage": 0.0, "n_selected": 0, "accuracy": None, "threshold": None}
    hits = 0.0
    for index, (confidence, hit) in enumerate(pairs):
        hits += hit
        count = index + 1
        boundary = count == n or pairs[index + 1][0] < confidence
        if boundary and count >= min_selected and hits / count >= target - 1e-12:
            best = {
                "coverage": count / n,
                "n_selected": count,
                "accuracy": hits / count,
                "threshold": confidence,
            }
    return best


def compute_metrics(
    probs: list[float],
    outcomes: list[int],
    selected: list[bool] | None = None,
    selected_high: list[bool] | None = None,
) -> dict:
    """All benchmark metrics for one group of predictions (p = P(first wins), y = first won)."""
    n = len(probs)
    if n == 0:
        return {"n": 0}
    log_loss = brier = hits = 0.0
    confidences, correct = [], []
    for p, y in zip(probs, outcomes):
        q = _clip(p)
        log_loss -= math.log(q) if y else math.log(1.0 - q)
        brier += (p - y) ** 2
        hit = _correct(p, y)
        hits += hit
        confidences.append(max(p, 1.0 - p))
        correct.append(hit)
    thresholds = {}
    for threshold in THRESHOLDS:
        chosen = [hit for conf, hit in zip(confidences, correct) if conf >= threshold - 1e-12]
        thresholds[f"{threshold:.2f}"] = {
            "accuracy": sum(chosen) / len(chosen) if chosen else None,
            "coverage": len(chosen) / n,
            "n_selected": len(chosen),
        }
    detail = coverage_at_accuracy(confidences, correct)
    result = {
        "n": n,
        "log_loss": log_loss / n,
        "brier": brier / n,
        "accuracy": hits / n,
        "ece": expected_calibration_error(probs, outcomes),
        "thresholds": thresholds,
        "coverage_at_80": detail["coverage"],
        "coverage_at_80_detail": detail,
    }
    for name, flags in (("select", selected), ("select_high", selected_high)):
        if flags is not None:
            chosen = [hit for hit, keep in zip(correct, flags) if keep]
            result[name] = {
                "accuracy": sum(chosen) / len(chosen) if chosen else None,
                "coverage": len(chosen) / n,
                "n_selected": len(chosen),
            }
    return result


def _transfer(records: list[dict], threshold: float | None, from_year: int) -> dict:
    if threshold is None:
        return {
            "from_year": from_year,
            "threshold": None,
            "accuracy": None,
            "coverage": 0.0,
            "n_selected": 0,
        }
    chosen = [_correct(r["p"], r["y"]) for r in records if r["confidence"] >= threshold]
    return {
        "from_year": from_year,
        "threshold": threshold,
        "accuracy": sum(chosen) / len(chosen) if chosen else None,
        "coverage": len(chosen) / len(records) if records else 0.0,
        "n_selected": len(chosen),
    }


def summarize(records: list[dict], has_select: bool, has_high: bool = False) -> dict[str, dict]:
    """Metrics per "tour/season", pooled "tour/all", with season-to-season transfer."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(f"{record['tour']}/{record['season']}", []).append(record)
        groups.setdefault(f"{record['tour']}/all", []).append(record)

    def group_metrics(items: list[dict]) -> dict:
        return compute_metrics(
            [r["p"] for r in items],
            [r["y"] for r in items],
            [bool(r["selected"]) for r in items] if has_select else None,
            [bool(r["selected_high"]) for r in items] if has_high else None,
        )

    metrics = {key: group_metrics(items) for key, items in sorted(groups.items())}
    for tour in sorted({record["tour"] for record in records}):
        seasons = sorted(
            int(key.split("/")[1])
            for key in metrics
            if key.startswith(f"{tour}/") and not key.endswith("/all")
        )
        for previous, season in zip(seasons, seasons[1:]):
            threshold = metrics[f"{tour}/{previous}"]["coverage_at_80_detail"]["threshold"]
            metrics[f"{tour}/{season}"]["transfer_80"] = _transfer(
                groups[f"{tour}/{season}"], threshold, previous
            )
    return metrics


# --------------------------------------------------------------------------- walk-forward


def check_years(eval_years: Iterable[int], locked_test: bool) -> list[int]:
    years = sorted({int(year) for year in eval_years})
    if not years:
        raise ValueError("Trebuie cel puțin un an de evaluare.")
    if not locked_test and any(year >= LOCKED_TEST_YEAR for year in years):
        raise LockedTestError(
            f"Anul {LOCKED_TEST_YEAR}+ este testul blocat; tunează doar pe 2023/2024. "
            "Pentru rularea finală, o singură dată pe versiune, folosește locked_test=True "
            "(--locked-test)."
        )
    return years


def run_benchmark(
    factory: Callable[[], Any],
    eval_years: Iterable[int],
    eval_tours: Iterable[str] = ("atp",),
    first_year: int = 1990,
    feed_tours: Iterable[str] = TOURS,
    *,
    data_dir: str | Path = DATA_DIR,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
    return_records: bool = False,
    locked_test: bool = False,
    salt: str = "",
    rows: list[Row] | None = None,
    odds: str | None = None,
    odds_dir: str | Path | None = None,
) -> dict:
    """Walk-forward benchmark. Returns {"metrics", "meta"[, "records"]}.

    ``metrics`` maps "tour/season" (and pooled "tour/all") to ``compute_metrics`` output.
    ``records`` (when ``return_records``) holds one dict per evaluated match with the
    context (``ctx``), ``p``, ``y`` (1 when first won), ``confidence`` and ``selected``.
    ``rows`` lets callers pass pre-loaded rows (for example to run many candidates).
    ``odds`` ("avg", "ps", ...) joins tennis-data.co.uk closing prices (``odds_dir``); the
    join statistics go to ``meta["odds"]``.
    """
    years = check_years(eval_years, locked_test)
    eval_tours = tuple(dict.fromkeys(eval_tours))
    feed_tours = tuple(dict.fromkeys(feed_tours))
    if first_year > years[0]:
        raise ValueError("first_year trebuie să fie <= primul an evaluat.")
    started = time.perf_counter()
    if rows is None:
        tours = tuple(dict.fromkeys(feed_tours + eval_tours))
        rows = load_rows(first_year, years[-1], tours, data_dir=data_dir, cache_dir=cache_dir)
    odds_stats = None
    if odds:
        from .odds import DEFAULT_ODDS_DIR, attach_odds

        rows, odds_stats = attach_odds(
            rows, odds, directory=odds_dir or DEFAULT_ODDS_DIR, cache_dir=cache_dir
        )
    loaded = time.perf_counter()
    model = factory()
    select = getattr(model, "select", None)
    has_select = callable(select)
    select_high = getattr(model, "select_high", None)
    has_high = callable(select_high)
    wanted_years, wanted_tours, fed_tours = set(years), set(eval_tours), set(feed_tours)
    records: list[dict] = []
    fed = 0
    for row in rows:
        season, tour = row["season"], row["tour"]
        if season < first_year or season > years[-1]:
            continue
        evaluated = season in wanted_years and tour in wanted_tours and is_evaluable(row)
        if evaluated:
            ctx, winner_first = make_context(row, salt)
            p = float(model.predict(ctx))
            if not 0.0 <= p <= 1.0:  # also rejects NaN
                raise ValueError(f"predict() a întors {p!r} pentru {row!r}; trebuie în [0, 1].")
            chosen = bool(select(ctx, p)) if has_select else None
            chosen_high = bool(select_high(ctx, p)) if has_high else None
            records.append(
                {
                    "tour": tour,
                    "season": season,
                    "ctx": ctx,
                    "p": p,
                    "y": 1 if winner_first else 0,
                    "confidence": max(p, 1.0 - p),
                    "selected": chosen,
                    "selected_high": chosen_high,
                }
            )
        elif tour not in fed_tours:
            continue
        model.update(row)
        fed += 1
    finished = time.perf_counter()
    result = {
        "metrics": summarize(records, has_select, has_high),
        "meta": {
            "eval_years": years,
            "eval_tours": list(eval_tours),
            "feed_tours": list(feed_tours),
            "first_year": first_year,
            "rows_fed": fed,
            "rows_evaluated": len(records),
            "has_select": has_select,
            "has_select_high": has_high,
            "load_seconds": round(loaded - started, 2),
            "run_seconds": round(finished - loaded, 2),
            "locked_test": locked_test,
        },
    }
    if odds:
        result["meta"]["odds"] = {"source": odds, **odds_stats}
    if return_records:
        result["records"] = records
    return result


# --------------------------------------------------------------------------- CLI


def load_factory(spec: str) -> Callable[..., Any]:
    module_name, _, attribute = spec.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attribute or "factory")


def _parse_params(items: list[str]) -> dict:
    params = {}
    for item in items:
        name, separator, text = item.partition("=")
        if not separator:
            raise ValueError(f"Parametru invalid {item!r}; folosește nume=valoare.")
        try:
            params[name] = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            params[name] = text
    return params


def _fmt(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_table(metrics: dict[str, dict]) -> str:
    lines = [
        f"{'grup':<16}{'n':>6}{'logloss':>9}{'brier':>8}{'acc':>8}{'ece':>8}"
        f"{'cov@80':>8}{'thr80':>7}{'acc@.72':>9}{'cov@.72':>9}{'transfer80':>16}{'select':>16}"
        f"{'select_high':>16}"
    ]
    for key, m in metrics.items():
        if not m.get("n"):
            continue
        t72 = m["thresholds"]["0.72"]
        transfer = m.get("transfer_80")
        transfer_text = (
            f"{_fmt(transfer['accuracy'], 3)}/{_fmt(transfer['coverage'], 3)}" if transfer else "-"
        )
        chosen = m.get("select")
        select_text = (
            f"{_fmt(chosen['accuracy'], 3)}/{_fmt(chosen['coverage'], 3)}" if chosen else "-"
        )
        high = m.get("select_high")
        high_text = f"{_fmt(high['accuracy'], 3)}/{_fmt(high['coverage'], 3)}" if high else "-"
        lines.append(
            f"{key:<16}{m['n']:>6}{m['log_loss']:>9.4f}{m['brier']:>8.4f}{m['accuracy']:>8.4f}"
            f"{m['ece']:>8.4f}{m['coverage_at_80']:>8.4f}"
            f"{_fmt(m['coverage_at_80_detail']['threshold'], 3):>7}"
            f"{_fmt(t72['accuracy']):>9}{t72['coverage']:>9.4f}{transfer_text:>16}"
            f"{select_text:>16}{high_text:>16}"
        )
    return "\n".join(lines)


def _json_ready(result: dict) -> dict:
    payload = {key: value for key, value in result.items() if key != "records"}
    return payload


def _records_ready(records: list[dict]) -> list[dict]:
    ready = []
    for record in records:
        item = {key: value for key, value in record.items() if key != "ctx"}
        context = record["ctx"].as_dict()
        context["date"] = context["date"].isoformat()
        item.update(context)
        ready.append(item)
    return ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tenisPrediction.benchmark",
        description="Benchmark walk-forward fără leakage pentru modelele de tenis.",
    )
    parser.add_argument(
        "--model",
        default="tenisPrediction.candidates.baseline:factory",
        help="modul:factory, de ex. tenisPrediction.candidates.baseline:factory",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="nume=valoare transmis la factory (se poate repeta)",
    )
    parser.add_argument("--years", default="2023,2024", help="ani evaluați, separați prin virgulă")
    parser.add_argument("--tours", default="atp", help="circuite evaluate (atp,wta,challenger)")
    parser.add_argument(
        "--feed-tours",
        default=",".join(TOURS),
        help="circuite trimise la update() (implicit toate)",
    )
    parser.add_argument("--first-year", type=int, default=1990)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--json", type=Path, help="scrie metricile în acest fișier JSON")
    parser.add_argument("--records", type=Path, help="scrie predicțiile meci cu meci (JSON)")
    parser.add_argument("--salt", default="", help="sare pentru orientarea first/second")
    parser.add_argument(
        "--odds",
        choices=("none", "avg", "ps", "b365", "max"),
        default="none",
        help="cote de închidere tennis-data.co.uk în ctx (implicit: fără cote)",
    )
    parser.add_argument("--odds-dir", type=Path, help="dosarul cu {tour}_{an}.xlsx")
    parser.add_argument(
        "--locked-test",
        action="store_true",
        help=f"permite anul de test blocat {LOCKED_TEST_YEAR} (o dată pe versiune)",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    try:
        years = [int(part) for part in args.years.split(",") if part.strip()]
        check_years(years, args.locked_test)
        params = _parse_params(args.param)
    except LockedTestError as error:
        print(f"REFUZAT: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Eroare: {error}", file=sys.stderr)
        return 2
    if args.locked_test:
        banner = "!" * 78
        print(
            f"{banner}\n!!! ATENȚIE: rulezi TESTUL BLOCAT ({args.years}). "
            "Rezultatul NU se folosește pentru tuning.\n"
            f"!!! Rulează-l o singură dată pe versiune de model.\n{banner}",
            file=sys.stderr,
        )
    factory = load_factory(args.model)
    if params:
        base_factory = factory

        def factory():
            return base_factory(**params)

    tours = [part.strip() for part in args.tours.split(",") if part.strip()]
    feed = [part.strip() for part in args.feed_tours.split(",") if part.strip()]
    result = run_benchmark(
        factory,
        years,
        tours,
        args.first_year,
        feed,
        data_dir=args.data_dir,
        cache_dir=None if args.no_cache else args.cache_dir,
        return_records=args.records is not None,
        locked_test=args.locked_test,
        salt=args.salt,
        odds=None if args.odds == "none" else args.odds,
        odds_dir=args.odds_dir,
    )
    result["meta"]["model"] = args.model
    result["meta"]["params"] = params
    print(format_table(result["metrics"]))
    meta = result["meta"]
    print(
        f"rânduri: {meta['rows_fed']} trimise, {meta['rows_evaluated']} evaluate; "
        f"încărcare {meta['load_seconds']}s, rulare {meta['run_seconds']}s"
    )
    if "odds" in meta:
        rates = ", ".join(
            f"{key} {group['with_odds']}/{group['played']} ({group['rate']:.1%})"
            for key, group in meta["odds"]["groups"].items()
            if group["played"]
        )
        print(f"cote {meta['odds']['source']}: {rates}")
    if args.json:
        args.json.write_text(
            json.dumps(_json_ready(result), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if args.records:
        args.records.write_text(
            json.dumps(_records_ready(result["records"]), ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
