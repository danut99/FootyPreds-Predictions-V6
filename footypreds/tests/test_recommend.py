"""Ticket optimizer (recommend.optimize) against a brute-force oracle, and the leg rules."""

import asyncio
import itertools
import math
import random
import types
from datetime import datetime, timedelta, timezone

import pytest

from footypreds import recommend as rc
from footypreds.domain import Match
from footypreds.sports import analyze_match

NOW = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def leg(match, odds, probability, key="1", home=None, away=None, sport="football", hour=15):
    return {
        "match_id": str(match),
        "sport": sport,
        "home": home or f"Home {match}",
        "away": away or f"Away {match}",
        "competition": "Liga",
        "competition_id": "c|liga",
        "kickoff": (NOW + timedelta(hours=hour)).isoformat(),
        "key": key,
        "label": key,
        "odds": odds,
        "probability": probability,
        "grade": "B",
        "confidence": 60,
        "status": "pending",
        "score": None,
    }


# --- oracle -------------------------------------------------------------------------------


def oracle(legs, target, max_legs, shrink=0.0):
    """(probability, odds) of the best valid combination, by brute force."""
    low, high = rc.window_of(target)
    low, high = low * math.exp(shrink), high / math.exp(shrink)
    group = {id(item): n for n, members in enumerate(rc.conflict_groups(legs)) for item in members}
    best = None
    for size in range(1, max_legs + 1):
        for combo in itertools.combinations(legs, size):
            if len({group[id(item)] for item in combo}) != size:
                continue
            odds = math.prod(item["odds"] for item in combo)
            if not low <= odds <= high:
                continue
            value = (math.prod(item["probability"] for item in combo), odds)
            best = value if best is None or value > best else best
    return best


def random_legs(rng, count):
    legs = []
    for n in range(count):
        match = rng.randint(0, 7)
        home = f"Home {match}" if rng.random() > 0.1 else "Home 0"  # a team in two fixtures
        legs.append(
            leg(
                match,
                round(1.05 + rng.random() * 3, 2),
                round(0.3 + rng.random() * 0.65, 3),
                key=f"k{n}",
                home=home,
                hour=rng.randint(1, 20),
            )
        )
    return legs


@pytest.mark.parametrize("seed", range(150))
def test_optimizer_matches_the_brute_force_oracle(seed):
    rng = random.Random(seed)
    legs = random_legs(rng, rng.randint(0, 10))
    target = round(1.2 + rng.random() * rng.choice((2, 8, 30)), 2)
    max_legs = rng.randint(1, 5)
    chosen = rc.optimize(legs, target, max_legs)
    best = oracle(legs, target, max_legs)
    if best is None:
        assert chosen == []
        return
    probability = math.prod(item["probability"] for item in chosen)
    odds = math.prod(item["odds"] for item in chosen)
    low, high = rc.window_of(target)
    # Always a valid ticket made of the given legs, at their real prices.
    assert 1 <= len(chosen) <= max_legs and low - 1e-9 <= odds <= high + 1e-9
    assert all(any(item is given for given in legs) for item in chosen)
    groups = rc.conflict_groups(legs)
    assert len({n for n, g in enumerate(groups) for item in chosen if item in g}) == len(chosen)
    # Optimal: never better than the oracle, never worse than any combination away from the
    # window edges by more than the discretisation.
    assert probability <= best[0] + 1e-12
    inner = oracle(legs, target, max_legs, shrink=max_legs * rc.BUCKET)
    if inner is not None:
        assert probability >= inner[0] - 1e-12
    # Deterministic and independent of the input order.
    assert rc.optimize(legs, target, max_legs) == chosen
    shuffled = legs[:]
    rng.shuffle(shuffled)
    again = rc.optimize(shuffled, target, max_legs)
    assert math.prod(item["probability"] for item in again) == pytest.approx(probability)


