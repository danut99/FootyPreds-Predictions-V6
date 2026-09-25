"""Multi-sport FlashScore parsing on real captured payloads (tests/fixtures/flashscore/).

Network-free: payloads are served by httpx.MockTransport or parsed directly.
"""

import asyncio
import itertools
import json
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import footypreds.provider as provider_module
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.provider import LIVE_TTL, FlashScore, normalize_matches, period_of
from footypreds.sports.odds import merge_odds, parse_odds
from footypreds.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
# Home/away eventParticipantId of the three captured odds payloads.
PARTICIPANTS = {
    "football": ("QsL3TXzh", "CbJBRB54"),
    "basketball": ("tM1zGJSk", "EZarEcc2"),
    "tennis": ("j1EyxKUg", "bRHqzba6"),
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )


def run_provider(tmp_path, handler, work):
    async def run():
        settings = Settings(api_key="k", database=tmp_path / "p.db")
        provider = FlashScore(settings, Store(settings.database), httpx.MockTransport(handler))
        try:
            return await work(provider)
        finally:
            await provider.client.aclose()

    return asyncio.run(run())


# --- list-by-date ---------------------------------------------------------------------------


def test_tennis_list_tags_sport_keeps_doubles_and_participant_ids():
    matches, rejected = normalize_matches(load("list_tennis.json"), sport="tennis")
    assert rejected == 0 and len(matches) == 30
    assert {m.sport for m in matches} == {"tennis"}
    first = next(m for m in matches if m.id == "KnR6QDo1")
    assert first.league == "ATP - SINGLES: Chengdu (China), hard"
    assert (first.home, first.away) == ("Cerundolo J. M.", "Davidovich Fokina A.")
    assert (first.home_participant_id, first.away_participant_id) == ("j1EyxKUg", "bRHqzba6")
    assert first.odds == {"1": 2.3, "2": 1.57}
    doubles = next(m for m in matches if m.id == "nkZ86rbL")
    assert doubles.home == "Peers J./Venus M." and doubles.away == "Johnson L./Zielinski J."
    assert doubles.home_participant_id == "63u6D4em"
    interrupted = next(m for m in matches if m.id == "zPlWkU9C")
    assert interrupted.status == "live" and interrupted.live["stage"] == "Interrupted"
    assert interrupted.live["period"] == "INT"


def test_basketball_list_tags_sport_and_keeps_empty_odds_lists():
    matches, rejected = normalize_matches(load("list_basketball.json"), sport="basketball")
    assert rejected == 0 and len(matches) == 30
    assert all(m.sport == "basketball" and m.status == "scheduled" for m in matches)
    assert any(m.odds == {} for m in matches)
    # "1": 13.5, "X": null, "2": 1.01 -> no draw price for basketball.
    assert next(m for m in matches if m.id == "KMHepeEM").odds == {"1": 13.5, "2": 1.01}


def test_default_sport_is_football_and_old_rows_stay_valid():
    old = Match.model_validate(
        {
            "id": "x",
            "kickoff": "2026-01-01T12:00:00+00:00",
            "league": "L",
            "home": "A",
            "away": "B",
        }
    )
    assert old.sport == "football" and old.live == {} and old.finish_type == ""


@pytest.mark.parametrize(
    "sport, score, valid",
    [
        ("football", 50, True),
        ("football", 51, False),
        ("basketball", 135, True),
        ("basketball", 401, False),
        ("tennis", 3, True),
        ("tennis", 6, False),
    ],
)
def test_score_bounds_per_sport(sport, score, valid):
    data = dict(
        id="m",
        kickoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
        league="L",
        home="A",
        away="B",
        status="finished",
        home_goals=score,
        away_goals=0,
        sport=sport,
    )
    if valid:
        assert Match(**data).home_goals == score
    else:
        with pytest.raises(ValidationError):
            Match(**data)


def test_unknown_sport_is_rejected():
    with pytest.raises(ValidationError):
        Match(
            id="m",
            kickoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
            league="L",
            home="A",
            away="B",
            sport="curling",
        )


