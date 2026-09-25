import math
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class Match(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    kickoff: datetime
    league: str = Field(min_length=1, max_length=160)
    country: str = ""
    home: str = Field(min_length=1, max_length=120)
    away: str = Field(min_length=1, max_length=120)
    home_id: str = ""
    away_id: str = ""
    status: str = "scheduled"
    home_goals: int | None = Field(default=None, ge=0, le=50)
    away_goals: int | None = Field(default=None, ge=0, le=50)
    odds: dict[str, float] = Field(default_factory=dict)
    source: str = "flashscore"

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

    @model_validator(mode="after")
    def finished_score(self):
        if self.status == "finished" and (self.home_goals is None or self.away_goals is None):
            raise ValueError("Un rezultat final trebuie să aibă ambele scoruri.")
        if self.home == self.away:
            raise ValueError("Echipele trebuie să fie diferite.")
        return self
