"""Bilete compuse cu cotă minimă (tenisPrediction.tickets și POST /api/ticket)."""

import itertools
import math
import random

from fastapi.testclient import TestClient

from tenisPrediction import tickets
from tenisPrediction.app import create_app

from .test_compact_tennis import named_predictor


def leg(match, key, probability, odds, won=None, home=None, away=None):
    return tickets.TicketLeg(
        match_id=str(match),
        key=key,
        label=f"{key} {match}",
        probability=probability,
        odds=odds,
        kickoff=f"2026-09-29T{10 + int(match) % 10:02d}:00:00+00:00",
        home=home or f"Home {match}",
        away=away or f"Away {match}",
        won=won,
    )


def request(legs, min_odds, max_legs=None):
    return tickets.TicketRequest(min_odds=min_odds, max_legs=max_legs, legs=legs)


def test_ticket_reaches_the_minimum_odds_with_one_leg_per_match():
    legs = [
        leg(1, "1", 0.80, 1.20),
        leg(1, "2", 0.20, 4.50),
        leg(2, "1", 0.75, 1.30),
        leg(3, "2", 0.70, 1.40),
        leg(4, "1", 0.60, 1.60),
    ]
    found = tickets.build(request(legs, 2.0))
    ticket = found["ticket"]
    assert ticket["total_odds"] >= 2.0
    assert ticket["total_odds"] <= 2.0 * tickets.MAX_RATIO
    ids = [item["match_id"] for item in ticket["legs"]]
    assert len(ids) == len(set(ids))
    assert math.isclose(
        ticket["probability"],
        math.prod(item["probability"] for item in ticket["legs"]),
        rel_tol=1e-5,
    )
    assert ticket["status"] == "pending"


def test_ticket_is_the_most_likely_combination_brute_force():
    rng = random.Random(7)
    legs = []
    for match in range(8):
        for key in ("1", "2"):
            odds = round(rng.uniform(1.1, 3.5), 2)
            legs.append(leg(match, key, min(0.97, 1 / odds * rng.uniform(0.95, 1.05)), odds))
    min_odds = 4.0
    found = tickets.build(request(legs, min_odds, max_legs=4))
    best = 0.0
    for size in range(1, 5):
        for combo in itertools.combinations(legs, size):
            if len({item.match_id for item in combo}) < size:
                continue
            total = math.prod(item.odds for item in combo)
            if min_odds <= total <= min_odds * tickets.MAX_RATIO:
                best = max(best, math.prod(item.probability for item in combo))
    assert best > 0
    assert math.isclose(found["ticket"]["probability"], best, rel_tol=1e-4)


def test_alternatives_use_completely_different_matches():
    legs = [leg(match, "1", 0.72, 1.45) for match in range(12)]
    found = tickets.build(request(legs, 2.0))
    used = [item["match_id"] for item in found["ticket"]["legs"]]
    for alternative in found["alternatives"]:
        ids = [item["match_id"] for item in alternative["legs"]]
        assert not set(ids) & set(used)
        used += ids
    assert len(found["alternatives"]) == tickets.ALTERNATIVES


def test_same_player_never_appears_twice():
    legs = [
        leg(1, "1", 0.8, 1.5, home="Sinner J.", away="Alcaraz C."),
        leg(2, "1", 0.8, 1.5, home="Sinner J.", away="Zverev A."),
    ]
    found = tickets.build(request(legs, 2.0, max_legs=2))
    assert found["ticket"] is None
    assert found["reason"]


def test_impossible_target_explains_why():
    legs = [leg(match, "1", 0.8, 1.2) for match in range(3)]
    found = tickets.build(request(legs, 50.0, max_legs=3))
    assert found["ticket"] is None
    assert "cota maximă posibilă" in found["reason"]


def test_legs_outside_the_accepted_odds_are_ignored():
    legs = [leg(1, "1", 0.99, 1.01), leg(2, "1", 0.05, 15.0), leg(3, "1", 0.6, 1.7)]
    found = tickets.build(request(legs, 1.5))
    assert found["candidates"] == 1
    assert [item["match_id"] for item in found["ticket"]["legs"]] == ["3"]


def test_retroactive_ticket_is_settled_from_the_legs():
    won = [leg(1, "1", 0.7, 1.5, won=True), leg(2, "1", 0.7, 1.5, won=True)]
    assert tickets.build(request(won, 2.0))["ticket"]["status"] == "won"
    lost = [leg(1, "1", 0.7, 1.5, won=True), leg(2, "1", 0.7, 1.5, won=False)]
    assert tickets.build(request(lost, 2.0))["ticket"]["status"] == "lost"


def test_ticket_endpoint():
    payload = {
        "min_odds": 2.5,
        "max_legs": 3,
        "legs": [
            {
                "match_id": str(match),
                "key": "1",
                "label": "Jucător 1",
                "probability": 0.7,
                "odds": 1.4,
                "home": f"A{match}",
                "away": f"B{match}",
            }
            for match in range(6)
        ],
    }
    with TestClient(create_app(include_core=False, predictor=named_predictor())) as http:
        body = http.post("/api/ticket", json=payload).json()
        assert body["ticket"]["total_odds"] >= 2.5
        assert len(body["ticket"]["legs"]) == 3
        assert body["max_legs"] == 3
        too_low = http.post("/api/ticket", json=payload | {"min_odds": 1.0})
        assert too_low.status_code == 422
