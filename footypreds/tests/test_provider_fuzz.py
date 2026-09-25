"""Adversarial payloads for the FlashScore normalizer, standings parser and HTTP client."""

import asyncio
import itertools
import json
import math
import random
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import footypreds.provider as provider_module
import footypreds.store as store_module
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.provider import FlashScore, ProviderError, normalize_matches, parse_standings
from footypreds.store import Store
from footypreds.tests.helpers import fixture, h2h_payload

SECRET = "sk-test-DO-NOT-LEAK-7f3a9c"
NOW = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
UNAVAILABLE = ("Postponed", "CANCELLED", "Abandoned", "Awarded", "postponed - TBA", "Cancel.")


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    """The client waits 0.35 s between requests; a fake clock keeps the suite fast."""
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )


# --- random payload generator -------------------------------------------------------------


GOAL_VALUES = [None, "", 0, 1, 3, "0", "2", "10", "-", "abc", -1, 51, 2.5, "", None]
TIMESTAMPS = [
    NOW.timestamp(),
    (NOW - timedelta(days=400)).timestamp(),
    -631152000,  # 1950, negative on every platform
    -1e10,
    1e20,
    float("nan"),
    float("inf"),
    "1700000000",
    "yesterday",
    None,
    0,
]
NAMES = [
    "Arsenal",
    "Steaua București",
    "Ölympiakos ⚽",
    "北京国安",
    "",
    None,
    42,
    "A" * 200,
    "Team 🏆",
]


def random_row(rng, number):
    home = rng.choice(NAMES[:4] + NAMES[6:])
    away = rng.choice(NAMES)
    row = {
        "match_id": rng.choice([f"id{number}", number, f"id{number}"]),
        "timestamp": rng.choice(TIMESTAMPS),
        "home_team": {"name": home},
        "away_team": {"name": away},
    }
    if rng.random() < 0.3:
        row["home_team"]["team_id"] = rng.choice(["h1", "", None])
    if rng.random() < 0.7:
        row["scores"] = rng.choice(
            [
                None,
                {},
                {"home": rng.choice(GOAL_VALUES), "away": rng.choice(GOAL_VALUES)},
                {"home": rng.choice(GOAL_VALUES)},
            ]
        )
    elif rng.random() < 0.5:
        row["home_team"]["score"] = rng.choice(GOAL_VALUES)
        row["away_team"]["score"] = rng.choice(GOAL_VALUES)
    if rng.random() < 0.5:
        row["match_status"] = {
            "is_finished": rng.choice([True, False, None]),
            "is_started": rng.choice([True, False]),
        }
    if rng.random() < 0.3:
        row["status"] = rng.choice(["FINISHED", "SCHEDULED", *UNAVAILABLE, None, 3])
    if rng.random() < 0.3:
        row["odds"] = rng.choice(
            [None, {}, {"1": "1.5", "X": "-", "2": None}, {"1": "nan", "X": "inf", "2": 0.5}]
        )
    if rng.random() < 0.3:
        row["tournament_name"] = rng.choice([None, "", "Cup", "ENGLAND: Premier League"])
    for key in rng.sample(["match_id", "timestamp", "home_team", "away_team"], rng.randint(0, 1)):
        if rng.random() < 0.3:
            del row[key]
    return row


def leaves(rows):
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("matches"), list):
            yield from leaves(row["matches"])
        else:
            yield row


def raw_goals(row, side):
    scores = row.get("scores") or {}
    return scores.get(side, (row.get(f"{side}_team") or {}).get("score"))


