"""Shared legs/tickets: candidate rules, ticket maths and settlement."""

from datetime import datetime, timedelta, timezone

import pytest

from footypreds.domain import Match
from footypreds.sports import analyze_match
from footypreds.sports.legs import (
    candidate_legs,
    settle_leg,
    settled_odds,
    ticket,
    ticket_status,
)

NOW = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)


def basketball(**changes):
    data = dict(
        id="b1",
        kickoff=NOW + timedelta(hours=6),
        league="USA: NBA",
        country="USA",
        home="A",
        away="B",
        sport="basketball",
        odds={"1": 1.5, "2": 2.6, "over_180.5": 1.9, "under_180.5": 1.9},
    )
    return Match(**(data | changes))


def sufficient(analysis):
    return analysis | {"grade": "B", "quality": "sufficient"}


def test_candidate_legs_need_a_price_a_grade_and_a_future_kickoff():
    match = basketball()
    analysis = sufficient(analyze_match(match, []))
    legs = candidate_legs(match, analysis, NOW)
    assert {leg["key"] for leg in legs} == {"1", "2", "over_180.5", "under_180.5"}
    first = next(leg for leg in legs if leg["key"] == "1")
    assert first["odds"] == 1.5 and first["sport"] == "basketball"
    assert first["competition_id"] == "basketball:usa|nba" and first["status"] == "pending"
    assert first["ev"] == pytest.approx(first["probability"] * 1.5 - 1)
    assert candidate_legs(match, analysis | {"grade": "D"}, NOW) == []
    assert candidate_legs(match, analysis, match.kickoff) == []
    live = match.model_copy(update={"status": "live"})
    assert candidate_legs(live, analysis, NOW) == []


def test_ticket_maths_and_unavailable_ticket():
    match = basketball()
    legs = candidate_legs(match, sufficient(analyze_match(match, [])), NOW)
    one, two = legs[0], legs[1] | {"match_id": "b2"}
    built = ticket([one, two], target_odds=4, day=NOW.date())
    assert built["total_odds"] == pytest.approx(one["odds"] * two["odds"])
    assert built["probability"] == pytest.approx(one["probability"] * two["probability"])
    assert built["status"] == "pending" and built["day"] == "2026-06-01"
    empty = ticket([], target_odds=100, reason="Nimic")
    assert empty["status"] == "unavailable" and empty["reason"] == "Nimic"


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["won", "won"], "won"),
        (["won", "lost"], "lost"),
        (["won", "pending"], "pending"),
        (["void", "won"], "won"),
        (["void", "void"], "void"),
        (["lost", "pending"], "lost"),
        ([], "unavailable"),
    ],
)
def test_ticket_status(statuses, expected):
    assert ticket_status(statuses) == expected


def test_settle_leg_for_every_outcome():
    match = basketball()
    item = next(
        leg
        for leg in candidate_legs(match, sufficient(analyze_match(match, [])), NOW)
        if leg["key"] == "over_180.5"
    )
    final = match.model_copy(update={"status": "finished", "home_goals": 95, "away_goals": 90})
    assert settle_leg(item, final)["status"] == "won"
    assert settle_leg(item, final)["score"] == "95-90"
    low = final.model_copy(update={"home_goals": 80})
    assert settle_leg(item, low)["status"] == "lost"
    assert settle_leg(item, match)["status"] == "pending"
    assert settle_leg(item, None)["status"] == "pending"
    off = match.model_copy(update={"status": "unavailable"})
    assert settle_leg(item, off)["status"] == "void"
    legs = [item | {"status": "void"}, item | {"status": "won", "odds": 2.0}]
    assert settled_odds(legs) == 2.0
