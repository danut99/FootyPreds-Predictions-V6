"""Live HTTP API (footypreds/live_api.py): real captured payloads, caching, errors."""

import itertools
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import footypreds.live_api as live_api
import footypreds.provider as provider_module
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
SPORT_FILES = {"1": "football", "2": "tennis", "3": "basketball"}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Fake:
    """Mock FlashScore: live lists and match stats from the captured files."""

    def __init__(self):
        self.calls = []
        self.stats_status = 200

    def __call__(self, request):
        path, params = request.url.path, dict(request.url.params)
        self.calls.append((path, params))
        if path.endswith("matches/live"):
            return httpx.Response(200, json=load(f"live_{SPORT_FILES[params['sport_id']]}.json"))
        if path.endswith("matches/match/stats"):
            if self.stats_status != 200:
                return httpx.Response(self.stats_status, json={})
            return httpx.Response(200, json=load("stats_live_football.json"))
        raise AssertionError(f"unexpected FlashScore call {path}")

    def count(self, suffix):
        return sum(p.endswith(suffix) for p, _ in self.calls)


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )


@pytest.fixture
def now(monkeypatch):
    """The live_api clock, moved by tests."""
    state = {"t": 1000.0}
    monkeypatch.setattr(live_api, "clock", lambda: state["t"])
    return state


@pytest.fixture
def fake():
    return Fake()


def make_client(tmp_path, fake, key="k"):
    settings = Settings(api_key=key, database=tmp_path / "live.db")
    return TestClient(create_app(settings, httpx.MockTransport(fake)))


@pytest.fixture
def client(tmp_path, fake):
    with make_client(tmp_path, fake) as test_client:
        yield test_client


@pytest.mark.parametrize("sport,count", [("football", 29), ("basketball", 8), ("tennis", 17)])
def test_live_list_per_sport(client, fake, now, sport, count):
    body = client.get(f"/api/live?sport={sport}").json()
    assert body["sport"] == sport and body["cached"] is False
    assert body["count"] == count == len(body["matches"])
    assert body["updated_at"] == body["updated"]
    assert "dinainte de meci" in body["odds_note"] and "18+" in body["disclaimer"]
    for item in body["matches"]:
        assert item["match"]["sport"] == sport and item["match"]["status"] == "live"
        assert item["competition_id"] and item["summary"]
        assert all(m["odds"] is None for m in item["markets"])
        assert len(item["suggestions"]) <= 3
    assert fake.count("matches/live") == 1


def test_football_list_is_sorted_by_board_priority_and_parses_live_info(client, now):
    items = client.get("/api/live").json()["matches"]
    by_id = {i["match"]["id"]: i for i in items}
    late = by_id["jFX2883N"]
    assert late["minute"] == 91 and late["clock"] == "90+1" and late["period"] == "2H"
    assert late["score"] == {"home": 1, "away": 2}
    assert late["probabilities"]["2"] > 0.85
    assert by_id["dMHjuuk4"]["period"] == "HT"


def test_repeated_requests_inside_the_ttl_never_reach_the_provider(client, fake, now):
    first = client.get("/api/live?sport=tennis").json()
    for _ in range(3):
        again = client.get("/api/live?sport=tennis").json()
        assert again["cached"] is True
        assert again["matches"] == first["matches"]
    assert fake.count("matches/live") == 1
    # Another sport is a separate cache entry.
    client.get("/api/live?sport=basketball")
    assert fake.count("matches/live") == 2


def test_the_cache_expires_and_refresh_bypasses_it(client, fake, now):
    client.get("/api/live")
    now["t"] += live_api.RESPONSE_TTL + 1
    expired = client.get("/api/live").json()
    assert expired["cached"] is False
    refreshed = client.get("/api/live?refresh=true").json()
    assert refreshed["cached"] is False
    assert fake.count("matches/live") == 2  # refresh=true always asks FlashScore


def test_unknown_sport_is_a_422_without_provider_calls(client, fake):
    for url in ("/api/live?sport=golf", "/api/live/abc?sport=FOOTBALL"):
        assert client.get(url).status_code == 422
    assert client.get("/api/live/bad$id").status_code in (404, 422)
    assert fake.calls == []


