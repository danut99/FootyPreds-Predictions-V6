"""Walk-forward, leak-free multi-market benchmark for fotbalPrediction candidates.

Every football candidate is measured with this module, so the rules below are binding.

Protocol (football-data season codes)
-------------------------------------
- history / burn-in: every season up to 2122 (default first season 0506);
- TUNE only on 2223 and 2324 (the default ``--seasons``);
- CONFIRM once on 2425 (allowed, with a warning banner);
- LOCKED TEST 2526: refused unless ``locked_test=True`` (``--locked-test``), run exactly once
  per model version by the integrator;
- 2627 (running season) is never evaluated.
Seasons after the last evaluated season are never loaded.

Data
----
Rows come from ``fotbalPrediction.data.load_rows`` (``MatchRow``), sorted by (date, time,
league, home). They are walked ONE DATE AT A TIME: first ``predict(ctx)`` for every evaluated
row of that date (all leagues), then ``update(row)`` for every row of that date. A prediction
therefore only ever depends on rows with an earlier date (football-data kick-off times are
missing before 1920, so same-date rows are never trusted to be earlier).

Model protocol
--------------
A candidate is a ``factory(**params)`` returning a fresh model object::

    class Model:
        def predict(self, ctx) -> dict[str, float]   # {market_key: probability}
        def update(self, row) -> None                # full finished MatchRow (all columns)
        def select(self, ctx, key, p) -> bool        # optional; default: p >= 0.80
        def select_high(self, ctx, key, p) -> bool   # optional; default: p >= 0.85

Keys are ``fotbalPrediction.markets.CATALOGUE`` keys (unknown keys raise). A probability is
P(outcome = 1 | not a full refund) (see ``markets``). A key may be omitted (no opinion); it is
then simply not evaluated for that match. Only ``Market.selectable`` keys can be selected.

``ctx`` is a frozen ``MatchContext`` holding ONLY pre-match fields::

    match_id, league, country, tier, season, date, time, home, away, extra
    referee        football-data referee name, or None (see below; ``--no-referee`` hides it)
    odds           None, or {market_key: price} of the ``--odds`` source (read-only mapping)
    odds_source    "avg" | "max" | "ps" | "b365" | "bfe" | "*_closing" | None
    odds_closing   True when the prices are CLOSING prices (football-data "C" columns)

Referee choice: English and Scottish referees are announced several days before kick-off
(and fixtures.csv carries them), so the referee is treated as pre-match information and
passed by default. Only England and Scotland fill it; elsewhere it is None. In production it
must come from the fixtures file; ``--no-referee`` measures how much a model depends on it.

Odds: without ``--odds`` the context never has prices. ``--odds avg|max|ps|b365|bfe`` passes
the PRE-CLOSING prices (collected on Tuesday/Friday; Pinnacle "ps" is empty from mid-2526);
``--odds closing`` (= ``avg_closing``) and ``*_closing`` pass CLOSING prices, which are only
known at kick-off and must be reported as such.

Metrics
-------
Per market key and per season (plus "all"), on settled matches (full refunds excluded):
``n``, ``base_rate``, ``mean_p``, ``log_loss``, ``brier``, ``accuracy`` (p >= 0.5 predicts a
win), ``ece`` (10 equal-width bins), ``thresholds`` (accuracy / coverage / mean fair odds 1/p
/ ROI at p >= 0.70..0.90), ``cov_80`` / ``cov_85`` (hindsight: the largest p-sorted prefix,
cut where p changes, holding >= 50 picks with accuracy >= target; with its threshold),
``transfer_80`` / ``transfer_85`` (the previous evaluated season's hindsight threshold applied
to this season: honest out-of-sample), ``select`` / ``select_high`` (the model's rules).

Per group (``markets.GROUPS`` plus "all"): the BEST PICK per match among the group's
selectable keys (the highest p; the choice never looks at the result) with the same
threshold / hindsight / transfer figures, where coverage = picks / matches with a prediction;
and the ``select`` rule pooled over the group (picks, accuracy, share of matches with at
least one pick, mean fair odds, ROI).

Per question ("1x2", "ht_1x2", "cs"): multi-class log loss, Brier (summed over classes),
argmax accuracy and top-class ECE.

ROI: flat 1-unit stakes on selected picks at REAL prices of ``--roi-odds`` (default "avg",
the pre-closing market average; quarter lines split the stake). football-data only prices
1X2, over/under 2.5 and one Asian line per match; double chance uses the price derived from
the 1X2 prices (1 / (1/a + 1/b), margin kept). Every other market has fair odds only: never
call such a selection profitable.

CLI::

    python -m fotbalPrediction.benchmark --model fotbalPrediction.candidates.baseline:factory \
        --seasons 2223,2324 [--leagues E0,SP1|main|all] [--markets goals,dc|all] \
        [--odds none|avg|max|ps|b365|bfe|closing] [--json out.json] [--records out.jsonl]
"""

from __future__ import annotations

import argparse
import ast
import importlib
import itertools
import json
import math
import sys
import time
from array import array
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np

from . import data
from . import markets as mk

