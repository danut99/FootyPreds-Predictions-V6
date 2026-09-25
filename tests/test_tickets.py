import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain import Match
from app.provider import ProviderError
from app.store import Store
from app.tickets import PlanBuilder, PlanRequest, choose_ticket, settle_plans


def leg(number, odds=2, probability=0.6, **kwargs):
    return (
        dict(
            match_id=str(number),
            home=f"Home {number}",
            away=f"Away {number}",
            league=f"League {number}",
            odds=odds,
            probability=probability,
            key="1",
            status="pending",
        )
        | kwargs
    )


def test_combined_odds_are_product_of_actual_quotes():
    ticket = choose_ticket([leg(1), leg(2), leg(3, 2.5)], 10, 3)
    assert ticket["total_odds"] == 10
    assert ticket["estimated_probability"] == pytest.approx(0.6**3)
    assert len(ticket["legs"]) == 3


def test_no_fabricated_ticket_when_target_impossible():
    assert choose_ticket([leg(1)], 10, 3) is None


def test_mutually_exclusive_same_match_and_repeated_teams_rejected():
    assert choose_ticket([leg(1), leg(1, key="2")], 4, 2) is None
    assert choose_ticket([leg(1), leg(2, home="Home 1")], 4, 2) is None


def test_league_diversity_can_be_configured():
    candidates = [leg(1), leg(2, league="League 1")]
    assert choose_ticket(candidates, 4, 2, True) is None
    assert choose_ticket(candidates, 4, 2, False)["total_odds"] == 4


def test_match_count_limit_and_target_tolerance():
    assert choose_ticket([leg(i) for i in range(5)], 32, 3) is None
    assert choose_ticket([leg(1, 1.7)], 2, 1) is None
    assert choose_ticket([leg(1, 1.95)], 2, 1)["total_odds"] == 1.95


def test_plan_validation_refuses_past_and_extreme_settings():
    today = datetime.now(timezone.utc).date()
    with pytest.raises(ValidationError):
        PlanRequest(start_date=today - timedelta(days=1))
    with pytest.raises(ValidationError):
        PlanRequest(start_date=today, target_odds=1000)
    with pytest.raises(ValidationError):
        PlanRequest(start_date=today, max_legs=10)


def test_week_persists_seven_days_and_survives_builder_restart(tmp_path):
    store = Store(tmp_path / "plans.db")

    async def run():
        builder = PlanBuilder(store, None)
        request = PlanRequest(
            start_date=datetime.now(timezone.utc).date() + timedelta(days=1), demo=True
        )
        plan = builder.start(request)
        with pytest.raises(ProviderError):
            builder.start(request)
        await builder.task
        saved = store.plan(plan["id"])
        assert len(saved["days"]) == 7
        assert saved["progress"] == 7
        assert all(day["ticket"] for day in saved["days"])
        assert saved["source"] == "synthetic"
        assert store.matches() == [] and store.predictions() == []
        PlanBuilder(store, None)
        assert store.plan(plan["id"])["status"] == "ready"

    asyncio.run(run())


def test_api_failure_stops_generation_and_saves_error(tmp_path):
    class FailingProvider:
        calls = 0

        async def fixtures(self, day):
            self.calls += 1
            raise ProviderError("Quota reached", 429)

    store = Store(tmp_path / "plans.db")
    provider = FailingProvider()

    async def run():
        builder = PlanBuilder(store, provider)
        plan = builder.start(PlanRequest(start_date=datetime.now(timezone.utc).date()))
        await builder.task
        assert store.plan(plan["id"])["status"] == "failed"
        assert provider.calls == 1

    asyncio.run(run())


def test_ticket_settlement_and_demo_isolation(tmp_path):
    store = Store(tmp_path / "plans.db")
    plan = {
        "id": "test",
        "created": 1,
        "request": {"demo": False},
        "status": "ready",
        "days": [{"ticket": choose_ticket([leg(1), leg(2)], 4, 2)}],
    }
    store.save_plan(plan)
    for i, hg, ag in ((1, 2, 0), (2, 0, 1)):
        store.save_matches(
            [
                Match(
                    id=str(i),
                    kickoff=datetime.now(timezone.utc) - timedelta(days=1),
                    league=f"League {i}",
                    home=f"Home {i}",
                    away=f"Away {i}",
                    status="finished",
                    home_goals=hg,
                    away_goals=ag,
                )
            ]
        )
    settle_plans(store)
    saved = store.plan("test")
    assert saved["days"][0]["ticket"]["status"] == "lost"
    assert [leg["status"] for leg in saved["days"][0]["ticket"]["legs"]] == ["won", "lost"]
    settle_plans(store)
    assert store.plan("test") == saved
