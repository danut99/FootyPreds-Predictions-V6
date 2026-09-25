"""Tennis model: Elo book, market blend, set and game distributions, analysis shape."""

import itertools
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import footypreds.provider as provider_module
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.engine import HistoryIndex
from footypreds.provider import normalize_matches
from footypreds.sports import analyze_match, main_markets, validate_analysis
from footypreds.sports import tennis as t
from footypreds.sports.odds import parse_odds
from footypreds.sports.settle import is_settleable, settle
from footypreds.tests.test_sports_api import Fake

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
KICKOFF = datetime(2026, 6, 1, 18, tzinfo=timezone.utc)
MODEL = t.TennisParams(market_weight=0.5, experience=0.0)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def game(match_id, days_before, home, away, hs, as_, **extra):
    return Match(
        id=match_id,
        kickoff=KICKOFF - timedelta(days=days_before),
        league=extra.pop("league", "ATP - SINGLES: Madrid (Spain), clay"),
        home=home,
        away=away,
        status=extra.pop("status", "finished"),
        home_goals=hs,
        away_goals=as_,
        sport="tennis",
        **extra,
    )


def target(**kwargs):
    data = dict(
        id="t",
        kickoff=KICKOFF,
        league="ATP - SINGLES: Madrid (Spain), clay",
        home="Strong P.",
        away="Weak P.",
        sport="tennis",
    )
    return Match(**(data | kwargs))


def ladder(n=30):
    rows = []
    for i in range(n):
        rows.append(game(f"s{i}", 4 * (i + 1), "Strong P.", f"Opp{i}", 2, 0))
        rows.append(game(f"w{i}", 4 * (i + 1) + 1, f"Opp{i}", "Weak P.", 2, 1))
    return rows


def real_rows():
    rows, rejected = normalize_matches(load("h2h_tennis.json"), results=True, sport="tennis")
    assert rows and rejected == 0
    return rows


def real_fixtures():
    matches, _ = normalize_matches(load("list_tennis.json"), sport="tennis")
    return matches


def probabilities(analysis):
    return {m["key"]: m["probability"] for m in analysis["markets"]}


# --- analysis shape on the captured FlashScore payloads -------------------------------------


def test_real_fixture_and_h2h_rows_give_a_valid_analysis():
    match = next(m for m in real_fixtures() if m.id == "KnR6QDo1")
    odds = parse_odds(load("odds_tennis.json"), "tennis", "j1EyxKUg", "bRHqzba6", 3)
    fixture = match.model_copy(
        update={
            "kickoff": max(r.kickoff for r in real_rows()) + timedelta(days=2),
            "odds": {**{k: v["best"] for k, v in odds.items()}, **match.odds},
        }
    )
    analysis = validate_analysis(analyze_match(fixture, real_rows()))
    assert analysis["version"] == t.VERSION and analysis["sport"] == "tennis"
    assert analysis["sample"]["home"] > 10 and analysis["sample"]["away"] > 10
    assert analysis["expected"]["best_of"] == 3 and analysis["expected"]["surface"] == "hard"
    by_key = {m["key"]: m for m in analysis["markets"]}
    # Every games line with a price is shown with its price, and never selectable.
    for key in odds:
        if key.startswith("games_"):
            assert by_key[key]["odds"] == odds[key]["best"] and by_key[key]["ev"] is not None
            assert by_key[key]["selectable"] is False
    assert by_key["sets_2-1"]["odds"] == 5.0 and by_key["ah_1_+1.5"]["odds"] == 1.52
    for market in analysis["markets"]:
        assert market["selectable"] == is_settleable("tennis", market["key"]), market["key"]
    assert any("Rating Elo" in note for note in analysis["insights"])
    assert "game-uri" in analysis["summary"]
    assert analysis["components"]["elo_matches"]["home"] > 10