LOCKED_TEST_SEASON = data.LOCKED_TEST_SEASON
RUNNING_SEASON = data.RUNNING_SEASON
CONFIRM_SEASON = data.CONFIRM_SEASON
DEFAULT_SEASONS = data.TUNE_SEASONS
DEFAULT_FIRST_SEASON = data.FIRST_SEASON
THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.90)
TARGETS = (0.80, 0.85)
DEFAULT_SELECT = 0.80
DEFAULT_SELECT_HIGH = 0.85
MIN_SELECTED = 50
EPS = 1e-12
ODDS_CHOICES = ("none", "closing") + data.ODDS_SOURCES
HEADLINE_KEYS = (
    "1",
    "X",
    "2",
    "1X",
    "X2",
    "12",
    "over05",
    "over15",
    "over25",
    "under25",
    "under35",
    "under45",
    "btts",
    "no_btts",
    "home_over05",
    "away_over05",
    "ah_1_-0.5",
    "ah_2_-0.5",
    "ah_1_+1.5",
    "ah_2_+1.5",
    "ht_over05",
    "ht_under15",
    "corners_over_8.5",
    "corners_under_11.5",
    "cards_over_2.5",
    "cards_under_5.5",
    "bookings_under_6.5",
    "sot_over_6.5",
)


class LockedTestError(ValueError):
    """The locked test season (or the running season) was requested without permission."""


@dataclass(frozen=True)
class MatchContext:
    match_id: str
    league: str
    country: str
    tier: int
    season: str
    date: date
    time: str | None
    home: str
    away: str
    referee: str | None = None
    odds: Mapping[str, float] | None = None
    odds_source: str | None = None
    odds_closing: bool = False
    extra: bool = False

    def __getitem__(self, name: str) -> Any:
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(name) from None

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    def as_dict(self) -> dict:
        output = asdict(self) if self.odds is None else {**asdict(self), "odds": dict(self.odds)}
        output["date"] = self.date.isoformat()
        return output


def resolve_odds_source(odds: str | None) -> str | None:
    if odds in (None, "", "none"):
        return None
    if odds == "closing":
        return "avg_closing"
    if odds not in data.ODDS_SOURCES:
        raise ValueError(f"Sursă de cote necunoscută: {odds}")
    return odds


def make_context(row, odds_source: str | None = None, referee: bool = True) -> MatchContext:
    """Pre-match context of a row: never its goals, stats or (unless asked) its prices."""
    prices = None
    if odds_source:
        found = row.odds.get(odds_source)
        prices = MappingProxyType(dict(found)) if found else None
    return MatchContext(
        match_id=row.id,
        league=row.league,
        country=row.country,
        tier=row.tier,
        season=row.season,
        date=row.date,
        time=row.time,
        home=row.home,
        away=row.away,
        referee=row.referee if referee else None,
        odds=prices,
        odds_source=odds_source if prices else None,
        odds_closing=bool(prices) and data.is_closing(odds_source),
        extra=row.extra,
    )


def real_price(key: str, prices: Mapping[str, float] | None) -> float | None:
    """Real decimal price of `key`, or None (double chance is derived from 1X2 prices)."""
    if not prices:
        return None
    price = prices.get(key)
    if price:
        return price
    if key in ("1X", "X2", "12"):
        pair = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}[key]
        if all(prices.get(k) for k in pair):
            return 1.0 / sum(1.0 / prices[k] for k in pair)
    return None


def check_seasons(seasons: Iterable[str], locked_test: bool) -> list[str]:
    ordered = sorted({str(s) for s in seasons}, key=data.season_start)
    if not ordered:
        raise ValueError("Trebuie cel puțin un sezon de evaluare.")
    for season in ordered:
        if len(season) != 4 or not season.isdigit():
            raise ValueError(f"Sezon invalid: {season!r} (de ex. 2324).")
    limit = data.season_start(LOCKED_TEST_SEASON)
    if any(data.season_start(s) > limit for s in ordered):
        raise LockedTestError(
            f"Sezonul {RUNNING_SEASON} este sezonul curent (parțial): doar producție, "
            "niciodată evaluare sau tuning."
        )
    if LOCKED_TEST_SEASON in ordered and not locked_test:
        raise LockedTestError(
            f"Sezonul {LOCKED_TEST_SEASON} este testul blocat; tunează doar pe "
            f"{'/'.join(DEFAULT_SEASONS)} și confirmă o dată pe {CONFIRM_SEASON}. Pentru rularea "
            "finală, o singură dată pe versiune, folosește locked_test=True (--locked-test)."
        )
    return ordered


def resolve_leagues(spec: str | Iterable[str] | None) -> tuple[str, ...]:
    if spec is None:
        return tuple(data.MAIN_LEAGUES)
    items = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if s.strip()]
    output: dict[str, None] = {}
    for item in items:
        if item == "main":
            output.update(dict.fromkeys(data.MAIN_LEAGUES))
        elif item == "extra":
            output.update(dict.fromkeys(data.EXTRA_LEAGUES))
        elif item == "all":
            output.update(dict.fromkeys(data.ALL_LEAGUES))
        elif item in data.ALL_LEAGUES:
            output[item] = None
        else:
            raise ValueError(f"Ligă necunoscută: {item}")
    if not output:
        raise ValueError("Nicio ligă selectată.")
    return tuple(output)


