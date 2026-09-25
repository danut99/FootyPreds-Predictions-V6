"""HTTP API of the daily AI recommendations and the one-click ticket generator.

GET  /api/recommendations?day&sports=football,basketball,tennis&targets=2,5,10,100&refresh
GET  /api/recommendations/history?sports&days
POST /api/tickets/generate {day, target_odds, sports, max_legs?, exclude_match_ids?}

No endpoint takes a minimum probability: the optimizer picks the likeliest combination of
real priced legs for the requested odds (footypreds/recommend.py, docs/CONTRACTS.md §10.2).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from footypreds import recommend
from footypreds.media import with_leg_media
from footypreds.sports import SPORTS

router = APIRouter(prefix="/api", tags=["recommendations"])
ALL_SPORTS = ",".join(SPORTS)
DEFAULT_TARGETS = ",".join(f"{t:g}" for t in recommend.TARGETS)


class GenerateRequest(BaseModel):
    # Unknown fields (e.g. an old "min_probability") are refused, not silently ignored.
    model_config = ConfigDict(extra="forbid")

    day: date
    target_odds: float = Field(ge=1.2, le=1000, allow_inf_nan=False)
    sports: list[str] = Field(default_factory=lambda: list(SPORTS), min_length=1, max_length=3)
    max_legs: int | None = Field(default=None, ge=1, le=recommend.LEGS_LIMIT)
    exclude_match_ids: list[str] = Field(default_factory=list, max_length=300)

    @field_validator("sports")
    @classmethod
    def known_sports(cls, value):
        return recommend.parse_sports(value)


def sports_of(text):
    try:
        return recommend.parse_sports(text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/recommendations")
async def recommendations(
    request: Request,
    day: date,
    sports: Annotated[str, Query(max_length=60)] = ALL_SPORTS,
    targets: Annotated[str, Query(max_length=80)] = DEFAULT_TARGETS,
    refresh: bool = False,
):
    chosen = sports_of(sports)
    try:
        values = recommend.parse_targets(targets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    found = await recommend.recommendations(request.app.state, day, chosen, values, refresh)
    # Legs stored before logos existed get them from the stored match (media.py).
    return with_leg_media(found, request.app.state.store)


@router.get("/recommendations/history")
def recommendations_history(
    request: Request,
    sports: Annotated[str, Query(max_length=60)] = ALL_SPORTS,
    days: Annotated[int, Query(ge=1, le=365)] = 60,
):
    return recommend.history(request.app.state.store, sports_of(sports), days)


@router.post("/tickets/generate")
async def generate_ticket(request: Request, body: GenerateRequest):
    found = await recommend.generate(
        request.app.state,
        body.day,
        body.target_odds,
        body.sports,
        body.max_legs,
        body.exclude_match_ids,
    )
    return with_leg_media(found, request.app.state.store)
