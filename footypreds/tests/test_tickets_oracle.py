"""choose_ticket against a brute-force oracle, and the plan builder with fake providers."""

import asyncio
import itertools
import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from footypreds.domain import Match
from footypreds.engine import canonical
from footypreds.provider import ProviderError
from footypreds.store import Store
from footypreds.tickets import ENRICH_BUDGET, PlanBuilder, PlanRequest, choose_ticket, settle_plans

# --- oracle -------------------------------------------------------------------------------


def valid(legs, target, diverse):
    size = len(legs)
    if len({leg["match_id"] for leg in legs}) != size:
        return False
    if (
        diverse
        and len({leg.get("competition_id", canonical(leg["league"])) for leg in legs}) != size
    ):
        return False
    teams = [canonical(t) for leg in legs for t in (leg["home"], leg["away"])]
    if len(set(teams)) != 2 * size:
        return False
    odds = math.prod(leg["odds"] for leg in legs)
    return target * 0.9 <= odds <= target * 1.1


def rank(legs, target):
    odds = math.prod(leg["odds"] for leg in legs)
    probability = math.prod(leg["probability"] for leg in legs)
    return (-abs(math.log(odds / target)), probability, -len(legs))


def oracle(candidates, target, max_legs, diverse):
    best = None
    for size in range(1, max_legs + 1):
        for legs in itertools.combinations(candidates, size):
            if valid(legs, target, diverse):
                current = rank(legs, target)
                best = current if best is None or current > best else best
    return best


