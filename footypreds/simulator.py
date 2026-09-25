"""Blind walk-forward bankroll simulator on past matches with real historical odds.

For every day D of the chosen period:

1. The model predicts D's matches from results played BEFORE D only. The fixture it receives
   is rebuilt from pre-match fields (teams, kickoff, competition, pre-match prices), so the
   final score does not exist in the prediction input. Results of D enter the history only
   after every prediction of D is made (``predict_period``).
2. A strategy chooses the bets of D from those predictions alone (``choose_*``: they never
   receive a result), and the stakes are fixed from the bankroll at the start of D.
3. Only then are D's results revealed and the bets settled at the historical price.

Predictions are the slow part (~17 s for a football season), so they are cached on disk per
independent unit (a league season, a tennis year, a month of local history), keyed by the
dataset rows the unit may see and a hash of the model code. A cached unit is recomputed as
soon as either changes. Everything is deterministic: the same request gives the same result.
"""

import argparse
import concurrent.futures
import hashlib
import itertools
import json
import math
import os
import sys
import time
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from functools import lru_cache
from pathlib import Path

from footypreds.competitions import competition_name
from footypreds.config import DATA, PACKAGE
from footypreds.domain import Match
from footypreds.engine import PARAMS, HistoryIndex, analyze, fit_history
from footypreds.sports import analyze_match
from footypreds.sports.keys import market_margin
from footypreds.sports.settle import can_push, is_settleable, settle

PREDICTION_VERSION = "sim-3"
CACHE_DIR = DATA / "sim_cache"
THRESHOLD = 0.85
GRADES_OK = ("A", "B", "C")
MODES = ("singles", "ticket", "value")
STAKINGS = ("flat", "percent", "kelly")
MAX_TICKET_LEGS = 10
# Accepted total odds of a ticket, relative to the target (recommend.ODDS_WINDOW).
TICKET_RANGE = (0.93, 1.12)
VALUE_MIN_EV = 0.02
VALUE_MIN_PROBABILITY = 0.35
KELLY_CAP = 0.1
MAX_ROWS = 500
MAX_DAYS = 1100
MIN_STAKE = 0.01
# History window of a prediction unit (except football-data leagues, which keep every season
# like the benchmark). Every analyzer looks back at most ~2 years; tennis Elo starts 3 years back.
HISTORY_DAYS = 1100
RESULT_KEYS = {"football": ("1", "X", "2"), "basketball": ("1", "2"), "tennis": ("1", "2")}
LOGO_FIELDS = ("home_logo", "away_logo", "league_logo")
DISCLAIMER = (
    "Simulare cu bani virtuali pe meciuri din trecut. Estimări statistice, nu garanții. 18+."
)


class SimulationError(ValueError):
    """Invalid request; the message is Romanian and shown to the user."""


# --- prediction phase (blind) --------------------------------------------------------------


# Modules that shape one sport's predictions (plus engine/*.py and domain.py for every sport).
SPORT_MODULES = {
    "football": ("__init__.py", "keys.py", "settle.py"),
    "basketball": ("__init__.py", "common.py", "keys.py", "settle.py", "basketball.py"),
    "tennis": ("__init__.py", "common.py", "keys.py", "settle.py", "tennis.py"),
}


@lru_cache(maxsize=8)
def code_hash(sport="football"):
    """Hash of the code behind `sport`'s predictions; a change invalidates the disk cache."""
    digest = hashlib.sha256(f"{PREDICTION_VERSION}|{sport}".encode())
    paths = sorted((PACKAGE / "engine").glob("*.py")) + [PACKAGE / "domain.py"]
    paths += [PACKAGE / "sports" / name for name in SPORT_MODULES[sport]]
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def blind_fixture(match):
    """The fixture as it was known before kickoff: no score, no status, no live details."""
    return Match(
        id=match.id,
        kickoff=match.kickoff,
        league=match.league,
        country=match.country,
        home=match.home,
        away=match.away,
        home_id=match.home_id,
        away_id=match.away_id,
        odds=dict(match.odds),
        source=match.source,
        sport=match.sport,
        home_participant_id=match.home_participant_id,
        away_participant_id=match.away_participant_id,
    )


def prediction_row(match, analysis):
    """What a strategy may see about one fixture: model probabilities and real prices."""
    markets = []
    for market in analysis["markets"]:
        price = market.get("odds")
        if not (market["selectable"] and price and price > 1):
            continue
        if not is_settleable(match.sport, market["key"]):
            continue
        markets.append(
            {
                "key": market["key"],
                "label": market["label"],
                "group": market["group"],
                "probability": market["probability"],
                "fair_odds": market["fair_odds"],
                "odds": price,
                # Probability of a refund (football whole lines): such legs are never bet.
                "push": market.get("push", 0.0),
                # Overround of this market when all its outcomes are priced (else None).
                "margin": market_margin(match.sport, market["key"], match.odds),
            }
        )
    return {
        "id": match.id,
        "day": match.kickoff.date().isoformat(),
        "kickoff": match.kickoff.isoformat(),
        "sport": match.sport,
        "league": match.league,
        "home": match.home,
        "away": match.away,
        "grade": analysis["grade"],
        "confidence": analysis["confidence"],
        "odds": {k: v for k, v in match.odds.items() if k in RESULT_KEYS[match.sport]},
        "markets": markets,
        # Original upstream image URLs (never a result); legs expose them as display URLs.
        **{name: getattr(match, name, None) for name in LOGO_FIELDS},
    }


def _analyze(fixture, index, ratings, fit_ratings):
    if fixture.sport == "football" and fit_ratings:
        return analyze(fixture, index, THRESHOLD, params=PARAMS, ratings=ratings)
    return analyze_match(fixture, index, THRESHOLD)


def predict_period(population, target_ids, period_start, fit_ratings=False):
    """Blind walk-forward predictions for `target_ids`, day by day, from `population` only.

    population: finished matches of ONE independent history (sorted), ending with the period.
    Results of a day enter the history only after every prediction of that day; the fixture
    passed to the model is ``blind_fixture`` (no score). Football-data leagues refit team
    ratings on each betting day from results before ``day - cutoff_hours`` (the benchmark rule).
    """
    index, ratings, rows = HistoryIndex(), None, []
    earlier = [m for m in population if m.kickoff.date() < period_start]
    index.extend(earlier)
    later = population[len(earlier) :]
    for day, group in itertools.groupby(later, key=lambda m: m.kickoff.date()):
        batch = list(group)
        targets = [m for m in batch if m.id in target_ids]
        if targets:
            if fit_ratings:
                start = datetime.combine(day, dtime.min, timezone.utc)
                visible = index.before(start - timedelta(hours=PARAMS.cutoff_hours))
                ratings = fit_history(visible, start, PARAMS, init=ratings)
            for match in targets:
                fixture = blind_fixture(match)
                rows.append(prediction_row(match, _analyze(fixture, index, ratings, fit_ratings)))
        # Only now, after the whole day is predicted, do its results become history.
        index.extend(batch)
    return rows


def _unit_worker(task):
    population, target_ids, period_start, fit_ratings = task
    return predict_period(population, set(target_ids), period_start, fit_ratings)


