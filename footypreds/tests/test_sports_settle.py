"""Universal settlement: every key family, every sport, void and push rules."""

import pytest

from footypreds.engine.markets import FT_MARKETS, outcome
from footypreds.sports.keys import FOOTBALL_ALIASES, fmt_line, fmt_signed, handicap, label, parse
from footypreds.sports.settle import is_settleable, settle

CASES = [
    # sport, key, home, away, expected
    ("football", "1", 2, 1, True),
    ("football", "X", 1, 1, True),
    ("football", "2", 0, 3, True),
    ("football", "1X", 1, 1, True),
    ("football", "X2", 2, 1, False),
    ("football", "12", 0, 0, False),
    ("football", "over25", 2, 1, True),
    ("football", "under25", 2, 1, False),
    ("football", "btts", 1, 0, False),
    ("football", "no_btts", 1, 0, True),
    ("football", "home_over15", 2, 0, True),
    ("football", "1X_under35", 2, 2, False),
    ("football", "over_2.5", 2, 1, True),
    ("football", "over_3", 2, 0, False),
    ("football", "over_3", 2, 2, True),
    ("football", "over_3", 3, 0, None),  # push
    ("football", "under_3", 3, 0, None),
    ("football", "home_over_1.5", 2, 0, True),
    ("football", "away_under_0.5", 2, 0, True),
    ("football", "ah_1_-1.5", 2, 0, True),
    ("football", "ah_1_-1.5", 1, 0, False),
    ("football", "ah_1_-1", 1, 0, None),
    ("football", "ah_2_+1", 1, 0, None),
    ("football", "ah_2_+0.5", 1, 1, True),
    ("football", "ah_1_0", 1, 1, None),
    ("football", "ah_1_-0.25", 2, 0, None),  # quarter lines are not supported
    ("football", "dnb_1", 1, 1, None),
    ("football", "dnb_2", 0, 1, True),
    ("football", "odd", 2, 1, True),
    ("football", "even", 2, 1, False),
    ("football", "cs_2-1", 2, 1, True),
    ("football", "cs_2-1", 1, 2, False),
    ("football", "ht_1", 2, 0, None),  # needs the half-time score
    ("football", "1/1", 2, 0, None),
    ("basketball", "1", 101, 99, True),
    ("basketball", "2", 101, 99, False),
    ("basketball", "over_180.5", 90, 91, True),
    ("basketball", "under_180.5", 90, 91, False),
    ("basketball", "over_181", 90, 91, None),
    ("basketball", "ah_1_-5.5", 100, 94, True),
    ("basketball", "ah_1_-5.5", 100, 95, False),
    ("basketball", "ah_2_+5.5", 100, 95, True),
    ("basketball", "ah_2_+5", 100, 95, None),
    ("basketball", "home_over_89.5", 90, 70, True),
    ("basketball", "away_over_89.5", 90, 70, False),
    ("basketball", "odd", 90, 91, True),
    ("basketball", "btts", 90, 91, None),  # football-only key
    ("tennis", "1", 2, 1, True),
    ("tennis", "2", 2, 1, False),
    ("tennis", "sets_2-0", 2, 0, True),
    ("tennis", "sets_2-0", 2, 1, False),
    ("tennis", "over_2.5", 2, 1, True),
    ("tennis", "under_2.5", 2, 0, True),
    ("tennis", "ah_1_-1.5", 2, 0, True),
    ("tennis", "ah_1_-1.5", 2, 1, False),
    ("tennis", "ah_2_+1.5", 2, 1, True),
    ("tennis", "ah_2_+1.5", 2, 0, False),
    ("tennis", "games_over_22.5", 2, 1, None),  # total games is not in a set score
    ("tennis", "unknown", 2, 1, None),
]


@pytest.mark.parametrize("sport, key, home, away, expected", CASES)
def test_settlement_table(sport, key, home, away, expected):
    assert settle(sport, key, home, away) is expected


@pytest.mark.parametrize("key", sorted(FT_MARKETS))
def test_football_legacy_keys_delegate_to_the_engine(key):
    for home in range(5):
        for away in range(5):
            assert settle("football", key, home, away) is outcome(key, home, away)


@pytest.mark.parametrize("generic, legacy", sorted(FOOTBALL_ALIASES.items()))
def test_generic_aliases_settle_like_their_legacy_key(generic, legacy):
    for home in range(5):
        for away in range(5):
            assert settle("football", generic, home, away) is outcome(legacy, home, away)


@pytest.mark.parametrize("status", ["retired", "walkover", "unavailable", "postponed", "cancelled"])
@pytest.mark.parametrize("sport, key", [("tennis", "1"), ("tennis", "sets_2-0"), ("football", "1")])
def test_void_statuses(status, sport, key):
    assert settle(sport, key, 2, 0, status) is None


@pytest.mark.parametrize("status", ["scheduled", "live", ""])
def test_not_final_is_not_settled(status):
    assert settle("basketball", "1", 80, 70, status) is None


def test_after_extra_time_and_penalties_use_the_stored_final_score():
    assert settle("basketball", "2", 110, 112, "aet") is True
    assert settle("football", "X", 1, 1, "penalties") is True


def test_missing_scores_are_not_settled():
    assert settle("football", "1", None, 1) is None


@pytest.mark.parametrize(
    "sport, key, expected",
    [
        ("football", "over25", True),
        ("football", "ht_1", False),
        ("football", "1/1", False),
        ("football", "ah_1_-0.75", False),
        ("basketball", "over_180.5", True),
        ("basketball", "ah_1_-5", True),
        ("tennis", "sets_2-1", True),
        ("tennis", "games_over_22.5", False),
        ("tennis", "whatever", False),
    ],
)
def test_is_settleable(sport, key, expected):
    assert is_settleable(sport, key) is expected


def test_line_formatting_and_parsing_round_trip():
    assert fmt_line(2.5) == "2.5" and fmt_line(180.0) == "180" and fmt_line(0) == "0"
    assert fmt_signed(-1.5) == "-1.5" and fmt_signed(4.5) == "+4.5" and fmt_signed(-0.0) == "0"
    assert handicap("1", -1.5) == "ah_1_-1.5" and handicap("2", 4.5) == "ah_2_+4.5"
    assert parse("ah_2_+4.5") == ("handicap", ("2", "+4.5"))
    assert parse("home_over_89.5") == ("team_total", ("home", "over", "89.5"))
    assert parse("nonsense") is None


def test_labels_are_romanian_per_sport():
    assert label("football", "over25") == "Peste 2.5 goluri"
    assert label("basketball", "over_180.5") == "Peste 180.5 puncte"
    assert label("tennis", "over_2.5") == "Peste 2.5 seturi"
    assert label("tennis", "sets_2-1") == "Scor la seturi 2-1"
    assert label("basketball", "ah_1_-5.5") == "Handicap gazde -5.5"
    assert label("tennis", "games_over_22.5") == "Peste 22.5 game-uri"
