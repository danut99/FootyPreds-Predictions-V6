"""/api/simulate and /api/simulate/datasets through the real app (TestClient, no network)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.evaluation import sim_datasets as sd
from footypreds.tests.test_simulator import football_records
from footypreds.tests.test_simulator_datasets import write_benchmark


def offline(request):
    return httpx.Response(500, json={"message": "offline"})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "_MEMO", {})
    app = create_app(
        Settings(api_key="", database=tmp_path / "sim.db"), httpx.MockTransport(offline)
    )
    app.state.sim_benchmark_dir = write_benchmark(tmp_path / "bench", football_records())
    app.state.sim_cache_dir = tmp_path / "cache"
    app.state.sim_workers = 1
    with TestClient(app) as test_client:
        yield test_client


BASE = {"dataset": "football", "bankroll": 1000, "start": "2024-08-03", "end": "2025-05-03"}


def test_datasets_endpoint(client):
    data = client.get("/api/simulate/datasets").json()
    items = {item["id"]: item for item in data["datasets"]}
    assert list(items) == list(sd.DATASET_IDS)
    football = items["football"]
    assert football["available"] and football["sport"] == "football"
    assert (football["start"], football["end"], football["matches"]) == (
        "2024-06-30",
        "2025-05-03",
        240,
    )
    assert items["football-plus"]["available"] is False
    assert "--download" in items["football-plus"]["hint"]
    assert items["local-basketball"]["sport"] == "basketball"
    assert items["local-basketball"]["available"] is False


def test_contract_form_daily_ticket(client):
    body = BASE | {"strategy": "flat", "stake": 10, "target_odds": 2}
    response = client.post("/api/simulate", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "ticket" and data["strategy"] == "flat"
    for key in (
        "dataset",
        "sport",
        "start",
        "end",
        "initial",
        "final",
        "profit",
        "staked",
        "roi",
        "bets",
        "won",
        "lost",
        "void",
        "hit_rate",
        "max_drawdown",
        "peak",
        "history",
        "rows",
        "method",
        "warning",
    ):
        assert key in data, key
    assert data["initial"] == 1000 and data["bets"] > 0
    assert data["dataset"]["id"] == "football" and data["cache"]["computed"] == 1
    assert all(row["stake"] == 10 for row in data["rows"])
    again = client.post("/api/simulate", json=body).json()
    assert again["cache"]["computed"] == 0
    assert again["rows"] == data["rows"] and again["final"] == data["final"]


def test_long_form_singles_with_percent_staking(client):
    body = BASE | {
        "strategy": "singles",
        "staking": "percent",
        "stake": 0.02,
        "picks_per_day": 2,
        "seed": 7,
    }
    data = client.post("/api/simulate", json=body).json()
    assert data["mode"] == "singles" and data["staking"] == "percent"
    assert data["max_bets_per_day"] == 2 and data["seed"] == 7
    per_day = {}
    for row in data["rows"]:
        per_day[row["date"]] = per_day.get(row["date"], 0) + 1
    assert max(per_day.values()) <= 2
    first = data["rows"][0]
    assert first["stake"] == pytest.approx(0.02 * 1000, abs=0.01)


def test_kelly_accepts_kelly_fraction(client):
    body = BASE | {"strategy": "kelly", "kelly_fraction": 0.5, "kelly_cap": 0.05}
    data = client.post("/api/simulate", json=body).json()
    assert data["staking"] == "kelly" and data["stake"] == 0.5
    assert all(row["stake"] <= 0.05 * row["bankroll_before"] + 0.01 for row in data["rows"])
    assert data["baseline"]["staking"] == "percent"


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        (BASE | {"bankroll": 0}, 422, "Suma inițială"),
        (BASE | {"bankroll": 20_000_000}, 422, "Suma inițială"),
        (BASE | {"strategy": "ticket"}, 422, "cota țintă"),
        (BASE | {"strategy": "percent", "stake": 0.9}, 422, "Procentul"),
        (BASE | {"start": "2019-01-01"}, 422, "Intervalul trebuie"),
        (BASE | {"start": "2025-04-01", "end": "2025-03-01"}, 422, "Data de început"),
        (BASE | {"dataset": "cricket"}, 422, "Set de date necunoscut"),
        (BASE | {"dataset": "football-plus"}, 404, "nu este disponibil"),
        (BASE | {"dataset": "local-tennis"}, 404, "nu este disponibil"),
        (BASE | {"sport": "tennis"}, 422, "conține doar football"),
        (BASE | {"bankroll": "multi"}, 422, "Parametri invalizi"),
        (BASE | {"strategy": "martingale"}, 422, "Strategie necunoscută"),
    ],
)
def test_invalid_simulations_return_romanian_errors(client, body, status, text):
    response = client.post("/api/simulate", json=body)
    assert response.status_code == status, response.text
    assert text in response.json()["detail"]


def test_local_dataset_via_api(client):
    from footypreds.domain import Match

    records = football_records()
    store = client.app.state.store
    store.save_matches(
        [
            Match.model_validate(r["match"]).model_copy(
                update={"source": "flashscore", "odds": r["reference_odds"]}
            )
            for r in records[-40:]
        ]
        + [Match.model_validate(r["match"]) for r in records[:-40]]
    )
    items = {i["id"]: i for i in client.get("/api/simulate/datasets").json()["datasets"]}
    assert items["local-football"]["available"] and items["local-football"]["bettable"] == 40
    data = client.post(
        "/api/simulate", json={"dataset": "local-football", "strategy": "singles", "stake": 5}
    ).json()
    assert data["dataset"]["id"] == "local-football" and data["sport"] == "football"


def test_sport_alone_picks_that_sports_default_dataset(client):
    body = {"sport": "football", "strategy": "singles", "start": "2024-08-03", "end": "2024-10-01"}
    data = client.post("/api/simulate", json=body).json()
    assert data["dataset"]["id"] == "football"
    response = client.post("/api/simulate", json={"sport": "basketball"})
    assert response.status_code == 404 and "Sincronizează" in response.json()["detail"]
    assert client.post("/api/simulate", json={"sport": "golf"}).status_code == 422
