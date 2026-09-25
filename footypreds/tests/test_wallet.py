"""Virtual wallet (paper trading): deposits, bets on current legs, automatic settlement."""

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import wallet as wl
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.store import Store
from footypreds.tests.helpers import KICKOFF, fixture, league_history

ODDS = {"1": 1.4, "X": 4.5, "2": 8.0, "over25": 1.7, "under25": 2.1}
NOW = KICKOFF - timedelta(days=1)


def offline(request):
    return httpx.Response(500, json={"message": "offline"})


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(wl, "utcnow", lambda: state["now"])
    return state


@pytest.fixture
def client(tmp_path, clock):
    settings = Settings(api_key="", database=tmp_path / "wallet.db")
    app = create_app(settings, httpx.MockTransport(offline))
    store = app.state.store
    store.save_matches(league_history(days=300))
    store.save_matches(
        [
            fixture(odds=ODDS),
            fixture(id="second", home="Mid", away="Other", odds=ODDS),
            fixture(id="late", home="Other", away="Weak", kickoff=KICKOFF - timedelta(days=3)),
        ]
    )
    with TestClient(app) as test_client:
        yield test_client


def finish(client, match_id, home_goals, away_goals, status="finished"):
    store = client.app.state.store
    match = store.match(match_id)
    store.save_matches(
        [
            match.model_copy(
                update={"status": status, "home_goals": home_goals, "away_goals": away_goals}
            )
        ]
    )


def test_empty_wallet_says_money_is_virtual(client):
    data = client.get("/api/wallet").json()
    assert data["balance"] == 0 and data["bets"] == [] and data["history"] == []
    assert "bani fictivi" in data["notice"] and "18+" in data["notice"]
    for key in ("balance", "deposited", "staked_open", "profit", "bets", "history"):
        assert key in data


def test_deposit_bet_and_winning_settlement(client):
    assert client.post("/api/wallet/deposit", json={"amount": 100}).json()["balance"] == 100
    response = client.post(
        "/api/wallet/bet", json={"stake": 10, "legs": [{"match_id": "fixture", "key": "1"}]}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["balance"] == 90 and data["staked_open"] == 10 and data["open"] == 1
    bet = data["bets"][0]
    assert bet["status"] == "pending" and bet["total_odds"] == 1.4 and bet["source"] == "custom"
    assert bet["legs"][0]["odds"] == 1.4 and bet["legs"][0]["home"] == "Strong"
    # A later price change never alters the locked price.
    store = client.app.state.store
    store.save_matches([store.match("fixture").model_copy(update={"odds": ODDS | {"1": 1.2}})])
    finish(client, "fixture", 2, 0)
    data = client.get("/api/wallet").json()
    bet = data["bets"][0]
    assert bet["status"] == "won" and bet["payout"] == 14 and bet["legs"][0]["score"] == "2-0"
    assert data["balance"] == 104 and data["profit"] == 4 and data["deposited"] == 100
    assert [h["type"] for h in data["history"]] == ["payout", "bet", "deposit"]
    assert [h["balance"] for h in data["history"]] == [104, 90, 100]
    # Settled once: reading again pays nothing more.
    assert client.get("/api/wallet").json()["balance"] == 104


def test_lost_and_void_bets(client):
    client.post("/api/wallet/deposit", json={"amount": 50})
    client.post(
        "/api/wallet/bet", json={"stake": 10, "legs": [{"match_id": "fixture", "key": "over25"}]}
    )
    client.post("/api/wallet/bet", json={"stake": 5, "legs": [{"match_id": "second", "key": "1"}]})
    finish(client, "fixture", 1, 0)
    finish(client, "second", None, None, status="unavailable")
    data = client.get("/api/wallet").json()
    statuses = {b["legs"][0]["match_id"]: (b["status"], b["payout"]) for b in data["bets"]}
    assert statuses == {"fixture": ("lost", 0.0), "second": ("void", 5)}
    assert data["balance"] == 40 and data["profit"] == -10 and data["void"] == 1


def test_ticket_with_two_legs_pays_the_product(client):
    client.post("/api/wallet/deposit", json={"amount": 20})
    legs = [{"match_id": "fixture", "key": "1"}, {"match_id": "second", "key": "over25"}]
    data = client.post("/api/wallet/bet", json={"stake": 10, "legs": legs}).json()
    assert data["bets"][0]["total_odds"] == pytest.approx(1.4 * 1.7)
    assert data["bets"][0]["label"] == "Bilet 2 selecții"
    finish(client, "fixture", 3, 1)
    assert client.get("/api/wallet").json()["bets"][0]["status"] == "pending"
    finish(client, "second", 2, 2)
    data = client.get("/api/wallet").json()
    assert data["bets"][0]["status"] == "won" and data["balance"] == pytest.approx(33.8)


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"stake": 500, "legs": [{"match_id": "fixture", "key": "1"}]}, 400, "Sold insuficient"),
        ({"stake": 5, "legs": [{"match_id": "nope", "key": "1"}]}, 404, "nu există"),
        ({"stake": 5, "legs": [{"match_id": "fixture", "key": "btts"}]}, 400, "nu mai poate"),
        ({"stake": 5, "legs": [{"match_id": "late", "key": "1"}]}, 400, "nu mai poate"),
        (
            {
                "stake": 5,
                "legs": [
                    {"match_id": "fixture", "key": "1"},
                    {"match_id": "fixture", "key": "over25"},
                ],
            },
            400,
            "o selecție pe meci",
        ),
        ({"stake": 0, "legs": [{"match_id": "fixture", "key": "1"}]}, 422, "Miza"),
        ({"stake": 5}, 422, "Alege selecțiile"),
        ({"stake": 5, "legs": [{"match_id": "fixture", "key": "1"}] * 21}, 422, "între 1 și 20"),
    ],
)
def test_invalid_bets_are_refused_without_touching_the_balance(client, body, status, text):
    client.post("/api/wallet/deposit", json={"amount": 100})
    response = client.post("/api/wallet/bet", json=body)
    assert response.status_code == status, response.text
    assert text in response.json()["detail"]
    assert client.get("/api/wallet").json()["balance"] == 100