def _fingerprint(match):
    return json.dumps(
        [
            match.id,
            match.kickoff.isoformat(),
            match.league,
            match.home,
            match.away,
            match.home_id,
            match.away_id,
            match.home_goals,
            match.away_goals,
            match.status,
            match.finish_type,
            sorted(match.odds.items()),
        ],
        ensure_ascii=False,
    ).encode()


def units(dataset, start, end):
    """Independent prediction units that overlap [start, end].

    Each unit is (group, period, population, target_ids, period_start, key): the key hashes the
    code and every row the unit can see, so poisoning a later row never touches an earlier
    unit, and any change to a visible row recomputes the unit.
    """
    output = []
    for group, rows in dataset.groups.items():
        digest = hashlib.sha256(f"{code_hash(dataset.sport_of(group))}|{group}".encode())
        position = 0
        dates = [m.kickoff.date() for m in rows]
        for period, members in itertools.groupby(rows, key=dataset.period_of):
            members = list(members)
            for match in members:
                digest.update(_fingerprint(match))
            position += len(members)
            first, last = members[0].kickoff.date(), members[-1].kickoff.date()
            if last < start or first > end:
                continue
            targets = [m.id for m in members if dataset.is_target(m)]
            if not targets:
                continue
            key = digest.copy()
            key.update(f"|{period}|{dataset.fit_ratings}|{HISTORY_DAYS}".encode())
            if dataset.targets is not None:
                key.update(json.dumps(targets).encode())
            lowest = 0
            if not dataset.fit_ratings:
                lowest = bisect_left(dates, first - timedelta(days=HISTORY_DAYS), 0, position)
            population = rows[lowest:position]
            output.append(
                {
                    "group": group,
                    "period": period,
                    "population": population,
                    "targets": targets,
                    "start": first,
                    "key": key.hexdigest()[:24],
                }
            )
    return output


def _cache_path(cache_dir, dataset_id, unit):
    name = f"{unit['group']}-{unit['period']}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return Path(cache_dir) / dataset_id / f"{safe}-{unit['key']}.json"


def _read_cache(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload.get("rows") if isinstance(payload, dict) else None


def _write_cache(path, unit, rows):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = path.name[: -len(unit["key"]) - 5]
        for old in path.parent.glob(f"{prefix}*.json"):
            if old != path:
                old.unlink(missing_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"key": unit["key"], "rows": rows}, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        pass  # A read-only disk only costs speed.


def predictions(dataset, start, end, cache_dir=CACHE_DIR, workers=None):
    """({day: [prediction rows]} for [start, end], stats). Missing units run in parallel."""
    started = time.perf_counter()
    todo, found, cached = [], [], 0
    for unit in units(dataset, start, end):
        path = _cache_path(cache_dir, dataset.id, unit) if cache_dir else None
        rows = _read_cache(path) if path else None
        if rows is None:
            todo.append((unit, path))
        else:
            found.extend(rows)
            cached += 1
    tasks = [
        (unit["population"], unit["targets"], unit["start"], dataset.fit_ratings)
        for unit, _ in todo
    ]
    workers = (os.cpu_count() or 2) - 1 if workers is None else workers
    workers = max(1, min(workers, 8, len(tasks)))
    if workers > 1:
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_unit_worker, tasks))
        except (OSError, RuntimeError, concurrent.futures.process.BrokenProcessPool):
            results = [_unit_worker(task) for task in tasks]
    else:
        results = [_unit_worker(task) for task in tasks]
    for (unit, path), rows in zip(todo, results, strict=True):
        if path:
            _write_cache(path, unit, rows)
        found.extend(rows)
    by_day = {}
    first, last = start.isoformat(), end.isoformat()
    for row in sorted(found, key=lambda r: (r["kickoff"], r["id"])):
        if first <= row["day"] <= last:
            by_day.setdefault(row["day"], []).append(row)
    stats = {
        "units": len(todo) + cached,
        "computed": len(todo),
        "seconds": round(time.perf_counter() - started, 2),
    }
    return by_day, stats


# --- choosing bets (never sees a result) ---------------------------------------------------


def display_logo(url):
    """Same-origin "/api/img?u=..." URL of an allowed upstream image, else None."""
    if not url:
        return None
    try:
        from footypreds.media import display_url
    except ImportError:  # pragma: no cover - media.py ships with the app
        return None
    return display_url(url)


def leg_of(row, market, probability=None):
    probability = market["probability"] if probability is None else probability
    return {
        "match_id": row["id"],
        "sport": row["sport"],
        "kickoff": row["kickoff"],
        "competition": competition_name(row["league"]),
        "home": row["home"],
        "away": row["away"],
        "key": market["key"],
        "label": market["label"],
        "group": market["group"],
        "probability": probability,
        "odds": market["odds"],
        "fair_odds": 1 / probability if probability > 0 else None,
        "ev": probability * market["odds"] - 1,
        "grade": row["grade"],
        "confidence": row["confidence"],
        "status": "pending",
        "score": None,
        **{name: display_logo(row.get(name)) for name in LOGO_FIELDS},
    }


@dataclass(frozen=True)
class Rules:
    """How legs are filtered and combined: the SAME rules as the daily recommendations."""

    optimize: Callable  # (legs, target, max_legs) -> legs
    max_legs: Callable  # target -> int
    leg_odds: tuple = (1.08, 4.0)
    min_value: float | None = 0.95
    max_value: float | None = 1.05
    single_min_odds: float = 1.2
    window: tuple = TICKET_RANGE
    source: str = "local"


def local_rules():
    return Rules(
        optimize=lambda legs, target, max_legs: best_combination(legs, target, max_legs),
        max_legs=lambda target: MAX_TICKET_LEGS,
    )


def product_rules():
    """recommend.py's optimizer, odds band and value floor when present; else a local copy.

    Using the recommendations' own code keeps the simulation honest: it bets exactly what the
    home page would have recommended on those days (same leg filter, same optimizer).
    """
    try:
        from footypreds import recommend
    except ImportError:
        return local_rules()
    return Rules(
        optimize=lambda legs, target, max_legs: recommend.optimize(legs, target, max_legs),
        max_legs=recommend.auto_max_legs,
        leg_odds=tuple(recommend.LEG_ODDS),
        min_value=recommend.MIN_VALUE,
        max_value=recommend.MAX_VALUE,
        single_min_odds=recommend.SINGLE_MIN_ODDS,
        window=tuple(recommend.ODDS_WINDOW),
        source="recommend",
    )


def leg_allowed(sport, key, probability, odds, rules, push=False, margin=None):
    """recommend.leg_allowed (the app's own leg rule) with these rules' bounds."""
    try:
        from footypreds.recommend import leg_allowed as allowed
    except ImportError:  # pragma: no cover - recommend.py ships with the app
        allowed = None
    if allowed is not None:
        return allowed(
            sport,
            key,
            probability,
            odds,
            push=push,
            leg_odds=rules.leg_odds,
            min_value=rules.min_value,
            max_value=rules.max_value,
            margin=margin,
        )
    value = probability * odds * (margin or 1.06)
    return (
        not push
        and not can_push(sport, key)
        and probability > 0
        and rules.leg_odds[0] <= odds <= rules.leg_odds[1]
        and (rules.min_value is None or value >= rules.min_value)
        and (rules.max_value is None or value <= rules.max_value)
    )