def random_candidates(rng, count):
    matches = max(1, count - rng.randint(0, count // 2))
    teams = [f"Club {n}" for n in range(2 * matches + rng.randint(0, 3))]
    fixtures = {}
    for n in range(matches):
        if rng.random() < 0.15:  # a team reused in another fixture
            fixtures[str(n)] = (rng.choice(teams), f"Other {n}")
        else:
            fixtures[str(n)] = (teams[2 * n % len(teams)], teams[(2 * n + 1) % len(teams)])
    rows = []
    for _ in range(count):
        match_id = rng.choice(sorted(fixtures))
        home, away = fixtures[match_id]
        rows.append(
            {
                "match_id": match_id,
                "home": home if rng.random() > 0.05 else home.upper(),  # canonical duplicates
                "away": away,
                "league": f"League {rng.randint(0, 3)}",
                "competition_id": f"c|{rng.randint(0, 3)}",
                "key": rng.choice(["1", "X", "2", "over25", "btts"]),
                "odds": round(1.05 + rng.random() * 3, 2),
                "probability": round(0.35 + rng.random() * 0.6, 3),
                "status": "pending",
            }
        )
    return rows


@pytest.mark.parametrize("seed", range(120))
def test_choose_ticket_matches_the_brute_force_oracle(seed):
    rng = random.Random(seed)
    candidates = random_candidates(rng, rng.randint(0, 11))
    target = round(1.2 + rng.random() * rng.choice((2, 8, 20)), 2)
    max_legs = rng.randint(1, 5)
    diverse = rng.random() < 0.6
    ticket = choose_ticket(candidates, target, max_legs, diverse)
    expected = oracle(candidates, target, max_legs, diverse)
    if expected is None:
        assert ticket is None
        return
    legs = ticket["legs"]
    # Products are taken in a different leg order: equal up to float rounding.
    assert rank(legs, target) == pytest.approx(expected, rel=1e-12, abs=1e-12)
    # Never fabricated: real candidate legs, the product of their real quotes.
    assert all(any(leg is c for c in candidates) for leg in legs)
    assert ticket["total_odds"] == pytest.approx(math.prod(leg["odds"] for leg in legs))
    assert ticket["estimated_probability"] == pytest.approx(
        math.prod(leg["probability"] for leg in legs)
    )
    assert valid(legs, target, diverse) and 1 <= len(legs) <= max_legs
    assert ticket["target_odds"] == target and ticket["status"] == "pending"
    # Deterministic, and the choice does not depend on the input order.
    assert choose_ticket(candidates, target, max_legs, diverse) == ticket
    shuffled = candidates[:]
    rng.shuffle(shuffled)
    assert rank(choose_ticket(shuffled, target, max_legs, diverse)["legs"], target) == (
        pytest.approx(expected, rel=1e-12, abs=1e-12)
    )


def test_choose_ticket_does_not_mutate_its_input():
    rng = random.Random(1)
    candidates = random_candidates(rng, 10)
    snapshot = [dict(c) for c in candidates]
    choose_ticket(candidates, 3.0, 3)
    assert candidates == snapshot


def test_ticket_exactly_at_the_tolerance_edges():
    def leg(n, odds):
        return {
            "match_id": str(n),
            "home": f"H{n}",
            "away": f"A{n}",
            "league": f"L{n}",
            "odds": odds,
            "probability": 0.6,
            "key": "1",
        }

    assert choose_ticket([leg(1, 1.8)], 2.0, 1)["total_odds"] == 1.8
    assert choose_ticket([leg(1, 2.2)], 2.0, 1)["total_odds"] == 2.2
    assert choose_ticket([leg(1, 1.79)], 2.0, 1) is None
    assert choose_ticket([leg(1, 2.21)], 2.0, 1) is None
    assert choose_ticket([], 2.0, 5) is None
    same_team = leg(2, 2.0) | {"away": "h2"}  # home and away canonicalize to the same club
    assert choose_ticket([same_team], 2.0, 1) is None


def test_closest_odds_win_then_probability_then_fewer_legs():
    def leg(n, odds, probability, league=None):
        return {
            "match_id": str(n),
            "home": f"H{n}",
            "away": f"A{n}",
            "league": league or f"L{n}",
            "odds": odds,
            "probability": probability,
            "key": "1",
        }

    closer = choose_ticket([leg(1, 2.1, 0.9), leg(2, 2.0, 0.4)], 2.0, 1)
    assert closer["legs"][0]["match_id"] == "2"
    likelier = choose_ticket([leg(1, 2.0, 0.4), leg(2, 2.0, 0.5)], 2.0, 1)
    assert likelier["legs"][0]["match_id"] == "2"
    shorter = choose_ticket([leg(1, 4.0, 0.25), leg(2, 2.0, 0.5), leg(3, 2.0, 0.5)], 4.0, 2)
    assert len(shorter["legs"]) == 1


# --- plan requests ------------------------------------------------------------------------


def today():
    return datetime.now(timezone.utc).date()


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": today() - timedelta(days=1)},
        {"start_date": today() + timedelta(days=31)},
        {"target_odds": 1.19},
        {"target_odds": 100.01},
        {"max_legs": 0},
        {"max_legs": 6},
        {"min_probability": 0.34},
        {"min_probability": 0.91},
        {"competitions": [""]},
        {"competitions": ["england premier league"]},
        {"competitions": ["x|" + "y" * 200]},
        {"competitions": [f"c|{n}" for n in range(31)]},
        {"mode": "month"},
    ],
)
def test_plan_request_rejects_out_of_range_settings(changes):
    with pytest.raises(ValidationError):
        PlanRequest(**({"start_date": today()} | changes))


def test_plan_request_edges_are_accepted():
    PlanRequest(start_date=today() + timedelta(days=30), target_odds=100, max_legs=5)
    PlanRequest(start_date=today(), target_odds=1.2, max_legs=1, min_probability=0.9)


# --- plan builder with fake providers -----------------------------------------------------


def target_day():
    return today() + timedelta(days=1)


def day_matches(count=12, odds=None):
    kickoff = datetime.combine(target_day(), datetime.min.time(), timezone.utc) + timedelta(
        hours=15
    )
    return [
        Match(
            id=f"m{n:02}",
            kickoff=kickoff + timedelta(minutes=n),
            league=f"ENGLAND: League {n}",
            country="England",
            home=f"Home{n}",
            away=f"Away{n}",
            odds=odds or {"1": 2.0, "X": 3.5, "2": 4.0},
        )
        for n in range(count)
    ]


