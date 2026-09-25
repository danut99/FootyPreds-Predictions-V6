"""End-to-end run of the whole multi-sport app through TestClient.

One app, one tmp_path database, and a MockTransport that serves the REAL FlashScore payloads
captured in tests/fixtures/flashscore (tennis and basketball day lists, live lists for the
three sports, H2H, matches/odds and live stats). Football has no captured day list, so a small
Premier League day is built here; its first match answers matches/odds with the captured
football payload. Every feature endpoint is exercised in the order a user would: sports,
boards, recommendations, ticket generator, live, simulator and wallet. Nothing reaches the
network and the clocks the features use are frozen.
"""

import itertools
import json
import math
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

import footypreds.provider as provider_module
from footypreds import recommend, wallet
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.evaluation import sim_datasets
from footypreds.sports import validate_analysis
from footypreds.sports.settle import can_push, is_settleable
from footypreds.tests.test_simulator import football_records
from footypreds.tests.test_simulator_datasets import write_benchmark

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
SPORT_IDS = {"1": "football", "2": "tennis", "3": "basketball"}
# Matches whose matches/odds and matches/h2h answers are the captured payloads.
CAPTURED = {"fb0": "football", "KnR6QDo1": "tennis", "KMHepeEM": "basketball"}
DAY = "2026-09-26"
START = datetime(2026, 9, 26, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
FOOTBALL = [("Arsenal", "Chelsea"), ("Liverpool", "Everton"), ("Leeds", "Burnley")]
FLASHSCORE_PATHS = (
    "list-by-date",
    "matches/live",
    "matches/h2h",
    "matches/odds",
    "matches/standings",
    "match/stats",
)
FEATURE_ROUTES = {
    ("GET", "/api/sports"),
    ("GET", "/api/predictions"),
    ("GET", "/api/recommendations"),
    ("GET", "/api/recommendations/history"),
    ("POST", "/api/tickets/generate"),
    ("GET", "/api/live"),
    ("GET", "/api/live/{match_id}"),
    ("GET", "/api/simulate/datasets"),
    ("POST", "/api/simulate"),
    ("GET", "/api/wallet"),
    ("POST", "/api/wallet/deposit"),
    ("POST", "/api/wallet/bet"),
    ("POST", "/api/wallet/reset"),
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def football_day():
    rows = []
    for n, (home, away) in enumerate(FOOTBALL):
        home_team = {"name": home, "team_id": f"t-{home}"}
        away_team = {"name": away, "team_id": f"t-{away}"}
        if n == 0:
            home_team["event_participant_id"] = "QsL3TXzh"
            away_team["event_participant_id"] = "CbJBRB54"
        rows.append(
            {
                "match_id": f"fb{n}",
                "timestamp": (START + timedelta(hours=12 + 2 * n)).timestamp(),
                "home_team": home_team,
                "away_team": away_team,
                "match_status": {"is_started": False, "is_finished": False},
                "scores": {"home": None, "away": None},
                "odds": {"1": round(1.6 + 0.2 * n, 2), "X": 4.0, "2": 5.5},
            }
        )
    return [{"name": "ENGLAND: Premier League", "country_name": "England", "matches": rows}]


def football_history():
    """Twelve recent results per side: the home sides win, the away sides lose."""
    rows = []
    for n, (home, away) in enumerate(FOOTBALL):
        for i in range(12):
            common = {"league": "Premier League", "country": "England", "status": "finished"}
            rows.append(
                Match(
                    id=f"hist-h{n}-{i}",
                    kickoff=START - timedelta(days=4 * i + 3),
                    home=home,
                    away=f"Rival H{n}-{i}",
                    home_goals=2 + i % 2,
                    away_goals=i % 2,
                    **common,
                )
            )
            rows.append(
                Match(
                    id=f"hist-a{n}-{i}",
                    kickoff=START - timedelta(days=4 * i + 2),
                    home=f"Rival A{n}-{i}",
                    away=away,
                    home_goals=1 + i % 3,
                    away_goals=i % 2,
                    **common,
                )
            )
    return rows


class Fake:
    """Mock FlashScore serving the captured payloads; records (path, params)."""

    def __init__(self):
        self.calls = []

    def __call__(self, request):
        path, params = request.url.path, dict(request.url.params)
        self.calls.append((path, params))
        sport = SPORT_IDS.get(params.get("sport_id", "1"))
        if path.endswith("list-by-date"):
            return httpx.Response(
                200, json=football_day() if sport == "football" else load(f"list_{sport}.json")
            )
        if path.endswith("matches/live"):
            return httpx.Response(200, json=load(f"live_{sport}.json"))
        captured = CAPTURED.get(params.get("match_id", ""))
        if path.endswith("matches/h2h"):
            known = captured in ("tennis", "basketball")
            return httpx.Response(200, json=load(f"h2h_{captured}.json") if known else [])
        if path.endswith("matches/odds"):
            return httpx.Response(200, json=load(f"odds_{captured}.json") if captured else [])
        if path.endswith("matches/standings"):
            return httpx.Response(200, json=[])
        if path.endswith("match/stats"):
            return httpx.Response(200, json=load("stats_live_football.json"))
        raise AssertionError(f"unexpected FlashScore request {path}")

    def count(self, suffix):
        return sum(path.endswith(suffix) for path, _ in self.calls)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )
    monkeypatch.setattr(recommend, "utcnow", lambda: NOW)
    monkeypatch.setattr(wallet, "utcnow", lambda: NOW)
    monkeypatch.setattr(sim_datasets, "_MEMO", {})


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def app(tmp_path, fake):
    settings = Settings(api_key="k", database=tmp_path / "e2e.db")
    application = create_app(settings, httpx.MockTransport(fake))
    application.state.sim_benchmark_dir = write_benchmark(tmp_path / "bench", football_records())
    application.state.sim_cache_dir = tmp_path / "sim-cache"
    application.state.sim_workers = 1
    application.state.store.save_matches(football_history())
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def assert_leg(leg):
    assert leg["sport"] in ("football", "basketball", "tennis")
    assert leg["odds"] > 1 and 0 < leg["probability"] < 1
    # Grade D only on a fully priced market (probability anchored to the fair price).
    assert leg["grade"] in ("A", "B", "C") or leg["margin"] is not None
    assert is_settleable(leg["sport"], leg["key"]) and not can_push(leg["sport"], leg["key"])
    assert recommend.LEG_ODDS[0] <= leg["odds"] <= recommend.LEG_ODDS[1]
    assert datetime.fromisoformat(leg["kickoff"]) > NOW
    assert isinstance(leg["reason"], str) and leg["reason"]


def assert_ticket(ticket):
    if ticket["status"] == "unavailable":
        assert ticket["legs"] == [] and ticket["reason"]
        return
    assert ticket["status"] == "pending"
    low, high = ticket["window"]
    assert low - 1e-9 <= ticket["total_odds"] <= high + 1e-9
    assert math.isclose(ticket["total_odds"], math.prod(leg["odds"] for leg in ticket["legs"]))
    assert math.isclose(
        ticket["probability"], math.prod(leg["probability"] for leg in ticket["legs"])
    )
    assert len({leg["match_id"] for leg in ticket["legs"]}) == len(ticket["legs"])
    assert len(ticket["legs"]) <= ticket["max_legs"]
    for leg in ticket["legs"]:
        assert_leg(leg)


def test_every_feature_router_is_wired_before_the_static_mounts(app):
    routes = list(app.routes)
    first_mount = next(i for i, r in enumerate(routes) if isinstance(r, Mount))
    wired = {
        (method, route.path)
        for route in routes[:first_mount]
        for method in getattr(route, "methods", set()) or ()
    }
    assert FEATURE_ROUTES <= wired
    assert not any(
        getattr(route, "path", "").startswith("/api") for route in routes[first_mount:]
    ), "an API route sits behind the static mount at '/' and would never be reached"


def test_full_day_across_every_feature(client, app, fake):
    store = app.state.store

    # 1. Sports registry.
    sports = client.get("/api/sports").json()["sports"]
    assert [s["key"] for s in sports] == ["football", "basketball", "tennis"]

    # 2. Board of every sport, from the captured day lists.
    for sport, total in (("football", 3), ("basketball", 30), ("tennis", 30)):
        board = client.get(f"/api/predictions?day={DAY}&sport={sport}&limit=100")
        assert board.status_code == 200, board.text
        body = board.json()
        assert body["sport"] == sport and body["total"] == total
        for item in body["items"]:
            assert item["sport"] == sport and item["main"]
            assert not can_push(sport, item["tip"]["key"])
    football_board = client.get(f"/api/predictions?day={DAY}").json()
    assert {i["grade"] for i in football_board["items"]} == {"A"}

    # 3. Full analysis of each captured match: odds merged into the stored match.
    for match_id, sport in CAPTURED.items():
        response = client.post(f"/api/analyze/{match_id}?sport={sport}", json={"enrich": True})
        assert response.status_code == 200, response.text
        validate_analysis(response.json()["prediction"])
        assert len(store.match(match_id).odds) > 3, match_id
    assert store.match("fb0").odds["1"] == 1.6  # the date-list 1X2 price is never overwritten

    # 4. Daily AI recommendations: x2, x5, x10, x100 across the three sports.
    before = len(fake.calls)
    data = client.get(f"/api/recommendations?day={DAY}").json()
    assert data["sports"] == ["football", "basketball", "tennis"]
    assert [t["target"] for t in data["tickets"]] == [2, 5, 10, 100]
    assert set(data["analyzed"]) == {"football", "basketball", "tennis"}
    assert data["disclaimer"] == recommend.DISCLAIMER
    assert all(count <= recommend.ENRICH_BUDGET for count in data["enriched"].values())
    for ticket in data["tickets"]:
        assert_ticket(ticket)
    assert data["tickets"][0]["status"] == "pending"
    assert data["singles"]
    for leg in data["singles"]:
        assert_leg(leg)
    probabilities = [leg["probability"] for leg in data["singles"]]
    assert probabilities == sorted(probabilities, reverse=True)
    assert {leg["sport"] for leg in data["singles"]} >= {"football", "tennis"}
    assert len(fake.calls) > before
    # The stored set is served again without any provider call.
    before = len(fake.calls)
    again = client.get(f"/api/recommendations?day={DAY}").json()
    assert again["tickets"] == data["tickets"] and len(fake.calls) == before
    history = client.get("/api/recommendations/history?days=30").json()
    assert {"days", "summary", "note", "disclaimer"} <= set(history)

    # 5. One-click ticket: only the target odds, never a minimum probability.
    generated = client.post("/api/tickets/generate", json={"day": DAY, "target_odds": 3})
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert_ticket(body["ticket"])
    assert body["ticket"]["status"] == "pending"
    used = {leg["match_id"] for leg in body["ticket"]["legs"]}
    for alternative in body["alternatives"]:
        assert_ticket(alternative)
        assert used.isdisjoint(leg["match_id"] for leg in alternative["legs"])
    excluded = client.post(
        "/api/tickets/generate",
        json={"day": DAY, "target_odds": 3, "exclude_match_ids": sorted(used)},
    ).json()
    assert used.isdisjoint(leg["match_id"] for leg in excluded["ticket"]["legs"])
    refused = client.post(
        "/api/tickets/generate", json={"day": DAY, "target_odds": 3, "min_probability": 0.6}
    )
    assert refused.status_code == 422

    # 6. Live: what can be bet now, for every sport.
    for sport in ("football", "basketball", "tennis"):
        live = client.get(f"/api/live?sport={sport}")
        assert live.status_code == 200, live.text
        body = live.json()
        assert body["sport"] == sport and body["count"] == len(body["matches"]) > 0
        for item in body["matches"]:
            assert item["match"]["status"] == "live" and item["match"]["sport"] == sport
            assert len(item["suggestions"]) <= 3
            for market in item["markets"]:
                assert market["odds"] is None and 0 <= market["probability"] <= 1
                if market["selectable"]:
                    assert is_settleable(sport, market["key"])
    first = client.get("/api/live?sport=football").json()["matches"][0]["match"]["id"]
    detail = client.get(f"/api/live/{first}?sport=football")
    assert detail.status_code == 200 and detail.json()["stats"]
    assert client.get("/api/live/fb0?sport=football").status_code == 404

    # 7. Blind bankroll simulation on the benchmark written for this test.
    datasets = {d["id"]: d for d in client.get("/api/simulate/datasets").json()["datasets"]}
    assert list(datasets) == list(sim_datasets.DATASET_IDS)
    assert datasets["football"]["available"] and not datasets["tennis"]["available"]
    assert "tennis_eval --download" in datasets["tennis"]["hint"]
    assert "Errno" not in json.dumps(datasets)
    simulation = client.post(
        "/api/simulate",
        json={
            "dataset": "football",
            "bankroll": 1000,
            "mode": "ticket",
            "target_odds": 2,
            "staking": "flat",
            "stake": 10,
            "start": "2024-08-03",
            "end": "2025-05-03",
        },
    )
    assert simulation.status_code == 200, simulation.text
    run = simulation.json()
    assert run["initial"] == 1000 and run["bets"] > 0
    assert run["rules"]["source"] == "recommend"
    assert run["won"] + run["lost"] + run["void"] == run["bets"]
    assert math.isclose(run["final"], run["history"][-1]["bankroll"], abs_tol=0.01)
    missing = client.post("/api/simulate", json={"dataset": "tennis", "bankroll": 100})
    assert missing.status_code == 404 and "Errno" not in missing.text

    # 8. Virtual wallet: deposit, bet a single and the AI x2 ticket, settle, reset.
    assert client.get("/api/wallet").json()["balance"] == 0
    assert client.post("/api/wallet/deposit", json={"amount": 100}).json()["balance"] == 100
    single = data["singles"][0]
    placed = client.post(
        "/api/wallet/bet",
        json={"stake": 10, "legs": [{"match_id": single["match_id"], "key": single["key"]}]},
    )
    assert placed.status_code == 200, placed.text
    assert placed.json()["balance"] == 90 and placed.json()["staked_open"] == 10
    ai = client.post("/api/wallet/bet", json={"stake": 20, "day": DAY, "target": 2})
    assert ai.status_code == 200, ai.text
    ai_bet = next(b for b in ai.json()["bets"] if b["source"] == "ai")
    assert [(leg["match_id"], leg["key"]) for leg in ai_bet["legs"]] == [
        (leg["match_id"], leg["key"]) for leg in data["tickets"][0]["legs"]
    ]
    too_much = client.post(
        "/api/wallet/bet",
        json={"stake": 1000, "legs": [{"match_id": single["match_id"], "key": single["key"]}]},
    )
    assert too_much.status_code == 400
    # Every match finishes 0-0 (sets 2-0 in tennis would also do); the wallet settles itself.
    finished = []
    for leg in [single, *ai_bet["legs"]]:
        match = store.match(leg["match_id"])
        score = (2, 0) if match.sport == "tennis" else (0, 0)
        if match.sport == "basketball":
            score = (80, 70)
        finished.append(
            match.model_copy(
                update={"status": "finished", "home_goals": score[0], "away_goals": score[1]}
            )
        )
    store.save_matches(finished)
    settled = client.get("/api/wallet").json()
    assert settled["open"] == 0 and settled["won"] + settled["lost"] + settled["void"] == 2
    assert math.isclose(
        settled["balance"], 70 + sum(b["payout"] or 0 for b in settled["bets"]), abs_tol=0.01
    )
    reset = client.post("/api/wallet/reset").json()
    assert reset["balance"] == 0 and reset["bets"] == []

    # Every request went to a known FlashScore endpoint through the mock transport.
    assert all(path.endswith(FLASHSCORE_PATHS) for path, _ in fake.calls)
    assert fake.count("matches/odds") >= 3


@pytest.mark.parametrize(
    "url",
    [
        "/api/predictions?day=2026-09-26&sport=golf",
        "/api/recommendations?day=2026-09-26&sports=golf",
        "/api/recommendations?day=2026-09-26&targets=0.5",
        "/api/live?sport=golf",
    ],
)
def test_bad_query_values_are_422_without_provider_calls(client, fake, url):
    assert client.get(url).status_code == 422
    assert fake.calls == []