def model_legs(row, rules):
    """Bettable legs of one fixture with the SAME rules as the recommendations: selectable,
    real price, grade A-C (grade D only on fully priced markets, see sports.legs.candidate_legs),
    odds band, MIN_VALUE <= fair value <= MAX_VALUE, no refunds."""
    grade_ok = row["grade"] in GRADES_OK
    return [
        leg_of(row, market)
        for market in row["markets"]
        if (grade_ok or market.get("margin") is not None)
        and leg_allowed(
            row["sport"],
            market["key"],
            market["probability"],
            market["odds"],
            rules,
            push=(market.get("push") or 0) > 1e-12,
            margin=market.get("margin"),
        )
    ]


RESULT_LABELS = {
    "football": {"1": "Victorie gazde", "X": "Egal", "2": "Victorie oaspeți"},
    "basketball": {"1": "Victorie gazde", "2": "Victorie oaspeți"},
    "tennis": {"1": "Victorie jucătorul 1", "2": "Victorie jucătorul 2"},
}


def favourite_leg(row, rules):
    """The bookmaker favourite (lowest price of the result market), margin-free probability."""
    keys = RESULT_KEYS[row["sport"]]
    prices = row["odds"]
    if not all(prices.get(k, 0) > 1 for k in keys):
        return None
    inverse = {k: 1 / prices[k] for k in keys}
    total = sum(inverse.values())
    key = min(keys, key=lambda k: (prices[k], k))
    if not rules.leg_odds[0] <= prices[key] <= rules.leg_odds[1]:
        return None
    label = RESULT_LABELS[row["sport"]][key]
    market = {"key": key, "label": label, "group": "Rezultat final", "odds": prices[key]}
    return leg_of(row, market, inverse[key] / total)


def _singles(legs, count, min_odds, score):
    """Best leg per match by `score`, then the `count` best matches (deterministic)."""
    best = {}
    for item in legs:
        if item["odds"] < min_odds:
            continue
        current = best.get(item["match_id"])
        if current is None or score(item) > score(current):
            best[item["match_id"]] = item
    ranked = sorted(best.values(), key=lambda x: (score(x), x["match_id"]), reverse=True)
    return [{"legs": [x]} for x in ranked[:count]]


def _safest(item):
    return (item["probability"], item["odds"], item["key"])


def choose_singles(rows, count, rules):
    legs = [x for r in rows for x in model_legs(r, rules)]
    return _singles(legs, count, rules.single_min_odds, _safest)


def choose_value(rows, count, rules):
    # The value strategy exists to test the model's disagreements with the price, so it is the
    # one strategy without the recommendations' MAX_VALUE cap (the app never recommends these).
    uncapped = replace(rules, max_value=None)
    legs = [
        x
        for r in rows
        for x in model_legs(r, uncapped)
        if x["ev"] >= VALUE_MIN_EV and x["probability"] >= VALUE_MIN_PROBABILITY
    ]
    return _singles(legs, count, rules.single_min_odds, lambda x: (x["ev"], x["probability"]))


def best_combination(legs, target, max_legs=MAX_TICKET_LEGS, window=TICKET_RANGE, step=0.005):
    """Local fallback optimizer: one leg per match, total odds in the window, max probability.

    Knapsack in log space: the state is (legs, rounded log of the total odds), the value the
    log of the probability. Deterministic for the same input.
    """
    low, high = math.log(target * window[0]), math.log(target * window[1])
    by_match = {}
    for item in legs:
        if item["odds"] > 1 and item["probability"] > 0 and math.log(item["odds"]) <= high:
            by_match.setdefault(item["match_id"], []).append(item)
    states = {(0, 0): (0.0, 0.0, ())}
    for match_id in sorted(by_match):
        options = sorted(by_match[match_id], key=lambda x: (-x["probability"], x["odds"], x["key"]))
        options = [(x, math.log(x["odds"]), math.log(x["probability"])) for x in options]
        updated = dict(states)
        for value, weight, chosen in states.values():
            if len(chosen) >= max_legs:
                continue
            for item, log_odds, log_p in options:
                total = weight + log_odds
                if total > high + 1e-12:
                    continue
                score = value + log_p
                slot = (len(chosen) + 1, int(total / step))
                current = updated.get(slot)
                if current is None or score > current[0] + 1e-12:
                    updated[slot] = (score, total, (*chosen, item))
        states = updated
    valid = [s for s in states.values() if s[2] and low - 1e-12 <= s[1] <= high + 1e-12]
    if not valid:
        return []
    best = max(valid, key=lambda s: (round(s[0], 9), s[1], -len(s[2])))
    return list(best[2])


def choose_ticket(rows, target, rules):
    legs = [x for r in rows for x in model_legs(r, rules)]
    chosen = rules.optimize(legs, target, rules.max_legs(target))
    return [{"legs": list(chosen)}] if chosen else []


def choose_baseline(rows, mode, count, target, rules):
    """Same bet count, bookmaker favourites instead of the model (no model involved)."""
    legs = [x for x in (favourite_leg(r, rules) for r in rows) if x]
    if mode == "ticket":
        chosen = rules.optimize(legs, target, rules.max_legs(target))
        return [{"legs": list(chosen)}] if chosen else []
    return _singles(legs, count, rules.single_min_odds, _safest)


# --- staking and settlement ---------------------------------------------------------------


def money(value):
    return round(value + 1e-9, 2)


def bet_odds(bet):
    return math.prod(x["odds"] for x in bet["legs"])


def bet_probability(bet):
    return math.prod(x["probability"] for x in bet["legs"])


def kelly_fraction(probability, odds):
    """Full-Kelly fraction of the bankroll (0 when there is no edge)."""
    if odds <= 1:
        return 0.0
    return max(0.0, (probability * odds - 1) / (odds - 1))


def stakes_for(bets, bankroll, staking, stake, kelly_cap=KELLY_CAP):
    """Stakes of one day's bets from the bankroll at the start of the day; never above it."""
    amounts = []
    for bet in bets:
        if staking == "flat":
            amount = stake
        elif staking == "percent":
            amount = stake * bankroll
        else:
            fraction = min(kelly_cap, stake * kelly_fraction(bet_probability(bet), bet_odds(bet)))
            amount = fraction * bankroll
        amounts.append(math.floor(max(0.0, amount) * 100 + 1e-6) / 100)
    total = sum(amounts)
    if total > bankroll:
        scale = bankroll / total
        amounts = [math.floor(a * scale * 100 + 1e-6) / 100 for a in amounts]
    return [a if a >= MIN_STAKE else 0.0 for a in amounts]


def settle_bet(bet, results):
    """Legs with won/lost/void from the revealed results; (status, payout multiplier)."""
    legs = []
    for item in bet["legs"]:
        match = results.get(item["match_id"])
        won = None
        if match is not None:
            won = settle(
                match.sport,
                item["key"],
                match.home_goals,
                match.away_goals,
                match.finish_type or match.status,
            )
        status = "void" if won is None else ("won" if won else "lost")
        score = f"{match.home_goals}-{match.away_goals}" if match is not None else None
        legs.append(item | {"status": status, "score": score})
    statuses = [x["status"] for x in legs]
    if "lost" in statuses:
        return legs, "lost", 0.0
    if all(s == "void" for s in statuses):
        return legs, "void", 1.0
    return legs, "won", math.prod(1.0 if x["status"] == "void" else x["odds"] for x in legs)