def test_optimizer_prefers_the_likeliest_combination_not_the_closest_odds():
    legs = [leg(1, 2.0, 0.40), leg(2, 1.9, 0.52), leg(3, 1.45, 0.72), leg(4, 1.4, 0.74)]
    chosen = rc.optimize(legs, 2.0, 3)
    # 1.45 * 1.4 = 2.03 at 53% beats a single 2.0 (40%) and 1.9 (52%).
    assert [item["match_id"] for item in chosen] == ["3", "4"]


def test_one_leg_per_match_and_per_team():
    legs = [leg(1, 1.5, 0.7, key="1"), leg(1, 1.4, 0.72, key="1X")]
    assert rc.optimize(legs, 2.1, 3) == []
    shared = [leg(1, 1.45, 0.7), leg(2, 1.45, 0.7, home="Home 1")]
    assert rc.optimize(shared, 2.1, 3) == []
    doubles = [
        leg(1, 1.45, 0.7, home="Sinner J.", sport="tennis"),
        leg(2, 1.45, 0.7, home="Sinner J./Alcaraz C.", sport="tennis"),
    ]
    assert rc.optimize(doubles, 2.1, 3) == []
    ok = [leg(1, 1.45, 0.7), leg(2, 1.45, 0.7)]
    assert len(rc.optimize(ok, 2.1, 3)) == 2


def test_odds_window_and_leg_limit():
    legs = [leg(n, 1.5, 0.66) for n in range(10)]
    assert rc.optimize(legs, 2.0, 1) == []  # 1.5 < 1.86
    assert rc.optimize(legs, 2.0, 3) == []  # 2.25 > 2.24
    assert len(rc.optimize(legs, 5.0, 4)) == 4  # 1.5^4 = 5.06
    assert rc.optimize(legs, 5.0, 3) == []
    many = [leg(n, 1.5, 0.66) for n in range(12)]
    assert rc.optimize(many, 100, 14) == []  # 1.5^11 = 86.5 and 1.5^12 = 129.7 both miss
    assert len(rc.optimize(many + [leg(12, 1.1, 0.9)], 100, 14)) == 12  # 1.5^11 * 1.1 = 95.1
    assert rc.optimize(many + [leg(12, 1.1, 0.9)], 100, 11) == []  # needs 12 legs
    assert rc.optimize([], 2.0) == [] and rc.optimize(legs, 1.0) == []


def test_ties_prefer_higher_odds_then_fewer_legs():
    legs = [leg(1, 2.0, 0.5), leg(2, 2.1, 0.5)]
    assert rc.optimize(legs, 2.0, 1)[0]["match_id"] == "2"
    fewer = [leg(1, 4.0, 0.25), leg(2, 2.0, 0.5), leg(3, 2.0, 0.5)]
    assert [item["match_id"] for item in rc.optimize(fewer, 4.0, 2)] == ["1"]


@pytest.mark.parametrize(
    "target,expected", [(2, 3), (5, 5), (10, 7), (100, 14), (1.2, 2), (3, 4), (1000, 15)]
)
def test_auto_max_legs(target, expected):
    assert rc.auto_max_legs(target) == expected


def test_large_pool_is_fast_and_valid():
    rng = random.Random(7)
    legs = []
    for n in range(200):
        for k in range(rng.randint(1, 3)):
            p = 0.35 + rng.random() * 0.6
            legs.append(leg(n, round(min(4.0, max(1.08, 1 / p * 0.98)), 2), p, key=f"k{k}"))
    for target in (2, 5, 10, 100):
        chosen = rc.optimize(legs, target)
        odds = math.prod(item["odds"] for item in chosen)
        low, high = rc.window_of(target)
        assert chosen and low <= odds <= high and len(chosen) <= rc.auto_max_legs(target)
        assert len({item["match_id"] for item in chosen}) == len(chosen)


# --- tickets and explanations ---------------------------------------------------------------