def test_live_detail_with_stats_and_stats_cache(client, fake, now):
    body = client.get("/api/live/rm79HbRr?sport=football").json()
    assert body["match"]["id"] == "rm79HbRr" and body["minute"] == 76
    assert "match" in body["stats"] and "1st-half" in body["stats"]
    xg = next(r for r in body["stats"]["match"] if r["name"] == "Expected goals (xG)")
    assert xg["home_value"] == 0
    assert body["markets"] and body["suggestions"]
    assert fake.count("matches/match/stats") == 1
    client.get("/api/live/rm79HbRr?sport=football")
    assert fake.count("matches/match/stats") == 1
    now["t"] += live_api.STATS_TTL + 1
    client.get("/api/live/rm79HbRr?sport=football&refresh=true")
    assert fake.count("matches/match/stats") == 2


def test_live_detail_for_tennis_and_basketball(client, fake, now):
    tennis = client.get("/api/live/Kvc7yWkB?sport=tennis").json()
    assert tennis["period"] == "S3" and tennis["pre_match"]["expected"]["best_of"] == 3
    basketball = client.get("/api/live/AH16qYjm?sport=basketball").json()
    assert basketball["score"] == {"home": 35, "away": 27}
    assert basketball["probabilities"]["1"] > 0.8


def test_live_detail_404_when_not_live(client, fake, now):
    response = client.get("/api/live/nothere?sport=football")
    assert response.status_code == 404
    assert "live" in response.json()["detail"]
    assert fake.count("matches/match/stats") == 0
    # The game exists, but in another sport.
    assert client.get("/api/live/rm79HbRr?sport=tennis").status_code == 404


def test_stats_failure_degrades_to_a_note(client, fake, now):
    fake.stats_status = 500
    body = client.get("/api/live/rm79HbRr?sport=football").json()
    assert body["stats"] == {}
    assert body["notes"][0].startswith("Statisticile live")
    assert body["markets"]


def test_missing_key_is_a_503(tmp_path, fake):
    with make_client(tmp_path, fake, key="") as test_client:
        response = test_client.get("/api/live")
    assert response.status_code == 503
    assert fake.calls == []


def test_stored_prematch_analysis_is_used_without_the_live_score(client, fake, now):
    store = client.app.state.store
    kickoff = datetime.now(timezone.utc) - timedelta(minutes=40)
    stored = Match(
        id="AH16qYjm",
        kickoff=kickoff,
        league="TEST: League",
        home="Home Team",
        away="Away Team",
        sport="basketball",
        odds={"1": 1.36, "2": 2.8, "over_170.5": 1.9, "under_170.5": 1.9},
    )
    store.save_matches([stored])
    seen = []
    original = client.app.state.cache.get

    def spy(match, threshold=0.85):
        seen.append(match)
        return original(match, threshold)

    client.app.state.cache.get = spy
    item = next(
        i
        for i in client.get("/api/live?sport=basketball").json()["matches"]
        if i["match"]["id"] == "AH16qYjm"
    )
    assert item["pre_match"]["source"] == "analysis"
    assert item["pre_match"]["grade"] in ("A", "B", "C", "D")
    # The analysis saw the stored pre-match fixture, never the live score.
    assert [m.id for m in seen] == ["AH16qYjm"]
    assert seen[0].status == "scheduled" and seen[0].home_goals is None
    # Stored prices (e.g. totals from enrichment) are merged into the live match.
    assert item["match"]["odds"]["over_170.5"] == 1.9
    total = item["pre_match"]["expected"]["total"]
    assert 160 < total < 180


def test_analysis_budget_limits_cpu_work(client, fake, now, monkeypatch):
    monkeypatch.setattr(live_api, "ANALYSIS_BUDGET", 0)
    calls = []
    client.app.state.cache.get = lambda match, threshold=0.85: calls.append(match)
    body = client.get("/api/live").json()
    assert body["count"] == 29 and calls == []


def test_one_broken_game_does_not_break_the_list(client, fake, now, monkeypatch):
    original = live_api.live_item

    def flaky(match, analysis=None, stats=None):
        if match.id == "rm79HbRr":
            raise ValueError("boom")
        return original(match, analysis, stats)

    monkeypatch.setattr(live_api, "live_item", flaky)
    body = client.get("/api/live").json()
    assert body["count"] == 28
    assert "rm79HbRr" not in {i["match"]["id"] for i in body["matches"]}
