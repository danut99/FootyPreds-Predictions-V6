import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.competitions import catalog, competition_id, match_competition, predict_competition
from app.config import Settings
from app.main import create_app
from app.store import Store
from app.tickets import PlanBuilder, PlanRequest, choose_ticket
from tests.test_model import fixture, strong_history


def tomorrow():
    return datetime.now(timezone.utc).date() + timedelta(days=1)


def test_competition_identity_preserves_country_gender_and_division():
    assert competition_id("EUROPE: Champions League - League phase") == competition_id(
        "UEFA Champions League - Play Offs", "Europe"
    )
    assert competition_id("Premier League", "England") != competition_id("Premier League", "Bhutan")
    assert competition_id("Champions League - Women", "Europe") != competition_id(
        "Champions League", "Europe"
    )
    assert competition_id("Prva Liga - RS") != competition_id("Prva Liga - FBiH")


def test_national_competitions_are_in_catalog_and_remain_distinct():
    matches = [
        fixture(league="EUROPE: UEFA Nations League - League A", country="Europe"),
        fixture(league="WORLD: Friendly International", country="World"),
    ]
    found = {c["id"]: c for c in catalog(matches)}
    assert found[match_competition(matches[0])]["count"] == 1
    assert found[match_competition(matches[1])]["count"] == 1
    assert competition_id("UEFA Nations League - League A", "Europe") != competition_id(
        "UEFA Nations League - League B", "Europe"
    )


def test_stage_history_is_used_but_other_cups_and_countries_are_excluded():
    match = fixture(league="EUROPE: Champions League - League phase", country="Europe")
    history = [
        m.model_copy(update={"league": "Champions League - Play Offs", "country": "Europe"})
        for m in strong_history()
    ]
    baseline = predict_competition(match, history)
    assert baseline["quality"] == "sufficient"
    pollution = [
        m.model_copy(
            update={
                "id": "other-" + m.id,
                "home_goals": 0,
                "away_goals": 20,
                "league": "Europa League",
            }
        )
        for m in history
    ]
    assert predict_competition(match, history + pollution) == baseline


def test_single_competition_disables_diversity_and_deduplicates():
    request = PlanRequest(start_date=tomorrow(), competitions=["europe|champions league"] * 2)
    assert request.competitions == ["europe|champions league"]
    assert request.diverse_leagues is False


def test_catalog_local_and_demo_do_not_call_external_api(tmp_path):
    def refuse(request):
        raise AssertionError("Unexpected external API request")

    app = create_app(Settings(database=tmp_path / "test.db"), httpx.MockTransport(refuse))
    with TestClient(app) as client:
        rows = client.get("/api/competitions", params={"day": str(tomorrow())}).json()
        assert any(c["name"] == "Champions League" for c in rows["competitions"])
        demo = client.get("/api/competitions", params={"day": str(tomorrow()), "demo": True}).json()
        assert len(demo["competitions"]) == 6
        assert all(c["name"].startswith("Demo League") for c in demo["competitions"])


def test_custom_search_reaches_beyond_first_eight_using_local_history(tmp_path):
    store = Store(tmp_path / "test.db")
    when = datetime.combine(tomorrow(), datetime.min.time(), timezone.utc) + timedelta(hours=18)
    matches = [
        fixture(
            id=f"m{i:02}",
            home=f"Strong{i}",
            away=f"Weak{i}",
            kickoff=when,
            league=f"League {i}",
            country="England",
            odds={"1": 2 if i == 9 else 1.1},
        )
        for i in range(12)
    ]
    for match in matches:
        store.save_matches(
            [
                match.model_copy(
                    update={
                        "id": f"{match.id}-past-{j}",
                        "kickoff": when - timedelta(days=7 * (j + 1)),
                        "status": "finished",
                        "home_goals": 4,
                        "away_goals": 0,
                    }
                )
                for j in range(25)
            ]
        )

    class Provider:
        async def fixtures(self, day):
            return matches, True, 0

        async def history(self, match):
            raise AssertionError("Sufficient local history must not trigger API calls")

    async def run():
        builder = PlanBuilder(store, Provider())
        request = PlanRequest(mode="custom", start_date=tomorrow(), max_legs=1)
        plan = builder.start(request)
        await builder.task
        saved = store.plan(plan["id"])
        assert saved["status"] == "ready"
        assert saved["days"][0]["ticket"]["legs"][0]["match_id"] == "m09"
        assert saved["days"][0]["analyzed"] == 12

    asyncio.run(run())