# --- live ------------------------------------------------------------------------------------


def test_live_football_parses_minute_period_and_red_cards():
    matches, rejected = normalize_matches(load("live_football.json"), sport="football")
    assert rejected == 0 and len(matches) == 29
    assert all(m.status == "live" for m in matches)
    half_time = next(m for m in matches if m.id == "dMHjuuk4")
    assert (half_time.home_goals, half_time.away_goals) == (2, 0)
    assert half_time.live == {
        "stage": "Half Time",
        "clock": "Half Time",
        "minute": None,
        "period": "HT",
        "red_cards": {"home": 0, "away": 0},
    }
    early = next(m for m in matches if m.id == "hnSTROFt")
    assert early.live["minute"] == 2 and early.live["period"] == "1H"
    stoppage = next(m for m in matches if m.live["clock"] == "90+1")
    assert stoppage.live["minute"] == 91 and stoppage.live["period"] == "2H"


def test_live_basketball_accepts_points_above_fifty():
    matches, rejected = normalize_matches(load("live_basketball.json"), sport="basketball")
    assert rejected == 0 and len(matches) == 8
    periods = {m.live["period"] for m in matches}
    assert periods == {"Q1", "Q2"}
    assert max(m.home_goals for m in matches) == 35
    assert all("red_cards" not in m.live for m in matches)
    # A 1.0 "price" is not a price.
    assert next(m for m in matches if m.id == "d4hGjFGl").odds == {"1": 9.0}


def test_live_tennis_scores_are_sets_and_periods_are_sets():
    matches, rejected = normalize_matches(load("live_tennis.json"), sport="tennis")
    assert rejected == 0 and len(matches) == 17
    laver = next(m for m in matches if m.id == "Kvc7yWkB")
    assert (laver.home_goals, laver.away_goals) == (1, 1)
    assert laver.live["period"] == "S3"
    assert {m.live["period"] for m in matches} <= {"S1", "S2", "S3", ""}


@pytest.mark.parametrize(
    "sport, stage, code",
    [
        ("football", "1st Half", "1H"),
        ("football", "2nd Half", "2H"),
        ("football", "Half Time", "HT"),
        ("football", "Extra Time", "ET"),
        ("football", "Penalties", "PEN"),
        ("basketball", "3rd Quarter", "Q3"),
        ("basketball", "Overtime", "OT"),
        ("basketball", "Break Time", "BREAK"),
        ("tennis", "Set 5", "S5"),
        ("tennis", "Live", ""),
        ("tennis", None, ""),
    ],
)
def test_period_codes(sport, stage, code):
    assert period_of(sport, stage) == code


def test_live_endpoint_uses_sport_id_short_cache_and_keeps_only_live(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        payload = load("live_basketball.json")
        # One finished game in the feed must not be offered as live.
        payload[0]["matches"][0]["match_status"].update(is_finished=True, is_in_progress=False)
        return httpx.Response(200, json=payload)

    async def work(provider):
        first = await provider.live("basketball")
        second = await provider.live("basketball")
        return first, second

    (live, cached, rejected), (_, cached_again, _) = run_provider(tmp_path, handler, work)
    assert seen == [("/api/flashscore/v2/matches/live", {"sport_id": "3"})]
    assert not cached and cached_again and rejected == 0
    assert len(live) == 7 and all(m.status == "live" and m.live for m in live)
    assert LIVE_TTL <= 60


def test_fixtures_send_the_sport_id(tmp_path):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=load("list_tennis.json"))

    async def work(provider):
        football = await provider.fixtures(date(2026, 9, 25))
        tennis = await provider.fixtures(date(2026, 9, 25), sport="tennis")
        return football, tennis

    (football, _, _), (tennis, _, _) = run_provider(tmp_path, handler, work)
    assert [p["sport_id"] for p in seen] == ["1", "2"]
    assert {m.sport for m in football} == {"football"}
    assert {m.sport for m in tennis} == {"tennis"}


