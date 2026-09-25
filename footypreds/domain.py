import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Highest plausible final score per sport: goals, points (incl. overtime), sets won.
SCORE_LIMITS = {"football": 50, "basketball": 400, "tennis": 5}
# How a finished game ended when it was not a plain full-time result.
FINISH_TYPES = ("", "aet", "penalties", "retired", "walkover")
# Team crests, league logos and player flags come only from these exact https hosts.
IMAGE_HOSTS = frozenset({"static.flashscore.com", "flagcdn.com"})
IMAGE_URL_MAX = 300
_IMAGE_PATH = re.compile(r"/[A-Za-z0-9._~/-]{1,250}")


def image_url(value):
    """`value` when it is a plain https URL of an image on an allowed host, else None.

    Strict on purpose (the URL is later fetched by the /api/img proxy): printable ASCII only,
    scheme https, host exactly one of IMAGE_HOSTS (no userinfo, no port, no uppercase), a
    simple path without dot segments, no query and no fragment.
    """
    if not isinstance(value, str) or not value or len(value) > IMAGE_URL_MAX:
        return None
    if any(not " " < char <= "~" for char in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc not in IMAGE_HOSTS:
        return None
    if parts.query or parts.fragment or "?" in value or "#" in value:
        return None
    path = parts.path
    if not _IMAGE_PATH.fullmatch(path) or "//" in path or "/." in path:
        return None
    url = f"https://{parts.netloc}{path}"
    return url if url == value else None


class Match(BaseModel):
    # Immutable: the store's cache and the HistoryIndex share these objects with every
    # caller, so an in-place edit would silently corrupt later reads. Use model_copy().
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=120)
    kickoff: datetime
    league: str = Field(min_length=1, max_length=160)
    country: str = ""
    home: str = Field(min_length=1, max_length=120)
    away: str = Field(min_length=1, max_length=120)
    home_id: str = ""
    away_id: str = ""
    status: str = "scheduled"
    # Goals (football), points incl. overtime (basketball) or sets won (tennis); the per-sport
    # upper bound is SCORE_LIMITS, checked below.
    home_goals: int | None = Field(default=None, ge=0, le=400)
    away_goals: int | None = Field(default=None, ge=0, le=400)
    odds: dict[str, float] = Field(default_factory=dict)
    source: str = "flashscore"
    sport: str = "football"
    # FlashScore eventParticipantId of each side: the key of the matches/odds rows.
    home_participant_id: str = ""
    away_participant_id: str = ""
    # In-play details of a live game: stage, clock, minute, period, red_cards.
    live: dict[str, Any] = Field(default_factory=dict)
    finish_type: str = ""
    # Original upstream image URLs (domain.image_url); anything else is dropped to None. API
    # responses expose them as same-origin /api/img display URLs (footypreds/media.py).
    home_logo: str | None = None
    away_logo: str | None = None
    league_logo: str | None = None

    @field_validator("home_logo", "away_logo", "league_logo", mode="before")
    @classmethod
    def allowed_image(cls, value):
        # A bad logo never costs the fixture: it is simply dropped.
        return image_url(value)

    @field_validator("kickoff")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Ora trebuie să includă fusul orar (ex. +00:00).")
        return value

    @field_validator("odds")
    @classmethod
    def valid_odds(cls, value):
        return {k: v for k, v in value.items() if math.isfinite(v) and 1 < v < 1001}

    @field_validator("sport")
    @classmethod
    def known_sport(cls, value):
        if value not in SCORE_LIMITS:
            raise ValueError("Sport necunoscut.")
        return value

    @field_validator("finish_type")
    @classmethod
    def known_finish(cls, value):
        if value not in FINISH_TYPES:
            raise ValueError("Tip de final necunoscut.")
        return value

    @model_validator(mode="after")
    def finished_score(self):
        if self.status == "finished" and (self.home_goals is None or self.away_goals is None):
            raise ValueError("Un rezultat final trebuie să aibă ambele scoruri.")
        if self.home == self.away:
            raise ValueError("Echipele trebuie să fie diferite.")
        limit = SCORE_LIMITS[self.sport]
        if any(score is not None and score > limit for score in (self.home_goals, self.away_goals)):
            raise ValueError("Scor imposibil pentru acest sport.")
        return self