@pytest.mark.parametrize("seed", range(30))
@pytest.mark.parametrize("results", [False, True])
def test_normalizer_survives_random_payloads(seed, results):
    rng = random.Random(seed)
    body = [random_row(rng, n) for n in range(rng.randint(0, 40))]
    body += [None, "text", 7, [], [{"match_id": "inner"}]]
    groups = [
        {"name": "ENGLAND: Test", "country_name": "England", "matches": body[: len(body) // 2]},
        {"matches": [{"matches": body[len(body) // 2 :]}]},
    ]
    payload = rng.choice([groups, {"data": groups}, body])
    matches, rejected = normalize_matches(payload, results=results)
    assert isinstance(rejected, int) and rejected >= 0
    assert len(matches) + rejected <= len(
        list(leaves(payload if isinstance(payload, list) else groups))
    )
    assert len({m.id for m in matches}) == len(matches)
    raw = {str(r["match_id"]): r for r in leaves(body) if isinstance(r, dict) and "match_id" in r}
    for match in matches:
        assert isinstance(match, Match)
        assert match.kickoff.tzinfo is not None
        assert all(math.isfinite(v) and v > 1 for v in match.odds.values())
        row = raw[match.id]
        for side, goals in (("home", match.home_goals), ("away", match.away_goals)):
            source = raw_goals(row, side)
            if source in (None, ""):
                assert goals is None, "a missing score was invented"
            else:
                assert goals == int(source)
        if match.status == "finished":
            assert match.home_goals is not None and match.away_goals is not None
        if not results and match.status == "finished":
            assert (row.get("match_status") or {}).get("is_finished")
        raw_status = str(row.get("status", "")).lower()
        if any(s in raw_status for s in ("postpon", "cancel", "abandon", "award")):
            assert match.status == "unavailable"


@pytest.mark.parametrize("payload", [None, 3, "text", 2.5, True, {"nonsense": []}, {"data": {}}])
def test_non_list_roots_raise_provider_error_only(payload):
    with pytest.raises(ProviderError):
        normalize_matches(payload)


@pytest.mark.parametrize(
    "wrapper", [lambda rows: rows, lambda rows: {"data": rows}, lambda rows: {"results": rows}]
)
def test_supported_wrappers_parse_the_same_rows(wrapper):
    rows = h2h_payload(NOW, count=5)
    matches, rejected = normalize_matches(wrapper(rows), results=True)
    assert rejected == 0 and len(matches) == 7


def base_row(**changes):
    row = {
        "match_id": "m1",
        "timestamp": NOW.timestamp(),
        "home_team": {"name": "Home"},
        "away_team": {"name": "Away"},
    }
    return row | changes


def parse_one(row, results=False):
    matches, rejected = normalize_matches([row], results=results)
    assert len(matches) + rejected == 1
    return matches[0] if matches else None


@pytest.mark.parametrize("results", [False, True])
@pytest.mark.parametrize(
    "scores,expected",
    [
        (None, (None, None)),
        ({}, (None, None)),
        ({"home": None, "away": None}, (None, None)),
        ({"home": "", "away": ""}, (None, None)),
        ({"home": 0, "away": 0}, (0, 0)),
        ({"home": "0", "away": "0"}, (0, 0)),
        ({"home": "10", "away": "3"}, (10, 3)),
        ({"home": 2}, (2, None)),
    ],
)
def test_scores_are_parsed_without_inventing_goals(scores, expected, results):
    match = parse_one(base_row(scores=scores), results)
    if match is None:
        # A results feed may reject an empty-string score as incomplete; it must never
        # turn it into a goal.
        assert results and "" in (scores or {}).values()
        return
    assert (match.home_goals, match.away_goals) == expected
    if expected[0] is None or expected[1] is None:
        assert match.status == "scheduled"
    elif results:
        assert match.status == "finished"


@pytest.mark.parametrize(
    "scores", [{"home": -1, "away": 0}, {"home": 51, "away": 0}, {"home": "abc", "away": "1"}]
)
def test_impossible_scores_reject_the_row(scores):
    assert parse_one(base_row(scores=scores), results=True) is None


def test_placeholder_dash_score_never_becomes_a_result():
    assert parse_one(base_row(scores={"home": "-", "away": "-"}), results=True) is None


def test_placeholder_dash_score_keeps_the_fixture_without_goals():
    """The feed uses "-" as a placeholder (see odds in helpers.fixtures_payload); a scheduled
    fixture with a "-" score is still a fixture to predict."""
    match = parse_one(base_row(scores={"home": "-", "away": "-"}), results=False)
    assert match is not None and match.status == "scheduled"
    assert (match.home_goals, match.away_goals) == (None, None)


@pytest.mark.parametrize("status", UNAVAILABLE)
@pytest.mark.parametrize("finished", [True, False])
def test_unavailable_statuses_are_never_scheduled_or_finished(status, finished):
    row = base_row(
        status=status,
        match_status={"is_finished": finished},
        scores={"home": "3", "away": "0"} if finished else None,
    )
    for results in (False, True):
        assert parse_one(row, results).status == "unavailable"


@pytest.mark.parametrize(
    "timestamp", [1e20, -1e20, float("nan"), float("inf"), float("-inf"), "soon", None, [1]]
)
def test_absurd_timestamps_reject_only_that_row(timestamp):
    matches, rejected = normalize_matches(
        [base_row(timestamp=timestamp), base_row(match_id="ok")], results=True
    )
    assert [m.id for m in matches] == ["ok"] and rejected == 1


@pytest.mark.parametrize("timestamp", [-631152000, -2208988800, 0, "1700000000", 1.5e9])
def test_old_and_string_timestamps_are_parsed_as_utc(timestamp):
    match = parse_one(base_row(timestamp=timestamp))
    assert match is not None
    assert match.kickoff == datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=float(timestamp)
    )


@pytest.mark.parametrize(
    "home,away",
    [("Same", "Same"), ("", "Away"), (None, "Away"), ("X" * 121, "Away"), (5, "Away")],
)
def test_invalid_team_names_reject_the_row(home, away):
    assert parse_one(base_row(home_team={"name": home}, away_team={"name": away})) is None


def test_unicode_and_emoji_names_survive():
    match = parse_one(
        base_row(home_team={"name": "Steaua București ⭐"}, away_team={"name": "北京国安"})
    )
    assert (match.home, match.away) == ("Steaua București ⭐", "北京国安")


def test_duplicate_ids_keep_a_single_row():
    rows = [base_row(scores={"home": 1, "away": 0}), base_row(scores={"home": 2, "away": 2})]
    matches, rejected = normalize_matches(rows, results=True)
    assert len(matches) == 1 and rejected == 0


def test_numeric_match_ids_are_strings():
    assert parse_one(base_row(match_id=12345)).id == "12345"


def test_nested_groups_carry_league_and_country():
    payload = [
        {
            "name": "ROMANIA: Superliga",
            "country_name": "Romania",
            "matches": [{"name": "inner stage", "matches": [base_row()]}],
        }
    ]
    (match,), _ = normalize_matches(payload)
    assert match.league == "inner stage" and match.country == "Romania"


def test_group_with_null_country_keeps_its_matches():
    payload = [
        {"name": "WORLD: Friendly International", "country_name": None, "matches": [base_row()]}
    ]
    matches, rejected = normalize_matches(payload)
    assert rejected == 0 and len(matches) == 1
    assert matches[0].country == ""


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"scores": "1-0"}, id="scores-string"),
        pytest.param({"scores": [1, 0]}, id="scores-list"),
        pytest.param({"match_status": "FINISHED"}, id="match_status-string"),
        pytest.param({"home_team": "Home"}, id="home_team-string"),
        pytest.param({"away_team": ["Away"]}, id="away_team-list"),
        pytest.param({"home_team": None}, id="home_team-null"),
        pytest.param({"odds": [1.5, 3.2, 4.0]}, id="odds-list"),
        pytest.param({"odds": "1.5"}, id="odds-string"),
    ],
)
def test_wrong_container_types_reject_the_row_instead_of_crashing(changes):
    rows = [base_row(**changes), base_row(match_id="ok")]
    for results in (False, True):
        matches, rejected = normalize_matches(rows, results=results)
        assert "ok" in {m.id for m in matches}
        assert len(matches) + rejected == 2


def test_live_row_in_fixture_list_is_not_finished():
    row = base_row(
        match_status={"is_started": True, "is_in_progress": True, "is_finished": False},
        scores={"home": 1, "away": 0},
    )
    match = parse_one(row, results=False)
    assert match.status == "live" and match.home_goals == 1


@pytest.mark.parametrize("status", ["LIVE", "1st Half", "Half Time", "in progress"])
def test_results_mode_does_not_turn_a_live_status_into_a_result(status):
    """A results/H2H feed can carry a team's game in progress; its partial score is not a
    result. (match_status flags alone are unreliable in these feeds; see test_api.py
    test_results_parser_zero_is_valid, so only the explicit status text is checked.)"""
    row = base_row(status=status, scores={"home": "1", "away": "0"})
    assert parse_one(row, results=True).status != "finished"


# --- standings ----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_standings_parser_never_raises_on_random_rows(seed):
    rng = random.Random(seed)
    choices = [
        None,
        "header",
        7,
        [],
        {},
        {"name": "Team"},
        {"name": "Team", "goals": "12:3", "points": "30", "wins": 9, "matches_played": "12"},
        {"name": "Team", "goals": None},
        {"name": "Team", "goals": "12-3"},
        {"name": "Team", "goals": "a:b"},
        {"name": None, "goals": "1:1"},
        {"goals": "1:1"},
        {"name": "Team", "points": "x"},
    ]
    payload = [rng.choice(choices) for _ in range(rng.randint(0, 30))]
    table = parse_standings(payload)
    assert len(table) <= len(payload)
    positions = [row["position"] for row in table]
    assert positions == sorted(set(positions)) and all(p >= 1 for p in positions)
    for row in table:
        assert all(isinstance(row[k], int) for k in ("played", "wins", "scored", "points"))
        assert "name" in row


@pytest.mark.parametrize("payload", [None, {}, {"data": []}, "table", 5])
def test_standings_non_list_is_empty(payload):
    assert parse_standings(payload) == []


# --- HTTP client --------------------------------------------------------------------------


def run_with(tmp_path, handler, body, api_key=SECRET, **settings):
    async def main():
        config = Settings(api_key=api_key, database=tmp_path / "p.db", **settings)
        client = FlashScore(config, Store(config.database), httpx.MockTransport(handler))
        try:
            return await body(client)
        finally:
            await client.client.aclose()

    return asyncio.run(main())


def failing(response):
    calls = []

    def handler(request):
        calls.append(request)
        return response() if callable(response) else response

    return handler, calls


ERRORS = [
    (lambda: httpx.Response(401, text=SECRET), 503),
    (lambda: httpx.Response(403, json={"message": SECRET}), 503),
    (lambda: httpx.Response(429, json={"message": f"quota for {SECRET}"}), 429),
    (lambda: httpx.Response(500, text=f"trace {SECRET}"), 502),
    (lambda: httpx.Response(502, text=""), 502),
    (lambda: httpx.Response(301, headers={"location": f"https://evil/{SECRET}"}), 502),
    (lambda: httpx.Response(200, text=f"<html>{SECRET}</html>"), 502),
    (lambda: httpx.Response(200, json={"error": SECRET}), 502),
    (lambda: httpx.Response(200, json={"message": "You are not subscribed"}), 502),
]


@pytest.mark.parametrize("make,status", ERRORS)
def test_http_errors_map_to_provider_error_without_leaking_the_key(tmp_path, make, status):
    handler, calls = failing(make)

    async def body(client):
        with pytest.raises(ProviderError) as info:
            await client.get("matches/list-by-date", {"date": "2026-06-01"})
        return info.value

    error = run_with(tmp_path, handler, body)
    assert error.status == status
    assert SECRET not in str(error) and SECRET not in repr(error.args)
    assert len(calls) == 1, "errors must never be retried automatically"
    assert calls[0].headers["x-rapidapi-key"] == SECRET
    # Nothing was cached: the next call hits the network again.
    run_with(tmp_path, handler, body)
    assert len(calls) == 2


def test_transport_errors_are_wrapped(tmp_path):
    def handler(request):
        raise httpx.ConnectError(f"refused {SECRET}", request=request)

    async def body(client):
        with pytest.raises(ProviderError) as info:
            await client.get("matches/h2h", {"match_id": "x"})
        return info.value

    error = run_with(tmp_path, handler, body)
    assert error.status == 502 and SECRET not in str(error)


def test_missing_key_never_calls_the_network(tmp_path):
    handler, calls = failing(httpx.Response(200, json=[]))

    async def body(client):
        with pytest.raises(ProviderError) as info:
            await client.get("matches/h2h", {"match_id": "x"})
        return info.value

    assert run_with(tmp_path, handler, body, api_key="").status == 503
    assert calls == []


def test_cache_hit_refresh_and_ttl_expiry(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(store_module, "time", types.SimpleNamespace(time=lambda: clock[0]))
    handler, calls = failing(lambda: httpx.Response(200, json=[{"n": len(calls)}]))

    async def body(client):
        params = {"date": "2026-06-01", "sport_id": 1}
        first = await client.get("matches/list-by-date", params)
        second = await client.get("matches/list-by-date", dict(reversed(params.items())))
        refreshed = await client.get("matches/list-by-date", params, refresh=True)
        clock[0] += 899
        still = await client.get("matches/list-by-date", params)
        clock[0] += 2
        expired = await client.get("matches/list-by-date", params)
        custom = await client.get("matches/h2h", {"match_id": "a"}, ttl=10)
        clock[0] += 11
        custom_expired = await client.get("matches/h2h", {"match_id": "a"}, ttl=10)
        return first, second, refreshed, still, expired, custom, custom_expired

    first, second, refreshed, still, expired, custom, custom_expired = run_with(
        tmp_path, handler, body, cache_ttl=900
    )
    assert first[1] is False and second == (first[0], True)
    assert refreshed[1] is False and still[1] is True and still[0] == refreshed[0]
    assert expired[1] is False
    assert custom[1] is False and custom_expired[1] is False
    assert len(calls) == 5


def test_head_to_head_never_returns_rows_at_or_after_kickoff(tmp_path):
    kickoff = NOW + timedelta(days=2)
    rng = random.Random(3)
    rows = []
    for n in range(120):
        offset = rng.choice([-3000, -86400, -1, 0, 1, 7200, 86400 * 30])
        rows.append(
            base_row(
                match_id=f"r{n}",
                timestamp=kickoff.timestamp() + offset,
                status=rng.choice(["FINISHED", "Postponed", "Awarded", "FINISHED"]),
                scores=rng.choice([{"home": "1", "away": "0"}, {"home": "", "away": ""}, None]),
                home_team={"name": f"H{n % 7}"},
            )
        )

    async def body(client):
        return await client.head_to_head(fixture(id="target", kickoff=kickoff))

    history, cached, rejected = run_with(tmp_path, lambda r: httpx.Response(200, json=rows), body)
    assert history and not cached
    assert all(r.kickoff < kickoff and r.status == "finished" for r in history)
    assert all(r.home_goals is not None and r.away_goals is not None for r in history)
    assert json.dumps([r.model_dump(mode="json") for r in history])


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json=[]), ([], 0)),
        (httpx.Response(200, json={"data": []}), ([], 0)),
    ],
)
def test_empty_head_to_head(tmp_path, response, expected):
    async def body(client):
        rows, _, rejected = await client.head_to_head(fixture())
        return rows, rejected

    assert run_with(tmp_path, lambda r: response, body) == expected


@pytest.mark.parametrize("status,raises", [(429, True), (401, True), (500, False), (404, False)])
def test_standings_errors_only_propagate_quota_and_key_problems(tmp_path, status, raises):
    async def body(client):
        return await client.standings(fixture())

    handler = lambda request: httpx.Response(status, json={})  # noqa: E731
    if raises:
        with pytest.raises(ProviderError):
            run_with(tmp_path, handler, body)
    else:
        assert run_with(tmp_path, handler, body) == []