# --- h2h -------------------------------------------------------------------------------------


def h2h_fixture(sport, kickoff):
    return Match(
        id="target",
        kickoff=kickoff,
        league="L",
        home="Cerundolo J. M." if sport == "tennis" else "Perth",
        away="Other" if sport == "tennis" else "Adelaide",
        sport=sport,
    )


def test_tennis_h2h_sets_retirement_walkover_and_surface(tmp_path):
    def handler(request):
        return httpx.Response(200, json=load("h2h_tennis.json"))

    kickoff = datetime(2026, 9, 26, tzinfo=timezone.utc)

    async def work(provider):
        return await provider.head_to_head(h2h_fixture("tennis", kickoff))

    rows, _, rejected = run_provider(tmp_path, handler, work)
    # 80 rows: the walkover has no score and is skipped without counting as an error.
    assert rejected == 0 and len(rows) == 79
    assert all(r.sport == "tennis" and r.status == "finished" for r in rows)
    assert all(max(r.home_goals, r.away_goals) <= 3 for r in rows)
    retired = next(r for r in rows if r.id == "CKf4ry4j")
    assert retired.finish_type == "retired" and (retired.home_goals, retired.away_goals) == (1, 0)
    first = next(r for r in rows if r.id == "jVJBOq9M")
    assert first.league == "Chengdu, hard" and (first.home_goals, first.away_goals) == (2, 0)
    assert next(r for r in rows if r.id == "0GTuFzQh").away_goals == 3  # best of five


def test_basketball_h2h_points_and_overtime(tmp_path):
    def handler(request):
        return httpx.Response(200, json=load("h2h_basketball.json"))

    kickoff = datetime(2026, 9, 26, tzinfo=timezone.utc)

    async def work(provider):
        return await provider.head_to_head(h2h_fixture("basketball", kickoff))

    rows, _, rejected = run_provider(tmp_path, handler, work)
    assert rejected == 0 and len(rows) == 80
    assert all(r.sport == "basketball" for r in rows)
    first = next(r for r in rows if r.id == "8xXArSOO")
    assert (first.home_goals, first.away_goals) == (98, 97)
    overtime = [r for r in rows if r.finish_type == "aet"]
    assert len(overtime) == 2 and {(r.home_goals, r.away_goals) for r in overtime} == {
        (110, 112),
        (80, 81),
    }


def test_h2h_rows_after_the_fixture_are_dropped(tmp_path):
    def handler(request):
        return httpx.Response(200, json=load("h2h_basketball.json"))

    newest = max(row["timestamp"] for row in load("h2h_basketball.json"))
    kickoff = datetime.fromtimestamp(newest, timezone.utc)

    async def work(provider):
        return await provider.head_to_head(h2h_fixture("basketball", kickoff))

    rows, _, _ = run_provider(tmp_path, handler, work)
    assert rows and all(r.kickoff < kickoff for r in rows)


# --- matches/odds ----------------------------------------------------------------------------


def parsed(sport, **kwargs):
    home, away = PARTICIPANTS[sport]
    return parse_odds(load(f"odds_{sport}.json"), sport, home, away, **kwargs)


def test_football_odds_map_to_legacy_and_generic_keys():
    odds = parsed("football")
    best = {k: v["best"] for k, v in odds.items()}
    # England - Spain: 1X2 3.14 / 3.48 / 2.35 (1xBet), bet365 3.1 / 3.4 / 2.3.
    assert (best["1"], best["X"], best["2"]) == (3.14, 3.48, 2.35)
    assert odds["1"] == {"best": 3.14, "avg": pytest.approx(3.12), "books": 2}
    assert (best["1X"], best["X2"], best["12"]) == (1.64, 1.39, 1.34)
    assert (best["over25"], best["under25"]) == (1.84, 2.06)
    assert (best["btts"], best["no_btts"]) == (1.62, 2.2)
    assert (best["dnb_1"], best["dnb_2"]) == (2.2, 1.62)
    assert (best["ah_1_0"], best["ah_2_0"]) == (2.24, 1.71)
    assert (best["ah_1_-0.5"], best["ah_2_+0.5"]) == (3.0, 1.38)
    assert (best["ah_1_+1.5"], best["ah_2_-1.5"]) == (1.21, 4.25)
    assert (best["ht_1"], best["ht_X"], best["ht_2"]) == (3.65, 2.25, 2.88)
    assert best["ht_over05"] == 1.38 and best["1/1"] == 5.0 and best["cs_1-1"] == 6.5
    assert best["over_3"] == 2.3 and best["odd"] == 2.0
    # Legacy spellings only: no duplicate generic key for the same bet.
    assert "over_2.5" not in odds and "home_over_0.5" not in odds
    # Quarter lines are split bets and are never mapped.
    assert not any(k.endswith((".25", ".75")) for k in odds)


