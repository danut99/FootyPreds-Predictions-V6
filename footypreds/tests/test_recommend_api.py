"""Recommendations and ticket generator over HTTP: contract, enrichment budget, persistence,
settlement and track record. A synthetic multi-sport FlashScore day is served by MockTransport;
nothing reaches the network."""

import itertools
import json
import math
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import footypreds.provider as provider_module
from footypreds import recommend as rc
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.sports.settle import settle

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
DAY = datetime.now(timezone.utc).date() + timedelta(days=2)
START = datetime.combine(DAY, datetime.min.time(), timezone.utc)
MORNING = START + timedelta(hours=9)
SPORT_IDS = {"1": "football", "2": "tennis", "3": "basketball"}
LEAGUES = {
    "football": ("ENGLAND: Premier League", "England"),
    "basketball": ("USA: NBA", "USA"),
    "tennis": ("ATP - SINGLES: Tokyo (Japan), hard", ""),
}
COUNT = {"football": 12, "basketball": 4, "tennis": 4}
PREFIX = {"football": "f", "basketball": "b", "tennis": "t"}


def names(sport, n):
    if sport == "football":
        return f"Home{n}", f"Away{n}"
    if sport == "basketball":
        return f"BHome{n}", f"BAway{n}"
    return f"Player H{n}", f"Player A{n}"


def prices(sport, n):
    """A 2% margin book around a clear home favourite."""
    if sport == "football":
        home = 0.55 + 0.02 * n
        book = {"1": home, "X": (1 - home) * 0.55, "2": (1 - home) * 0.45}
    else:
        home = 0.6 + 0.05 * n
        book = {"1": home, "2": 1 - home}
    return {key: round(1 / (p * 1.02), 2) for key, p in book.items()}


def kickoff(sport, n):
    # f0 kicks off at 08:00, before the frozen clock (09:00): it must never be picked.
    base = {"football": 8, "basketball": 11, "tennis": 12}[sport]
    return START + timedelta(hours=base + n, minutes=5 * n)


def list_payload(sport):
    league, country = LEAGUES[sport]
    rows = []
    for n in range(COUNT[sport]):
        home, away = names(sport, n)
        match_id = f"{PREFIX[sport]}{n}"
        home_team = {"name": home, "team_id": f"{match_id}h"}
        away_team = {"name": away, "team_id": f"{match_id}a"}
        if match_id == "f1":  # its matches/odds answer is the captured football payload
            home_team["event_participant_id"] = "QsL3TXzh"
            away_team["event_participant_id"] = "CbJBRB54"
        rows.append(
            {
                "match_id": match_id,
                "timestamp": kickoff(sport, n).timestamp(),
                "home_team": home_team,
                "away_team": away_team,
                "match_status": {"is_started": False, "is_finished": False},
                "scores": {"home": None, "away": None},
                "odds": prices(sport, n),
            }
        )
    return [{"name": league, "country_name": country, "matches": rows}]


def history():
    """Ten recent results per side: home sides win, away sides lose."""
    rows = []
    for sport in COUNT:
        league, country = LEAGUES[sport]
        win, loss = {"football": ((2, 0), (0, 1)), "basketball": ((95, 84), (80, 91))}.get(
            sport, ((2, 0), (1, 2))
        )
        for n in range(COUNT[sport]):
            for side, team in enumerate(names(sport, n)):
                for i in range(10):
                    score = win if side == 0 else loss
                    rows.append(
                        Match(
                            id=f"hist-{sport}-{n}-{side}-{i}",
                            kickoff=START - timedelta(days=3 * i + 2),
                            league=league,
                            country=country,
                            home=team,
                            away=f"Rival {sport} {n}-{side}-{i}",
                            status="finished",
                            home_goals=score[0],
                            away_goals=score[1],
                            sport=sport,
                        )
                    )
    return rows


class Fake:
    """Mock FlashScore: the synthetic day for every sport; records (path, params)."""

    def __init__(self):
        self.calls = []

    def __call__(self, request):
        path, params = request.url.path, dict(request.url.params)
        self.calls.append((path, params))
        if path.endswith("list-by-date"):
            return httpx.Response(200, json=list_payload(SPORT_IDS[params.get("sport_id", "1")]))
        if path.endswith("matches/h2h") or path.endswith("matches/standings"):
            return httpx.Response(200, json=[])
        if path.endswith("matches/odds"):
            if params.get("match_id") == "f1":
                payload = json.loads((FIXTURES / "odds_football.json").read_text("utf-8"))
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    def count(self, suffix, prefix=""):
        return sum(
            p.endswith(suffix) and q.get("match_id", "").startswith(prefix) for p, q in self.calls
        )


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    counter = itertools.count(start=10_000, step=1_000)
    monkeypatch.setattr(
        provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
    )


@pytest.fixture
def clock(monkeypatch):
    state = {"now": MORNING}
    monkeypatch.setattr(rc, "utcnow", lambda: state["now"])
    return state


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def client(tmp_path, fake, clock, monkeypatch):
    monkeypatch.setattr(rc, "ENRICH_BUDGET", 3)
    settings = Settings(api_key="k", database=tmp_path / "reco.db")
    with TestClient(create_app(settings, httpx.MockTransport(fake))) as test_client:
        test_client.app.state.store.save_matches(history())
        yield test_client