# --------------------------------------------------------------------------- records


class Records:
    """Compact per-(match, key) prediction records (array-backed; millions fit in memory)."""

    def __init__(self, keys: tuple[str, ...]):
        self.keys = keys
        self.key_index = {key: i for i, key in enumerate(keys)}
        self.matches: list[tuple[str, str, str, str, str]] = []  # season, league, date, h, a
        self.match = array("i")
        self.key = array("H")
        self.p = array("d")
        self.y = array("b")  # 1 won, 0 lost, -1 full refund
        self.win = array("f")
        self.loss = array("f")
        self.price = array("d")  # nan without a real price
        self.sel = array("b")
        self.sel_high = array("b")

    def add_match(self, row) -> int:
        self.matches.append((row.season, row.league, row.date.isoformat(), row.home, row.away))
        return len(self.matches) - 1

    def add(self, match, key, p, y, win, loss, price, sel, sel_high) -> None:
        self.match.append(match)
        self.key.append(self.key_index[key])
        self.p.append(p)
        self.y.append(y)
        self.win.append(win)
        self.loss.append(loss)
        self.price.append(math.nan if price is None else price)
        self.sel.append(1 if sel else 0)
        self.sel_high.append(1 if sel_high else 0)

    def arrays(self) -> dict[str, np.ndarray]:
        seasons = sorted({m[0] for m in self.matches}, key=data.season_start)
        season_of = {s: i for i, s in enumerate(seasons)}
        leagues = sorted({m[1] for m in self.matches})
        league_of = {lg: i for i, lg in enumerate(leagues)}
        match_season = np.array([season_of[m[0]] for m in self.matches], dtype=np.int32)
        match_league = np.array([league_of[m[1]] for m in self.matches], dtype=np.int32)
        match = (
            np.frombuffer(self.match, dtype=np.int32) if len(self.match) else np.zeros(0, np.int32)
        )
        return {
            "seasons": seasons,
            "leagues": leagues,
            "match": match,
            "season": match_season[match] if len(match) else np.zeros(0, np.int32),
            "league": match_league[match] if len(match) else np.zeros(0, np.int32),
            "key": np.frombuffer(self.key, dtype=np.uint16).astype(np.int32),
            "p": np.frombuffer(self.p, dtype=np.float64),
            "y": np.frombuffer(self.y, dtype=np.int8).astype(np.int32),
            "win": np.frombuffer(self.win, dtype=np.float32).astype(np.float64),
            "loss": np.frombuffer(self.loss, dtype=np.float32).astype(np.float64),
            "price": np.frombuffer(self.price, dtype=np.float64),
            "sel": np.frombuffer(self.sel, dtype=np.int8).astype(bool),
            "sel_high": np.frombuffer(self.sel_high, dtype=np.int8).astype(bool),
        }

    def iter_dicts(self):
        for i in range(len(self.match)):
            season, league, day, home, away = self.matches[self.match[i]]
            price = self.price[i]
            yield {
                "season": season,
                "league": league,
                "date": day,
                "home": home,
                "away": away,
                "key": self.keys[self.key[i]],
                "p": round(self.p[i], 6),
                "y": None if self.y[i] < 0 else self.y[i],
                "win": round(self.win[i], 3),
                "loss": round(self.loss[i], 3),
                "price": None if math.isnan(price) else price,
                "selected": bool(self.sel[i]),
                "selected_high": bool(self.sel_high[i]),
            }


# --------------------------------------------------------------------------- metrics


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(p, EPS, 1.0 - EPS)