def history_for(match, count=12, goals=(2, 0)):
    """The home side wins goals[0]-goals[1] at home, the away side loses by the same score."""
    rows = []
    for n in range(count):
        for side, team in ((0, match.home), (1, match.away)):
            opponent = f"Rival{n}-{side}-{match.id}"
            rows.append(
                Match(
                    id=f"hist-{match.id}-{side}-{n}",
                    kickoff=match.kickoff - timedelta(days=3 * n + 2),
                    league="ENGLAND: League",
                    home=team,
                    away=opponent,
                    status="finished",
                    home_goals=goals[0] if side == 0 else goals[1],
                    away_goals=goals[1] if side == 0 else goals[0],
                )
            )
    return rows


class FakeProvider:
    def __init__(self, matches, h2h):
        self.matches, self.h2h = matches, h2h
        self.h2h_calls = []
        self.fixture_calls = 0

    async def fixtures(self, day, refresh=False, ttl=None):
        self.fixture_calls += 1
        return [m for m in self.matches if m.kickoff.date() == day], False, 0

    async def head_to_head(self, match, refresh=False):
        self.h2h_calls.append(match.id)
        outcome = self.h2h(match)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, False, 0


def run_plan(tmp_path, provider, **request):
    store = Store(tmp_path / "plans.db")

    async def main():
        builder = PlanBuilder(store, provider)
        plan = builder.start(
            PlanRequest(
                **(
                    {
                        "mode": "custom",
                        "start_date": target_day(),
                        "target_odds": 2.0,
                        "max_legs": 1,
                        "min_probability": 0.35,
                    }
                    | request
                )
            )
        )
        await builder.task
        return store.plan(plan["id"])

    return asyncio.run(main()), store


def test_enrichment_stops_at_the_budget(tmp_path):
    provider = FakeProvider(day_matches(12), lambda match: [])
    plan, _ = run_plan(tmp_path, provider)
    day = plan["days"][0]
    assert len(provider.h2h_calls) == ENRICH_BUDGET
    assert day["diagnostics"]["history_enriched"] == ENRICH_BUDGET
    assert day["ticket"] is None and day["status"] == "unavailable" and day["reason"]
    assert plan["status"] == "partial" and plan["progress"] == 1


def test_enrichment_stops_as_soon_as_a_ticket_exists(tmp_path):
    matches = day_matches(12)
    # 2-1 records keep the model close to the 2.0 price (p x odds ~1.02). With 2-0 records the
    # model claims p x odds ~1.09, which recommend.MAX_VALUE rejects as an unbacked disagreement.
    provider = FakeProvider(matches, lambda match: history_for(match, goals=(2, 1)))
    plan, store = run_plan(tmp_path, provider)
    day = plan["days"][0]
    assert len(provider.h2h_calls) == 1
    assert day["ticket"] is not None and plan["status"] == "ready"
    (leg,) = day["ticket"]["legs"]
    assert leg["odds"] == matches[int(leg["match_id"][1:])].odds[leg["key"]]
    assert 1.8 <= day["ticket"]["total_odds"] <= 2.2
    # Real fixtures and history are stored; no prediction enters the prospective ledger.
    assert store.predictions() == [] and store.assessment_count() == 0


@pytest.mark.parametrize("status", [429, 503])
def test_quota_or_key_errors_abort_the_plan(tmp_path, status):
    provider = FakeProvider(day_matches(12), lambda m: ProviderError("stop", status))
    plan, _ = run_plan(tmp_path, provider)
    assert len(provider.h2h_calls) == 1
    assert plan["status"] == "failed" and plan["message"] == "stop"
    assert plan["days"][0]["status"] == "failed"


