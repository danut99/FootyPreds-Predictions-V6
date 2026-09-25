import asyncio
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.provider import FlashScore
from app.store import Store
from app.tickets import PlanBuilder, PlanRequest
from tests.test_api import payload
from tests.test_model import fixture


def test_history_pagination_stops_on_repeated_page_and_deduplicates(tmp_path):
    async def run():
        provider = FlashScore(Settings(database=tmp_path / "test.db"), Store(tmp_path / "test.db"))
        calls = []

        async def get(endpoint, params):
            calls.append(params["page"])
            data = payload()
            data[0]["matches"][0]["scores"] = {"home": 2, "away": 0}
            return data, False

        provider.get = get
        try:
            rows, _ = await provider.history(fixture(home_id="home"), start_page=2, pages=3)
            assert calls == [2, 3]
            assert len(rows) == 1
        finally:
            await provider.client.aclose()

    asyncio.run(run())


def test_cup_generation_fetches_older_history_and_reaches_eligibility(tmp_path):
    when = datetime.now(timezone.utc) + timedelta(days=1)
    match = fixture(kickoff=when, league="Challenge Cup", country="Scotland", odds={"1": 2})
    past = [
        match.model_copy(
            update={
                "id": f"past{i}",
                "kickoff": when - timedelta(days=7 * (i + 1)),
                "status": "finished",
                "home_goals": 4,
                "away_goals": 0,
            }
        )
        for i in range(20)
    ]

    class Provider:
        calls = []

        async def fixtures(self, day):
            return [match], True, 0

        async def history(self, match, *, start_page=1, pages=1):
            self.calls.append((start_page, pages))
            return (past[:7] if start_page == 1 else past[7:]), []

    async def run():
        store = Store(tmp_path / "test.db")
        provider = Provider()
        builder = PlanBuilder(store, provider)
        plan = builder.start(
            PlanRequest(
                mode="custom", start_date=when.date(), competitions=["scotland|challenge cup"]
            )
        )
        await builder.task
        result = store.plan(plan["id"])
        assert result["status"] == "ready"
        assert provider.calls == [(1, 1), (2, 2)]
        diagnostics = result["days"][0]["diagnostics"]
        assert diagnostics["deep_history_matches"] == 1
        assert diagnostics["insufficient_history"] == 0
        assert diagnostics["match_details"][0]["sample"]["home"] == 20

    asyncio.run(run())