URL = f"/api/recommendations?day={DAY.isoformat()}"


def check_ticket(ticket, store):
    low, high = ticket["window"]
    assert low <= ticket["total_odds"] <= high
    assert ticket["total_odds"] == pytest.approx(math.prod(leg["odds"] for leg in ticket["legs"]))
    assert ticket["probability"] == pytest.approx(
        math.prod(leg["probability"] for leg in ticket["legs"])
    )
    assert ticket["ev"] == pytest.approx(ticket["probability"] * ticket["total_odds"] - 1)
    assert len({leg["match_id"] for leg in ticket["legs"]}) == len(ticket["legs"])
    assert len(ticket["legs"]) <= ticket["max_legs"]
    for leg in ticket["legs"]:
        match = store.match(leg["match_id"])
        # Never fabricated: the stored real price, a pre-match game, grade A-C.
        assert leg["odds"] == match.odds[leg["key"]]
        assert datetime.fromisoformat(leg["kickoff"]) > MORNING and leg["match_id"] != "f0"
        assert leg["grade"] in ("A", "B", "C") and leg["reason"]
        for key in ("sport", "competition", "home", "away", "label", "probability", "confidence"):
            assert key in leg


def test_recommendations_contract(client, fake):
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["day"] == DAY.isoformat() and body["targets"] == [2, 5, 10, 100]
    assert body["sports"] == ["football", "basketball", "tennis"]
    assert body["disclaimer"] == rc.DISCLAIMER and body["generated_at"]
    assert body["analyzed"] == {"football": 11, "basketball": 4, "tennis": 4}
    tickets = body["tickets"]
    assert [t["target_odds"] for t in tickets] == [2, 5, 10, 100]
    store = client.app.state.store
    for ticket in tickets:
        if ticket["status"] == "unavailable":
            assert ticket["reason"] and ticket["legs"] == []
        else:
            assert ticket["status"] == "pending" and ticket["rationale"]
            check_ticket(ticket, store)
    assert tickets[0]["status"] == tickets[1]["status"] == "pending"
    # Sports are mixed automatically.
    assert len({leg["sport"] for t in tickets for leg in t["legs"]}) >= 2
    singles = body["singles"]
    assert 1 <= len(singles) <= 10
    assert [s["probability"] for s in singles] == sorted(
        (s["probability"] for s in singles), reverse=True
    )
    assert all(s["odds"] >= rc.SINGLE_MIN_ODDS for s in singles)
    assert len({s["match_id"] for s in singles}) == len(singles)
    assert "f0" not in {s["match_id"] for s in singles}


def test_enrichment_budget_and_stored_results(client, fake):
    client.get(URL)
    # Budget 3 per sport: football has 11 upcoming games, the others 4.
    assert fake.count("matches/h2h", "f") == 3
    assert fake.count("matches/h2h", "b") == 3 and fake.count("matches/h2h", "t") == 3
    assert fake.count("matches/odds") == 9
    first = client.get(URL).json()
    calls = len(fake.calls)
    # Stored: no provider call at all while no leg has started.
    again = client.get(URL).json()
    assert len(fake.calls) == calls and again["tickets"] == first["tickets"]
    assert again["generated_at"] == first["generated_at"]
    # Refresh enriches fixtures not enriched yet, within the budget again.
    client.get(URL + "&refresh=true")
    assert fake.count("matches/h2h", "f") == 6
    assert fake.count("matches/h2h", "b") == 4 and fake.count("matches/h2h", "t") == 4
    # The generator only uses what is left of the day's budget: nothing here.
    before = fake.count("matches/h2h")
    body = {"day": DAY.isoformat(), "target_odds": 3}
    assert client.post("/api/tickets/generate", json=body).status_code == 200
    assert fake.count("matches/h2h") == before


def test_extra_targets_are_added_without_replacing_stored_tickets(client):
    first = client.get(URL + "&targets=2,5").json()
    later = client.get(URL + "&targets=5,3").json()
    assert later["targets"] == [5, 3]
    assert later["tickets"][0] == first["tickets"][1]
    assert later["tickets"][1]["target_odds"] == 3


def test_generate_ticket_contract(client):
    body = {"day": DAY.isoformat(), "target_odds": 3}
    response = client.post("/api/tickets/generate", json=body)
    assert response.status_code == 200
    data = response.json()
    ticket, store = data["ticket"], client.app.state.store
    assert ticket["status"] == "pending" and ticket["target_odds"] == 3
    check_ticket(ticket, store)
    assert 0 <= len(data["alternatives"]) <= 2
    used = {leg["match_id"] for leg in ticket["legs"]}
    for other in data["alternatives"]:
        check_ticket(other, store)
        ids = {leg["match_id"] for leg in other["legs"]}
        assert not ids & used
        used |= ids
    assert set(data["analyzed"]) == {"football", "basketball", "tennis"}
    # Excluded matches never appear; a single sport stays in that sport.
    excluded = [leg["match_id"] for leg in ticket["legs"]]
    body |= {"exclude_match_ids": excluded, "sports": ["football"], "max_legs": 2}
    other = client.post("/api/tickets/generate", json=body).json()["ticket"]
    assert not {leg["match_id"] for leg in other["legs"]} & set(excluded)
    assert all(leg["sport"] == "football" for leg in other["legs"])
    assert len(other["legs"]) <= 2
    # Deterministic for the same stored data.
    again = client.post("/api/tickets/generate", json=body).json()["ticket"]
    assert again["legs"] == other["legs"]