def test_basketball_odds_use_full_time_with_overtime_and_signed_handicaps():
    odds = parsed("basketball")
    best = {k: v["best"] for k, v in odds.items()}
    assert (best["1"], best["2"]) == (1.43, 2.92)
    assert odds["1"]["books"] == 2 and odds["2"]["avg"] == pytest.approx(2.9)
    assert "X" not in odds  # regulation-time 3-way is not the winner market
    totals = sorted(float(k.split("_")[1]) for k in odds if k.startswith("over_"))
    assert totals[0] == 182.5 and totals[-1] == 188.5 and 185.5 in totals
    assert odds["over_185.5"] == {"best": 1.87, "avg": pytest.approx(1.835), "books": 2}
    # Home favourite gives points, away underdog receives them.
    assert (best["ah_1_-5.5"], best["ah_2_+5.5"]) == (1.88, 1.92)
    assert not any(k.startswith("ah_1_+") or k.startswith("ah_2_-") for k in odds)
    # Inactive rows (e.g. 178.5) never become prices.
    assert "over_178.5" not in odds and "ah_1_-10.5" not in odds


def test_tennis_odds_winner_set_handicap_set_scores_and_games():
    odds = parsed("tennis")
    best = {k: v["best"] for k, v in odds.items()}
    assert (best["1"], best["2"]) == (2.38, 1.59)
    # Set handicap +/-1.5: the lower +1.5 and higher -1.5 of the two readings.
    assert (best["ah_1_+1.5"], best["ah_2_-1.5"]) == (1.52, 2.39)
    # Single, ambiguous quotes and every game handicap are skipped.
    assert not any(k.startswith("ah_") for k in odds if k not in ("ah_1_+1.5", "ah_2_-1.5"))
    assert (best["sets_2-0"], best["sets_0-2"], best["sets_2-1"]) == (4.0, 2.38, 5.0)
    assert best["games_over_22.5"] == 2.0 and odds["games_over_22.5"]["books"] == 2
    assert not any(k.startswith(("over_", "under_")) for k in odds)
    assert "odd" not in odds  # total games parity cannot be settled from sets
    # Men's Grand Slam singles are best of five: the set handicap cannot be told apart.
    assert "ah_1_+1.5" not in parsed("tennis", sets=5)


def test_participants_are_inferred_when_the_match_has_none():
    for sport in PARTICIPANTS:
        assert parse_odds(load(f"odds_{sport}.json"), sport) == parsed(sport)


def test_swapped_participant_ids_swap_the_sides():
    home, away = PARTICIPANTS["basketball"]
    swapped = parse_odds(load("odds_basketball.json"), "basketball", away, home)
    assert swapped["1"]["best"] == 2.92 and swapped["ah_2_-5.5"]["best"] == 1.88


def test_contradictory_handicap_quotes_are_dropped():
    home, away = PARTICIPANTS["basketball"]
    rows = [
        {"eventParticipantId": home, "value": "1.5", "handicap": {"value": "-5.5"}},
        {"eventParticipantId": away, "value": "1.5", "handicap": {"value": "5.5"}},
    ]
    book = {"name": "b", "odds": [{"bettingType": "ASIAN_HANDICAP"}]}
    book["odds"][0] |= {"bettingScope": "FULL_TIME_OVER_TIME", "odds": rows}
    # 1/1.5 + 1/1.5 = 1.33: not a two-way book, so the side reading is doubtful.
    assert parse_odds([book], "basketball", home, away) == {}