def run_bankroll(days, results, choose, *, bankroll, staking, stake, kelly_cap=KELLY_CAP):
    """Day-by-day bankroll: choose from predictions, fix stakes, THEN reveal and settle."""
    balance = peak = bankroll
    history, rows = [], []
    counts = {"won": 0, "lost": 0, "void": 0}
    staked = drawdown = 0.0
    odds_sum = streak = longest = 0
    stopped = None
    for day in sorted(days):
        if balance < MIN_STAKE:
            stopped = stopped or day
            break
        bets = choose(days[day])
        if not bets:
            continue
        amounts = stakes_for(bets, balance, staking, stake, kelly_cap)
        placed = [(bet, amount) for bet, amount in zip(bets, amounts, strict=True) if amount > 0]
        if not placed:
            continue
        opening = balance
        balance = money(balance - sum(amount for _, amount in placed))
        day_rows = []
        # Results are looked up only here, after every stake of the day is fixed.
        for bet, amount in placed:
            legs, status, multiplier = settle_bet(bet, results)
            odds = bet_odds(bet)
            payout = money(amount * multiplier) if status != "lost" else 0.0
            counts[status] += 1
            staked += amount
            odds_sum += odds
            if status == "lost":
                streak += 1
                longest = max(longest, streak)
            elif status == "won":
                streak = 0
            balance = money(balance + payout)
            day_rows.append(
                {
                    "date": day,
                    "legs": legs,
                    "stake": amount,
                    "odds": round(odds, 4),
                    "probability": bet_probability(bet),
                    "result": status,
                    "payout": payout,
                    "return": payout,
                }
            )
        for row in day_rows:
            first = row["legs"][0]
            single = len(row["legs"]) == 1
            row.update(
                match_id=first["match_id"] if single else "",
                home=first["home"] if single else "Bilet",
                away=first["away"] if single else f"{len(row['legs'])} selecții",
                competition=first["competition"] if single else "",
                key=first["key"] if single else "ticket",
                label=first["label"] if single else " + ".join(x["label"] for x in row["legs"]),
                bankroll=balance,
                bankroll_after=balance,
                bankroll_before=opening,
            )
        rows.extend(day_rows)
        peak = max(peak, balance)
        drawdown = max(drawdown, (peak - balance) / peak if peak else 0.0)
        history.append({"date": day, "bankroll": balance})
    if stopped is None and balance < MIN_STAKE and rows:
        stopped = rows[-1]["date"]
    bets = sum(counts.values())
    settled = counts["won"] + counts["lost"]
    profit = money(balance - bankroll)
    return {
        "initial": money(bankroll),
        "final": money(balance),
        "profit": profit,
        "staked": money(staked),
        "roi": profit / staked if staked else 0.0,
        "growth": profit / bankroll if bankroll else 0.0,
        "bets": bets,
        **counts,
        "hit_rate": counts["won"] / settled if settled else None,
        "max_drawdown": drawdown,
        "peak": money(peak),
        "longest_losing_streak": longest,
        "avg_odds": odds_sum / bets if bets else None,
        "betting_days": len(history),
        "stopped": stopped,
        "history": history,
        "rows": rows,
    }


# --- ladder (rollover) -------------------------------------------------------------------
#
# One ticket per day near the target odds; the whole ladder bankroll (or `reinvest` of it) rides
# on it. A lost ticket ends the ladder: what was not staked is kept (nothing with reinvest 1)
# and, with restart_on_loss, a new ladder starts on the next ticket day with the initial amount
# again, which counts as fresh money invested. A void ticket refunds its stake (the ladder
# goes on), a partly void ticket pays the settled odds of its decided legs. `max_days` cashes a
# ladder out after that many successful tickets and starts a new one.

LADDER_STATUSES = ("lost", "cashed", "open")
REINVEST_DEFAULT = 1.0
MAX_LADDER_DAYS = 365


def ladder_ticket(bet, legs=None):
    """A ladder day's ticket: legs (market, logos, odds, probability, result) and totals."""
    legs = legs if legs is not None else bet["legs"]
    return {
        "legs": [
            item | {"market": item["label"], "result": item.get("status", "pending")}
            for item in legs
        ],
        "total_odds": round(bet_odds(bet), 4),
        "probability": bet_probability(bet),
        "ev": bet_probability(bet) * bet_odds(bet) - 1,
    }


def _ladder_record(ladder):
    return {
        "index": ladder["index"],
        "start": ladder["start"],
        "end": ladder["end"],
        "days": ladder["days"],
        "tickets": ladder["tickets"],
        "won": ladder["won"],
        "void": ladder["void"],
        "invested": ladder["invested"],
        "peak": money(ladder["peak"]),
        "final": money(ladder["bankroll"]),
        "status": ladder["status"],
    }


