"""Aplicație web separată pentru modelul compact de tenis."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from footypreds.api import create_app as create_footypreds_app

from .model import CompactTennisModel

ROOT = Path(__file__).parent
WEB = ROOT / "web"
DATA = ROOT / "tml-data"


@lru_cache(maxsize=1)
def trained_model() -> CompactTennisModel:
    return CompactTennisModel.train_dataset(DATA, end_year=2025, seasons=4, stats_scale=8.0)


class ModelMatch(BaseModel):
    id: str = Field(max_length=120)
    home: str = Field(max_length=100)
    away: str = Field(max_length=100)
    surface: str = Field(default="Hard", pattern="^(Hard|Clay|Grass|Carpet)$")
    market_probability: float | None = Field(default=None, gt=0, lt=1)


def blend_probability(model_probability, market_probability, model_weight=0.10):
    if market_probability is None:
        return model_probability
    logit_model = math.log(model_probability / (1 - model_probability))
    logit_market = math.log(market_probability / (1 - market_probability))
    return 1 / (1 + math.exp(-((model_weight * logit_model) + ((1 - model_weight) * logit_market))))


def create_app(*, include_core: bool = True) -> FastAPI:
    app = FastAPI(title="TenisPrediction", version="1.0")
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    if include_core:
        app.mount("/core", create_footypreds_app(), name="footypreds-core")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        model = trained_model()
        return {"status": "ok", "players": len(model.played), "trained_through": "2025"}

    @app.get("/api/players")
    def players(q: str = Query("", max_length=80)):
        needle = " ".join(q.casefold().split())
        names = [name for name in trained_model().played if needle in name]
        names.sort(key=lambda name: trained_model().played[name], reverse=True)
        return {"players": names[:12]}

    @app.post("/api/tml-probabilities")
    def tml_probabilities(matches: list[ModelMatch]):
        model = trained_model()
        output = []
        for match in matches[:400]:
            home_key = model.resolve_player(match.home)
            away_key = model.resolve_player(match.away)
            known = home_key in model.played and away_key in model.played
            probability = model.probability(match.home, match.away, match.surface)
            blended = (
                blend_probability(probability, match.market_probability)
                if known
                else match.market_probability
            )
            output.append({
                "id": match.id,
                "model_probability": round(probability, 6) if known else None,
                "probability": round(blended, 6) if blended is not None else None,
                "known": known,
                "experience": min(model.played.get(home_key, 0), model.played.get(away_key, 0)),
            })
        return {"matches": output, "model_weight": 0.10, "stats_scale": model.stats_scale}

    @app.get("/api/predict")
    def predict(
        player_1: str = Query(min_length=2, max_length=80),
        player_2: str = Query(min_length=2, max_length=80),
        surface: str = Query("Hard", pattern="^(Hard|Clay|Grass|Carpet)$"),
        rank_1: int | None = Query(None, ge=1, le=3000),
        rank_2: int | None = Query(None, ge=1, le=3000),
        threshold: float = Query(0.72, ge=0.5, le=0.95),
    ):
        return trained_model().predict(
            player_1, player_2, surface, rank_1, rank_2, threshold
        ).as_dict()

    return app


app = create_app()