def test_every_listed_tennis_fixture_is_analyzed():
    rows = real_rows()
    for match in real_fixtures():
        fixture = match.model_copy(update={"kickoff": max(r.kickoff for r in rows) + timedelta(1)})
        analysis = validate_analysis(analyze_match(fixture, rows))
        keys = [m["key"] for m in main_markets(analysis)]
        assert keys[:2] == ["1", "2"] and keys[2] in ("over_2.5", "under_2.5")
        assert keys[3].startswith("sets_")
        if analysis["selection"]:
            assert is_settleable("tennis", analysis["selection"]["key"])


def test_market_only_prediction_is_the_margin_free_price():
    fixture = target(odds={"1": 1.5, "2": 2.6})
    p = probabilities(analyze_match(fixture, []))
    assert p["1"] == pytest.approx((1 / 1.5) / (1 / 1.5 + 1 / 2.6), abs=1e-9)


def test_elo_moves_the_market_price_only_with_experience():
    fixture = target(odds={"1": 2.0, "2": 2.0})
    history = ladder()
    blended = t.analyze(fixture, history, params=MODEL)
    assert blended["expected"]["home_win"] > 0.6
    assert blended["components"]["model_weight"] == pytest.approx(0.5)
    ramp = t.TennisParams(market_weight=0.5, experience=1000.0)
    shy = t.analyze(fixture, history, params=ramp)
    assert 0.5 < shy["expected"]["home_win"] < blended["expected"]["home_win"]
    market = t.TennisParams(market_weight=1.0)
    assert t.analyze(fixture, history, params=market)["expected"]["home_win"] == pytest.approx(0.5)


def test_without_prices_the_elo_view_decides():
    analysis = validate_analysis(analyze_match(target(), ladder(), 0.6))
    p = probabilities(analysis)
    assert p["1"] > 0.75 and p["1"] + p["2"] == pytest.approx(1)
    assert analysis["components"]["market_home_win"] is None
    assert analysis["components"]["elo_home"] > analysis["components"]["elo_away"]


def test_insights_are_romanian_and_name_the_surface():
    history = ladder()
    analysis = analyze_match(target(), history)
    text = " ".join(analysis["insights"])
    assert "Suprafață: zgură" in text and "pe zgură" in text
    assert "meci în cel mult 3 seturi" in text


# --- sets ------------------------------------------------------------------------------------


@pytest.mark.parametrize("sets", [3, 5])
@pytest.mark.parametrize("spread", [0.0, 0.8])
@pytest.mark.parametrize("p", [0.1, 0.35, 0.5, 0.72, 0.93])
def test_set_distribution_sums_to_one_and_matches_the_win_probability(sets, spread, p):
    q = t.set_probability(p, sets, spread)
    scores = t.set_scores(q, sets, spread)
    need = sets // 2 + 1
    assert len(scores) == 2 * need
    assert sum(scores.values()) == pytest.approx(1)
    assert sum(v for (h, _), v in scores.items() if h == need) == pytest.approx(p, abs=1e-9)
    assert t.match_probability(q, sets, spread) == pytest.approx(p, abs=1e-9)


def test_spread_puts_more_weight_on_straight_sets():
    def straight(spread):
        q = t.set_probability(0.6, 3, spread)
        scores = t.set_scores(q, 3, spread)
        return scores[2, 0] + scores[0, 2]

    assert straight(0.8) > straight(0.4) > straight(0.0)


@pytest.mark.parametrize("sets", [3, 5])
def test_analysis_set_markets_are_consistent(sets):
    league = "ATP - SINGLES: US Open (USA), hard" if sets == 5 else "ATP - SINGLES: Metz, hard"
    analysis = validate_analysis(
        analyze_match(target(league=league, odds={"1": 1.4, "2": 3.1}), ladder())
    )
    p = probabilities(analysis)
    exact = {k: v for k, v in p.items() if k.startswith("sets_")}
    assert sum(exact.values()) == pytest.approx(1)
    assert sum(v for k, v in exact.items() if k[5] > k[7]) == pytest.approx(p["1"], abs=1e-9)
    for key, prob in p.items():
        if key.startswith(("over_", "ah_")):
            won = sum(v for k, v in exact.items() if settle("tennis", key, int(k[5]), int(k[7])))
            assert prob == pytest.approx(won, abs=1e-9), key
    if sets == 5:
        assert {"over_3.5", "over_4.5", "ah_1_-2.5", "ah_2_+2.5"} <= set(p)
    else:
        assert "over_3.5" not in p and "ah_1_-2.5" not in p


