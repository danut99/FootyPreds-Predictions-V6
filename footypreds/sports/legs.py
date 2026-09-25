"""Shared bet building blocks: legs, tickets and their settlement (docs/CONTRACTS.md).

Recommendations, the ticket generator, the wallet and the simulator all speak this shape, so
a leg produced by one can be settled, displayed or staked by any other.
"""

import math
from datetime import datetime, timezone

from footypreds.competitions import competition_name, match_competition
from footypreds.media import match_media
from footypreds.sports.keys import market_margin
from footypreds.sports.settle import settle

LEG_STATUSES = ("pending", "won", "lost", "void")
TICKET_STATUSES = ("pending", "won", "lost", "void", "unavailable")


def leg(match, analysis, market):
    """One selection on one match, with the price it was taken at."""
    return {
        "match_id": match.id,
        "sport": match.sport,
        "kickoff": match.kickoff.isoformat(),
        "competition": competition_name(match.league),
        "competition_id": match_competition(match),
        "home": match.home,
        "away": match.away,
        "key": market["key"],
        "label": market["label"],
        "group": market["group"],
        "probability": market["probability"],
        "odds": market["odds"],
        "fair_odds": market["fair_odds"],
        "ev": market["ev"],
        "grade": analysis["grade"],
        "confidence": analysis["confidence"],
        "status": "pending",
        "score": None,
        # Same-origin display URLs of the crests/flags and league logo (or None).
        **match_media(match),
    }


def candidate_legs(match, analysis, now=None):
    """Bettable legs of one pre-match fixture: selectable markets with a real price.

    Grade A-C: every priced market. Grade D (thin form data): only markets whose every outcome
    is priced, because there the probability is anchored to the bookmakers' margin-free price
    and the leg carries a known `margin`. Never a live, finished or started game (no result may
    be known when a leg is chosen).
    """
    now = now or datetime.now(timezone.utc)
    if match.status != "scheduled" or match.kickoff <= now:
        return []
    output = []
    for market in analysis["markets"]:
        if not (market["selectable"] and market["odds"] is not None and market["odds"] > 1):
            continue
        margin = market_margin(match.sport, market["key"], match.odds)
        if analysis["grade"] == "D" and margin is None:
            continue
        output.append(leg(match, analysis, market) | {"margin": margin})
    return output


def ticket(legs, target_odds=None, day=None, reason=None):
    """Ticket dict from legs (independence approximation for the probability)."""
    legs = list(legs)
    if not legs:
        return {
            "day": day.isoformat() if day else None,
            "target_odds": target_odds,
            "total_odds": None,
            "probability": None,
            "ev": None,
            "legs": [],
            "status": "unavailable",
            "reason": reason or "Nu există selecții potrivite.",
        }
    total = math.prod(item["odds"] for item in legs)
    probability = math.prod(item["probability"] for item in legs)
    return {
        "day": day.isoformat() if day else None,
        "target_odds": target_odds,
        "total_odds": total,
        "probability": probability,
        "ev": probability * total - 1,
        "legs": legs,
        "status": ticket_status([item["status"] for item in legs]),
        "reason": reason,
    }


def settle_leg(item, match):
    """Leg with status/score from a finished match (unchanged while not final)."""
    if match is None or match.status not in ("finished", "unavailable"):
        return item
    if match.status == "unavailable":
        return item | {"status": "void"}
    won = settle(
        match.sport,
        item["key"],
        match.home_goals,
        match.away_goals,
        match.finish_type or match.status,
    )
    status = "void" if won is None else "won" if won else "lost"
    return item | {"status": status, "score": f"{match.home_goals}-{match.away_goals}"}


def ticket_status(statuses):
    """Any lost leg loses; all void is void; all won/void wins; otherwise pending."""
    statuses = list(statuses)
    if not statuses:
        return "unavailable"
    if "lost" in statuses:
        return "lost"
    if "pending" in statuses:
        return "pending"
    if all(s == "void" for s in statuses):
        return "void"
    return "won"


def settled_odds(legs):
    """Payout multiplier of a finished ticket: void legs count as 1.0."""
    return math.prod(1.0 if item["status"] == "void" else item["odds"] for item in legs)
