import asyncio
from datetime import datetime, timedelta, timezone

from footypreds.config import Settings
from footypreds.provider import FlashScore
from footypreds.store import Store
from footypreds.tests.helpers import fixture, fixtures_payload, h2h_payload
from footypreds.tickets import PlanBuilder, PlanRequest


def test_history_pagination_stops_on_repeated_page_and_deduplicates(tmp_path):
    async def run():
        provider = FlashScore(Settings(database=tmp_path / "test.db"), Store(tmp_path / "test.db"))
        calls = []

        async def get(endpoint, params, refresh=False, ttl=None):
            calls.append(params["page"])
            data = fixtures_payload()
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


def test_head_to_head_drops_rows_at_or_after_kickoff_and_uses_long_cache(tmp_path):
    kickoff = datetime.now(timezone.utc) + timedelta(days=1)

    async def run():
        settings = Settings(database=tmp_path / "test.db", history_ttl=7200)
        provider = FlashScore(settings, Store(settings.database))
        seen = {}

        async def get(endpoint, params, refresh=False, ttl=None):
            seen.update(endpoint=endpoint, params=params, ttl=ttl)
            return h2h_payload(kickoff), False

        provider.get = get
        try:
            rows, _, rejected = await provider.head_to_head(fixture(id="m1", kickoff=kickoff))
        finally:
            await provider.client.aclose()
        assert seen == {"endpoint": "matches/h2h", "params": {"match_id": "m1"}, "ttl": 7200}
        assert rejected == 0 and "future" not in {r.id for r in rows}
        assert all(r.kickoff < kickoff for r in rows)

    asyncio.run(run())


def test_cup_ticket_reaches_eligibility_with_one_h2h_request(tmp_path):
    """Form from league games makes a cup fixture predictable (V7 needed cup-only history)."""
    when = datetime.now(timezone.utc) + timedelta(days=1)
    match = fixture(kickoff=when, league="Challenge Cup", country="Scotland", odds={"1": 1.25})
    # 3-1 wins keep the model near the 1.25 price (p x odds ~1.04); 4-0 wins every week would
    # claim ~1.23, which recommend.MAX_VALUE rejects as an unbacked disagreement with the market.
    past = [
        match.model_copy(
            update={
                "id": f"past{i}",
                "kickoff": when - timedelta(days=7 * (i + 1)),
                "league": "SCOTLAND: Championship",
                "home": "Strong" if i % 2 == 0 else f"Club{i}",
                "away": f"Club{i}" if i % 2 == 0 else "Weak",
                "status": "finished",
                "home_goals": 3,
                "away_goals": 1,
                "odds": {},
            }
        )
        for i in range(20)
    ]

    class Provider:
        calls = 0

        async def fixtures(self, day):
            return [match], True, 0

        async def head_to_head(self, match):
            self.calls += 1
            return past, False, 0

    async def run():
        store = Store(tmp_path / "test.db")
        provider = Provider()
        builder = PlanBuilder(store, provider)
        plan = builder.start(
            PlanRequest(
                mode="custom",
                start_date=when.date(),
                competitions=["scotland|challenge cup"],
                target_odds=1.25,
                max_legs=1,
            )
        )
        await builder.task
        result = store.plan(plan["id"])
        assert result["status"] == "ready", result["days"][0].get("reason")
        assert provider.calls == 1
        diagnostics = result["days"][0]["diagnostics"]
        assert diagnostics["history_enriched"] == 1
        assert diagnostics["insufficient_history"] == 0
        assert diagnostics["match_details"][0]["sample"]["home"] == 10

    asyncio.run(run())
