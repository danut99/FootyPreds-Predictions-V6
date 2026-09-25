"""In-play models (footypreds/live.py): coherent probabilities for every sport and odd stage."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from footypreds import live as lv
from footypreds.domain import Match
from footypreds.provider import normalize_matches, parse_stats
from footypreds.sports.keys import fmt_line, parse
from footypreds.sports.settle import is_settleable

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
KICKOFF = datetime(2026, 9, 25, 18, tzinfo=timezone.utc)
ITEM_KEYS = (
    "match",
    "competition",
    "competition_id",
    "minute",
    "period",
    "markets",
    "suggestions",
    "summary",
    "score",
    "probabilities",
    "pre_match",
    "notes",
)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def live_matches(sport):
    matches, rejected = normalize_matches(load(f"live_{sport}.json"), sport=sport)
    assert rejected == 0
    return matches


def game(sport="football", home=0, away=0, odds=None, league="Test League", **live):
    info = {"stage": "", "clock": "", "minute": None, "period": ""} | live
    return Match(
        id=f"{sport}-live",
        kickoff=KICKOFF,
        league=league,
        home="Gazde",
        away="Oaspeți",
        status="live",
        home_goals=home,
        away_goals=away,
        odds=odds if odds is not None else {},
        sport=sport,
        live=info,
    )


EVEN = {"1": 2.6, "X": 3.2, "2": 2.9}


def probs(item):
    return {m["key"]: m["probability"] for m in item["markets"]}


def check_item(item):
    """The LiveItem contract plus internal coherence of every market."""
    assert all(k in item for k in ITEM_KEYS), set(ITEM_KEYS) - set(item)
    assert item["match"]["status"] == "live"
    assert isinstance(item["summary"], str) and item["summary"]
    assert any("dinainte de meci" in note for note in item["notes"])
    keys = [m["key"] for m in item["markets"]]
    assert len(keys) == len(set(keys))
    sport = item["match"]["sport"]
    for m in item["markets"]:
        assert 0 <= m["probability"] <= 1
        assert m["odds"] is None and m["ev"] is None
        assert m["fair_odds"] == pytest.approx(1 / m["probability"], rel=1e-3)
        assert isinstance(m["why"], str) and m["why"]
        if m["selectable"]:
            assert is_settleable(sport, m["key"]), m["key"]
            assert 0 < m["probability"] < 1, "decided markets are not offered"
    by_key = {m["key"]: m for m in item["markets"]}
    assert len(item["suggestions"]) <= lv.MAX_SUGGESTIONS
    groups = [s["group"] for s in item["suggestions"]]
    assert len(groups) == len(set(groups))
    for s in item["suggestions"]:
        source = by_key[s["key"]]
        assert source["selectable"] and source["reliable"]
        assert s["probability"] == source["probability"]
        assert s["min_odds"] >= s["fair_odds"] - 1e-9
        assert s["kind"] in ("sigur", "echilibrat") and s["why"]
    return by_key


# --- football ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        {"1": 0.45, "X": 0.27, "2": 0.28},
        {"1": 0.8, "X": 0.13, "2": 0.07},
        {"1": 0.2, "X": 0.3, "2": 0.5},
        {"1": 0.33, "X": 0.34, "2": 0.33},
    ],
)
def test_fitted_lambdas_reproduce_the_1x2(target):
    home, away = lv.fit_lambdas(target)
    fitted = lv.one_x_two(home, away)
    for got, want in zip(fitted, (target["1"], target["X"], target["2"])):
        assert got == pytest.approx(want, abs=0.01)


def test_fit_rejects_bad_targets_and_prematch_falls_back():
    assert lv.fit_lambdas({"1": 0.5, "X": None, "2": 0.3}) is None
    assert lv.implied_1x2({"1": 1.9, "2": 2.0}) is None
    assert lv.implied_1x2({"1": 1.0, "X": 3.0, "2": 4.0}) is None
    default = lv.football_prematch({})
    assert default["source"] == "default"
    assert (default["home"], default["away"]) == lv.DEFAULT_LAMBDAS
    favourite = lv.football_prematch({"1": 1.3, "X": 5.5, "2": 10})
    assert favourite["source"] == "odds" and favourite["home"] > 2 * favourite["away"]


def test_prematch_prefers_a_graded_analysis_over_odds():
    markets = [{"key": k, "probability": p} for k, p in (("1", 0.2), ("X", 0.3), ("2", 0.5))]
    analysis = {"grade": "B", "markets": markets, "expected": {"home": 1.0, "away": 1.5}}
    pre = lv.football_prematch({"1": 1.5, "X": 4, "2": 6}, analysis)
    assert pre["source"] == "analysis" and pre["away"] > pre["home"]
    weak = lv.football_prematch({"1": 1.5, "X": 4, "2": 6}, {**analysis, "grade": "D"})
    assert weak["source"] == "odds" and weak["home"] > weak["away"]


def test_every_captured_football_game_is_coherent():
    items = [lv.live_item(m) for m in live_matches("football")]
    assert len(items) == 29
    for item in items:
        by_key = check_item(item)
        p = probs(item)
        assert p["1"] + p["X"] + p["2"] == pytest.approx(1, abs=1e-9)
        assert p["1X"] == pytest.approx(p["1"] + p["X"])
        assert p["dnb_1"] + p["dnb_2"] == pytest.approx(1)
        total = item["score"]["home"] + item["score"]["away"]
        overs = [
            by_key[lv.football_total_key("over", total + step)]["probability"]
            for step in (0.5, 1.5, 2.5)
        ]
        assert overs[0] > overs[1] > overs[2]
        for step in (0.5, 1.5, 2.5):
            over = p[lv.football_total_key("over", total + step)]
            under = p[lv.football_total_key("under", total + step)]
            assert over + under == pytest.approx(1)
        nxt = p["next_goal_1"] + p["next_goal_none"] + p["next_goal_2"]
        assert nxt == pytest.approx(1)
        assert not by_key["next_goal_1"]["selectable"]


def test_football_keys_use_the_legacy_spelling():
    p = probs(lv.live_item(game(home=1, away=1, odds=EVEN, period="2H", minute=60)))
    assert {"over25", "over35", "over45", "under25"} <= set(p)
    assert "over_2.5" not in p
    late = probs(lv.live_item(game(home=3, away=2, odds=EVEN, period="2H", minute=60)))
    assert "over_5.5" in late and "over_7.5" in late


def test_a_team_leading_late_is_a_big_favourite():
    p = probs(lv.live_item(game(home=1, away=0, odds=EVEN, period="2H", minute=88)))
    assert p["1"] > 0.88
    assert p["X"] > p["2"]
    trailing = probs(lv.live_item(game(home=0, away=1, odds=EVEN, period="2H", minute=88)))
    assert trailing["2"] > 0.88


def test_goalless_at_89_minutes_is_most_likely_a_draw():
    p = probs(lv.live_item(game(odds=EVEN, period="2H", minute=89)))
    assert p["X"] > 0.8
    assert p["under05"] > 0.8 and p["no_btts"] > 0.95


def test_win_probability_of_the_leader_grows_with_the_clock():
    values = [
        probs(lv.live_item(game(home=1, odds=EVEN, period=period, minute=minute)))["1"]
        for period, minute in (("1H", 10), ("1H", 40), ("HT", None), ("2H", 60), ("2H", 85))
    ]
    assert values == sorted(values)
    assert values[-1] > 0.8


def test_red_cards_move_the_probabilities_the_right_way():
    base = probs(lv.live_item(game(odds=EVEN, period="1H", minute=30)))
    home_red = lv.live_item(
        game(odds=EVEN, period="1H", minute=30, red_cards={"home": 1, "away": 0})
    )
    away_red = probs(
        lv.live_item(game(odds=EVEN, period="1H", minute=30, red_cards={"home": 0, "away": 1}))
    )
    reds = probs(home_red)
    assert reds["1"] < base["1"] and reds["2"] > base["2"]
    assert reds["next_goal_2"] > base["next_goal_2"]
    assert away_red["1"] > base["1"] and away_red["2"] < base["2"]
    assert any("roșu" in note for note in home_red["notes"])
    two = probs(
        lv.live_item(game(odds=EVEN, period="1H", minute=30, red_cards={"home": 2, "away": 0}))
    )
    assert two["1"] < reds["1"]
    assert lv.red_card_factors({"home": 9, "away": 0})[0] >= 0.3


def test_decided_markets_are_not_offered():
    item = lv.live_item(game(home=2, away=1, odds=EVEN, period="2H", minute=50))
    p = probs(item)
    assert "btts" not in p and "no_btts" not in p
    assert "home_win_nil" not in p and "away_win_nil" not in p
    assert "over25" not in p  # 3 goals already: the lines start at 3.5
    assert "over35" in p


def test_half_time_and_unknown_minutes():
    half = lv.live_item(game(home=1, odds=EVEN, period="HT", stage="Half Time"))
    assert half["model"]["remaining"]["share"] == pytest.approx(1 - lv.FIRST_HALF_GOALS)
    unknown = lv.live_item(game(odds=EVEN, period="2H", stage="2nd Half"))
    assert any("presupunem" in n for n in unknown["notes"])
    known = lv.live_item(game(odds=EVEN, period="2H", minute=67))
    assert probs(unknown)["X"] == pytest.approx(probs(known)["X"])
    phase = lv.live_item(game(odds=EVEN, period="", stage="Live"))
    assert any("Faza meciului" in n for n in phase["notes"])
    check_item(phase)
    # A minute without a period still places the game in the right half.
    assert lv.football_clock("BREAK", 70)[0] == lv.football_clock("2H", 70)[0]


def test_stoppage_time_keeps_a_little_time_left():
    share, elapsed, _ = lv.football_clock("2H", 98)
    assert share > 0 and elapsed == 98
    p = probs(lv.live_item(game(odds=EVEN, period="2H", minute=98, clock="90+8")))
    assert 0.9 < p["X"] < 1


@pytest.mark.parametrize("period", ["ET", "PEN"])
def test_extra_time_and_penalties_have_no_final_result_markets(period):
    item = lv.live_item(game(home=1, away=1, odds=EVEN, period=period))
    assert item["markets"] == [] and item["suggestions"] == []
    assert any("90 de minute" in n for n in item["notes"])
    assert item["summary"]


def test_without_prematch_information_nothing_is_suggested_early():
    item = lv.live_item(game(period="1H", minute=20))
    check_item(item)
    assert item["pre_match"]["source"] == "default"
    assert item["suggestions"] == []
    late = lv.live_item(game(home=2, period="2H", minute=80))
    assert late["suggestions"]


def test_live_xg_momentum_is_bounded_and_directional():
    pre = {"home": 1.4, "away": 1.2}
    stats = {
        "match": [
            {"name": "Expected goals (xG)", "home_value": 2.5, "away_value": 0.1},
            {"name": "Shots on target", "home_value": 8, "away_value": 0},
        ]
    }
    (home, away), observed = lv.momentum(stats, pre, 0.5, 45)
    assert home == lv.MOMENTUM_BOUNDS[1] and away < 1
    assert away >= lv.MOMENTUM_BOUNDS[0] and observed["source"] == "xG"
    assert lv.momentum(stats, pre, 0.1, 10) == ((1.0, 1.0), None)
    shots = {"match": [{"name": "Shots on target", "home_value": 0, "away_value": 5}]}
    (home, away), observed = lv.momentum(shots, pre, 0.5, 45)
    assert away > 1 > home and observed["source"] == "șuturi pe poartă"
    # The captured payload: all zeros early in the game -> no adjustment at all.
    captured = parse_stats(load("stats_live_football.json"))
    assert lv.momentum(captured, pre, 0.5, 45) == ((1.0, 1.0), None)


def test_stats_change_the_live_estimate():
    stats = {"match": [{"name": "Expected goals (xG)", "home_value": 2.4, "away_value": 0.2}]}
    base = lv.live_item(game(odds=EVEN, period="2H", minute=60))
    pushed = lv.live_item(game(odds=EVEN, period="2H", minute=60), stats=stats)
    assert probs(pushed)["1"] > probs(base)["1"]
    assert any("xG" in n for n in pushed["notes"])
    check_item(pushed)


# --- tennis --------------------------------------------------------------------------------


def test_tennis_markov_known_values():
    assert lv.tennis_match_win(0.5, 1, 0, 3) == pytest.approx(0.75)
    assert lv.tennis_match_win(0.5, 0, 1, 3) == pytest.approx(0.25)
    assert lv.tennis_match_win(0.5, 1, 1, 3) == pytest.approx(0.5)
    assert lv.tennis_match_win(0.5, 2, 0, 5) == pytest.approx(1 - 0.5**3)
    q = 0.6
    assert lv.tennis_match_win(q, 0, 0, 3) == pytest.approx(q**2 * (1 + 2 * (1 - q)))
    assert lv.tennis_match_win(q, 2, 1, 3) == 1.0
    assert lv.tennis_match_win(q, 0, 3, 5) == 0.0
    for sets in (3, 5):
        for h in range(sets // 2 + 1):
            for a in range(sets // 2 + 1):
                scores = lv.tennis_final_scores(0.37, h, a, sets)
                assert sum(scores.values()) == pytest.approx(1)
                assert all(fh >= h and fa >= a for fh, fa in scores)


@pytest.mark.parametrize("sets", [3, 5])
def test_set_probability_inverts_the_match_probability(sets):
    for p in (0.1, 0.35, 0.5, 0.8, 0.97):
        q = lv.set_probability(p, sets)
        assert lv.tennis_match_win(q, 0, 0, sets) == pytest.approx(p, abs=1e-6)
    assert lv.set_probability(0.7, 5) < lv.set_probability(0.7, 3)


def test_every_captured_tennis_game_is_coherent():
    items = [lv.live_item(m) for m in live_matches("tennis")]
    assert len(items) == 17
    for item in items:
        by_key = check_item(item)
        p = probs(item)
        assert p["1"] + p["2"] == pytest.approx(1)
        exact = {k: v for k, v in p.items() if k.startswith("sets_")}
        home_sets = sum(v for k, v in exact.items() if int(parse(k)[1][1]) > int(parse(k)[1][2]))
        assert home_sets == pytest.approx(p["1"]) or home_sets <= p["1"]
        if "next_set_1" in by_key:
            assert not by_key["next_set_1"]["selectable"]
            assert p["next_set_1"] + p["next_set_2"] == pytest.approx(1)


def test_tennis_live_uses_prematch_odds_and_the_set_score():
    odds = {"1": 1.5, "2": 2.6}
    even = probs(lv.live_item(game("tennis", 0, 0, odds, period="S1")))
    ahead = probs(lv.live_item(game("tennis", 1, 0, odds, period="S2")))
    behind = probs(lv.live_item(game("tennis", 0, 1, odds, period="S2")))
    assert behind["1"] < even["1"] < ahead["1"]
    assert even["1"] == pytest.approx(1 / 1.5 / (1 / 1.5 + 1 / 2.6), abs=1e-6)
    # At 1-1 in a best of 3 the next set decides: "1" equals "sets_2-1" and "over_2.5" is sure.
    decider = probs(lv.live_item(game("tennis", 1, 1, odds, period="S3")))
    assert decider["1"] == pytest.approx(decider["sets_2-1"])
    assert "over_2.5" not in decider and "under_2.5" not in decider


def test_tennis_best_of_five_and_mismatched_stage():
    slam = "ATP - SINGLES: US Open (USA), hard"
    item = lv.live_item(game("tennis", 2, 0, {"1": 1.8, "2": 2.0}, league=slam, period="S2"))
    check_item(item)
    assert item["pre_match"]["expected"]["best_of"] == 5
    p = probs(item)
    assert {"sets_3-0", "sets_3-1", "sets_3-2", "sets_2-3"} <= set(p)
    assert "ah_1_-2.5" in p
    assert any("nu se potrivește" in n for n in item["notes"])


def test_tennis_without_odds_is_shown_but_not_suggested():
    item = lv.live_item(game("tennis", 1, 0, period="S2"))
    assert probs(item)["1"] == pytest.approx(0.75)
    assert item["suggestions"] == []


# --- basketball ----------------------------------------------------------------------------


def test_every_captured_basketball_game_is_coherent():
    items = [lv.live_item(m) for m in live_matches("basketball")]
    assert len(items) == 8
    for item in items:
        by_key = check_item(item)
        p = probs(item)
        assert p["1"] + p["2"] == pytest.approx(1)
        overs = sorted((float(k[5:]), v) for k, v in p.items() if k.startswith("over_"))
        assert [v for _, v in overs] == sorted((v for _, v in overs), reverse=True)
        for line, value in overs:
            assert value + p[f"under_{fmt_line(line)}"] == pytest.approx(1)
        homes = sorted((float(parse(k)[1][1]), v) for k, v in p.items() if k.startswith("ah_1_"))
        assert [v for _, v in homes] == sorted(v for _, v in homes)
        assert all(by_key[k]["selectable"] for k in by_key)


def test_basketball_near_the_end_of_the_game():
    odds = {"1": 1.9, "2": 1.9}
    leading = probs(lv.live_item(game("basketball", 80, 72, odds, period="Q4", minute=39)))
    assert leading["1"] > 0.97
    unknown = probs(lv.live_item(game("basketball", 80, 72, odds, period="Q4")))
    assert unknown["1"] > 0.9
    tied = probs(lv.live_item(game("basketball", 75, 75, odds, period="Q4", minute=39)))
    assert tied["1"] == pytest.approx(0.5, abs=0.05)
    early = probs(lv.live_item(game("basketball", 10, 2, odds, period="Q1", minute=4)))
    assert 0.5 < early["1"] < leading["1"]


def test_basketball_clock_parsing():
    elapsed, remaining, _ = lv.basketball_clock("HT", None, 10, 80, 160)
    assert elapsed == remaining == 0.5
    elapsed, remaining, notes = lv.basketball_clock("OT", None, 10, 170, 160)
    assert elapsed == 1 and 0 < remaining < 0.1 and notes
    # A small minute is inside the quarter; a large one is the game minute.
    assert lv.basketball_clock("Q3", 5, 10, 0, 160)[0] == pytest.approx(25 / 40)
    assert lv.basketball_clock("Q3", 25, 10, 0, 160)[0] == pytest.approx(25 / 40)
    # No minute: the score places the game inside the quarter.
    elapsed, _, notes = lv.basketball_clock("Q2", None, 10, 60, 160)
    assert 0.25 < elapsed < 0.5 and any("Minutul" in n for n in notes)
    elapsed, _, notes = lv.basketball_clock("BREAK", None, 10, 118, 160)
    assert elapsed == 0.75 and notes
    assert lv.basketball_clock("", None, 10, 0, 160)[0] == 0.25


def test_basketball_quarter_length_and_overtime():
    nba = Match(
        id="nba",
        kickoff=KICKOFF,
        league="NBA",
        country="USA",
        home="Lakers",
        away="Celtics",
        status="live",
        home_goals=100,
        away_goals=100,
        sport="basketball",
        live={"stage": "Overtime", "period": "OT", "minute": None, "clock": ""},
    )
    assert lv.quarter_minutes(nba) == 12
    assert lv.quarter_minutes(game("basketball", league="EUROLEAGUE")) == 10
    item = lv.live_item(nba)
    check_item(item)
    assert probs(item)["1"] == pytest.approx(0.5, abs=0.02)


def test_basketball_pace_and_uncertain_totals():
    odds = {"1": 1.5, "2": 2.6}
    # Clock known and half the game played: the observed pace drives the total.
    fast = lv.live_item(game("basketball", 60, 55, odds, period="Q3", minute=20))
    assert fast["model"]["projected"]["total"] > 200
    assert any(m["group"] == "Total puncte" and m["reliable"] for m in fast["markets"])
    # No clock and no predicted total: totals are shown but never suggested.
    blind = lv.live_item(game("basketball", 20, 18, odds, period="Q1"))
    totals = [m for m in blind["markets"] if m["group"] == "Total puncte"]
    assert totals and not any(m["reliable"] for m in totals)
    assert all(s["group"] != "Total puncte" for s in blind["suggestions"])
    assert all(float(parse(m["key"])[1][1]) > 38 for m in totals)


def test_basketball_prematch_from_an_analysis():
    analysis = {
        "grade": "B",
        "expected": {"home": 90.0, "away": 80.0, "margin_sd": 11.0, "total_sd": 15.0},
    }
    pre = lv.basketball_prematch({"1": 3.0, "2": 1.4}, analysis)
    assert pre["source"] == "analysis" and pre["margin"] == 10 and pre["total"] == 170
    item = lv.live_item(game("basketball", 0, 0, {}, period="Q1", minute=1), analysis)
    assert probs(item)["1"] > 0.75 and item["pre_match"]["grade"] == "B"


# --- odd stages through the real parser ----------------------------------------------------


def stage_payload(sport, stage, home=1, away=0, minute=None, reds=None):
    team = {"home": {"name": "A", "team_id": "a"}, "away": {"name": "B", "team_id": "b"}}
    if reds is not None:
        team["home"]["red_cards"], team["away"]["red_cards"] = reds
    return [
        {
            "name": "TEST: League",
            "country_name": "Test",
            "matches": [
                {
                    "match_id": "odd1",
                    "timestamp": 1790348400,
                    "match_status": {
                        "stage": stage,
                        "is_started": True,
                        "is_in_progress": True,
                        "is_finished": False,
                        "live_time": str(minute) if minute is not None else stage,
                        "live_minute": minute,
                    },
                    "home_team": team["home"],
                    "away_team": team["away"],
                    "scores": {"home": home, "away": away},
                    "odds": {"1": 2.0, "X": 3.3, "2": 3.6}
                    if sport == "football"
                    else {"1": 1.7, "X": None, "2": 2.1},
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    "sport,stage,home,away,minute,period",
    [
        ("football", "Half Time", 1, 0, None, "HT"),
        ("football", "Break Time", 0, 0, None, "BREAK"),
        ("football", "Penalties", 1, 1, None, "PEN"),
        ("football", "Extra Time", 2, 2, 105, "ET"),
        ("football", "Interrupted", 0, 1, 70, "INT"),
        ("football", "Awaiting extra time", 1, 1, None, "ET"),
        ("basketball", "Overtime", 88, 88, None, "OT"),
        ("basketball", "Break Time", 50, 44, None, "BREAK"),
        ("basketball", "Half Time", 40, 45, None, "HT"),
        ("basketball", "4th Quarter", 70, 60, None, "Q4"),
        ("tennis", "Set 3", 1, 1, None, "S3"),
        ("tennis", "Set 5", 2, 2, None, "S5"),
        ("tennis", "Interrupted", 1, 0, None, "INT"),
        ("tennis", "Live", 0, 0, None, ""),
    ],
)
def test_odd_stages_never_break_the_analysis(sport, stage, home, away, minute, period):
    matches, rejected = normalize_matches(
        stage_payload(sport, stage, home, away, minute, (0, 1) if sport == "football" else None),
        sport=sport,
    )
    assert rejected == 0 and len(matches) == 1
    match = matches[0]
    assert match.status == "live" and match.live["period"] == period
    item = lv.live_item(match)
    check_item(item)
    json.dumps(item)  # JSON-ready
    if item["markets"] and "1" in probs(item):
        p = probs(item)
        assert p["1"] + p["2"] + p.get("X", 0) == pytest.approx(1)


def test_fair_odds_round_up_for_the_minimum_price():
    assert lv.minimum_odds(1.845) == 1.85
    assert lv.minimum_odds(2.0) == 2.0
    assert math.isclose(lv.minimum_odds(1.0001), 1.01)
