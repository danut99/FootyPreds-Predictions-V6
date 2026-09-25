import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, parse_csv
from app.model import predict
from app.provider import FlashScore, ProviderError, normalize_matches
from app.store import Store
from tests.test_model import fixture, strong_history


def payload():
    return [
        {
            "name": "Test",
            "country_name": "England",
            "matches": [
                {
                    "match_id": "fixture",
                    "timestamp": (datetime.now(timezone.utc) + timedelta(days=1)).timestamp(),
                    "home_team": {"name": "Strong", "team_id": "h"},
                    "away_team": {"name": "Weak", "team_id": "a"},
                    "match_status": {"is_started": False, "is_finished": False},
                    "scores": {"home": None, "away": None},
                    "odds": {"1": 1.5, "2": "-", "X": None},
                }
            ],
        }
    ]


@pytest.fixture
def client(tmp_path):
    settings = Settings(api_key="test-secret", database=tmp_path / "test.db")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload()))
    with TestClient(create_app(settings, transport)) as test_client:
        yield test_client


def test_normalizer_preserves_nulls_and_handles_bad_rows():
    data = payload()
    data[0]["matches"].append({"match_id": "broken"})
    rows, rejected = normalize_matches(data)
    assert len(rows) == 1 and rejected == 1
    assert rows[0].home_goals is None
    assert rows[0].status == "scheduled"
    assert rows[0].odds == {"1": 1.5}


def test_results_parser_zero_is_valid():
    data = payload()
    data[0]["matches"][0]["scores"] = {"home": 0, "away": 0}
    rows, _ = normalize_matches(data, results=True)
    assert rows[0].status == "finished"
    assert rows[0].home_goals == 0


def test_unknown_schema_is_explicit_error():
    with pytest.raises(ProviderError):
        normalize_matches({"nonsense": []})


def test_home_health_static_and_secret_not_exposed(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/.env").status_code == 404
    health = client.get("/api/health")
    assert health.json()["api_configured"]
    assert "test-secret" not in health.text
    assert "frame-ancestors 'none'" in health.headers["content-security-policy"]


def test_live_server_cors_only_allows_known_local_origins(client):
    allowed = client.get("/api/health", headers={"origin": "http://127.0.0.1:5500"})
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5500"
    preflight = client.options(
        "/api/demo/backtest",
        headers={
            "origin": "http://localhost:5501",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    blocked = client.get("/api/health", headers={"origin": "https://other.example"})
    assert "access-control-allow-origin" not in blocked.headers


def test_fixture_loading_and_cache(client):
    first = client.get("/api/matches?day=2026-06-01").json()
    second = client.get("/api/matches?day=2026-06-01").json()
    assert not first["cached"] and second["cached"]
    assert len(second["matches"]) == 1


def test_missing_match_invalid_params_and_csrf(client):
    assert client.post("/api/analyze/missing", json={}).status_code == 404
    assert client.get("/api/matches?day=bad").status_code == 422
    assert client.get("/api/demo?threshold=1.5").status_code == 422
    assert (
        client.post("/api/backtest", headers={"origin": "https://evil.example"}).status_code == 403
    )


def test_analyze_refuses_started_match(client):
    store = client.app.state.store
    store.save_matches([fixture(kickoff=datetime.now(timezone.utc) - timedelta(hours=1))])
    assert client.post("/api/analyze/fixture", json={"enrich": False}).status_code == 409


def test_demo_never_persists_real_matches_or_predictions(client):
    assert client.get("/api/demo").json()["source"] == "synthetic"
    assert client.app.state.store.matches() == []
    assert client.get("/api/results").json()["metrics"]["selected"] == 0


def test_snapshot_immutable_and_settlement_idempotent(tmp_path):
    store = Store(tmp_path / "ledger.db")
    match = fixture()
    p = predict(match, strong_history())
    now = match.kickoff - timedelta(days=1)
    assert store.snapshot(match, p, now)
    assert not store.snapshot(match, p | {"threshold": 0.7}, now)
    final = match.model_copy(update={"status": "finished", "home_goals": 4, "away_goals": 0})
    assert store.settle([final]) == 1
    assert store.settle([final]) == 0
    assert store.predictions()[0]["prediction"]["threshold"] == 0.85
    assert store.predictions()[0]["result"]["won"]


def test_late_and_synthetic_predictions_not_saved(tmp_path):
    store = Store(tmp_path / "ledger.db")
    match = fixture()
    p = predict(match, strong_history())
    assert not store.snapshot(match, p, match.kickoff)
    assert not store.snapshot(
        match.model_copy(update={"source": "synthetic"}), p, match.kickoff - timedelta(days=1)
    )
    assert store.assessment_count() == 0


def test_assessed_abstentions_count_towards_coverage(tmp_path):
    store = Store(tmp_path / "ledger.db")
    match = fixture()
    assert not store.snapshot(match, predict(match, []), match.kickoff - timedelta(days=1))
    assert store.assessment_count() == 1
    assert store.predictions() == []


def test_provider_429_has_no_retry_or_secret_leak(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "do not expose token"})

    async def run():
        settings = Settings(api_key="private", database=tmp_path / "cache.db")
        provider = FlashScore(settings, Store(settings.database), httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderError) as exc:
                await provider.get("matches/list-by-date", {})
            assert exc.value.status == 429
            assert "private" not in str(exc.value)
            assert len(calls) == 1
        finally:
            await provider.client.aclose()

    asyncio.run(run())


CSV_HEADER = "id,kickoff,league,home,away,home_goals,away_goals\n"


def test_csv_rejects_missing_timezone_and_duplicates():
    with pytest.raises(ValueError):
        parse_csv(CSV_HEADER + "a,2025-01-01,League,A,B,1,0\n")
    row = "a,2025-01-01T18:00:00Z,League,A,B,1,0\n"
    with pytest.raises(ValueError):
        parse_csv(CSV_HEADER + row + row)
    assert len(parse_csv(CSV_HEADER + row)) == 1


def test_csv_endpoint_isolated_and_size_limited(client):
    response = client.post(
        "/api/backtest/csv", content=CSV_HEADER + "a,2025-01-01T18:00:00Z,League,A,B,1,0\n"
    )
    assert response.status_code == 200
    assert response.json()["metrics"]["accuracy"] is None
    assert client.app.state.store.matches() == []
    assert client.post("/api/backtest/csv", content="x" * 2_000_001).status_code == 413