def test_impossible_generation_is_explained(client):
    body = {"day": DAY.isoformat(), "target_odds": 1000, "max_legs": 1}
    response = client.post("/api/tickets/generate", json=body)
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert ticket["status"] == "unavailable" and "Cota maximă" in ticket["reason"]
    assert response.json()["alternatives"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"target_odds": 3},
        {"day": "x", "target_odds": 3},
        {"day": DAY.isoformat(), "target_odds": 1.1},
        {"day": DAY.isoformat(), "target_odds": 1001},
        {"day": DAY.isoformat(), "target_odds": 3, "sports": ["golf"]},
        {"day": DAY.isoformat(), "target_odds": 3, "sports": []},
        {"day": DAY.isoformat(), "target_odds": 3, "max_legs": 16},
        # No minimum probability: the optimizer chooses the legs by itself.
        {"day": DAY.isoformat(), "target_odds": 3, "min_probability": 0.6},
    ],
)
def test_generate_rejects_invalid_input(client, fake, body):
    assert client.post("/api/tickets/generate", json=body).status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "query", ["&sports=golf", "&sports=", "&targets=abc", "&targets=1.1", "&targets=5000"]
)
def test_recommendations_reject_invalid_parameters(client, fake, query):
    assert client.get(URL + query).status_code == 422
    assert fake.calls == []


def score_for(sport, key, won=True):
    scores = {
        "football": [(h, a) for h in range(7) for a in range(7)],
        "basketball": [(101, 90), (90, 101)],
        "tennis": [(2, 0), (0, 2), (2, 1), (1, 2)],
    }[sport]
    return next(s for s in scores if settle(sport, key, *s) is won)


def finish(store, legs, won=True):
    rows = []
    for leg in legs:
        h, a = score_for(leg["sport"], leg["key"], won)
        match = store.match(leg["match_id"])
        rows.append(
            match.model_copy(update={"status": "finished", "home_goals": h, "away_goals": a})
        )
    store.save_matches(rows)


def test_settlement_locking_and_track_record(client, clock):
    body = client.get(URL + "&targets=2,5").json()
    two, five = body["tickets"]
    assert two["status"] == five["status"] == "pending"
    store = client.app.state.store
    # Results arrive: the x5 ticket wins, the x2 ticket loses (its first leg).
    finish(store, five["legs"])
    used = {leg["match_id"] for leg in five["legs"]}
    losing = [leg for leg in two["legs"] if leg["match_id"] not in used]
    if losing:
        finish(store, losing[:1], won=False)
    clock["now"] = START + timedelta(days=1, hours=2)
    later = client.get(URL + "&targets=2,5").json()
    won = later["tickets"][1]
    assert won["status"] == "won" and all(leg["status"] == "won" for leg in won["legs"])
    assert won["payout_odds"] == pytest.approx(won["total_odds"])
    assert all(leg["score"] for leg in won["legs"])
    if losing:
        assert later["tickets"][0]["status"] == "lost"
    # A refresh after kickoff can never rewrite the track record.
    refreshed = client.get(URL + "&targets=2,5&refresh=true").json()
    assert [leg["match_id"] for leg in refreshed["tickets"][1]["legs"]] == [
        leg["match_id"] for leg in five["legs"]
    ]
    assert refreshed["tickets"][1]["status"] == "won"
    record = client.get("/api/recommendations/history").json()
    assert record["days"][0]["day"] == DAY.isoformat()
    summary = {row["target"]: row for row in record["summary"]}
    assert summary[5.0]["won"] == 1 and summary[5.0]["tickets"] == 1
    assert summary[5.0]["profit"] == pytest.approx(won["total_odds"] - 1)
    assert summary[5.0]["hit_rate"] == 1.0 and summary[5.0]["roi"] == pytest.approx(
        won["total_odds"] - 1
    )
    if losing:
        assert summary[2.0]["lost"] == 1 and summary[2.0]["profit"] == -1
    assert record["disclaimer"] == rc.DISCLAIMER


def test_history_hides_future_days_and_filters_by_sports(client, clock):
    client.get(URL + "&targets=2")
    clock["now"] = MORNING - timedelta(days=1)
    assert client.get("/api/recommendations/history").json()["days"] == []
    clock["now"] = MORNING
    assert len(client.get("/api/recommendations/history").json()["days"]) == 1
    assert client.get("/api/recommendations/history?sports=tennis").json()["days"] == []
    assert client.get("/api/recommendations/history?sports=golf").status_code == 422
