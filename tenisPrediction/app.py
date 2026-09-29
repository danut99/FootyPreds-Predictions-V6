"""Aplicație web separată pentru modelul de tenis v3 (tenisPrediction)."""

from __future__ import annotations

import logging
import math
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import tickets
from .model import (
    NO_BET,
    ODDS_MARGIN,
    PRODUCTION_ODDS_WEIGHTS,
    VERSION,
    TennisPredictor,
    market_probability_of,
    match_facts,
)

ROOT = Path(__file__).parent
WEB = ROOT / "web"
DATA = ROOT / "tml-data"
LOG = logging.getLogger(__name__)

# Amestec model/piață pe scara logit (v3, vezi EXPERIMENTS.md): p = sigmoid(w_model·logit(p_model)
# + w_piață·logit(p_piață)), cu marja scoasă prin metoda „power”. Pe cotele de ÎNCHIDERE
# tennis-data.co.uk (media pieței AvgW/AvgL, nu Pinnacle) ponderile potrivite walk-forward sunt
# ~(0.1, 0.9); cotele FlashScore de dinaintea meciului sunt mai timpurii și mai zgomotoase, iar cu
# zgomot pe logit-ul pieței optimul se mută spre (0.25–0.35, 0.6–0.7). De aceea aplicația folosește
# perechea conservatoare PRODUCTION_ODDS_WEIGHTS, validată și ea în benchmark (2021–2023, confirmată
# pe 2024). Fără cote, probabilitatea afișată este cea a modelului v3. Decizia „selectează” și
# `pick` vin din probabilitatea amestecată (cea afișată), iar modelul de producție urmărește în
# fereastra rulantă tot amestecul (antrenare cu cotele de închidere când fișierele există).
MARKET_WEIGHTS = PRODUCTION_ODDS_WEIGHTS
# cotele de închidere tennis-data.co.uk folosite la antrenare (footypreds/data/benchmark/tennis/raw,
# descărcate cu `python -m footypreds.evaluation.tennis_eval --download`); lipsa lor = model pur
TRAIN_ODDS = "avg"
LOADING = "Modelul v3 se antrenează (câteva minute la prima pornire); reîncearcă în curând."

_lock = threading.Lock()
_warm_lock = threading.Lock()
_warm_thread: threading.Thread | None = None
_trained: TennisPredictor | None = None


def trained_model() -> TennisPredictor:
    """Predictorul antrenat (din cache-ul pickle, reantrenat când se schimbă datele)."""
    global _trained
    with _lock:
        if _trained is None:
            _trained = TennisPredictor.load_or_train(
                DATA, odds=TRAIN_ODDS, odds_weights=MARKET_WEIGHTS
            )
        return _trained


def _warm_up() -> None:
    try:
        trained_model()
    except Exception:  # noqa: BLE001 - the next request retries; never kill the server
        LOG.exception("Antrenarea modelului tenisPrediction a eșuat.")


def ensure_warming() -> None:
    """Start the (single) background training thread unless the model is ready or loading."""
    global _warm_thread
    with _warm_lock:
        if _trained is not None or (_warm_thread is not None and _warm_thread.is_alive()):
            return
        _warm_thread = threading.Thread(target=_warm_up, name="tenis-warm-up", daemon=True)
        _warm_thread.start()


class ModelMatch(BaseModel):
    id: str = Field(max_length=120)
    home: str = Field(max_length=100)
    away: str = Field(max_length=100)
    surface: str = Field(default="Hard", pattern="^(Hard|Clay|Grass|Carpet)$")
    league: str | None = Field(default=None, max_length=200)
    # cotele reale 1/2 (zecimale); din ele se scoate marja cu metoda validată
    odds_1: float | None = Field(default=None, gt=1, lt=1001)
    odds_2: float | None = Field(default=None, gt=1, lt=1001)
    # sau direct probabilitatea fără marjă a jucătorului 1; None când nu există cote
    market_probability: float | None = Field(default=None, gt=0, lt=1)
    day: date | None = None

    def market(self) -> float | None:
        fair = market_probability_of(self.odds_1, self.odds_2, ODDS_MARGIN)
        return self.market_probability if fair is None else fair


def blend_probability(model_probability, market_probability, weights=MARKET_WEIGHTS):
    """Probabilitatea amestecată pe scara logit; fără preț de piață rămâne modelul."""
    if market_probability is None or not weights:
        return model_probability
    logit_model = math.log(model_probability / (1 - model_probability))
    logit_market = math.log(market_probability / (1 - market_probability))
    return 1 / (1 + math.exp(-(weights[0] * logit_model + weights[1] * logit_market)))