def run_ladder(
    day_keys,
    by_day,
    results,
    choose,
    *,
    bankroll,
    reinvest=REINVEST_DEFAULT,
    restart_on_loss=True,
    max_days=None,
    no_rows_reason="Nu există meciuri cu cote în această zi.",
):
    """Day-by-day ladder: choose ONE ticket from blind predictions, fix the stake, THEN reveal.

    day_keys: every ISO day to walk (days without rows are logged as skipped); by_day: blind
    prediction rows per day; results: match id -> finished Match, read only when settling.
    """
    initial = money(bankroll)
    ladders, entries, equity, rows = [], [], [], []
    current = None
    invested = returned = staked = 0.0
    counts = {"won": 0, "lost": 0, "void": 0}
    odds_sum = losing = longest_losing = 0
    peak_value, drawdown = initial, 0.0
    stopped = None

    def value():
        """Money position: returned + open ladder - invested, on top of the initial amount."""
        open_value = current["bankroll"] if current else 0.0
        return initial + returned + open_value - invested

    for day in day_keys:
        rows_of_day = by_day.get(day) or []
        bets = choose(rows_of_day) if rows_of_day else []
        if not bets:
            if not rows_of_day:
                reason = no_rows_reason
            elif all(isinstance(r, dict) and r.get("grade") not in GRADES_OK for r in rows_of_day):
                reason = (
                    "Toate meciurile zilei au date insuficiente (nota D); încarcă mai multe zile "
                    "de istoric."
                )
            else:
                reason = "Niciun bilet nu atinge cota țintă cu selecții eligibile în această zi."
            waiting = current["bankroll"] if current else (0.0 if ladders else initial)
            entries.append(
                {
                    "date": day,
                    "ticket": None,
                    "stake": 0.0,
                    "result": "skipped",
                    "reason": reason,
                    "payout": 0.0,
                    "bankroll_before": money(waiting),
                    "bankroll_after": money(waiting),
                    "ladder_index": current["index"] if current else len(ladders) + 1,
                    "streak_day": current["days"] if current else 0,
                }
            )
            continue
        bet = bets[0]
        if current is None:
            current = {
                "index": len(ladders) + 1,
                "start": day,
                "end": None,
                "days": 0,
                "tickets": 0,
                "won": 0,
                "void": 0,
                "invested": initial,
                "bankroll": initial,
                "peak": initial,
                "status": "open",
            }
            invested += initial
        opening = current["bankroll"]
        stake = math.floor(reinvest * opening * 100 + 1e-6) / 100
        if stake < MIN_STAKE:
            # Only reachable with a tiny amount and a small reinvest share: nothing to bet.
            entries.append(
                {
                    "date": day,
                    "ticket": None,
                    "stake": 0.0,
                    "result": "skipped",
                    "reason": "Miza ar fi sub 0.01; ziua a fost sărită.",
                    "payout": 0.0,
                    "bankroll_before": money(opening),
                    "bankroll_after": money(opening),
                    "ladder_index": current["index"],
                    "streak_day": current["days"],
                }
            )
            continue
        # Results are looked up only here, after the ticket and the stake are fixed.
        legs, status, multiplier = settle_bet(bet, results)
        payout = money(stake * multiplier) if status != "lost" else 0.0
        current["bankroll"] = money(opening - stake + payout)
        current["tickets"] += 1
        counts[status] += 1
        staked += stake
        odds = bet_odds(bet)
        odds_sum += odds
        if status == "lost":
            losing += 1
            longest_losing = max(longest_losing, losing)
        else:
            if status == "won":
                losing = 0
                current["won"] += 1
            else:
                current["void"] += 1
            current["days"] += 1
            current["peak"] = max(current["peak"], current["bankroll"])
        entry = {
            "date": day,
            "ticket": ladder_ticket(bet, legs),
            "stake": stake,
            "result": status,
            "payout": payout,
            "odds": round(odds, 4),
            "probability": bet_probability(bet),
            "bankroll_before": money(opening),
            "bankroll_after": current["bankroll"],
            "ladder_index": current["index"],
            "streak_day": current["tickets"],
        }
        rows.append(
            {
                "date": day,
                "legs": legs,
                "stake": stake,
                "odds": round(odds, 4),
                "probability": bet_probability(bet),
                "result": status,
                "payout": payout,
                "return": payout,
                "bankroll_before": money(opening),
                "bankroll_after": current["bankroll"],
                "bankroll": current["bankroll"],
                "ladder_index": current["index"],
            }
        )
        ended = None
        if status == "lost":
            ended = "lost"
        elif max_days and current["days"] >= max_days:
            ended = "cashed"
        if ended:
            current.update(status=ended, end=day)
            returned += current["bankroll"]
            ladders.append(_ladder_record(current))
            current = None
            if ended == "lost" and not restart_on_loss:
                stopped = day
        entries.append(entry)
        position = value()
        peak_value = max(peak_value, position)
        if peak_value > 0:
            # Restarts can take the position below zero; a drawdown is at most everything.
            drawdown = min(1.0, max(drawdown, (peak_value - position) / peak_value))
        equity.append(
            {
                "date": day,
                "bankroll": entry["bankroll_after"],
                "net": money(position - initial),
                "value": money(position),
                "ladder_index": entry["ladder_index"],
            }
        )
        if stopped:
            break
    if current is not None:
        current["end"] = entries[-1]["date"] if entries else current["start"]
        returned += current["bankroll"]
        ladders.append(_ladder_record(current))
        current = None
    net = money(returned - invested)
    first = ladders[0] if ladders else None
    longest = max(ladders, key=lambda x: (x["days"], -x["index"]), default=None)
    bets = sum(counts.values())
    settled = counts["won"] + counts["lost"]
    ladder = {
        "initial": initial,
        "reinvest": reinvest,
        "restart_on_loss": restart_on_loss,
        "max_days": max_days,
        "first_run_days": first["days"] if first else 0,
        "first_run_peak": first["peak"] if first else initial,
        "first_run_status": first["status"] if first else None,
        "longest_streak": longest["days"] if longest else 0,
        "longest_streak_peak": longest["peak"] if longest else initial,
        "best_peak": max((x["peak"] for x in ladders), default=initial),
        "ladders": ladders,
        "restarts": max(0, len(ladders) - 1),
        "lost_ladders": sum(x["status"] == "lost" for x in ladders),
        "cashed_ladders": sum(x["status"] == "cashed" for x in ladders),
        "total_invested": money(invested),
        "total_returned": money(returned),
        "net": net,
        "days_without_ticket": sum(e["result"] == "skipped" for e in entries),
        "stopped": stopped,
    }
    return {
        "initial": initial,
        "final": money(initial + net),
        "profit": net,
        "staked": money(staked),
        "roi": net / invested if invested else 0.0,
        "growth": net / initial if initial else 0.0,
        "bets": bets,
        **counts,
        "hit_rate": counts["won"] / settled if settled else None,
        "max_drawdown": drawdown,
        "peak": money(peak_value),
        "longest_losing_streak": longest_losing,
        "avg_odds": odds_sum / bets if bets else None,
        "betting_days": bets,
        "stopped": stopped,
        "ladder": ladder,
        "days": entries,
        "history": equity,
        "rows": rows,
    }


def validate_ladder(bankroll, target_odds, reinvest, max_days):
    """Checked ladder values (reinvest, max_days); SimulationError with a Romanian message."""
    if target_odds is None:
        raise SimulationError("Alege cota țintă a biletului zilnic (1.2–100).")
    if not (isinstance(target_odds, (int, float)) and 1.2 <= target_odds <= 100):
        raise SimulationError("Cota țintă trebuie să fie între 1.2 și 100.")
    reinvest = REINVEST_DEFAULT if reinvest is None else reinvest
    if not (isinstance(reinvest, (int, float)) and math.isfinite(reinvest)):
        raise SimulationError("Partea reinvestită trebuie să fie un număr.")
    if not 0 < reinvest <= 1:
        raise SimulationError("Partea reinvestită trebuie să fie între 0 și 1 (1 = tot).")
    if math.floor(reinvest * bankroll * 100 + 1e-6) / 100 < MIN_STAKE:
        raise SimulationError("Miza primului bilet ar fi sub 0.01. Mărește suma sau partea.")
    if max_days is not None and not (
        isinstance(max_days, int) and 1 <= max_days <= MAX_LADDER_DAYS
    ):
        raise SimulationError(
            f"Numărul maxim de zile al unei scări este între 1 și {MAX_LADDER_DAYS}."
        )
    return reinvest, max_days