def test_build_ticket_shape_and_rationale():
    legs = [leg(1, 1.45, 0.72), leg(2, 1.4, 0.74, sport="tennis")]
    built = rc.build_ticket(legs, 2.0, NOW.date())
    assert built["status"] == "pending" and built["target_odds"] == 2.0
    assert built["total_odds"] == pytest.approx(1.45 * 1.4)
    assert built["probability"] == pytest.approx(0.72 * 0.74)
    assert built["expected_value"] == pytest.approx(built["ev"])
    assert built["max_legs"] == 3 and built["window"] == [1.86, 2.24]
    assert "fotbal" in built["rationale"] and "tenis" in built["rationale"]
    assert "independen" in built["assumption"]


def test_impossible_targets_are_explained():
    none = rc.build_ticket([], 5, NOW.date())
    assert none["status"] == "unavailable" and "Nu există selecții" in none["reason"]
    short = rc.build_ticket([leg(1, 1.3, 0.8), leg(2, 1.3, 0.8)], 100, NOW.date())
    assert short["status"] == "unavailable" and "Cota maximă realizabilă" in short["reason"]
    long = rc.build_ticket([leg(1, 3.5, 0.3)], 1.3, NOW.date())
    assert "Cea mai mică cotă" in long["reason"]
    gap = rc.build_ticket([leg(1, 1.5, 0.66), leg(2, 1.5, 0.66)], 2.0, NOW.date())
    assert "Nicio combinație" in gap["reason"] and gap["legs"] == []


def test_safest_singles_one_per_match_with_a_real_price():
    legs = [
        leg(1, 1.1, 0.95),  # too short to recommend on its own
        leg(2, 1.25, 0.85, key="1"),
        leg(2, 1.3, 0.84, key="1X"),
        leg(3, 1.6, 0.66),
        leg(4, 1.22, 0.85),
    ]
    singles = rc.safest_singles(legs)
    assert [(s["match_id"], s["key"]) for s in singles] == [("2", "1"), ("4", "1"), ("3", "1")]
    assert len(rc.safest_singles([leg(n, 1.3, 0.8) for n in range(30)])) == rc.SINGLES


# --- eligible legs --------------------------------------------------------------------------


def basketball(**changes):
    data = dict(
        id="b1",
        kickoff=NOW + timedelta(hours=6),
        league="USA: NBA",
        country="USA",
        home="A",
        away="B",
        sport="basketball",
        odds={"1": 1.5, "2": 2.6, "over_180.5": 1.9, "under_180.5": 1.9, "ah_1_-20.5": 9.0},
    )
    return Match(**(data | changes))


def test_eligible_legs_filter_band_value_grade_and_kickoff():
    match = basketball()
    analysis = analyze_match(match, []) | {"grade": "B", "quality": "sufficient"}
    legs = rc.eligible_legs(match, analysis, NOW)
    keys = {item["key"] for item in legs}
    assert "ah_1_-20.5" not in keys  # 9.0 is outside the leg odds band
    for item in legs:
        assert rc.LEG_ODDS[0] <= item["odds"] <= rc.LEG_ODDS[1]
        assert rc.fair_value(item["probability"], item["odds"], item["margin"]) >= rc.MIN_VALUE
        assert item["reason"] and "Probabilitate estimată" in item["reason"]
    # Grade D keeps only fully priced markets (1/2 and the 180.5 total are, the handicap is not).
    grade_d = rc.eligible_legs(match, analysis | {"grade": "D"}, NOW)
    assert all(item["margin"] is not None for item in grade_d)
    assert {item["key"] for item in grade_d} <= {"1", "2", "over_180.5", "under_180.5"}
    assert rc.eligible_legs(match, analysis, match.kickoff) == []
    started = match.model_copy(update={"status": "live"})
    assert rc.eligible_legs(started, analysis, NOW) == []


def test_refundable_lines_never_enter_tickets():
    match = Match(
        id="f1",
        kickoff=NOW + timedelta(hours=6),
        league="ENGLAND: Premier League",
        country="England",
        home="Strong",
        away="Weak",
        odds={"1": 1.5, "X": 4.4, "2": 7.0, "dnb_1": 1.2, "over_3": 1.9, "ah_1_-1.5": 2.4},
    )
    analysis = analyze_match(match, []) | {"grade": "B", "quality": "sufficient"}
    keys = {item["key"] for item in rc.eligible_legs(match, analysis, NOW, min_value=None)}
    assert "dnb_1" not in keys and "over_3" not in keys
    assert {"1", "ah_1_-1.5"} <= keys