def create_app(
    *,
    include_core: bool = True,
    predictor: TennisPredictor | None = None,
    warm: bool = True,
) -> FastAPI:
    """``warm`` trains the model in a background thread at startup (no injected predictor),
    so the first request never holds the page while training runs."""

    @asynccontextmanager
    async def lifespan(_app):
        if warm and predictor is None:
            ensure_warming()
        yield

    app = FastAPI(title="TenisPrediction", version="3.0", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    if include_core:
        from footypreds.api import create_app as create_footypreds_app

        app.mount("/core", create_footypreds_app(), name="footypreds-core")

    def ready() -> TennisPredictor | None:
        return predictor if predictor is not None else _trained

    def model() -> TennisPredictor:
        current = ready()
        if current is None:
            ensure_warming()
            raise HTTPException(status_code=503, detail=LOADING)
        return current

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        current = ready()
        if current is None:
            ensure_warming()
            return {"status": "loading", "players": 0, "trained_through": None, "version": VERSION}
        return {
            "status": "ok",
            "players": current.player_count,
            "trained_through": current.trained_through,
            "version": VERSION,
        }

    @app.get("/api/players")
    def players(q: str = Query("", max_length=80)):
        return {"players": model().search(q)}

    @app.post("/api/tml-probabilities")
    def tml_probabilities(matches: list[ModelMatch]):
        current = model()
        output = []
        for match in matches[:400]:
            facts = {
                "surface": match.surface,
                "tour": None,
                "best_of": None,
                "level": None,
                "indoor": None,
                "validated": True,
            }
            if match.league:
                facts.update(match_facts(SimpleNamespace(league=match.league, source="")))
                facts["surface"] = facts["surface"] if "," in match.league else match.surface
            market = match.market()
            result = current.predict(
                match.home,
                match.away,
                facts["surface"],
                tour=facts["tour"],
                best_of=facts["best_of"],
                level=facts["level"],
                when=match.day,
                indoor=facts["indoor"],
                selectable=facts["validated"],
                market_probability=market,
                market_weights=MARKET_WEIGHTS,
            )
            known = result.key_1 is not None and result.key_2 is not None
            # the displayed probability is the blend (model alone without a price); `pick`,
            # "selectează" and the high-precision decision all come from that same number
            probability = result.probability_1 if known else market
            output.append(
                {
                    "id": match.id,
                    "model_probability": round(result.model_probability_1, 6) if known else None,
                    "market_probability": round(market, 6) if market is not None else None,
                    "probability": round(probability, 6) if probability is not None else None,
                    "known": known,
                    "pick": (("1" if probability >= 0.5 else "2") if known else None),
                    "pick_name": result.winner if known else None,
                    "decision": result.decision if known else NO_BET,
                    "decision_high": result.decision_high if known else NO_BET,
                    "validated": facts["validated"],
                    "threshold": round(result.threshold, 4) if result.threshold else None,
                    "threshold_high": (
                        round(result.threshold_high, 4) if result.threshold_high else None
                    ),
                    "experience": min(result.experience_1, result.experience_2),
                }
            )
        return {"matches": output, "market_weights": list(MARKET_WEIGHTS), "version": VERSION}

    @app.post("/api/ticket")
    def ticket(request: tickets.TicketRequest):
        # Nu are nevoie de model: primește selecțiile cu probabilitatea deja afișată în pagină.
        return tickets.build(request)

    @app.get("/api/predict")
    def predict(
        player_1: str = Query(min_length=2, max_length=80),
        player_2: str = Query(min_length=2, max_length=80),
        surface: str = Query("Hard", pattern="^(Hard|Clay|Grass|Carpet)$"),
        rank_1: int | None = Query(None, ge=1, le=3000),
        rank_2: int | None = Query(None, ge=1, le=3000),
        tour: str | None = Query(None, pattern="^(atp|wta|challenger)$"),
        best_of: int | None = Query(None, ge=3, le=5),
        threshold: float | None = Query(None, ge=0.5, le=0.95),
        day: date | None = None,
        odds_1: float | None = Query(None, gt=1, lt=1001),
        odds_2: float | None = Query(None, gt=1, lt=1001),
    ):
        return (
            model()
            .predict(
                player_1,
                player_2,
                surface,
                rank_1,
                rank_2,
                threshold,
                tour=tour,
                best_of=5 if best_of == 5 else (3 if best_of else None),
                when=day,
                market_probability=market_probability_of(odds_1, odds_2, ODDS_MARGIN),
                market_weights=MARKET_WEIGHTS,
            )
            .as_dict()
        )

    return app


app = create_app()