def calendar_days(start, end):
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def simulate_ladder(
    dataset,
    *,
    bankroll,
    target_odds,
    reinvest,
    restart_on_loss,
    max_days,
    start,
    end,
    cache_dir,
    workers,
    rules,
):
    reinvest, max_days = validate_ladder(bankroll, target_odds, reinvest, max_days)
    days, stats = predictions(dataset, start, end, cache_dir, workers)
    results = {m.id: m for m in dataset.matches if start <= m.kickoff.date() <= end}
    rules = rules or product_rules()
    # A fixed window (the last N days) logs every calendar day, loaded or not; historical
    # datasets log the days that have priced matches.
    if dataset.window:
        day_keys = calendar_days(start, end)
        empty = "Nu există meciuri terminate cu cote pentru această zi în baza locală."
    else:
        day_keys = sorted(days)
        empty = "Nu există meciuri cu cote în această zi."
    options = {
        "bankroll": bankroll,
        "reinvest": reinvest,
        "restart_on_loss": restart_on_loss,
        "max_days": max_days,
        "no_rows_reason": empty,
    }
    run = run_ladder(
        day_keys, days, results, lambda rows: choose_ticket(rows, target_odds, rules), **options
    )
    baseline = run_ladder(
        day_keys,
        days,
        results,
        lambda rows: choose_baseline(rows, "ticket", 1, target_odds, rules),
        **options,
    )
    warnings = ladder_warnings(dataset, run, days, day_keys, start, end)
    entries = run.pop("days")
    rows = run.pop("rows")
    history = run.pop("history")
    summary = {
        "start": run["initial"],
        "final": run["final"],
        "profit": run["profit"],
        "roi": run["roi"],
        "yield": run["roi"],
        "growth": run["growth"],
        "bets": run["bets"],
        "won": run["won"],
        "lost": run["lost"],
        "void": run["void"],
        "hit_rate": run["hit_rate"],
        "max_drawdown": run["max_drawdown"],
        "longest_losing_streak": run["longest_losing_streak"],
        "avg_odds": run["avg_odds"],
    }
    base_rows = baseline.pop("rows")
    baseline.pop("days")
    baseline.pop("history")
    baseline.update(
        label="Favoritul casei de pariuri (aceeași scară)",
        staking="ladder",
        rows_total=len(base_rows),
    )
    return {
        "dataset": dataset.describe(),
        "sport": dataset.sport,
        "sports": dataset.sports,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mode": "ladder",
        "strategy": "ladder",
        "staking": "ladder",
        "stake": None,
        "target_odds": target_odds,
        "reinvest": reinvest,
        "restart_on_loss": restart_on_loss,
        "max_days": max_days,
        "max_bets_per_day": 1,
        **run,
        "yield": run["roi"],
        "days": entries,
        "days_count": len(entries),
        "summary": summary,
        "history": history,
        "equity": history,
        "rows": rows[-MAX_ROWS:],
        "rows_total": len(rows),
        "baseline": baseline,
        "method": ladder_method(rules, target_odds, reinvest, restart_on_loss, max_days),
        "rules": {
            "source": rules.source,
            "leg_odds": list(rules.leg_odds),
            "min_value": rules.min_value,
            "max_value": rules.max_value,
            "single_min_odds": rules.single_min_odds,
            "window": list(rules.window),
            "max_legs": rules.max_legs(target_odds),
        },
        "warnings": warnings,
        "warning": " ".join(warnings),
        "disclaimer": DISCLAIMER,
        "cache": stats,
    }


def ladder_method(rules, target_odds, reinvest, restart_on_loss, max_days):
    share = "tot bankroll-ul scării" if reinvest >= 1 else f"{reinvest:.0%} din bankroll-ul scării"
    restart = (
        "După un bilet pierdut, scara se încheie și a doua zi pornește o scară nouă cu suma "
        "inițială (bani noi investiți)."
        if restart_on_loss
        else "După primul bilet pierdut simularea se oprește."
    )
    cash = ""
    if max_days:
        cash = (
            f" După {max_days} bilete reușite (câștigate sau anulate) scara se încasează și "
            "pornește alta cu suma inițială, chiar și fără repornire după pierdere."
        )
    return (
        "Scară (rollover), walk-forward orb: în fiecare zi un singur bilet cu cota totală între "
        f"{rules.window[0]:g} și {rules.window[1]:g} × cota țintă {target_odds:g}, ales de același "
        "optimizator și aceleași reguli de selecție ca recomandările zilnice (probabilitate × "
        f"cotă între {value_window(rules)}), doar din predicțiile făcute cu rezultatele zilelor "
        f"anterioare. Miza este {share}; biletul se fixează înainte ca rezultatele zilei să fie "
        "dezvăluite. Un bilet anulat returnează miza, iar ziua contează ca zi supraviețuită a "
        f"scării. {restart}{cash}"
    )


RO_MONTHS = (
    "ian.",
    "feb.",
    "mar.",
    "apr.",
    "mai",
    "iun.",
    "iul.",
    "aug.",
    "sept.",
    "oct.",
    "nov.",
    "dec.",
)
# The football season on which the goals calibration and MAX_VALUE were fitted (validation).
FIT_SEASON = (date(2024, 7, 1), date(2025, 6, 30))


def ro_date(value):
    """ "2026-04-20" -> "20 apr. 2026" (Romanian short month)."""
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day.day} {RO_MONTHS[day.month - 1]} {day.year}"


def ro_times(count):
    """Romanian "o dată" / "de 5 ori" / "de 50 de ori" (numbers >= 20 take "de")."""
    if count == 1:
        return "o dată"
    rest = count % 100
    joiner = " de" if count >= 20 and (rest == 0 or rest >= 20) else ""
    return f"de {count}{joiner} ori"


def ro_money(value, currency="RON"):
    """12345.5 -> "12.345,50 RON" (the UI's ro-RO format)."""
    text = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{'-' if value < 0 else ''}{text} {currency}"


def source_notes(dataset, start, end):
    """Price source, price-source mismatch, bias and in-sample notes shared by every strategy."""
    notes = []
    if dataset.id == "recent":
        notes.append(
            "Cotele sunt cotele 1X2 din lista FlashScore salvate în baza locală înainte de "
            "start (cotele extinse încărcate mai târziu sunt ignorate); la o casă reală prețul "
            "obținut putea fi altul."
        )
        notes.append(
            "Meciurile amânate sau anulate cu cote intră ca bilete anulate (miză returnată); "
            "meciurile rămase fără rezultat în baza locală nu apar deloc."
        )
    elif dataset.id.startswith("local-"):
        notes.append(
            "Cotele sunt cele salvate de FlashScore în baza locală; pot fi cote de închidere."
        )
    elif "football" in dataset.sports:
        notes.append(
            "Cotele istorice sunt medii de piață (coloanele Avg ale football-data.co.uk), nu "
            "cele mai bune cote pe care le compară aplicația; de aceea selecțiile peste/sub 2.5 "
            "(probabilitate egală cu piața) cad aici sub pragul de valoare și nu apar deloc. La "
            "o casă reală prețul obținut putea fi altul."
        )
    else:
        notes.append(
            "Cotele istorice sunt medii de piață; la o casă reală prețul obținut putea fi altul."
        )
    fit_first, fit_last = FIT_SEASON
    if dataset.id in ("football", "football-plus") and start <= fit_last and end >= fit_first:
        notes.append(
            "Sezonul 2024-25 este sezonul pe care au fost potrivite calibrarea golurilor și "
            "plafonul de valoare: rezultatele din acest interval sunt în eșantion (optimiste). "
            "Cifrele în afara eșantionului sunt cele din 2025-26 și de după."
        )
    return notes


def ladder_warnings(dataset, run, days, day_keys, start, end):
    notes = []
    if dataset.id == "recent":
        notes.append(
            "Zilele recente au un istoric de formă subțire în baza locală: multe meciuri primesc "
            "nota D și nu intră pe bilet, iar selecțiile rămase se sprijină mai mult pe cotele "
            "pieței decât pe formă."
        )
    notes.extend(source_notes(dataset, start, end))
    if dataset.id == "recent":
        missing = [d for d in day_keys if not days.get(d)]
        if missing:
            notes.append(
                f"{len(missing)} din {len(day_keys)} zile nu au meciuri terminate cu cote în baza "
                "locală; pregătește ultimele zile ca să le încarci."
            )
    notes.append(
        "Probabilitatea biletului presupune că meciurile sunt independente; o scară lungă "
        "înmulțește riscul: la cota 2, chiar și un bilet de 50% supraviețuiește 5 zile la rând "
        "doar o dată din 32."
    )
    ladder = run["ladder"]
    if run["bets"] == 0:
        notes.append("Niciun bilet nu a îndeplinit condițiile în intervalul ales.")
    elif ladder["restarts"]:
        notes.append(
            f"Scara a fost repornită {ro_times(ladder['restarts'])}; totalul investit "
            f"({ro_money(ladder['total_invested'])}) include fiecare repornire."
        )
    if ladder["stopped"]:
        notes.append(
            f"Scara s-a încheiat pe {ro_date(ladder['stopped'])} după un bilet pierdut; "
            "repornirea este dezactivată."
        )
    notes.append(
        "Rezultatele din trecut nu garantează rezultate viitoare. Bani virtuali, fără miză reală."
    )
    return notes


