"""Leg value against the margin-free market price (regression: recent-days simulator had no
ticket because a 10%-margin book made every agreeing leg look like p x odds ~ 0.91)."""

from datetime import timedelta

import pytest

from footypreds import recommend as rc
from footypreds import simulator as sim
from footypreds.sports.keys import complements, market_margin
from footypreds.sports.legs import candidate_legs
from footypreds.tests.helpers import KICKOFF, fixture

NOW = KICKOFF - timedelta(days=1)
# FlashScore-like list prices with ~10% overround on 1X2.
BOOK = {"1": 1.75, "X": 3.3, "2": 4.2, "over25": 1.8, "under25": 1.85}


def margin_free(odds, keys):
    inverse = {k: 1 / odds[k] for k in keys}
    total = sum(inverse.values())
    return {k: v / total for k, v in inverse.items()}


def agreeing_analysis(grade):
    """The model says exactly what the bookmaker says (margin removed)."""
    fair = margin_free(BOOK, ("1", "X", "2")) | margin_free(BOOK, ("over25", "under25"))
    markets = [
        {
            "key": key,
            "label": key,
            "group": "g",
            "probability": p,
            "fair_odds": 1 / p,
            "odds": BOOK[key],
            "ev": p * BOOK[key] - 1,
            "selectable": True,
        }
        for key, p in fair.items()
    ]
    return {"grade": grade, "confidence": 50, "markets": markets, "insights": [], "summary": "."}


@pytest.mark.parametrize(
    "key,group",
    [
        ("1", ("1", "X", "2")),
        ("over25", ("over_2.5", "under_2.5")),
        ("under_2.5", ("over_2.5", "under_2.5")),
        ("btts", ("btts", "no_btts")),
        ("ah_2_+1.5", ("ah_2_+1.5", "ah_1_-1.5")),
        ("home_over_1.5", ("home_over_1.5", "home_under_1.5")),
        ("1X", None),
        ("cs_1-0", None),
    ],
)
def test_complements(key, group):
    assert complements("football", key) == group


def test_market_margin_needs_every_outcome_and_a_sane_book():
    assert market_margin("football", "1", BOOK) == pytest.approx(1 / 1.75 + 1 / 3.3 + 1 / 4.2)
    assert market_margin("football", "over_2.5", BOOK) == pytest.approx(1 / 1.8 + 1 / 1.85)
    assert market_margin("football", "btts", BOOK) is None  # not priced
    assert market_margin("football", "1", {"1": 1.75, "2": 4.2}) is None  # X missing
    assert market_margin("tennis", "1", {"1": 1.5, "2": 2.6}) == pytest.approx(1 / 1.5 + 1 / 2.6)
    assert market_margin("football", "1", {"1": 3.1, "X": 3.1, "2": 3.1}) is None  # < 1.0
    assert market_margin("football", "1", {"1": 1.1, "X": 1.1, "2": 1.1}) is None  # absurd


def test_agreeing_with_a_high_margin_book_is_fair_value_one():
    for market in agreeing_analysis("B")["markets"]:
        margin = market_margin("football", market["key"], BOOK)
        assert market["probability"] * market["odds"] < 0.95  # the old rule rejected these
        assert rc.fair_value(market["probability"], market["odds"], margin) == pytest.approx(1)


@pytest.mark.parametrize("grade", ["A", "B", "C", "D"])
def test_agreeing_legs_are_eligible_even_for_grade_d(grade):
    match = fixture(kickoff=KICKOFF, odds=BOOK)
    legs = rc.eligible_legs(match, agreeing_analysis(grade), NOW)
    assert {leg["key"] for leg in legs} == {"1", "X", "over25", "under25"}  # "2" = 4.2 > band
    assert all(leg["margin"] > 1 for leg in legs)


def test_grade_d_needs_a_fully_priced_market():
    partial = {"1": 1.75, "over25": 1.8}
    match = fixture(kickoff=KICKOFF, odds=partial)
    assert candidate_legs(match, agreeing_analysis("D"), NOW) == []
    assert candidate_legs(match, agreeing_analysis("B"), NOW)  # A-C keep priced legs


def test_clear_disagreement_is_still_capped():
    match = fixture(kickoff=KICKOFF, odds=BOOK)
    analysis = agreeing_analysis("A")
    for market in analysis["markets"]:
        if market["key"] == "1":
            market["probability"] = 0.75  # market fair ~0.52
    keys = {leg["key"] for leg in rc.eligible_legs(match, analysis, NOW)}
    assert "1" not in keys


def test_simulator_uses_the_same_rule_with_the_row_margin():
    rules = sim.product_rules()
    margin = market_margin("football", "1", BOOK)
    p = margin_free(BOOK, ("1", "X", "2"))["1"]
    base = {
        "id": "m",
        "day": "2026-09-20",
        "kickoff": "2026-09-20T18:00:00+00:00",
        "sport": "football",
        "league": "L",
        "home": "H",
        "away": "A",
        "confidence": 30,
        "odds": {"1": 1.75, "X": 3.3, "2": 4.2},
    }
    market = {"key": "1", "label": "1", "group": "g", "probability": p, "fair_odds": 1 / p}
    priced = base | {"grade": "D", "markets": [market | {"odds": 1.75, "margin": margin}]}
    unpriced = base | {"grade": "D", "markets": [market | {"odds": 1.75, "margin": None}]}
    assert [leg["key"] for leg in sim.model_legs(priced, rules)] == ["1"]
    assert sim.model_legs(unpriced, rules) == []