@pytest.mark.parametrize(
    "payload",
    [None, {}, "x", [], [1, "a"], [{"odds": "x"}], [{"odds": [{"odds": [None, 1]}]}]],
)
def test_malformed_odds_payloads_give_no_prices(payload):
    assert parse_odds(payload, "football") == {}


def test_merge_keeps_list_by_date_1x2_and_adds_every_other_price():
    current = {"1": 3.0, "X": 3.3, "2": 2.4, "over25": 1.7}
    merged = merge_odds(current, parsed("football"))
    assert (merged["1"], merged["X"], merged["2"]) == (3.0, 3.3, 2.4)
    assert merged["over25"] == 1.84 and merged["btts"] == 1.62
    assert merge_odds({}, parsed("basketball"))["1"] == 1.43


def test_match_odds_requests_and_caches_one_payload(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=load("odds_tennis.json"))

    match = Match(
        id="KnR6QDo1",
        kickoff=datetime.now(timezone.utc) + timedelta(days=1),
        league="ATP - SINGLES: Chengdu (China), hard",
        home="Cerundolo J. M.",
        away="Davidovich Fokina A.",
        sport="tennis",
        home_participant_id="j1EyxKUg",
        away_participant_id="bRHqzba6",
    )

    async def work(provider):
        return await provider.match_odds(match), await provider.match_odds(match)

    first, second = run_provider(tmp_path, handler, work)
    assert first == second and first["ah_1_+1.5"]["best"] == 1.52
    assert seen == [("/api/flashscore/v2/matches/odds", {"match_id": "KnR6QDo1"})]


# --- live statistics -------------------------------------------------------------------------


def test_live_stats_parse_numbers_and_periods(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=load("stats_live_football.json"))

    async def work(provider):
        return await provider.match_stats("hnSTROFt"), await provider.match_stats("hnSTROFt")

    stats, again = run_provider(tmp_path, handler, work)
    assert stats == again and len(seen) == 1
    assert seen[0] == ("/api/flashscore/v2/matches/match/stats", {"match_id": "hnSTROFt"})
    assert set(stats) == {"match", "1st-half"}
    names = [row["name"] for row in stats["match"]]
    assert len(names) == len(set(names)) and "Expected goals (xG)" in names
    possession = next(row for row in stats["match"] if row["name"] == "Ball possession")
    assert possession["home"] == "29%" and possession["home_value"] == 29.0
    assert possession["away_value"] == 71.0
    passes = next(row for row in stats["match"] if row["name"] == "Passes")
    assert passes["away_value"] == 88.0


@pytest.mark.parametrize("payload", [None, [], "x", {"match": "x"}, {"match": [1, {"n": 2}]}])
def test_malformed_stats_are_empty(payload):
    from footypreds.provider import parse_stats

    assert all(rows == [] for rows in parse_stats(payload).values())


def test_walkover_rows_are_skipped_in_results_and_unavailable_in_fixtures():
    row = {
        "match_id": "wo",
        "timestamp": 1_790_000_000,
        "status": "WALKOVER",
        "home_team": {"name": "A"},
        "away_team": {"name": "B"},
        "scores": {"home": None, "away": None},
    }
    assert normalize_matches([row], results=True, sport="tennis") == ([], 0)
    (fixture_,), rejected = normalize_matches([row], sport="tennis")
    assert rejected == 0 and fixture_.status == "unavailable"
    assert fixture_.finish_type == "walkover"
    scored = row | {"status": "FINISHED", "scores": {"home": "2", "away": "1"}}
    (done,), _ = normalize_matches([scored], results=True, sport="tennis")
    assert done.status == "finished" and done.finish_type == ""