# --- request handling ---------------------------------------------------------------------


def resolve_strategy(strategy="flat", staking=None, mode=None, target_odds=None):
    """(mode, staking) from the contract form (strategy = staking, target_odds -> ticket) or the
    long form (strategy = singles|ticket|value, staking = flat|percent|kelly)."""
    strategy = (strategy or "flat").strip().lower()
    if strategy in STAKINGS:
        staking = staking or strategy
        mode = mode or ("ticket" if target_odds else "singles")
    elif strategy in MODES:
        mode = mode or strategy
        staking = staking or "flat"
    else:
        raise SimulationError(
            "Strategie necunoscută. Alege flat, percent, kelly sau ladder (ori singles, ticket, "
            "value)."
        )
    if staking not in STAKINGS:
        raise SimulationError("Miza trebuie să fie flat, percent sau kelly.")
    if mode not in MODES:
        raise SimulationError("Modul trebuie să fie singles, ticket sau value.")
    return mode, staking


def validate(dataset, *, bankroll, mode, staking, stake, target_odds, per_day, start, end):
    """Checked request values (start, end, stake, target); SimulationError on bad input."""
    if not (isinstance(bankroll, (int, float)) and math.isfinite(bankroll)):
        raise SimulationError("Suma inițială trebuie să fie un număr.")
    if not 0 < bankroll <= 10_000_000:
        raise SimulationError("Suma inițială trebuie să fie între 0 și 10.000.000.")
    first, last = dataset.bounds()
    if first is None:
        raise SimulationError("Setul de date nu are meciuri cu cote pentru simulare.")
    if end is None:
        end = last
    if start is None:
        start = max(first, end - timedelta(days=364))
    if start > end:
        raise SimulationError("Data de început trebuie să fie înaintea datei de sfârșit.")
    if start < first or end > last:
        raise SimulationError(
            f"Intervalul trebuie să fie între {first.isoformat()} și {last.isoformat()} "
            "pentru acest set de date."
        )
    if (end - start).days > MAX_DAYS:
        raise SimulationError("Intervalul maxim al unei simulări este de 3 ani.")
    if stake is None:
        stake = {"flat": money(bankroll * 0.01), "percent": 0.01, "kelly": 0.25}[staking]
    if not (isinstance(stake, (int, float)) and math.isfinite(stake)):
        raise SimulationError("Miza trebuie să fie un număr.")
    if staking == "flat" and not 0 < stake <= bankroll:
        raise SimulationError("Miza fixă trebuie să fie pozitivă și cel mult suma inițială.")
    if staking == "percent" and not 0.001 <= stake <= 0.2:
        raise SimulationError("Procentul din bankroll trebuie să fie între 0,1% și 20%.")
    if staking == "kelly" and not 0.1 <= stake <= 1:
        raise SimulationError("Fracția Kelly trebuie să fie între 0,1 și 1.")
    if mode == "ticket":
        if target_odds is None:
            raise SimulationError("Alege cota țintă a biletului zilnic (1.2–100).")
        if not 1.2 <= target_odds <= 100:
            raise SimulationError("Cota țintă trebuie să fie între 1.2 și 100.")
    if not 1 <= per_day <= 20:
        raise SimulationError("Numărul de pariuri pe zi trebuie să fie între 1 și 20.")
    return start, end, stake


def simulate(
    dataset,
    *,
    bankroll=1000.0,
    strategy="flat",
    staking=None,
    mode=None,
    stake=None,
    target_odds=None,
    max_bets_per_day=3,
    start=None,
    end=None,
    kelly_cap=KELLY_CAP,
    cache_dir=CACHE_DIR,
    workers=None,
    rules=None,
    reinvest=None,
    restart_on_loss=True,
    max_days=None,
    last_days=None,
):
    """Full simulation of one strategy plus the bookmaker-favourite baseline.

    strategy "ladder": one daily ticket at `target_odds` staking `reinvest` of the ladder
    bankroll (see ``run_ladder``). `last_days` without `start` simulates the last N days that
    end on `end` (default: the dataset's last day).
    """
    if last_days is not None:
        if not (isinstance(last_days, int) and 1 <= last_days <= MAX_DAYS):
            raise SimulationError(f"Numărul de zile trebuie să fie între 1 și {MAX_DAYS}.")
        if start is None:
            last = end or dataset.bounds()[1]
            start = last - timedelta(days=last_days - 1) if last else None
            first = dataset.bounds()[0]
            if start and first and start < first:
                start = first
    if (strategy or "").strip().lower() == "ladder" or mode == "ladder":
        start, end, _ = validate(
            dataset,
            bankroll=bankroll,
            mode="ticket",
            staking="flat",
            # The ladder stake is a share of its bankroll; any valid flat value passes here.
            stake=bankroll if isinstance(bankroll, (int, float)) else None,
            target_odds=target_odds,
            per_day=1,
            start=start,
            end=end,
        )
        return simulate_ladder(
            dataset,
            bankroll=bankroll,
            target_odds=target_odds,
            reinvest=reinvest,
            restart_on_loss=restart_on_loss,
            max_days=max_days,
            start=start,
            end=end,
            cache_dir=cache_dir,
            workers=workers,
            rules=rules,
        )
    mode, staking = resolve_strategy(strategy, staking, mode, target_odds)
    per_day = 1 if mode == "ticket" else int(max_bets_per_day or 3)
    start, end, stake = validate(
        dataset,
        bankroll=bankroll,
        mode=mode,
        staking=staking,
        stake=stake,
        target_odds=target_odds,
        per_day=per_day,
        start=start,
        end=end,
    )
    if not 0 < kelly_cap <= 0.5:
        raise SimulationError("Plafonul Kelly trebuie să fie între 0 și 50% din bankroll.")
    days, stats = predictions(dataset, start, end, cache_dir, workers)
    results = {m.id: m for m in dataset.matches if start <= m.kickoff.date() <= end}

    rules = rules or product_rules()

    def choose(rows):
        if mode == "singles":
            return choose_singles(rows, per_day, rules)
        if mode == "value":
            return choose_value(rows, per_day, rules)
        return choose_ticket(rows, target_odds, rules)

    def choose_market(rows):
        return choose_baseline(rows, mode, per_day, target_odds, rules)

    run = run_bankroll(
        days, results, choose, bankroll=bankroll, staking=staking, stake=stake, kelly_cap=kelly_cap
    )
    # Kelly never finds an edge in margin-free bookmaker prices: the baseline bets 1%.
    base_staking, base_stake = (staking, stake) if staking != "kelly" else ("percent", 0.01)
    baseline = run_bankroll(
        days, results, choose_market, bankroll=bankroll, staking=base_staking, stake=base_stake
    )
    warnings = warnings_for(dataset, mode, staking, run, days, start, end)
    rows = run.pop("rows")
    history = run.pop("history")
    summary = {
        "start": run["initial"],
        "final": run["final"],
        "profit": run["profit"],
        "roi": run["roi"],
        "yield": run["roi"],
        "growth": run["growth"],
        "bets": run["bets"],
        "won": run["won"],
        "lost": run["lost"],
        "void": run["void"],
        "hit_rate": run["hit_rate"],
        "max_drawdown": run["max_drawdown"],
        "longest_losing_streak": run["longest_losing_streak"],
        "avg_odds": run["avg_odds"],
    }
    base_rows = baseline.pop("rows")
    baseline.pop("history")
    baseline.update(
        label="Favoritul casei de pariuri (aceeași miză)",
        staking=base_staking,
        rows_total=len(base_rows),
    )
    return {
        "dataset": dataset.describe(),
        "sport": dataset.sport,
        "sports": dataset.sports,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mode": mode,
        "strategy": staking,
        "staking": staking,
        "stake": stake,
        "target_odds": target_odds,
        "max_bets_per_day": per_day,
        **run,
        "yield": run["roi"],
        "days": len(days),
        "summary": summary,
        "history": history,
        "equity": history,
        "rows": rows[-MAX_ROWS:],
        "rows_total": len(rows),
        "baseline": baseline,
        "method": method_text(mode, staking, rules, kelly_cap),
        "rules": {
            "source": rules.source,
            "leg_odds": list(rules.leg_odds),
            "min_value": rules.min_value,
            "max_value": None if mode == "value" else rules.max_value,
            "single_min_odds": rules.single_min_odds,
            "window": list(rules.window),
            "max_legs": rules.max_legs(target_odds) if target_odds else None,
        },
        "warnings": warnings,
        "warning": " ".join(warnings),
        "disclaimer": DISCLAIMER,
        "cache": stats,
    }


