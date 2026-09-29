"""Aplicație web separată pentru modelul de fotbal (fotbalPrediction), portul 8020.

    python -m uvicorn fotbalPrediction.app:app --host 127.0.0.1 --port 8020

Ca ``tenisPrediction/app.py``: servește pagina din ``web/`` și montează aplicația FootyPreds sub
``/core`` (programul zilei FlashScore, LIVE). Pagina cere tabla zilei de la
``/core/api/predictions?sport=football``, apoi trimite meciurile (cu cotele lor) la
``POST /api/fotbal-probabilities``; serverul întoarce toate piețele modelului, deciziile
„selectează”/„fără pariu” (80% și strict 85%) și, pentru meciurile încheiate, câștigat/pierdut.

Decontare: golurile din scorul final FlashScore (nu după prelungiri/penalty-uri: nedecontat);
pauza, cornerele, cartonașele și șuturile pe poartă NUMAI din rândul football-data al meciului,
când fișierul sezonului curent l-a primit (``python -m fotbalPrediction.data --download
--refresh``). Fără el piața rămâne „nedecontată” și nu intră în statistici. Zero apeluri RapidAPI
în plus. După un ``--refresh`` (sau o modificare a codului modelului) aplicația observă noua cheie
a pickle-ului (``ensure_fresh``, cel mult o dată la 5 minute) și reantrenează în fundal.

Predicțiile făcute înainte de începerea meciului se păstrează într-un jurnal SQLite
(``footypreds/data/fotbalPrediction/journal.sqlite3``); pentru o zi încheiată se afișează
predicția din jurnal. Fără jurnal, dacă modelul a fost antrenat și pe ziua respectivă (sau are deja
rândul football-data al meciului, ±1 zi), predicția este marcată „retroactivă” (modelul putea
vedea deja rezultatul) și nu intră în precizia zilei.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import markets as mk
from .model import (
    MAX_P,
    NO_BET,
    PRODUCTION_ODDS_WEIGHTS,
    STORE_DIR,
    THRESHOLD,
    THRESHOLD_HIGH,
    VERSION,
    FootballPredictor,
    predictor_key,
    settle,
)

ROOT = Path(__file__).parent
WEB = ROOT / "web"
JOURNAL = STORE_DIR / "journal.sqlite3"
LOG = logging.getLogger(__name__)
LOADING = "Modelul de fotbal se antrenează (câteva minute la prima pornire); reîncearcă în curând."
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}
EXTRA_TIME = ("aet", "penalties")
MAX_MATCHES = 400
# După `python -m fotbalPrediction.data --download --refresh` (sau o modificare a codului
# modelului) cheia pickle-ului se schimbă: aplicația o verifică cel mult o dată la 5 minute și
# reantrenează în fundal, servind între timp modelul vechi.
RELOAD_CHECK_SECONDS = 300.0

_lock = threading.Lock()
_warm_lock = threading.Lock()
_warm_thread: threading.Thread | None = None
_trained: FootballPredictor | None = None
_checked_at: float | None = None


def trained_model() -> FootballPredictor:
    """Predictorul antrenat (pickle în cache, reantrenat când se schimbă datele)."""
    global _trained
    with _lock:
        if _trained is None:
            _trained = FootballPredictor.load_or_train()
        return _trained


def _warm_up() -> None:
    try:
        trained_model()
    except Exception:  # noqa: BLE001 - the next request retries; never kill the server
        LOG.exception("Antrenarea modelului fotbalPrediction a eșuat.")


def ensure_warming() -> None:
    """Pornește (o singură dată) antrenarea în fundal, dacă modelul nu e gata sau în lucru."""
    global _warm_thread
    with _warm_lock:
        if _trained is not None or (_warm_thread is not None and _warm_thread.is_alive()):
            return
        _warm_thread = threading.Thread(target=_warm_up, name="fotbal-warm-up", daemon=True)
        _warm_thread.start()


def _reload(key: str) -> None:
    """Reantrenează (sau încarcă pickle-ul nou) și înlocuiește modelul servit."""
    global _trained
    try:
        fresh = FootballPredictor.load_or_train()
    except Exception:  # noqa: BLE001 - keep serving the previous model
        LOG.exception("Reîncărcarea modelului fotbalPrediction a eșuat.")
        return
    with _lock:
        _trained = fresh
    LOG.info("Model fotbalPrediction reîncărcat (%s -> %s).", key, fresh.key)


def ensure_fresh(clock: Callable[[], float] = time.monotonic) -> bool:
    """Dacă datele locale (sau codul modelului) s-au schimbat față de modelul servit, pornește
    reîncărcarea în fundal. Verifică cel mult o dată la ``RELOAD_CHECK_SECONDS``."""
    global _warm_thread, _checked_at
    with _warm_lock:
        if _trained is None or (_warm_thread is not None and _warm_thread.is_alive()):
            return False
        moment = clock()
        if _checked_at is not None and moment - _checked_at < RELOAD_CHECK_SECONDS:
            return False
        _checked_at = moment
        try:
            key = predictor_key()
        except OSError:
            return False
        if key == _trained.key:
            return False
        _warm_thread = threading.Thread(
            target=_reload, args=(_trained.key,), name="fotbal-reload", daemon=True
        )
        _warm_thread.start()
        return True


class Journal:
    """Predicțiile pre-meci (ultima versiune dinainte de start) pe id FlashScore."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        connection = sqlite3.connect(self.path, timeout=10)
        if not self._ready:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS predictions (match_id TEXT PRIMARY KEY, day TEXT, "
                "version TEXT, created TEXT, payload TEXT)"
            )
            self._ready = True
        return connection

    def get(self, ids: list[str]) -> dict[str, dict]:
        if self.path is None or not ids or not self.path.exists():
            return {}
        with self._lock:
            connection = self._connect()
            try:
                found = {}
                for start in range(0, len(ids), 400):
                    chunk = ids[start : start + 400]
                    marks = ",".join("?" * len(chunk))
                    query = (
                        f"SELECT match_id, payload FROM predictions WHERE version = ? "
                        f"AND match_id IN ({marks})"
                    )
                    for match_id, payload in connection.execute(query, [VERSION, *chunk]):
                        found[match_id] = json.loads(payload)
                return found
            finally:
                connection.close()

    def put(self, match_id: str, day: date, payload: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO predictions VALUES (?, ?, ?, ?, ?)",
                        (
                            match_id,
                            day.isoformat(),
                            VERSION,
                            datetime.now(UTC).isoformat(timespec="seconds"),
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
            finally:
                connection.close()


class FotbalMatch(BaseModel):
    id: str = Field(max_length=120)
    home: str = Field(max_length=100)
    away: str = Field(max_length=100)
    league: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=80)
    kickoff: datetime | None = None
    day: date | None = None
    status: str | None = Field(default=None, max_length=20)
    home_goals: int | None = Field(default=None, ge=0, le=99)
    away_goals: int | None = Field(default=None, ge=0, le=99)
    finish_type: str | None = Field(default=None, max_length=20)
    # cotele reale FlashScore (zecimale) pe cheile pieței; 1/X/2 vin cu tabla zilei
    odds: dict[str, float] | None = None
    # probabilitățile modelului de bază FootyPreds: folosite doar ca rezervă, marcate clar
    probabilities: dict[str, float] | None = None

    @field_validator("odds")
    @classmethod
    def _prices(cls, value):
        if value is None:
            return None
        return {
            k: float(v)
            for k, v in list(value.items())[:80]
            if k in mk.CATALOGUE and isinstance(v, (int, float)) and 1.0 < v < 1001
        }

    @field_validator("probabilities")
    @classmethod
    def _probabilities(cls, value):
        if value is None:
            return None
        return {
            k: float(v)
            for k, v in list(value.items())[:80]
            if k in mk.CATALOGUE and isinstance(v, (int, float)) and 0.0 <= v <= 1.0
        }

    def match_day(self) -> date:
        if self.day is not None:
            return self.day
        if self.kickoff is not None:
            kickoff = self.kickoff
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=UTC)
            return kickoff.astimezone(UTC).date()
        return datetime.now(UTC).date()

    def score(self) -> tuple[int, int] | None:
        if self.status != "finished" or self.home_goals is None or self.away_goals is None:
            return None
        return self.home_goals, self.away_goals