@pytest.mark.parametrize("error", [ProviderError("down", 502), ProviderError("missing", 404)])
def test_other_provider_errors_skip_that_match_and_continue(tmp_path, error):
    provider = FakeProvider(day_matches(12), lambda m: error)
    plan, _ = run_plan(tmp_path, provider)
    day = plan["days"][0]
    assert len(provider.h2h_calls) == ENRICH_BUDGET
    assert day["diagnostics"]["history_errors"] == ENRICH_BUDGET
    assert len(plan["warnings"]) == ENRICH_BUDGET
    assert plan["status"] == "partial"


def test_unexpected_errors_fail_the_plan_but_keep_it_saved(tmp_path):
    provider = FakeProvider(day_matches(3), lambda m: RuntimeError("boom"))
    plan, _ = run_plan(tmp_path, provider)
    assert plan["status"] == "failed" and "boom" not in plan["message"]
    assert plan["days"][0]["status"] == "failed"


def test_fixtures_quota_error_stops_after_one_request(tmp_path):
    class Quota(FakeProvider):
        async def fixtures(self, day, refresh=False, ttl=None):
            self.fixture_calls += 1
            raise ProviderError("quota", 429)

    provider = Quota([], lambda m: [])
    plan, _ = run_plan(tmp_path, provider, mode="week")
    assert provider.fixture_calls == 1
    assert plan["status"] == "failed"
    assert all(day["status"] == "failed" for day in plan["days"])


def test_no_fixtures_explains_why(tmp_path):
    plan, _ = run_plan(tmp_path, FakeProvider([], lambda m: []))
    day = plan["days"][0]
    assert day["ticket"] is None and "Nu există meciuri viitoare" in day["reason"]


def test_matches_without_odds_are_never_analysed_or_enriched(tmp_path):
    matches = [m.model_copy(update={"odds": {}}) for m in day_matches(5)]
    provider = FakeProvider(matches, lambda m: [])
    plan, _ = run_plan(tmp_path, provider)
    assert provider.h2h_calls == []
    assert "cote" in plan["days"][0]["reason"]


# --- settlement of saved plans ------------------------------------------------------------


def saved_plan(store, legs, plan_id="p", demo=False, status="ready"):
    store.save_plan(
        {
            "id": plan_id,
            "created": 1,
            "status": status,
            "request": {"demo": demo},
            "days": [{"ticket": {"legs": legs, "status": "pending"}}],
        }
    )


def leg(match_id, key="1"):
    return {"match_id": match_id, "key": key, "status": "pending"}


def final(match_id, home, away, status="finished"):
    return Match(
        id=match_id,
        kickoff=datetime.now(timezone.utc) - timedelta(days=1),
        league="L",
        home=f"H{match_id}",
        away=f"A{match_id}",
        status=status,
        home_goals=home if status == "finished" else None,
        away_goals=away if status == "finished" else None,
    )


@pytest.mark.parametrize(
    "scores,expected",
    [
        ({"a": (1, 0), "b": (2, 0)}, "won"),
        ({"a": (1, 0), "b": (0, 1)}, "lost"),
        ({"a": (0, 1)}, "lost"),  # one lost leg decides the ticket even while b is pending
        ({"a": (1, 0)}, "pending"),
        ({}, "pending"),
    ],
)
def test_ticket_status_follows_its_legs(tmp_path, scores, expected):
    store = Store(tmp_path / "settle.db")
    saved_plan(store, [leg("a"), leg("b")])
    store.save_matches([final(k, *v) for k, v in scores.items()])
    settle_plans(store)
    ticket = store.plan("p")["days"][0]["ticket"]
    assert ticket["status"] == expected
    settle_plans(store)
    assert store.plan("p")["days"][0]["ticket"] == ticket


def test_demo_and_generating_plans_are_never_settled(tmp_path):
    store = Store(tmp_path / "settle.db")
    saved_plan(store, [leg("a")], "demo", demo=True)
    saved_plan(store, [leg("a")], "busy", status="generating")
    store.save_matches([final("a", 1, 0)])
    settle_plans(store)
    for plan_id in ("demo", "busy"):
        assert store.plan(plan_id)["days"][0]["ticket"]["legs"][0]["status"] == "pending"