def test_best_of_five_favours_the_stronger_player_more():
    q = 0.6
    assert t.match_probability(q, 5) > t.match_probability(q, 3) > q


def test_home_and_away_are_symmetric():
    history = ladder()
    one = t.analyze(target(odds={"1": 1.6, "2": 2.4}), history, params=MODEL)
    swapped = target(home="Weak P.", away="Strong P.", odds={"1": 2.4, "2": 1.6})
    two = t.analyze(swapped, history, params=MODEL)
    a, b = probabilities(one), probabilities(two)
    assert a["1"] == pytest.approx(b["2"], abs=1e-9)
    for h, w in ((2, 0), (2, 1), (1, 2), (0, 2)):
        assert a[f"sets_{h}-{w}"] == pytest.approx(b[f"sets_{w}-{h}"], abs=1e-9)
    assert one["expected"]["games"] == pytest.approx(two["expected"]["games"], abs=1e-6)


def test_live_set_scores_continue_the_pre_match_distribution():
    q = 0.58
    assert t.live_set_scores(q, 3, 0, 0) == pytest.approx(t.set_scores(q, 3))
    after = t.live_set_scores(q, 3, 1, 0)
    assert set(after) == {(2, 0), (2, 1), (1, 2)}
    assert sum(after.values()) == pytest.approx(1)
    assert after[2, 0] == pytest.approx(q)
    assert t.live_set_scores(q, 5, 3, 1) == {(3, 1): 1.0}
    assert sum(t.live_set_scores(q, 5, 1, 2, spread=0.5).values()) == pytest.approx(1)


# --- games -----------------------------------------------------------------------------------


def test_hold_and_tiebreak_are_fair_between_equal_players():
    assert t.hold(0.5) == pytest.approx(0.5)
    assert t.hold(0.64) > t.hold(0.6) > 0.5
    assert t.tiebreak(0.62, 0.62) == pytest.approx(0.5)
    assert t.tiebreak(0.7, 0.6) > 0.5 > t.tiebreak(0.6, 0.7)
    outcomes = t.set_outcomes(0.63, 0.63)
    assert sum(p for _, _, p in outcomes) == pytest.approx(1)
    assert {g for _, g, _ in outcomes} == {6, 7, 8, 9, 10, 12, 13}


@pytest.mark.parametrize("sets", [3, 5])
def test_games_distribution_is_consistent_with_the_win_probability(sets):
    totals, (pa, pb) = t.games_totals(0.7, sets, 0.62)
    assert sum(totals.values()) == pytest.approx(1)
    assert pa > pb
    distribution = t.games_distribution(pa, pb, sets)
    assert sum(p for (won, _), p in distribution.items() if won) == pytest.approx(0.7, abs=1e-4)
    low = 12 if sets == 3 else 18
    assert min(totals) == low and max(totals) == 13 * sets


def test_best_of_five_has_more_games_and_spread_keeps_a_distribution():
    three = t.games_totals(0.6, 3, 0.62)[0]
    five = t.games_totals(0.6, 5, 0.62)[0]
    mean = lambda d: sum(g * p for g, p in d.items())  # noqa: E731
    assert mean(five) > mean(three) + 8
    spread = t.games_totals(0.6, 3, 0.62, spread=0.8)[0]
    assert sum(spread.values()) == pytest.approx(1)
    assert mean(spread) < mean(three)