def _upcoming(match: FotbalMatch, now: datetime) -> bool:
    if match.status not in (None, "scheduled") or match.kickoff is None:
        return False
    kickoff = match.kickoff if match.kickoff.tzinfo else match.kickoff.replace(tzinfo=UTC)
    return kickoff > now


def _core_markets(probabilities: dict[str, float]) -> list[dict]:
    """Piețele de rezervă din modelul de bază FootyPreds (niciodată selectabile)."""
    output = []
    for key, p in probabilities.items():
        market = mk.CATALOGUE[key]
        if market.stat != "goals" or not 0.0 < p < 1.0:
            continue
        output.append(
            {
                "key": key,
                "label": market.label,
                "group": market.group,
                "group_label": mk.GROUPS[market.group],
                "stat": market.stat,
                "probability": round(p, 6),
                "fair_odds": round(1.0 / p, 3),
                "odds": None,
                "selectable": False,
                "decision": NO_BET,
                "decision_high": NO_BET,
            }
        )
    order = {key: index for index, key in enumerate(mk.KEYS)}
    return sorted(output, key=lambda m: order[m["key"]])


def create_app(
    *,
    include_core: bool = True,
    predictor: FootballPredictor | None = None,
    warm: bool = True,
    journal_path: str | Path | None = JOURNAL,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """``warm`` antrenează modelul într-un fir de fundal la pornire (fără predictor injectat),
    ca prima cerere să nu aștepte antrenarea."""
    now = clock or (lambda: datetime.now(UTC))
    journal = Journal(journal_path)
    unresolved: dict[tuple[str, str], dict] = {}
    core_app = None

    @asynccontextmanager
    async def lifespan(_app):
        if warm and predictor is None:
            ensure_warming()
        yield
        # Starlette nu rulează lifespan-ul aplicației montate: închidem noi clientul HTTP.
        client = getattr(
            getattr(getattr(core_app, "state", None), "provider", None), "client", None
        )
        if client is not None and hasattr(client, "aclose"):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not fail
                LOG.debug("Închiderea clientului core a eșuat.", exc_info=True)

    app = FastAPI(title="FotbalPrediction", version=VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    if include_core:
        from footypreds.api import create_app as create_footypreds_app

        core_app = create_footypreds_app()
        app.mount("/core", core_app, name="footypreds-core")

    def ready() -> FootballPredictor | None:
        if predictor is not None:
            return predictor
        if _trained is not None:
            ensure_fresh()
        return _trained

    def model() -> FootballPredictor:
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
            return {
                "status": "loading",
                "teams": 0,
                "leagues": 0,
                "trained_through": None,
                "version": VERSION,
            }
        return {
            "status": "ok",
            "teams": current.team_count,
            "leagues": current.league_count,
            "trained_through": (
                current.trained_through.isoformat() if current.trained_through else None
            ),
            "version": VERSION,
            "thresholds": {"select": THRESHOLD, "select_high": THRESHOLD_HIGH, "max_p": MAX_P},
            "market_weights": list(PRODUCTION_ODDS_WEIGHTS),
        }

    @app.get("/api/teams")
    def teams(q: str = Query("", max_length=80)):
        return {"teams": model().search(q)}

    @app.get("/api/unresolved")
    def unresolved_names():
        items = sorted(unresolved.values(), key=lambda item: (item["league"], item["name"]))
        return {"unresolved": items[:300]}

    @app.get("/api/predict")
    def predict(
        home: str = Query(min_length=2, max_length=100),
        away: str = Query(min_length=2, max_length=100),
        league: str = Query(pattern="^[A-Z0-9]{2,3}$"),
        day: date | None = None,
        odds_1: float | None = Query(None, gt=1, lt=1001),
        odds_x: float | None = Query(None, gt=1, lt=1001),
        odds_2: float | None = Query(None, gt=1, lt=1001),
        full: bool = False,
    ):
        current = model()
        if league not in current.teams:
            raise HTTPException(status_code=404, detail="Liga nu este acoperită de model.")
        names = []
        for name in (home, away):
            found = current.resolver.resolve(league, name)
            if found is None:
                raise HTTPException(status_code=404, detail=f"Echipă nerecunoscută: {name}")
            names.append(found)
        odds = {k: v for k, v in (("1", odds_1), ("X", odds_x), ("2", odds_2)) if v}
        return current.predict(league, *names, day or now().date(), odds or None, full=full)

    @app.post("/api/fotbal-probabilities")
    def fotbal_probabilities(matches: list[FotbalMatch]):
        current = model()
        batch = matches[:MAX_MATCHES]
        stored = journal.get([match.id for match in batch])
        moment = now()
        output = []
        for match in batch:
            day = match.match_day()
            score = match.score()
            extra_time = (match.finish_type or "") in EXTRA_TIME
            resolved = current.resolve(match.league, match.country, match.home, match.away)
            entry = {
                "id": match.id,
                "known": False,
                "source": "none",
                "reason": resolved["reason"],
                "league_code": resolved["code"],
                "home_model": resolved["home"],
                "away_model": resolved["away"],
                "retro": False,
                "journal": False,
                "extra_time": extra_time,
                "settled_stats": False,
                "markets": [],
            }
            row = None
            if resolved["home"] is not None:
                # Înainte de start predicția se recalculează (cote noi) și suprascrie jurnalul, ca
                # el să păstreze ultima versiune dinainte de start; după start se folosește aceea.
                upcoming = _upcoming(match, moment)
                result = None if upcoming else stored.get(match.id)
                entry["journal"] = result is not None
                if result is None:
                    result = current.predict(
                        resolved["code"],
                        resolved["home"],
                        resolved["away"],
                        day,
                        match.odds,
                        match_id=f"fs-{match.id}",
                    )
                    if upcoming:
                        journal.put(match.id, day, result)
                # Ziua locală (București) poate fi cu o zi după data football-data (meci după
                # miezul nopții): dacă modelul are deja rândul meciului, predicția e retroactivă.
                found = current.find_row(resolved["code"], day, resolved["home"], resolved["away"])
                entry.update(
                    known=True,
                    source="model",
                    retro=(bool(result["retro"]) or found is not None) and not entry["journal"],
                    odds_blend=result["odds_blend"],
                    markets=[dict(market) for market in result["markets"]],
                )
                if score is not None:
                    row = found
                    if row is not None and (row.home_goals, row.away_goals) != score:
                        row = None  # alt meci sau scor corectat: nu decontăm statisticile
                entry["settled_stats"] = row is not None
            else:
                if resolved["code"] is not None:
                    for name in (match.home, match.away):
                        if current.resolver.resolve(resolved["code"], name) is None:
                            unresolved[(resolved["code"], name)] = {
                                "league": resolved["code"],
                                "name": name,
                                "flashscore_league": match.league,
                            }
                if match.probabilities:
                    entry.update(source="core", markets=_core_markets(match.probabilities))
            for market in entry["markets"]:
                market["won"] = (
                    None if score is None or extra_time else settle(market["key"], score, row)
                )
            output.append(entry)
        return {
            "matches": output,
            "version": VERSION,
            "market_weights": list(PRODUCTION_ODDS_WEIGHTS),
            "thresholds": {"select": THRESHOLD, "select_high": THRESHOLD_HIGH, "max_p": MAX_P},
            "trained_through": (
                current.trained_through.isoformat() if current.trained_through else None
            ),
        }

    return app


app = create_app()