def expected_calibration_error(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    if len(p) == 0:
        return 0.0
    index = np.minimum(bins - 1, (p * bins).astype(int))
    total = 0.0
    for b in range(bins):
        mask = index == b
        if mask.any():
            total += abs(p[mask].sum() - y[mask].sum())
    return float(total / len(p))


def _pick_stats(p, y, win, loss, price, chosen, denominator) -> dict:
    """Accuracy / coverage / mean fair odds / ROI of chosen picks (pushes: no accuracy)."""
    picked = int(chosen.sum())
    decided = chosen & (y >= 0)
    n = int(decided.sum())
    output = {
        "n_selected": picked,
        "coverage": picked / denominator if denominator else 0.0,
        "accuracy": float(y[decided].mean()) if n else None,
        "fair_odds": float(np.mean(1.0 / np.maximum(p[chosen], EPS))) if picked else None,
    }
    priced = chosen & ~np.isnan(price)
    k = int(priced.sum())
    output["n_priced"] = k
    if k:
        gains = win[priced] * (price[priced] - 1.0) - loss[priced]
        output["roi"] = float(gains.sum() / k)
        output["avg_price"] = float(price[priced].mean())
    else:
        output["roi"] = None
        output["avg_price"] = None
    return output


def coverage_at_accuracy(
    p: np.ndarray, y: np.ndarray, target: float, min_selected: int = MIN_SELECTED
) -> dict:
    """Largest p-sorted prefix (cut where p changes) with accuracy >= target (hindsight)."""
    decided = y >= 0
    p, y = p[decided], y[decided]
    n = len(p)
    best = {"coverage": 0.0, "n_selected": 0, "accuracy": None, "threshold": None}
    if n == 0:
        return best
    order = np.argsort(-p, kind="stable")
    ps, ys = p[order], y[order]
    hits = np.cumsum(ys)
    counts = np.arange(1, n + 1)
    boundary = np.ones(n, dtype=bool)
    boundary[:-1] = ps[1:] < ps[:-1]
    ok = boundary & (counts >= min_selected) & (hits / counts >= target - 1e-12)
    if ok.any():
        last = int(np.nonzero(ok)[0][-1])
        best = {
            "coverage": (last + 1) / n,
            "n_selected": last + 1,
            "accuracy": float(hits[last] / (last + 1)),
            "threshold": float(ps[last]),
        }
    return best


def binary_metrics(p, y, win, loss, price, sel, sel_high) -> dict:
    """Metrics of one market key (pushes are excluded from the probability scores)."""
    decided = y >= 0
    n = int(decided.sum())
    if n == 0:
        return {"n": 0}
    pd, yd = p[decided], y[decided].astype(float)
    q = _clip(pd)
    hits = np.where(pd == 0.5, 0.5, ((pd > 0.5) == (yd > 0.5)).astype(float))
    result = {
        "n": n,
        "n_push": int((~decided).sum()),
        "base_rate": float(yd.mean()),
        "mean_p": float(pd.mean()),
        "log_loss": float(-np.mean(yd * np.log(q) + (1 - yd) * np.log(1 - q))),
        "brier": float(np.mean((pd - yd) ** 2)),
        "accuracy": float(hits.mean()),
        "ece": expected_calibration_error(pd, yd),
        "thresholds": {
            f"{t:.2f}": _pick_stats(p, y, win, loss, price, p >= t - 1e-12, len(p))
            for t in THRESHOLDS
        },
        "select": _pick_stats(p, y, win, loss, price, sel, len(p)),
        "select_high": _pick_stats(p, y, win, loss, price, sel_high, len(p)),
    }
    for target in TARGETS:
        result[f"cov_{round(target * 100)}"] = coverage_at_accuracy(p, y, target)
    return result


def _transfer(p, y, win, loss, price, threshold, from_season) -> dict:
    if threshold is None:
        return {
            "from": from_season,
            "threshold": None,
            "accuracy": None,
            "coverage": 0.0,
            "n_selected": 0,
        }
    stats = _pick_stats(p, y, win, loss, price, p >= threshold - 1e-12, len(p))
    return {"from": from_season, "threshold": threshold, **stats}


def best_picks(arr: dict, rows: np.ndarray) -> np.ndarray:
    """Index (into arr) of the highest-p record per match among `rows` (ties: first key)."""
    if len(rows) == 0:
        return rows
    match, p = arr["match"][rows], arr["p"][rows]
    order = np.lexsort((arr["key"][rows], -p, match))
    ordered = rows[order]
    first = np.ones(len(ordered), dtype=bool)
    first[1:] = arr["match"][ordered][1:] != arr["match"][ordered][:-1]
    return ordered[first]


def _season_groups(arr: dict) -> list[tuple[str, np.ndarray]]:
    groups = [(season, arr["season"] == i) for i, season in enumerate(arr["seasons"])]
    groups.append(("all", np.ones(len(arr["season"]), dtype=bool)))
    return groups


def _with_transfer(per_season: dict, fetch) -> None:
    """Adds transfer_80/85 to each season from the previous season's hindsight threshold."""
    seasons = [s for s in per_season if s != "all"]
    for previous, season in zip(seasons, seasons[1:]):
        if not per_season[season].get("n") and not per_season[season].get("matches"):
            continue
        for target in TARGETS:
            name = f"cov_{round(target * 100)}"
            threshold = (per_season[previous].get(name) or {}).get("threshold")
            per_season[season][f"transfer_{round(target * 100)}"] = fetch(
                season, threshold, previous
            )


def key_metrics(arr: dict, keys: tuple[str, ...]) -> dict[str, dict]:
    output: dict[str, dict] = {}
    groups = _season_groups(arr)
    for index, key in enumerate(keys):
        on_key = arr["key"] == index
        if not on_key.any():
            continue
        per_season = {}
        cache = {}
        for season, mask in groups:
            rows = np.nonzero(on_key & mask)[0]
            if len(rows) == 0:
                continue
            cache[season] = rows
            per_season[season] = binary_metrics(
                arr["p"][rows],
                arr["y"][rows],
                arr["win"][rows],
                arr["loss"][rows],
                arr["price"][rows],
                arr["sel"][rows],
                arr["sel_high"][rows],
            )

        def fetch(season, threshold, previous, cache=cache):
            rows = cache[season]
            return _transfer(
                arr["p"][rows],
                arr["y"][rows],
                arr["win"][rows],
                arr["loss"][rows],
                arr["price"][rows],
                threshold,
                previous,
            )

        _with_transfer(per_season, fetch)
        output[key] = per_season
    return output


def _group_block(arr: dict, rows: np.ndarray) -> dict:
    """Best-pick and select-rule metrics of the records `rows` (one market group)."""
    matches = np.unique(arr["match"][rows])
    n_matches = len(matches)
    if n_matches == 0:
        return {"matches": 0}
    best = best_picks(arr, rows)
    p, y = arr["p"][best], arr["y"][best]
    win, loss, price = arr["win"][best], arr["loss"][best], arr["price"][best]
    block = {
        "matches": n_matches,
        "best": {
            f"{t:.2f}": _pick_stats(p, y, win, loss, price, p >= t - 1e-12, n_matches)
            for t in THRESHOLDS
        },
    }
    for target in TARGETS:
        detail = coverage_at_accuracy(p, y, target)
        if detail["n_selected"]:
            detail["coverage"] = detail["n_selected"] / n_matches
        block[f"cov_{round(target * 100)}"] = detail
    for name in ("sel", "sel_high"):
        chosen = rows[arr[name][rows]]
        stats = _pick_stats(
            arr["p"][chosen],
            arr["y"][chosen],
            arr["win"][chosen],
            arr["loss"][chosen],
            arr["price"][chosen],
            np.ones(len(chosen), dtype=bool),
            len(chosen),
        )
        stats["picks"] = stats.pop("n_selected")
        stats["coverage"] = len(np.unique(arr["match"][chosen])) / n_matches
        stats["picks_per_match"] = len(chosen) / n_matches
        block["select" if name == "sel" else "select_high"] = stats
    block["_best"] = best
    return block


def group_metrics(arr: dict, keys: tuple[str, ...]) -> dict[str, dict]:
    output: dict[str, dict] = {}
    selectable = np.array([mk.CATALOGUE[k].selectable for k in keys], dtype=bool)
    names = list(mk.GROUPS)
    key_group = np.array([names.index(mk.CATALOGUE[k].group) for k in keys], dtype=np.int32)
    usable = selectable[arr["key"]] if len(arr["key"]) else np.zeros(0, dtype=bool)
    record_group = key_group[arr["key"]] if len(arr["key"]) else np.zeros(0, dtype=np.int32)
    present = {names[i] for i in set(key_group.tolist())}
    groups = [g for g in names if g in present] + ["all"]
    for group in groups:
        if group == "all":
            on_group = usable
        else:
            on_group = usable & (record_group == names.index(group))
        per_season = {}
        for season, mask in _season_groups(arr):
            rows = np.nonzero(on_group & mask)[0]
            if len(rows):
                per_season[season] = _group_block(arr, rows)

        def fetch(season, threshold, previous, per_season=per_season):
            best = per_season[season]["_best"]
            n = per_season[season]["matches"]
            p = arr["p"][best]
            stats = _pick_stats(
                p,
                arr["y"][best],
                arr["win"][best],
                arr["loss"][best],
                arr["price"][best],
                p >= (threshold or 2.0) - 1e-12,
                n,
            )
            return {"from": previous, "threshold": threshold, **stats}

        _with_transfer(per_season, fetch)
        for block in per_season.values():
            block.pop("_best", None)
        if per_season:
            output[group] = per_season
    return output


def question_metrics(arr: dict, keys: tuple[str, ...]) -> dict[str, dict]:
    output: dict[str, dict] = {}
    index = {key: i for i, key in enumerate(keys)}
    n_matches = len(arr["season"]) and int(arr["match"].max()) + 1
    for question, members in mk.QUESTIONS.items():
        if not all(k in index for k in members):
            continue
        k = len(members)
        probs = np.full((n_matches, k), np.nan)
        truth = np.full((n_matches, k), -1, dtype=np.int32)
        for column, key in enumerate(members):
            rows = np.nonzero(arr["key"] == index[key])[0]
            probs[arr["match"][rows], column] = arr["p"][rows]
            truth[arr["match"][rows], column] = arr["y"][rows]
        complete_rows = ~np.isnan(probs).any(axis=1) & (truth >= 0).all(axis=1)
        complete_rows &= truth.sum(axis=1) == 1
        match_season = np.full(n_matches, -1)
        match_season[arr["match"]] = arr["season"]
        per_season = {}
        for position, season in enumerate(arr["seasons"] + ["all"]):
            mask = complete_rows & (
                (match_season == position) if season != "all" else np.ones(n_matches, bool)
            )
            n = int(mask.sum())
            if not n:
                continue
            p = probs[mask]
            p = p / np.maximum(p.sum(axis=1, keepdims=True), EPS)
            y = truth[mask]
            actual = y.argmax(axis=1)
            top = p.argmax(axis=1)
            confidence = p.max(axis=1)
            per_season[season] = {
                "n": n,
                "log_loss": float(-np.mean(np.log(_clip(p[np.arange(n), actual])))),
                "brier": float(np.mean(((p - y) ** 2).sum(axis=1))),
                "accuracy": float(np.mean(top == actual)),
                "ece": expected_calibration_error(confidence, (top == actual).astype(float)),
            }
        if per_season:
            output[question] = per_season
    return output


def league_metrics(arr: dict, keys: tuple[str, ...]) -> dict[str, dict]:
    """Per league (all evaluated seasons): best pick over all selectable keys and 1X2 accuracy."""
    output: dict[str, dict] = {}
    selectable = np.array([mk.CATALOGUE[k].selectable for k in keys], dtype=bool)
    usable = selectable[arr["key"]] if len(arr["key"]) else np.zeros(0, dtype=bool)
    for position, league in enumerate(arr["leagues"]):
        rows = np.nonzero(usable & (arr["league"] == position))[0]
        if len(rows) == 0:
            continue
        block = _group_block(arr, rows)
        block.pop("_best", None)
        output[league] = {
            "matches": block["matches"],
            "best_0.80": block["best"]["0.80"],
            "best_0.85": block["best"]["0.85"],
            "select": block["select"],
            "select_high": block["select_high"],
        }
    return output


def summarize(records: Records) -> dict:
    arr = records.arrays()
    keys = records.keys
    return {
        "questions": question_metrics(arr, keys),
        "groups": group_metrics(arr, keys),
        "keys": key_metrics(arr, keys),
        "leagues": league_metrics(arr, keys),
    }


# --------------------------------------------------------------------------- walk-forward


def _validate(prediction, ctx) -> dict[str, float]:
    if not isinstance(prediction, Mapping):
        raise TypeError(f"predict() trebuie să întoarcă un dict, nu {type(prediction).__name__}")
    output = {}
    for key, value in prediction.items():
        if key not in mk.CATALOGUE:
            raise ValueError(f"predict() a întors cheia necunoscută {key!r} ({ctx.match_id}).")
        p = float(value)
        if not 0.0 <= p <= 1.0:  # also rejects NaN
            raise ValueError(f"predict() a întors {p!r} pentru {key} ({ctx.match_id}).")
        output[key] = p
    return output


def run_benchmark(
    factory: Callable[[], Any],
    seasons: Iterable[str] = DEFAULT_SEASONS,
    leagues: Iterable[str] | None = None,
    first_season: str = DEFAULT_FIRST_SEASON,
    feed_leagues: Iterable[str] | None = None,
    *,
    raw_dir: str | Path = data.RAW_DIR,
    cache_dir: str | Path | None = data.CACHE_DIR,
    rows: list | None = None,
    markets: str | Iterable[str] | None = None,
    odds: str | None = None,
    roi_odds: str = "avg",
    referee: bool = True,
    locked_test: bool = False,
    return_records: bool = False,
) -> dict:
    """Walk-forward benchmark. Returns {"metrics", "meta"[, "records"]}.

    ``leagues`` are evaluated (default: the 22 main leagues); ``feed_leagues`` (default: the
    evaluated leagues plus every main league) reach ``update``. ``rows`` lets callers pass
    pre-loaded rows (they are filtered by season and league here).
    """
    seasons = check_seasons(seasons, locked_test)
    eval_leagues = set(resolve_leagues(leagues))
    feed = (
        set(resolve_leagues(feed_leagues))
        if feed_leagues is not None
        else (eval_leagues | set(data.MAIN_LEAGUES))
    )
    feed |= eval_leagues
    if data.season_start(first_season) > data.season_start(seasons[0]):
        raise ValueError("first_season trebuie să fie <= primul sezon evaluat.")
    odds_source = resolve_odds_source(odds)
    roi_source = resolve_odds_source(roi_odds) if roi_odds not in (None, "none") else None
    keys = mk.resolve_keys(markets)
    wanted = set(keys)
    started = time.perf_counter()
    last_start = data.season_start(seasons[-1])
    first_start = data.season_start(first_season)
    if rows is None:
        rows = data.load_rows(
            sorted(feed, key=data.ALL_LEAGUES.index),
            first_season,
            seasons[-1],
            raw_dir=raw_dir,
            cache_dir=cache_dir,
        )
    rows = [
        r
        for r in rows
        if r.league in feed and first_start <= data.season_start(r.season) <= last_start
    ]
    rows.sort(key=data.sort_key)
    loaded = time.perf_counter()

    model = factory()
    select = getattr(model, "select", None)
    select_high = getattr(model, "select_high", None)
    has_select, has_high = callable(select), callable(select_high)
    wanted_seasons = set(seasons)
    records = Records(keys)
    stats_by_key = {key: mk.CATALOGUE[key] for key in keys}
    fed = evaluated = predicted_keys = 0
    previous_day = None
    for day, batch_iter in itertools.groupby(rows, key=lambda r: r.date):
        if previous_day is not None and day <= previous_day:
            raise AssertionError("Rândurile nu sunt în ordine cronologică.")
        previous_day = day
        batch = list(batch_iter)
        for row in batch:
            if row.season not in wanted_seasons or row.league not in eval_leagues:
                continue
            ctx = make_context(row, odds_source, referee)
            prediction = _validate(model.predict(ctx), ctx)
            evaluated += 1
            match = records.add_match(row)
            prices = row.odds.get(roi_source) if roi_source else None
            pairs: dict[str, tuple[int, int] | None] = {}
            for key, p in prediction.items():
                if key not in wanted:
                    continue
                market = stats_by_key[key]
                if market.stat not in pairs:
                    pairs[market.stat] = mk.stat_pair(row, market.stat)
                pair = pairs[market.stat]
                if pair is None:
                    continue
                win, loss = market.settle_pair(*pair)
                y = mk.outcome_of(win, loss)
                chosen = market.selectable and (
                    bool(select(ctx, key, p)) if has_select else p >= DEFAULT_SELECT - 1e-12
                )
                chosen_high = market.selectable and (
                    bool(select_high(ctx, key, p)) if has_high else p >= DEFAULT_SELECT_HIGH - 1e-12
                )
                price = real_price(key, prices) if market.priced else None
                records.add(
                    match,
                    key,
                    p,
                    -1 if y is None else int(y),
                    win,
                    loss,
                    price,
                    chosen,
                    chosen_high,
                )
                predicted_keys += 1
        for row in batch:
            model.update(row)
            fed += 1
    finished = time.perf_counter()
    result = {
        "metrics": summarize(records),
        "meta": {
            "seasons": seasons,
            "leagues": sorted(eval_leagues, key=data.ALL_LEAGUES.index),
            "feed_leagues": sorted(feed, key=data.ALL_LEAGUES.index),
            "first_season": first_season,
            "rows_fed": fed,
            "matches_evaluated": evaluated,
            "records": predicted_keys,
            "has_select": has_select,
            "has_select_high": has_high,
            "default_select": None if has_select else DEFAULT_SELECT,
            "default_select_high": None if has_high else DEFAULT_SELECT_HIGH,
            "odds": odds_source,
            "odds_closing": bool(odds_source) and data.is_closing(odds_source),
            "roi_odds": roi_source,
            "referee": referee,
            "load_seconds": round(loaded - started, 2),
            "run_seconds": round(finished - loaded, 2),
            "locked_test": locked_test,
        },
    }
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


def _f(value, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _pct(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}"


def _pick(stats: dict | None) -> str:
    """acc%/cov%/mean fair odds[/roi%@priced picks]."""
    if not stats:
        return "-"
    accuracy, coverage = _pct(stats.get("accuracy")), _pct(stats.get("coverage"))
    text = f"{accuracy}/{coverage}/{_f(stats.get('fair_odds'), 2)}"
    if stats.get("roi") is not None:
        # The number of priced picks behind the ROI: a handful of bets means nothing.
        text += f"/{100 * stats['roi']:+.1f}@{stats.get('n_priced', 0)}"
    return text


def _rows(per_season: dict) -> list:
    """Season rows; the pooled "all" row is dropped when it repeats a single season."""
    items = list(per_season.items())
    if len(items) == 2 and items[-1][0] == "all":
        return items[:1]
    return items


def format_report(metrics: dict, keys: Iterable[str] | None = None) -> str:
    lines = [
        "ÎNTREBĂRI (multi-clasă)",
        f"{'sezon':<6}{'piață':<8}{'n':>7}{'logloss':>9}{'brier':>8}{'acc':>7}{'ece':>7}",
    ]
    for question, per_season in metrics["questions"].items():
        for season, m in _rows(per_season):
            lines.append(
                f"{season:<6}{question:<8}{m['n']:>7}{m['log_loss']:>9.4f}"
                f"{m['brier']:>8.4f}{_pct(m['accuracy']):>7}{m['ece']:>7.3f}"
            )
    lines += [
        "",
        "GRUPURI: cea mai probabilă piață pe meci (acc%/cov%/cota corectă medie"
        "[/ROI% la cote reale])",
        f"{'sezon':<6}{'grup':<13}{'meciuri':>8}{'best>=.80':>27}{'best>=.85':>27}"
        f"{'cov@80 (prag)':>18}{'cov@85 (prag)':>18}{'transfer80':>27}{'select':>27}"
        f"{'select_high':>27}",
    ]
    for group, per_season in metrics["groups"].items():
        for season, m in _rows(per_season):
            c80, c85 = m["cov_80"], m["cov_85"]
            lines.append(
                f"{season:<6}{group:<13}{m['matches']:>8}{_pick(m['best']['0.80']):>27}"
                f"{_pick(m['best']['0.85']):>27}"
                f"{_pct(c80['coverage']) + ' (' + _f(c80['threshold']) + ')':>18}"
                f"{_pct(c85['coverage']) + ' (' + _f(c85['threshold']) + ')':>18}"
                f"{_pick(m.get('transfer_80')):>27}{_pick(m['select']):>27}"
                f"{_pick(m['select_high']):>27}"
            )
    wanted = list(keys) if keys is not None else list(HEADLINE_KEYS)
    lines += [
        "",
        "PIEȚE (binare; acc%/cov%/cota corectă medie[/ROI%])",
        f"{'sezon':<6}{'cheie':<20}{'n':>7}{'bază':>7}{'logloss':>9}{'brier':>8}"
        f"{'acc':>7}{'ece':>7}{'cov@80 (prag)':>16}{'cov@85 (prag)':>16}"
        f"{'transfer80':>27}{'select':>27}{'select_high':>27}",
    ]
    for key in wanted:
        per_season = metrics["keys"].get(key)
        if not per_season:
            continue
        for season, m in _rows(per_season):
            if not m.get("n"):
                continue
            c80, c85 = m["cov_80"], m["cov_85"]
            lines.append(
                f"{season:<6}{key:<20}{m['n']:>7}{_pct(m['base_rate']):>7}{m['log_loss']:>9.4f}"
                f"{m['brier']:>8.4f}{_pct(m['accuracy']):>7}{m['ece']:>7.3f}"
                f"{_pct(c80['coverage']) + ' (' + _f(c80['threshold']) + ')':>16}"
                f"{_pct(c85['coverage']) + ' (' + _f(c85['threshold']) + ')':>16}"
                f"{_pick(m.get('transfer_80')):>27}{_pick(m['select']):>27}"
                f"{_pick(m['select_high']):>27}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fotbalPrediction.benchmark",
        description="Benchmark walk-forward fără leakage pentru modelele de fotbal.",
    )
    parser.add_argument(
        "--model", default="fotbalPrediction.candidates.baseline:factory", help="modul:factory"
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="nume=valoare transmis la factory (se poate repeta)",
    )
    parser.add_argument(
        "--seasons",
        default=",".join(DEFAULT_SEASONS),
        help="sezoane evaluate (coduri football-data, de ex. 2223,2324)",
    )
    parser.add_argument("--leagues", default="main", help="ligi evaluate: coduri, main, extra, all")
    parser.add_argument(
        "--feed-leagues", help="ligi trimise la update() (implicit: evaluate + main)"
    )
    parser.add_argument("--first-season", default=DEFAULT_FIRST_SEASON)
    parser.add_argument("--markets", default="all", help="chei, grupuri sau statistici evaluate")
    parser.add_argument(
        "--odds",
        choices=ODDS_CHOICES,
        default="none",
        help="cote în ctx (implicit fără; 'closing' = cote de ÎNCHIDERE)",
    )
    parser.add_argument(
        "--roi-odds",
        choices=ODDS_CHOICES,
        default="avg",
        help="cotele reale pentru ROI (implicit media pre-închidere)",
    )
    parser.add_argument("--no-referee", action="store_true", help="ascunde arbitrul din ctx")
    parser.add_argument("--raw-dir", type=Path, default=data.RAW_DIR)
    parser.add_argument("--cache-dir", type=Path, default=data.CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--json", type=Path, help="scrie metricile în acest fișier JSON")
    parser.add_argument("--records", type=Path, help="scrie predicțiile (JSONL, o linie pe piață)")
    parser.add_argument("--keys", help="chei afișate în tabelul pe piețe (implicit: principale)")
    parser.add_argument(
        "--locked-test",
        action="store_true",
        help=f"permite sezonul de test blocat {LOCKED_TEST_SEASON} (o dată pe versiune)",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    try:
        seasons = [part.strip() for part in args.seasons.split(",") if part.strip()]
        seasons = check_seasons(seasons, args.locked_test)
        params = _parse_params(args.param)
        leagues = resolve_leagues(args.leagues)
        feed = resolve_leagues(args.feed_leagues) if args.feed_leagues else None
        mk.resolve_keys(args.markets)
    except LockedTestError as error:
        print(f"REFUZAT: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Eroare: {error}", file=sys.stderr)
        return 2
    banner = "!" * 78
    if args.locked_test:
        print(
            f"{banner}\n!!! ATENȚIE: rulezi TESTUL BLOCAT ({','.join(seasons)}). "
            "Rezultatul NU se folosește pentru tuning.\n"
            f"!!! Rulează-l o singură dată pe versiune de model.\n{banner}",
            file=sys.stderr,
        )
    elif CONFIRM_SEASON in seasons:
        print(
            f"{banner}\n!!! Sezonul {CONFIRM_SEASON} este CONFIRMAREA: rulează-l o singură "
            f"dată, după ce regula e înghețată.\n{banner}",
            file=sys.stderr,
        )
    factory = load_factory(args.model)
    if params:
        base_factory = factory

        def factory():
            return base_factory(**params)

    result = run_benchmark(
        factory,
        seasons,
        leagues,
        args.first_season,
        feed,
        raw_dir=args.raw_dir,
        cache_dir=None if args.no_cache else args.cache_dir,
        markets=args.markets,
        odds=args.odds,
        roi_odds=args.roi_odds,
        referee=not args.no_referee,
        locked_test=args.locked_test,
        return_records=args.records is not None,
    )
    meta = result["meta"]
    meta["model"], meta["params"] = args.model, params
    keys = mk.resolve_keys(args.keys) if args.keys else None
    print(format_report(result["metrics"], keys))
    print(
        f"meciuri evaluate: {meta['matches_evaluated']}, predicții pe piețe: {meta['records']}, "
        f"rânduri trimise: {meta['rows_fed']}; încărcare {meta['load_seconds']}s, "
        f"rulare {meta['run_seconds']}s"
    )
    if meta["odds"]:
        kind = "ÎNCHIDERE" if meta["odds_closing"] else "pre-închidere"
        print(f"cote în ctx: {meta['odds']} ({kind})")
    if args.json:
        payload = {k: v for k, v in result.items() if k != "records"}
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.records:
        args.records.parent.mkdir(parents=True, exist_ok=True)
        with args.records.open("w", encoding="utf-8") as handle:
            for item in result["records"].iter_dicts():
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
