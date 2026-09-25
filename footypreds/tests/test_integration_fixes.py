"""Regression tests for the fixes made while integrating the parallel feature modules."""

from datetime import datetime, timedelta, timezone

import pytest

from footypreds import recommend
from footypreds import simulator as sim
from footypreds.api import board_item
from footypreds.domain import Match
from footypreds.evaluation import sim_datasets
from footypreds.sports import headline_tip, main_markets
from footypreds.sports.settle import can_push, is_settleable, settle
from footypreds.tests.test_simulator import row

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


def market(key, probability, odds=None, selectable=True, group="g", **extra):
    return {
        "key": key,
        "label": key,
        "group": group,
        "probability": probability,
        "fair_odds": 1 / probability,
        "odds": odds,
        "ev": probability * odds - 1 if odds else None,
        "selectable": selectable,
        **extra,
    }


def analysis(sport, markets, grade="B"):
    return {
        "sport": sport,
        "grade": grade,
        "confidence": 60,
        "markets": markets,
        "insights": [],
        "summary": ".",
    }


# --- can_push -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sport", "key", "expected"),
    [
        ("football", "over25", False),
        ("football", "1X", False),
        ("football", "dnb_1", True),
        ("football", "ah_1_0", True),
        ("football", "ah_1_-1", True),
        ("football", "ah_1_-1.5", False),
        ("football", "over_3", True),
        ("football", "over_3.5", False),
        ("basketball", "over_185", True),
        ("basketball", "over_185.5", False),
        ("basketball", "ah_2_+6", True),
        ("basketball", "home_over_90", True),
        ("basketball", "1", False),
        ("basketball", "odd", False),
        ("tennis", "ah_1_+1.5", False),
        ("tennis", "sets_2-1", False),
        ("tennis", "games_over_22", False),
    ],
)
def test_can_push_flags_exactly_the_refundable_keys(sport, key, expected):
    assert can_push(sport, key) is expected


@pytest.mark.parametrize(
    ("sport", "key"),
    [("football", "dnb_2"), ("football", "ah_1_-1"), ("basketball", "over_185")],
)
def test_a_refundable_key_really_settles_as_a_push_for_some_score(sport, key):
    assert is_settleable(sport, key)
    scores = [(h, a) for h in range(0, 200, 1) for a in (0, 1, 2, 90, 95)]
    assert any(settle(sport, key, h, a) is None for h, a in scores)


# --- refundable legs never reach tickets or simulated bets --------------------------------


def basketball_fixture():
    return Match(
        id="b1",
        kickoff=NOW + timedelta(hours=5),
        league="USA: NBA",
        home="Home",
        away="Away",
        sport="basketball",
        # A priced winner market: fair value = p x odds x its overround (1/1.5 + 1/2.6 ~ 1.051).
        odds={"1": 1.5, "2": 2.6},
    )


def test_recommendations_skip_basketball_whole_lines_without_a_push_flag():
    markets = [
        market("1", 0.66, 1.5),  # fair value 0.66 x 1.5 x 1.051 ~ 1.04
        market("over_185", 0.6, 1.7),  # whole line: can push, basketball sets no "push" key
        market("over_185.5", 0.58, 1.75),
    ]
    legs = recommend.eligible_legs(basketball_fixture(), analysis("basketball", markets), NOW)
    assert [leg["key"] for leg in legs] == ["1", "over_185.5"]


def test_simulator_skips_refundable_markets_like_the_recommendations():
    rules = sim.local_rules()
    rows = [
        row(
            "m1",
            [("dnb_1", 0.9, 1.2), ("ah_1_-1", 0.8, 1.3), ("over_185", 0.8, 1.3), ("1", 0.55, 1.85)],
            margin=1.0,
        )
    ]
    assert [leg["key"] for leg in sim.model_legs(rows[0], rules)] == ["1"]


# --- headline tip ----------------------------------------------------------------------------