@pytest.mark.parametrize(
    "text,expected",
    [("2,5,10,100", [2.0, 5.0, 10.0, 100.0]), ("3.5", [3.5]), (" 2 , 2 ", [2.0])],
)
def test_parse_targets(text, expected):
    assert rc.parse_targets(text) == expected


@pytest.mark.parametrize("text", ["", "1.1", "1001", "a", "nan", "1,2,3,4,5,6,7,8,9"])
def test_parse_targets_rejects_bad_values(text):
    with pytest.raises(ValueError):
        rc.parse_targets(text)


def test_parse_sports_keeps_registry_order():
    assert rc.parse_sports("tennis,football") == ["football", "tennis"]
    assert rc.parse_sports(["basketball"]) == ["basketball"]
    for bad in ("", "golf", "football,golf"):
        with pytest.raises(ValueError):
            rc.parse_sports(bad)


# --- candidate pool (collect) with a fake app state ----------------------------------------


def pool_state(tmp_path, errors=None):
    """app.state stand-in: day fixtures from memory, enrich() recording (or failing)."""
    from footypreds.api import AnalysisCache
    from footypreds.provider import ProviderError
    from footypreds.store import Store

    store = Store(tmp_path / "pool.db")
    games = [basketball(id=f"b{n}", home=f"H{n}", away=f"A{n}") for n in range(6)]
    calls = {"fixtures": [], "enrich": []}

    async def day_fixtures(day, sport="football", refresh=False):
        calls["fixtures"].append(sport)
        found = games if sport == "basketball" else []
        store.save_matches(found)
        return found, False, 0, 0

    async def enrich(match, refresh=False):
        calls["enrich"].append(match.id)
        status = (errors or {}).get(len(calls["enrich"]))
        if status:
            raise ProviderError("oprit", status)
        return [], []

    state = types.SimpleNamespace(
        store=store, cache=AnalysisCache(store), day_fixtures=day_fixtures, enrich=enrich
    )
    return state, calls


def test_collect_respects_the_budget_and_remembers_enriched_games(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "utcnow", lambda: NOW)
    state, calls = pool_state(tmp_path)
    pool = asyncio.run(rc.collect(state, NOW.date(), ["football", "basketball"], budget=2))
    assert calls["fixtures"] == ["football", "basketball"]
    assert len(calls["enrich"]) == 2 and pool.enriched == {"football": 0, "basketball": 2}
    assert pool.analyzed == {"football": 0, "basketball": 6}
    # A fresh budget goes to games not enriched yet; the remaining budget is then empty.
    asyncio.run(rc.collect(state, NOW.date(), ["basketball"], budget=2))
    assert len(calls["enrich"]) == 4 and len(set(calls["enrich"])) == 4
    asyncio.run(rc.collect(state, NOW.date(), ["basketball"], budget=4, fresh_budget=False))
    assert len(calls["enrich"]) == 4


def test_collect_stops_enriching_on_quota_and_propagates_a_bad_key(tmp_path, monkeypatch):
    from footypreds.provider import ProviderError

    monkeypatch.setattr(rc, "utcnow", lambda: NOW)
    state, calls = pool_state(tmp_path, errors={2: 429})
    pool = asyncio.run(rc.collect(state, NOW.date(), ["basketball"], budget=5))
    assert len(calls["enrich"]) == 2 and pool.enriched["basketball"] == 1
    assert any("oprit" in warning for warning in pool.warnings)
    assert pool.analyzed["basketball"] == 6
    state, calls = pool_state(tmp_path / "b", errors={1: 503})
    with pytest.raises(ProviderError):
        asyncio.run(rc.collect(state, NOW.date(), ["basketball"], budget=5))