def test_explicit_competition_never_falls_back_to_other_leagues(tmp_path):
    class Provider:
        async def fixtures(self, day):
            return (
                [
                    fixture(
                        kickoff=datetime.now(timezone.utc) + timedelta(days=1),
                        league="Other",
                        odds={"1": 2},
                    )
                ],
                False,
                0,
            )

        async def history(self, match):
            raise AssertionError("Filtered-out match should never fetch history")

    async def run():
        store = Store(tmp_path / "test.db")
        builder = PlanBuilder(store, Provider())
        plan = builder.start(
            PlanRequest(
                mode="custom", start_date=tomorrow(), competitions=["europe|champions league"]
            )
        )
        await builder.task
        day = store.plan(plan["id"])["days"][0]
        assert day["ticket"] is None and day["analyzed"] == 0
        assert "Competițiile selectate" in day["reason"]

    asyncio.run(run())


def test_demo_multiple_competition_filter_is_saved_and_applied(tmp_path):
    async def run():
        store = Store(tmp_path / "test.db")
        builder = PlanBuilder(store, None)
        selected = ["|demo league 1", "|demo league 2"]
        candidates, _ = builder.demo_candidates(tomorrow(), 0.55)
        filtered = [c for c in candidates if c["competition_id"] in selected]
        target = next(c["odds"] for c in filtered if 1.2 <= c["odds"] <= 100)
        plan = builder.start(
            PlanRequest(
                mode="custom",
                start_date=tomorrow(),
                demo=True,
                competitions=selected,
                target_odds=target,
            )
        )
        await builder.task
        saved = store.plan(plan["id"])
        assert saved["status"] == "ready"
        assert all(c["competition_id"] in selected for c in saved["days"][0]["ticket"]["legs"])
        assert saved["request"]["competitions"] == selected
        assert choose_ticket(filtered, target, 3) is not None

    asyncio.run(run())


def test_history_enrichment_respects_eight_match_budget(tmp_path):
    class Provider:
        calls = 0

        async def fixtures(self, day):
            return (
                [
                    fixture(
                        id=str(i),
                        league=f"League {i}",
                        odds={"1": 2},
                        kickoff=datetime.now(timezone.utc) + timedelta(days=1),
                    )
                    for i in range(30)
                ],
                False,
                0,
            )

        async def history(self, match):
            self.calls += 1
            return [], []

    async def run():
        store = Store(tmp_path / "test.db")
        provider = Provider()
        builder = PlanBuilder(store, provider)
        plan = builder.start(PlanRequest(mode="custom", start_date=tomorrow()))
        await builder.task
        day = store.plan(plan["id"])["days"][0]
        assert provider.calls == 8
        assert day["analyzed"] == 24 and day["diagnostics"]["with_odds"] == 30
        assert day["diagnostics"]["insufficient_history"] == 24
        assert "Istoric insuficient" in day["reason"]

    asyncio.run(run())


def test_catalog_refresh_loads_provider_competitions(tmp_path):
    from tests.test_api import payload

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload()))
    app = create_app(Settings(api_key="test", database=tmp_path / "test.db"), transport)
    with TestClient(app) as client:
        response = client.get("/api/competitions", params={"day": str(tomorrow()), "refresh": True})
        assert response.status_code == 200
        rows = response.json()["competitions"]
        assert next(c for c in rows if c["id"] == "england|test")["count"] == 1
