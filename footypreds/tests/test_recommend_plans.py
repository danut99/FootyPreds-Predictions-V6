"""Weekly/custom plans (tickets.PlanBuilder) on the recommendation optimizer: no minimum
probability, multi-sport candidates, the [0.93, 1.12] x target window and void tickets."""

import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from footypreds.domain import Match
from footypreds.recommend import ODDS_WINDOW
from footypreds.store import Store
from footypreds.tickets import PlanBuilder, PlanRequest, plan_ticket, settle_plans


def today():
    return datetime.now(timezone.utc).date()


def day():
    return today() + timedelta(days=1)


def leg(number, odds, probability, league=None):
    return {
        "match_id": str(number),
        "sport": "football",
        "home": f"H{number}",
        "away": f"A{number}",
        "league": league or f"L{number}",
        "competition_id": f"c|{league or number}",
        "kickoff": f"2026-01-01T{number % 24:02d}:00:00+00:00",
        "key": "1",
        "odds": odds,
        "probability": probability,
        "status": "pending",
    }


def test_min_probability_is_optional_and_ignored():
    request = PlanRequest(start_date=today())
    assert request.min_probability is None and request.sports == ["football"]
    assert PlanRequest(start_date=today(), min_probability=0.9).min_probability == 0.9
    ordered = PlanRequest(start_date=today(), sports=["tennis", "football", "tennis"])
    assert ordered.sports == ["football", "tennis"]
    for sports in (["golf"], [], ["tennis"] * 4):
        with pytest.raises(ValidationError):
            PlanRequest(start_date=today(), sports=sports)


def test_plan_ticket_maximizes_probability_inside_the_window():
    candidates = [leg(1, 2.0, 0.40), leg(2, 1.45, 0.72), leg(3, 1.4, 0.74)]
    ticket = plan_ticket(candidates, 2.0, 3)
    assert [item["match_id"] for item in ticket["legs"]] == ["2", "3"]
    assert ticket["total_odds"] == pytest.approx(1.45 * 1.4)
    assert ticket["estimated_probability"] == ticket["probability"] == pytest.approx(0.72 * 0.74)
    assert ticket["window"] == [2.0 * ODDS_WINDOW[0], 2.0 * ODDS_WINDOW[1]]
    assert ticket["status"] == "pending" and "Independență" in ticket["probability_assumption"]
    assert plan_ticket([leg(1, 1.5, 0.66)], 2.0, 3) is None


def test_plan_ticket_diverse_leagues():
    candidates = [leg(1, 1.45, 0.72, "Same"), leg(2, 1.4, 0.74, "Same")]
    assert plan_ticket(candidates, 2.0, 3, diverse=True) is None
    assert len(plan_ticket(candidates, 2.0, 3, diverse=False)["legs"]) == 2


class SportProvider:
    """fixtures() answers per sport; football calls keep the historical signature."""

    def __init__(self, by_sport):
        self.by_sport = by_sport
        self.fixture_calls = []
        self.h2h_calls = []

    async def fixtures(self, target, refresh=False, ttl=None, sport="football"):
        self.fixture_calls.append(sport)
        return self.by_sport.get(sport, []), False, 0

    async def head_to_head(self, match, refresh=False):
        self.h2h_calls.append(match.id)
        return [], False, 0


def basketball_day(count=4):
    kickoff = datetime.combine(day(), datetime.min.time(), timezone.utc) + timedelta(hours=18)
    games, history = [], []
    for n in range(count):
        home, away = f"BH{n}", f"BA{n}"
        p = 0.6 + 0.05 * n
        games.append(
            Match(
                id=f"b{n}",
                kickoff=kickoff + timedelta(minutes=n),
                league="USA: NBA",
                country="USA",
                home=home,
                away=away,
                sport="basketball",
                odds={"1": round(1 / (p * 1.02), 2), "2": round(1 / ((1 - p) * 1.02), 2)},
            )
        )
        for i in range(10):
            for side, team in enumerate((home, away)):
                history.append(
                    Match(
                        id=f"h-{n}-{side}-{i}",
                        kickoff=kickoff - timedelta(days=3 * i + 2),
                        league="USA: NBA",
                        country="USA",
                        home=team,
                        away=f"R{n}-{side}-{i}",
                        status="finished",
                        # 90-86 / 86-90 keeps the model near the prices (p x odds 0.98-1.03);
                        # 95-84 / 80-91 claimed up to 1.10, which recommend.MAX_VALUE rejects.
                        home_goals=90 if side == 0 else 86,
                        away_goals=86 if side == 0 else 90,
                        sport="basketball",
                    )
                )
    return games, history


def test_plan_builder_uses_every_requested_sport(tmp_path):
    games, history = basketball_day()
    store = Store(tmp_path / "plans.db")
    store.save_matches(history)
    provider = SportProvider({"basketball": games})

    async def main():
        builder = PlanBuilder(store, provider)
        plan = builder.start(
            PlanRequest(
                mode="custom",
                start_date=day(),
                target_odds=2.0,
                max_legs=3,
                sports=["football", "basketball"],
                diverse_leagues=False,
            )
        )
        await builder.task
        return store.plan(plan["id"])

    plan = asyncio.run(main())
    assert provider.fixture_calls == ["football", "basketball"]
    ticket = plan["days"][0]["ticket"]
    assert ticket is not None and plan["status"] == "ready"
    assert all(item["sport"] == "basketball" for item in ticket["legs"])
    low, high = ticket["window"]
    assert low <= ticket["total_odds"] <= high
    by_id = {g.id: g for g in games}
    for item in ticket["legs"]:
        assert item["odds"] == by_id[item["match_id"]].odds[item["key"]]
        assert item["league"] == "USA: NBA" and item["quoted_at"]
    assert ticket["total_odds"] == pytest.approx(math.prod(i["odds"] for i in ticket["legs"]))
    details = plan["days"][0]["diagnostics"]["match_details"]
    assert {d["sport"] for d in details} == {"basketball"}
    # History already made every game grade A-C: no FlashScore enrichment was needed.
    assert provider.h2h_calls == []


def test_all_void_plan_ticket_is_void_not_won(tmp_path):
    store = Store(tmp_path / "void.db")
    store.save_plan(
        {
            "id": "p",
            "created": 1,
            "status": "ready",
            "request": {"demo": False},
            "days": [{"ticket": {"legs": [{"match_id": "t", "key": "1", "status": "pending"}]}}],
        }
    )
    store.save_matches(
        [
            Match(
                id="t",
                kickoff=datetime.now(timezone.utc) - timedelta(days=1),
                league="ATP - SINGLES: X, hard",
                home="A",
                away="B",
                sport="tennis",
                status="finished",
                home_goals=1,
                away_goals=0,
                finish_type="retired",
            )
        ]
    )
    settle_plans(store)
    ticket = store.plan("p")["days"][0]["ticket"]
    assert ticket["legs"][0]["status"] == "void" and ticket["status"] == "void"
