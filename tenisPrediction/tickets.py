"""Bilete compuse cu o cotă totală minimă aleasă de utilizator.

Selecțiile vin din pagină, cu probabilitatea afișată (modelul v3 amestecat cu cotele la
câștigător, modelul de bază la celelalte piețe) și cota reală. Optimizatorul din
`footypreds.recommend` alege combinația cu cea mai mare probabilitate combinată a cărei cotă
totală este cel puțin cota minimă (și cel mult MAX_RATIO × cota minimă), cu cel mult o selecție
pe meci și niciodată același jucător de două ori. Probabilitatea biletului este produsul
probabilităților (meciuri presupuse independente).
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field

from footypreds.recommend import ASSUMPTION, LEGS_LIMIT, auto_max_legs, conflict_groups, optimize
from footypreds.sports.legs import ticket_status

# Fereastra cotei totale, relativ la cota minimă: niciodată sub ea; plafonul oprește biletele
# care urcă mult peste țintă doar pentru că ultima selecție are cotă mare.
MAX_RATIO = 1.5
# Cotele unei selecții luate în calcul: sub 1.08 adaugă risc fără cotă, peste 6 e loterie.
LEG_ODDS = (1.08, 6.0)
ALTERNATIVES = 2
DISCLAIMER = "Estimări statistice, nu garanții. Pariază responsabil. 18+."


class TicketLeg(BaseModel):
    match_id: str = Field(max_length=120)
    key: str = Field(max_length=60)
    label: str = Field(max_length=160)
    group: str = Field(default="", max_length=80)
    probability: float = Field(gt=0, le=1)
    odds: float = Field(gt=1, lt=1001)
    kickoff: str = Field(default="", max_length=40)
    home: str = Field(default="", max_length=100)
    away: str = Field(default="", max_length=100)
    competition: str = Field(default="", max_length=200)
    # rezultatul selecției pe zilele încheiate (bilet retroactiv); None = nedecontat
    won: bool | None = None


class TicketRequest(BaseModel):
    min_odds: float = Field(ge=1.1, le=1000)
    max_legs: int | None = Field(default=None, ge=1, le=LEGS_LIMIT)
    legs: list[TicketLeg] = Field(max_length=3000)


def eligible(legs: list[TicketLeg]) -> list[dict]:
    """Selecțiile acceptate, ca dicționare pentru optimizator."""
    low, high = LEG_ODDS
    return [leg.model_dump() for leg in legs if low <= leg.odds <= high]


def summary(chosen: list[dict], min_odds: float) -> dict:
    total = math.prod(item["odds"] for item in chosen)
    probability = math.prod(item["probability"] for item in chosen)
    statuses = [
        "pending" if item["won"] is None else ("won" if item["won"] else "lost") for item in chosen
    ]
    return {
        "legs": chosen,
        "total_odds": round(total, 4),
        "probability": round(probability, 6),
        "ev": round(probability * total - 1, 6),
        "status": ticket_status(statuses),
        "min_odds": min_odds,
    }


def reason_without_ticket(legs: list[dict], min_odds: float, max_legs: int) -> str:
    if not legs:
        return (
            "Nu există selecții eligibile: e nevoie de meciuri cu cote reale între "
            f"{LEG_ODDS[0]:.2f} și {LEG_ODDS[1]:.0f}. Schimbă filtrele sau alege altă zi."
        )
    tops = sorted(max(item["odds"] for item in group) for group in conflict_groups(legs))
    reachable = math.prod(tops[-max_legs:])
    if reachable < min_odds:
        return (
            f"Cu cel mult {max_legs} selecții cota maximă posibilă este {reachable:.2f}, sub "
            f"cota minimă {min_odds:g}. Mărește numărul de selecții sau alege o cotă mai mică."
        )
    return (
        f"Nicio combinație nu ajunge între {min_odds:.2f} și {min_odds * MAX_RATIO:.2f}. "
        "Încearcă altă cotă minimă sau mai multe selecții."
    )


def build(request: TicketRequest) -> dict:
    """Biletul cel mai probabil cu cota ≥ min_odds, plus alternative pe meciuri diferite."""
    min_odds = request.min_odds
    max_legs = max(1, min(LEGS_LIMIT, request.max_legs or auto_max_legs(min_odds)))
    legs = eligible(request.legs)
    tickets = []
    used: set[str] = set()
    while len(tickets) <= ALTERNATIVES:
        rest = [item for item in legs if item["match_id"] not in used]
        chosen = optimize(rest, min_odds, max_legs, window=(1.0, MAX_RATIO))
        if not chosen:
            break
        tickets.append(summary(chosen, min_odds))
        used |= {item["match_id"] for item in chosen}
    return {
        "ticket": tickets[0] if tickets else None,
        "alternatives": tickets[1:],
        "min_odds": min_odds,
        "max_odds": round(min_odds * MAX_RATIO, 4),
        "max_legs": max_legs,
        "candidates": len(legs),
        "matches": len({item["match_id"] for item in legs}),
        "reason": None if tickets else reason_without_ticket(legs, min_odds, max_legs),
        "assumption": ASSUMPTION,
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