def test_football_tip_stays_in_the_ledger_set():
    markets = [
        market("ah_1_+2.5", 0.99, 1.02),  # priced extended market, selectable
        market("1X", 0.8, 1.3),
        market("over15", 0.75),
    ]
    assert headline_tip(analysis("football", markets))["key"] == "1X"


def test_other_sports_tip_is_a_priced_non_refundable_market():
    markets = [
        market("over_145.5", 0.99, group="Total puncte"),  # far unpriced line
        market("over_185", 0.9, 1.9, group="Total puncte"),  # priced but can push
        market("ah_1_+20.5", 0.97, 1.03, group="Handicap"),  # price below TIP_MIN_ODDS
        market("1", 0.7, 1.4),
        market("2", 0.3, 3.2),
    ]
    assert headline_tip(analysis("basketball", markets))["key"] == "1"


def test_unpriced_tip_falls_back_to_the_headline_markets():
    markets = [
        market("1", 0.62),
        market("2", 0.38),
        market("over_145.5", 0.99, group="Total puncte"),
        market("over_175.5", 0.52, group="Total puncte"),
        market("under_175.5", 0.48, group="Total puncte"),
    ]
    assert headline_tip(analysis("basketball", markets))["key"] == "1"


def test_board_item_uses_the_headline_tip():
    fixture = Match(id="f", kickoff=NOW, league="L", home="H", away="A", sport="tennis")
    markets = [
        market("1", 0.6, 1.6),
        market("2", 0.4, 2.4),
        market("ah_1_+1.5", 0.85, 1.05, group="Handicap seturi"),
        market("over_2.5", 0.4, group="Total seturi"),
        market("under_2.5", 0.6, group="Total seturi"),
    ]
    data = analysis("tennis", markets) | {
        "tips": [],
        "confidence": 50,
        "sample": {"home": 1, "away": 1, "h2h": 0},
        "form": {"home": {"sequence": ""}, "away": {"sequence": ""}},
        "summary": ".",
        "selection": None,
        "expected": {"home_win": 0.6},
    }
    assert board_item(fixture, data)["tip"]["key"] == "1"


# --- main_markets tie-break ----------------------------------------------------------------


@pytest.mark.parametrize("p", [0.5 + 1e-15, 0.3, 0.7, 0.123456789])
def test_main_markets_prefers_over_when_over_and_under_tie(p):
    markets = [
        market("1", 0.6),
        market("2", 0.4),
        market("under_180.5", 1 - p, group="Total puncte"),
        market("over_180.5", p, group="Total puncte"),
        market("ah_2_+4.5", 1 - p, group="Handicap"),
        market("ah_1_-4.5", p, group="Handicap"),
    ]
    keys = [m["key"] for m in main_markets(analysis("basketball", markets))]
    assert keys == ["1", "2", "ah_1_-4.5", "over_180.5"]


# --- dataset errors are Romanian, never OS paths ---------------------------------------------


def test_dataset_reason_hides_os_errors_and_keeps_our_messages(tmp_path):
    missing = tmp_path / "nowhere" / "matches.jsonl"
    try:
        missing.read_text()
    except FileNotFoundError as error:
        reason = sim_datasets.reason_of(error)
    assert str(tmp_path) not in reason and "Errno" not in reason
    ours = FileNotFoundError("Arhivele tennis-data.co.uk nu sunt descărcate.")
    assert sim_datasets.reason_of(ours) == str(ours)
    assert sim_datasets.reason_of(ValueError("Schema CSV invalidă")) == "Schema CSV invalidă"


def test_unavailable_datasets_point_to_real_commands(tmp_path):
    items = {d["id"]: d for d in sim_datasets.availability(None, tmp_path)}
    assert items["tennis"]["hint"].endswith("footypreds.evaluation.tennis_eval --download")
    assert "Errno" not in items["football"]["hint"]
    assert str(tmp_path) not in items["football"]["hint"]