def test_games_markets_are_never_selectable_and_whole_lines_push():
    odds = {"1": 1.8, "2": 2.0, "games_over_22": 1.9, "games_under_22": 1.9}
    analysis = validate_analysis(t.analyze(target(odds=odds), [], threshold=0.01))
    games = [m for m in analysis["markets"] if m["key"].startswith("games_")]
    assert games and not any(m["selectable"] for m in games)
    assert all(m["group"] == "Total game-uri" for m in games)
    p = probabilities(analysis)
    assert p["games_over_22"] + p["games_under_22"] < 1
    for key in p:
        if key.startswith("games_over_") and not key.endswith("_22"):
            assert p[key] + p["games_under_" + key[11:]] == pytest.approx(1)
    assert analysis["selection"] is None or not analysis["selection"]["key"].startswith("games")


# --- Elo -------------------------------------------------------------------------------------


def test_elo_winner_gains_and_loser_loses():
    book = t.EloBook()
    book.update(game("a", 1, "A P.", "B P.", 2, 1))
    home, away = book.overall["a p."], book.overall["b p."]
    assert home > t.START > away
    assert home - t.START == pytest.approx(t.START - away)
    assert book.surface["a p.", "clay"] > t.START and book.played["a p."] == 1


def test_walkovers_and_level_scores_are_ignored_retirements_are_weighted():
    book = t.EloBook()
    book.update(game("w", 1, "A P.", "B P.", None, None, status="unavailable"))
    book.update(game("x", 1, "A P.", "B P.", 1, 1, finish_type="retired"))
    assert book.overall == {}
    full = t.EloBook(t.TennisParams(retired_k=1.0))
    half = t.EloBook(t.TennisParams(retired_k=0.5))
    none = t.EloBook(t.TennisParams(retired_k=0.0))
    for b in (full, half, none):
        b.update(game("r", 1, "A P.", "B P.", 1, 0, finish_type="retired"))
    assert none.overall == {}
    assert full.overall["a p."] - t.START == pytest.approx(2 * (half.overall["a p."] - t.START))


def test_straight_sets_count_more_with_margin_of_victory():
    plain = t.EloBook(t.TennisParams(mov=0.0))
    bonus = t.EloBook(t.TennisParams(mov=0.5))
    for b in (plain, bonus):
        b.update(game("a", 1, "A P.", "B P.", 2, 0))
    assert bonus.overall["a p."] - t.START == pytest.approx(1.5 * (plain.overall["a p."] - t.START))


def test_surface_rating_and_blend():
    params = t.TennisParams(surface_weight=1.0)
    book = t.EloBook(params)
    for i in range(10):
        book.update(game(f"g{i}", 30 - i, "Clay P.", f"X{i}", 2, 0))
    clay = book.rating("clay p.", "clay", KICKOFF)
    grass = book.rating("clay p.", "grass", KICKOFF)
    # An unseen surface starts from the overall rating.
    assert clay == pytest.approx(book.surface["clay p.", "clay"])
    assert grass == pytest.approx(book.overall["clay p."])


def test_inactivity_shrinks_a_rating_towards_the_start():
    params = t.TennisParams(idle_grace=60.0, idle_half_life=365.0)
    book = t.EloBook(params)
    book.update(game("a", 0, "A P.", "B P.", 2, 0))
    fresh = book.rating("a p.", "clay", KICKOFF + timedelta(days=30)) - t.START
    idle = book.rating("a p.", "clay", KICKOFF + timedelta(days=60 + 365)) - t.START
    assert fresh > 0 and idle == pytest.approx(fresh / 2)
    never = t.EloBook(t.TennisParams(idle_half_life=float("inf")))
    never.update(game("a", 0, "A P.", "B P.", 2, 0))
    assert never.rating("a p.", "clay", KICKOFF + timedelta(days=3000)) > t.START


def test_k_factor_decays_with_matches_played():
    book = t.EloBook()
    assert book.k(0) > book.k(10) > book.k(100) > 0


def test_book_cache_matches_a_fresh_build_and_follows_index_changes():
    index = HistoryIndex(ladder())
    cutoff = KICKOFF - timedelta(days=10)
    cached = t.book_before(index, cutoff)
    fresh = t.EloBook().advance(index.before(cutoff), len(index.before(cutoff)))
    assert cached.overall == pytest.approx(fresh.overall)
    later = t.book_before(index, KICKOFF)
    assert later.position > cached.position and t.book_before(index, cutoff) is cached
    index.extend([game("new", 50, "Strong P.", "Weak P.", 0, 2)])
    rebuilt = t.book_before(index, cutoff)
    assert rebuilt is not cached and rebuilt.overall != cached.overall


