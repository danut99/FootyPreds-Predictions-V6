"""Sport dispatch, the common analysis shape and the baseline basketball/tennis analyzers."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from footypreds.domain import Match
from footypreds.engine import HistoryIndex, analyze
from footypreds.provider import normalize_matches
from footypreds.sports import SPORTS, analyze_match, main_markets, sport_list, validate_analysis
from footypreds.sports.settle import is_settleable, settle
from footypreds.sports.tennis import best_of, surface_of
from footypreds.tests.helpers import fixture, strong_history

KICKOFF = datetime(2026, 6, 1, 18, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"


def game(sport, match_id, days_before, home, away, hs, as_, **extra):
    return Match(
        id=match_id,
        kickoff=KICKOFF - timedelta(days=days_before),
        league=extra.pop("league", "L"),
        home=home,
        away=away,
        status="finished",
        home_goals=hs,
        away_goals=as_,
        sport=sport,
        **extra,
    )


def basketball_history(n=30):
    rows = []
    for i in range(n):
        rows.append(game("basketball", f"b{i}", 3 * (i + 1), "Strong", f"Opp{i}", 102, 80))
        rows.append(game("basketball", f"w{i}", 3 * (i + 1) + 1, f"Opp{i}", "Weak", 95, 78))
    return rows


def tennis_history(n=30):
    rows = []
    for i in range(n):
        rows.append(game("tennis", f"s{i}", 4 * (i + 1), "Strong P.", f"Opp{i}", 2, 0))
        rows.append(game("tennis", f"w{i}", 4 * (i + 1) + 1, f"Opp{i}", "Weak P.", 2, 1))
    return rows


def target(sport, **kwargs):
    names = {"tennis": ("Strong P.", "Weak P.")}.get(sport, ("Strong", "Weak"))
    data = dict(id="t", kickoff=KICKOFF, league="L", home=names[0], away=names[1], sport=sport)
    return Match(**(data | kwargs))


def test_registry():
    assert SPORTS["football"]["id"] == 1 and SPORTS["tennis"]["id"] == 2
    assert SPORTS["basketball"] == {"id": 3, "label": "Baschet"}
    assert [s["key"] for s in sport_list()] == ["football", "basketball", "tennis"]


@pytest.mark.parametrize("sport", list(SPORTS))
def test_every_sport_without_data_is_grade_d_and_valid(sport):
    analysis = validate_analysis(analyze_match(target(sport), []))
    assert analysis["sport"] == sport and analysis["grade"] == "D"
    assert analysis["selection"] is None and analysis["quality"] == "insufficient"


@pytest.mark.parametrize(
    "sport, history",
    [("basketball", basketball_history()), ("tennis", tennis_history())],
)
def test_history_gives_a_confident_selectable_analysis(sport, history):
    analysis = validate_analysis(analyze_match(target(sport), history, 0.6))
    assert analysis["grade"] in ("A", "B")
    p = {m["key"]: m["probability"] for m in analysis["markets"]}
    assert p["1"] > 0.75 and p["1"] + p["2"] == pytest.approx(1)
    assert analysis["selection"] is not None
    assert analysis["sample"]["home"] > 0 and analysis["form"]["home"]["sequence"]


@pytest.mark.parametrize("sport", ["basketball", "tennis"])
def test_every_selectable_market_can_be_settled(sport):
    history = basketball_history() if sport == "basketball" else tennis_history()
    analysis = analyze_match(target(sport, odds={"1": 1.4, "2": 3.0}), history)
    for market in analysis["markets"]:
        if market["selectable"]:
            assert is_settleable(sport, market["key"]), market["key"]


def test_basketball_distribution_is_consistent_and_uses_market_prices():
    odds = {"1": 1.43, "2": 2.92, "over_185.5": 1.87, "under_185.5": 1.93, "ah_1_-5.5": 1.88}
    analysis = validate_analysis(analyze_match(target("basketball", odds=odds), []))
    by_key = {m["key"]: m for m in analysis["markets"]}
    expected = analysis["expected"]
    assert expected["home"] + expected["away"] == pytest.approx(185.5, abs=2)
    assert expected["home"] > expected["away"]
    assert by_key["1"]["probability"] == pytest.approx(1 / 1.43 / (1 / 1.43 + 1 / 2.92), abs=0.01)
    for key, m in by_key.items():
        if key.startswith("over_"):
            under = by_key["under_" + key[5:]]
            assert m["probability"] + under["probability"] == pytest.approx(1)
    assert by_key["ah_1_-5.5"]["odds"] == 1.88 and by_key["ah_1_-5.5"]["ev"] is not None
    assert by_key["ah_1_-5.5"]["probability"] + by_key["ah_2_+5.5"]["probability"] == (
        pytest.approx(1)
    )
    assert analysis["expected"]["margin_sd"] > 0 and analysis["expected"]["total_sd"] > 0


@pytest.mark.parametrize(
    "league, sets",
    [
        ("ATP - SINGLES: Wimbledon (United Kingdom), grass", 5),
        ("ATP - SINGLES: Chengdu (China), hard", 3),
        ("WTA - SINGLES: Wimbledon (United Kingdom), grass", 3),
        ("ATP - DOUBLES: Wimbledon (United Kingdom), grass", 3),
        ("CHALLENGER MEN - SINGLES: Genova 2 (Italy), clay", 3),
    ],
)
def test_tennis_best_of(league, sets):
    assert best_of(target("tennis", league=league)) == sets


def test_tennis_surface():
    assert surface_of("WTA - SINGLES: Singapore (Singapore), hard (indoor)") == "hard"
    assert surface_of("Chengdu, clay") == "clay" and surface_of("Laver Cup") == ""


@pytest.mark.parametrize("sets", [3, 5])
def test_tennis_set_markets_are_a_distribution(sets):
    league = "ATP - SINGLES: US Open (USA), hard" if sets == 5 else "L"
    analysis = validate_analysis(
        analyze_match(target("tennis", league=league, odds={"1": 1.5, "2": 2.6}), [])
    )
    assert analysis["expected"]["best_of"] == sets
    exact = [m for m in analysis["markets"] if m["key"].startswith("sets_")]
    assert len(exact) == 2 * (sets // 2 + 1)
    assert sum(m["probability"] for m in exact) == pytest.approx(1)
    p = {m["key"]: m["probability"] for m in analysis["markets"]}
    home_wins = sum(m["probability"] for m in exact if int(m["key"][5]) > int(m["key"][7]))
    assert home_wins == pytest.approx(p["1"], abs=1e-6)
    assert p["1"] == pytest.approx((1 / 1.5) / (1 / 1.5 + 1 / 2.6), abs=1e-6)
    assert p["ah_1_+1.5"] + p["ah_2_-1.5"] == pytest.approx(1)
    for key, probability in p.items():
        if key.startswith("sets_"):
            h, a = (int(x) for x in key[5:].split("-"))
            won = [m for m in analysis["markets"] if settle("tennis", m["key"], h, a)]
            assert all(m["probability"] >= probability - 1e-9 for m in won)


@pytest.mark.parametrize("sport", ["basketball", "tennis"])
def test_no_result_at_or_after_kickoff_minus_three_hours_is_used(sport):
    fixture_ = target(sport)
    home = fixture_.home
    hs = 120 if sport == "basketball" else 2
    late = [
        game(sport, f"late{i}", 0, home, f"X{i}", hs, 0).model_copy(
            update={"kickoff": KICKOFF - timedelta(hours=3) + timedelta(minutes=i)}
        )
        for i in range(5)
    ]
    clean = analyze_match(fixture_, [])
    leaked = analyze_match(fixture_, late)
    assert leaked["sample"] == clean["sample"] == {"home": 0, "away": 0, "h2h": 0}
    assert leaked["markets"] == clean["markets"]


def test_histories_of_different_sports_never_mix():
    # "Strong" is also a football club that always wins 4-0.
    football = strong_history()
    analysis = analyze_match(target("basketball"), football)
    assert analysis["sample"]["home"] == 0 and analysis["grade"] == "D"
    football_view = analyze_match(fixture(), football + basketball_history())
    assert football_view["sample"] == analyze(fixture(), football)["sample"]


def test_football_dispatch_is_the_unchanged_engine():
    history = strong_history()
    direct = analyze(fixture(), history)
    assert analyze_match(fixture(), history) == direct
    assert analyze_match(fixture(), HistoryIndex(history)) == direct
    validate_analysis(direct)
    assert direct["sport"] == "football" and direct["expected"] == direct["expected_goals"]


def test_main_markets_per_sport():
    football = analyze_match(fixture(), strong_history())
    assert [m["key"] for m in main_markets(football)] == ["1", "X", "2", "over25"]
    basketball = analyze_match(target("basketball"), basketball_history())
    keys = [m["key"] for m in main_markets(basketball)]
    assert keys[:2] == ["1", "2"] and keys[2].startswith("ah_") and keys[3].startswith("over_")
    tennis = analyze_match(target("tennis"), tennis_history())
    keys = [m["key"] for m in main_markets(tennis)]
    assert keys[:3] == ["1", "2", "over_2.5"] and keys[3].startswith("sets_")


def test_real_tennis_h2h_rows_feed_the_tennis_analyzer():
    rows, _ = normalize_matches(
        json.loads((FIXTURES / "h2h_tennis.json").read_text(encoding="utf-8")),
        results=True,
        sport="tennis",
    )
    newest = max(r.kickoff for r in rows)
    fixture_ = Match(
        id="next",
        kickoff=newest + timedelta(days=2),
        league="ATP - SINGLES: Chengdu (China), hard",
        home="Cerundolo J. M.",
        away="Davidovich Fokina A.",
        sport="tennis",
        odds={"1": 2.3, "2": 1.57},
    )
    analysis = validate_analysis(analyze_match(fixture_, rows))
    assert analysis["sample"]["home"] > 10
    assert analysis["expected"]["surface"] == "hard"
    assert analysis["form"]["home"]["last"][0]["competition"] == "Chengdu, hard"


def test_validate_analysis_rejects_a_broken_shape():
    analysis = analyze_match(target("basketball"), [])
    broken = {k: v for k, v in analysis.items() if k != "tips"}
    with pytest.raises(AssertionError):
        validate_analysis(broken)
    duplicated = analysis | {"markets": analysis["markets"] + analysis["markets"][:1]}
    with pytest.raises(AssertionError):
        validate_analysis(duplicated)