def test_bet_is_refused_once_the_game_started(client, clock):
    client.post("/api/wallet/deposit", json={"amount": 100})
    clock["now"] = KICKOFF + timedelta(minutes=1)
    response = client.post(
        "/api/wallet/bet", json={"stake": 5, "legs": [{"match_id": "fixture", "key": "1"}]}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("amount", [0, -5, 2_000_000])
def test_invalid_deposit(client, amount):
    response = client.post("/api/wallet/deposit", json={"amount": amount})
    assert response.status_code == 422 and "Suma" in response.json()["detail"]


def test_reset_empties_the_wallet(client):
    client.post("/api/wallet/deposit", json={"amount": 100})
    client.post("/api/wallet/bet", json={"stake": 5, "legs": [{"match_id": "fixture", "key": "1"}]})
    data = client.post("/api/wallet/reset").json()
    assert data["balance"] == 0 and data["bets"] == [] and data["deposited"] == 0
    assert [h["type"] for h in data["history"]] == ["reset"]


def test_bet_on_the_ai_ticket_of_the_day(client, monkeypatch):
    from footypreds import recommend

    calls = []

    async def fake(state, day, sports, targets, refresh=False):
        calls.append((day.isoformat(), sports, targets))
        legs = [{"match_id": "fixture", "key": "1"}, {"match_id": "second", "key": "over25"}]
        return {"tickets": [{"status": "pending", "legs": legs, "reason": None}]}

    monkeypatch.setattr(recommend, "recommendations", fake)
    client.post("/api/wallet/deposit", json={"amount": 100})
    response = client.post("/api/wallet/bet", json={"stake": 10, "day": "2026-06-01", "target": 2})
    assert response.status_code == 200, response.text
    bet = response.json()["bets"][0]
    assert bet["source"] == "ai" and bet["label"] == "Bilet AI cota 2 (2026-06-01)"
    assert [leg["key"] for leg in bet["legs"]] == ["1", "over25"]
    assert calls == [("2026-06-01", ["football", "basketball", "tennis"], [2.0])]


def test_ai_ticket_unavailable_returns_the_reason(client, monkeypatch):
    from footypreds import recommend

    async def fake(state, day, sports, targets, refresh=False):
        return {
            "tickets": [{"status": "unavailable", "legs": [], "reason": "Prea puține meciuri."}]
        }

    monkeypatch.setattr(recommend, "recommendations", fake)
    client.post("/api/wallet/deposit", json={"amount": 100})
    response = client.post("/api/wallet/bet", json={"stake": 10, "day": "2026-06-01", "target": 5})
    assert response.status_code == 400 and response.json()["detail"] == "Prea puține meciuri."
    assert (
        client.post(
            "/api/wallet/bet", json={"stake": 10, "day": "2026-06-01", "target": 1.01}
        ).status_code
        == 422
    )


def test_wallet_functions_on_a_bare_store(tmp_path):
    store = Store(tmp_path / "bare.db")
    wl.ensure_tables(store)
    wl.deposit(store, 30, NOW)
    leg = {"match_id": "fixture", "key": "1", "odds": 2.0, "status": "pending", "score": None}
    with pytest.raises(ValueError, match="Sold insuficient"):
        wl.place_bet(store, 31, [leg], "x", now=NOW)
    wl.place_bet(store, 30, [leg], "x", now=NOW)
    data = wl.wallet(store, NOW)
    assert data["balance"] == 0 and data["open"] == 1  # no result stored yet: stays open
    store.save_matches([fixture(status="finished", home_goals=1, away_goals=0)])
    assert wl.wallet(store, NOW)["balance"] == 60