def method_text(mode, staking, rules, kelly_cap=KELLY_CAP):
    low, high = rules.leg_odds
    band = f"cote între {low:g} și {high:g}"
    choice = {
        "singles": "în fiecare zi, cele mai sigure selecții (probabilitate maximă, o selecție pe "
        f"meci, cotă de cel puțin {rules.single_min_odds:g})",
        "value": "în fiecare zi, selecțiile cu valoare pozitivă (probabilitate × cotă ≥ "
        f"{1 + VALUE_MIN_EV:g}, probabilitate de cel puțin {VALUE_MIN_PROBABILITY:.0%}; fără "
        "plafonul de valoare al recomandărilor, deci pariuri pe care aplicația nu le recomandă)",
        "ticket": "în fiecare zi, biletul cu cea mai mare probabilitate și cota totală între "
        f"{rules.window[0]:g} și {rules.window[1]:g} × cota țintă, câte o selecție pe meci "
        "(același optimizator ca recomandările zilnice)",
    }[mode]
    stakes = {
        "flat": "miză fixă",
        "percent": "procent fix din bankroll-ul de la începutul zilei",
        "kelly": f"Kelly fracționat, plafonat la {kelly_cap:.0%} din bankroll pe pariu",
    }[staking]
    return (
        "Walk-forward orb: pentru fiecare zi, modelul vede doar rezultatele din zilele anterioare; "
        "scorul meciului evaluat nu există în datele de intrare. Miza se fixează la cota istorică "
        f"și abia apoi rezultatul este dezvăluit. Selecție: {choice}. Miză: {stakes}. "
        f"Doar notele A–C și {band} intră la pariu, cu probabilitate × cotă între "
        f"{value_window(replace(rules, max_value=None) if mode == 'value' else rules)}"
        f"{'' if mode == 'value' else ' (aceeași regulă ca recomandările)'}."
    )


def value_window(rules):
    low = "0" if rules.min_value is None else f"{rules.min_value:g}"
    high = "∞" if rules.max_value is None else f"{rules.max_value:g}"
    return f"{low} și {high}"


def warnings_for(dataset, mode, staking, run, days, start, end):
    notes = []
    if dataset.id == "recent":
        notes.append(
            "Zilele recente au un istoric de formă subțire în baza locală: selecțiile se sprijină "
            "mai mult pe cotele pieței decât pe formă."
        )
    notes.extend(source_notes(dataset, start, end))
    if "football" in dataset.sports:
        notes.append(
            "Modelul folosește și cotele 1X2 și peste/sub 2.5 de dinaintea meciului, ca în "
            "aplicație."
        )
    if mode == "ticket":
        notes.append("Probabilitatea biletului presupune că meciurile sunt independente.")
    if staking == "kelly" and run["bets"] == 0:
        notes.append("Kelly nu a găsit niciun avantaj față de cote, deci nu a pariat nimic.")
    if not days:
        notes.append("Nu există meciuri cu cote în intervalul ales.")
    elif run["bets"] == 0:
        notes.append("Nicio selecție nu a îndeplinit condițiile în intervalul ales.")
    if run["stopped"]:
        notes.append(f"Bankroll-ul s-a epuizat pe {ro_date(run['stopped'])}; simularea s-a oprit.")
    notes.append(
        "Rezultatele din trecut nu garantează rezultate viitoare. Bani virtuali, fără miză reală."
    )
    return notes


def main():
    from footypreds.evaluation.sim_datasets import cached_dataset

    parser = argparse.ArgumentParser(description="Simulator de bankroll walk-forward orb.")
    parser.add_argument("--dataset", default="football")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--mode", choices=(*MODES, "ladder"), default="singles")
    parser.add_argument("--reinvest", type=float, help="ladder: partea reinvestită (0-1]")
    parser.add_argument("--no-restart", action="store_true", help="ladder: fără repornire")
    parser.add_argument("--max-days", type=int, help="ladder: încasează după N bilete")
    parser.add_argument("--staking", choices=STAKINGS, default="flat")
    parser.add_argument("--stake", type=float)
    parser.add_argument("--target", type=float)
    parser.add_argument("--per-day", type=int, default=3)
    parser.add_argument(
        "--warm", action="store_true", help="precalculează predicțiile întregului set de date"
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dataset = cached_dataset(args.dataset)
    if args.warm:
        first, last = dataset.bounds()
        if first is None:
            print("Setul de date nu are meciuri cu cote.")
            return
        _, stats = predictions(dataset, args.start or first, args.end or last)
        print(json.dumps({"dataset": dataset.id, "start": str(first), "end": str(last)} | stats))
        return
    result = simulate(
        dataset,
        bankroll=args.bankroll,
        strategy=args.mode,
        staking=None if args.mode == "ladder" else args.staking,
        stake=args.stake,
        target_odds=args.target,
        max_bets_per_day=args.per_day,
        start=args.start,
        end=args.end,
        reinvest=args.reinvest,
        restart_on_loss=not args.no_restart,
        max_days=args.max_days,
    )
    keys = ("start", "end", "mode", "staking", "summary", "cache", "rules", "warnings")
    if args.mode == "ladder":
        keys += ("ladder",)
        result["ladder"] = {k: v for k, v in result["ladder"].items() if k != "ladders"}
    baseline = {k: v for k, v in result["baseline"].items() if not isinstance(v, list)}
    print(json.dumps({k: result[k] for k in keys} | {"baseline": baseline}, indent=2))


if __name__ == "__main__":
    main()