# --- anti-leakage and data handling --------------------------------------------------------


def test_results_at_or_after_kickoff_minus_three_hours_never_reach_the_model():
    fixture = target(odds={"1": 1.9, "2": 1.9})
    base = ladder()
    late = [
        game(f"late{i}", 0, "Weak P.", f"Z{i}", 2, 0).model_copy(
            update={"kickoff": KICKOFF - timedelta(hours=3) + timedelta(minutes=i)}
        )
        for i in range(6)
    ]
    clean = t.analyze(fixture, base, params=MODEL)
    leaked = t.analyze(fixture, base + late, params=MODEL)
    assert leaked["markets"] == clean["markets"]
    assert leaked["components"] == clean["components"]
    early = [
        m.model_copy(update={"kickoff": KICKOFF - timedelta(hours=3, minutes=1)}) for m in late
    ]
    seen = t.analyze(fixture, base + early, params=MODEL)
    assert seen["components"]["elo_away"] > clean["components"]["elo_away"]


def test_the_fixture_itself_is_never_part_of_its_history():
    fixture = target()
    finished = fixture.model_copy(update={"status": "finished", "home_goals": 2, "away_goals": 0})
    assert t.analyze(fixture, [finished]) == t.analyze(fixture, [])


def test_retired_rows_in_history_are_shown_but_do_not_break_the_analysis():
    history = ladder() + [game("ret", 2, "Strong P.", "Weak P.", 0, 1, finish_type="retired")]
    analysis = validate_analysis(analyze_match(target(), history))
    assert analysis["h2h"]["played"] == 1
    assert settle("tennis", "1", 0, 1, "retired") is None


def test_best_of_and_women_detection():
    assert t.best_of(target(league="ATP - SINGLES: Wimbledon (United Kingdom), grass")) == 5
    assert t.best_of(target(league="WTA - SINGLES: Wimbledon (United Kingdom), grass")) == 3
    hinted = target(league="ATP - SINGLES: Davis Cup, hard", source="tennis-data.co.uk;best_of=5")
    assert t.best_of(hinted) == 5
    assert t.is_women(target(league="WTA - SINGLES: Wuhan (China), hard"))
    assert t.is_women(target(league="L", source="tennis-data.co.uk;tour=wta"))
    assert not t.is_women(target(league="ATP - SINGLES: Metz, hard"))
    assert t.surface_of("ATP - SINGLES: Paris, hard (indoor)") == "hard"
    assert t.surface_of("Old Event, carpet") == "carpet"


def test_rank_hints_become_an_insight():
    fixture = target(source="tennis-data.co.uk;tour=atp;rank_home=12;rank_away=58")
    analysis = analyze_match(fixture, ladder())
    assert "Clasament: Strong P. #12, Weak P. #58." in analysis["insights"]


# --- through the API, with the captured payloads ---------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )
    settings = Settings(api_key="k", database=tmp_path / "api.db")
    with TestClient(create_app(settings, httpx.MockTransport(Fake()))) as test_client:
        yield test_client


def test_enriched_tennis_analysis_prices_sets_and_games(client):
    assert client.get("/api/matches", params={"day": "2026-09-26", "sport": "tennis"}).is_success
    response = client.post(
        "/api/analyze/KnR6QDo1", params={"sport": "tennis"}, json={"enrich": True}
    )
    assert response.status_code == 200, response.text
    prediction = validate_analysis(response.json()["prediction"])
    by_key = {m["key"]: m for m in prediction["markets"]}
    assert by_key["games_over_22.5"]["odds"] == 2.0
    assert by_key["games_over_22.5"]["selectable"] is False
    assert by_key["sets_2-0"]["odds"] == 4.0
    assert prediction["sample"]["home"] > 10
