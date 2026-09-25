import asyncio
import csv
import io
import json
import logging
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from footypreds.competitions import catalog, competition_name, match_competition, priority
from footypreds.config import PACKAGE, Settings
from footypreds.demo import demo_data
from footypreds.domain import Match
from footypreds.engine import VERSION, HistoryIndex, analyze, backtest, summarize
from footypreds.engine.backtest import compact
from footypreds.excel import build_sport_workbook, build_workbook
from footypreds.excel_api import router as excel_router
from footypreds.live_api import router as live_router
from footypreds.media import close as close_media
from footypreds.media import match_media, public_match
from footypreds.media import router as media_router
from footypreds.provider import FlashScore, ProviderError
from footypreds.recommend_api import router as recommend_router
from footypreds.sim_api import router as sim_router
from footypreds.sports import (
    SPORT_PATTERN,
    SPORTS,
    analyze_match,
    headline_tip,
    main_markets,
    sport_list,
)
from footypreds.sports.odds import merge_odds
from footypreds.sports.settle import settle
from footypreds.store import Store
from footypreds.tickets import PlanBuilder, PlanRequest, settle_plans

Threshold = Annotated[float, Query(ge=0.5, le=0.99)]
# ?sport=football|basketball|tennis (default football); an unknown sport is a 422.
Sport = Annotated[str, Query(pattern=SPORT_PATTERN)]
# Optional sport check on endpoints addressed by match id ("" = any sport).
AnySport = Annotated[str, Query(pattern="^$|" + SPORT_PATTERN)]
DEV_ORIGINS = [
    f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5500, 5501)
]
PAST_DAY_TTL = 30 * 86400
# The prospective ledger is comparable only at one fixed threshold, whatever the viewer asks.
LEDGER_THRESHOLD = 0.85
log = logging.getLogger(__name__)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}


def secure(response):
    """Every response carries the headers, including early 403s and 500s."""
    response.headers.update(SECURITY_HEADERS)
    return response


def past_day_ttl(day, today):
    """Long cache only for days whose results are final; yesterday can still change."""
    return PAST_DAY_TTL if day < today - timedelta(days=1) else None


class AnalysisRequest(BaseModel):
    threshold: float = Field(default=0.85, ge=0.5, le=0.99)
    enrich: bool = True
    refresh: bool = False


class SyncRequest(BaseModel):
    days: int = Field(default=21, ge=1, le=90)
    sports: list[str] = Field(default_factory=lambda: ["football"], min_length=1, max_length=3)

    @field_validator("sports")
    @classmethod
    def known_sports(cls, value):
        if any(sport not in SPORTS for sport in value):
            raise ValueError("Sport necunoscut.")
        return list(dict.fromkeys(value))


async def fetch_day(provider, day, sport="football", **kwargs):
    """provider.fixtures for one sport; football calls keep the historical signature."""
    if sport != "football":
        kwargs["sport"] = sport
    return await provider.fixtures(day, **kwargs)


def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    required = {"id", "kickoff", "league", "home", "away", "home_goals", "away_goals"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Coloane obligatorii: " + ", ".join(sorted(required)))
    matches, seen, fixtures = [], set(), set()
    for number, row in enumerate(reader, 2):
        if len(matches) >= 3000:
            raise ValueError("Maximum 3000 de meciuri per fișier.")
        try:
            match = Match(**{k: row[k] for k in required}, status="finished", source="import")
            identity = (match.kickoff, match.league, match.home, match.away)
            if match.id in seen or identity in fixtures:
                raise ValueError("Meci duplicat")
            if match.kickoff >= datetime.now(timezone.utc):
                raise ValueError("Rezultat cu dată în viitor")
            seen.add(match.id)
            fixtures.add(identity)
            matches.append(match)
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"Rândul {number}: date invalide sau meci duplicat.") from exc
    if not matches:
        raise ValueError("Fișierul nu conține meciuri.")
    return matches


def board_item(match, analysis):
    """Compact card for the daily board: the numbers a reader scans first (any sport)."""
    sport = analysis.get("sport", match.sport)
    p = {m["key"]: m["probability"] for m in analysis["markets"]}
    tip = headline_tip(analysis)
    main = main_markets(analysis)
    item = {
        "sport": sport,
        "match": public_match(match),
        # The same display logos at the top level, for cards that do not open `match`.
        **match_media(match),
        "competition": competition_name(match.league),
        "competition_id": match_competition(match),
        "probabilities": {m["key"]: m["probability"] for m in main},
        "main": main,
        "expected": analysis.get("expected"),
        "tip": {"key": tip["key"], "label": tip["label"], "probability": tip["probability"]},
        "tips": analysis["tips"],
        "grade": analysis["grade"],
        "confidence": analysis["confidence"],
        "sample": analysis["sample"],
        "form": {
            "home": analysis["form"]["home"]["sequence"],
            "away": analysis["form"]["away"]["sequence"],
        },
        "summary": analysis["summary"],
        "selection": analysis["selection"],
    }
    if sport == "football":
        item |= {
            "probabilities": {
                k: p[k]
                for k in ("1", "X", "2", "1X", "X2", "12", "over15", "over25", "over35", "btts")
            }
            | {k: p[k] for k in ("under25", "no_btts", "ht_1", "ht_X", "ht_2", "ht_over05")},
            "expected_goals": analysis["expected_goals"],
            "score": analysis["scores"][0],
            "scores": analysis["scores"][:3],
            "htft": analysis["htft"][0],
        }
    if match.status == "finished":
        item["result"] = {
            "score": f"{match.home_goals}-{match.away_goals}",
            "tip_won": settle(
                sport,
                tip["key"],
                match.home_goals,
                match.away_goals,
                match.finish_type or "finished",
            ),
        }
    return item


class HistorySync:
    """Loads finished results of past days: one request per day, never repeated."""

    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.task = None
        self.state = {"status": "idle", "done": 0, "total": 0, "matches": 0, "message": ""}

    def start(self, days, sports=("football",)):
        if self.task and not self.task.done():
            raise ProviderError("Sincronizarea rulează deja.", 409)
        today = datetime.now(timezone.utc).date()
        pending = []
        for sport in sports:
            synced = self.store.synced_days(sport)
            pending += [
                (sport, today - timedelta(days=n))
                for n in range(1, days + 1)
                if (today - timedelta(days=n)).isoformat() not in synced
            ]
        self.state = {
            "status": "running" if pending else "done",
            "done": 0,
            "total": len(pending),
            "matches": 0,
            "message": ""
            if pending
            else "Istoricul este deja sincronizat pentru această perioadă.",
        }
        if pending:
            self.task = asyncio.create_task(self.run(pending, today))
        return self.state

    async def run(self, pending, today):
        """pending: days (football) or (sport, day) pairs."""
        try:
            for item in pending:
                sport, day = item if isinstance(item, tuple) else ("football", item)
                label = "" if sport == "football" else f" {SPORTS[sport]['label'].lower()}"
                self.state["message"] = f"Rezultate{label} {day.isoformat()}…"
                # Yesterday is re-checked next time, so it must not be cached for 30 days.
                ttl = past_day_ttl(day, today)
                matches, _, _ = await fetch_day(self.provider, day, sport, ttl=ttl)
                finished = [m for m in matches if m.status == "finished"]
                self.store.save_matches(matches)
                self.store.settle(matches)
                # Yesterday can still receive late results; re-check it next time.
                if day <= today - timedelta(days=2):
                    if sport == "football":
                        self.store.mark_synced(day, len(finished))
                    else:
                        self.store.mark_synced(day, len(finished), sport)
                self.state["done"] += 1
                self.state["matches"] += len(finished)
            settle_plans(self.store)
            self.state.update(status="done", message="Istoric actualizat.")
        except ProviderError as exc:
            self.state.update(status="failed", message=str(exc))
        except asyncio.CancelledError:
            self.state.update(status="interrupted", message="Sincronizare întreruptă.")
            raise
        except Exception:
            # Anything else (e.g. a locked database) must not leave the UI polling "running".
            log.exception("History sync failed")
            self.state.update(
                status="failed",
                message="Sincronizarea a eșuat. Zilele deja salvate rămân; reîncearcă.",
            )

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class AnalysisCache:
    """Analyses depend only on the fixture, the stored history and the threshold.

    One HistoryIndex per sport: team names repeat across sports, so histories never mix.
    """

    def __init__(self, store, size=4000):
        self.store, self.size = store, size
        self.items = OrderedDict()
        self.indexes = {}
        # get() runs in several worker threads at once (board, analysis, export).
        self.lock = threading.RLock()

    def snapshot(self, sport="football"):
        """The sport's index and the store version it was built from, as a consistent pair."""
        with self.lock:
            index, version = self.indexes.get(sport, (None, -1))
            if index is None or version != self.store.version:
                version = self.store.version
                index = HistoryIndex(self.store.matches(sport=sport))
                self.indexes[sport] = (index, version)
            return index, version

    def history(self, sport="football"):
        return self.snapshot(sport)[0]

    def get(self, match, threshold=0.85):
        index, version = self.snapshot(match.sport)
        # Every fixture field feeds the analysis (kickoff, names, IDs, odds, status).
        key = (match.model_dump_json(), threshold, version)
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        analysis = analyze_match(match, index, threshold)
        with self.lock:
            self.items[key] = analysis
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return analysis


def create_app(settings=None, transport=None, image_transport=None):
    """The app; `transport` replaces FlashScore's HTTP transport and `image_transport` the
    /api/img proxy's (tests and scripts/mock_server.py never reach the network)."""
    settings = settings or Settings.load()
    store = Store(settings.database)
    provider = FlashScore(settings, store, transport)
    builder = PlanBuilder(store, provider)
    sync = HistorySync(store, provider)
    cache = AnalysisCache(store)

    @asynccontextmanager
    async def lifespan(app):
        builder.recover_interrupted()
        yield
        await builder.close()
        await sync.close()
        await close_media(app)
        await provider.client.aclose()

    app = FastAPI(title="FootyPreds", version="8.0.0", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def security(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            parsed = urlparse(origin)
            same_origin = (
                parsed.netloc == request.headers.get("host") and parsed.scheme == request.url.scheme
            )
            if not same_origin and origin not in DEV_ORIGINS:
                return secure(JSONResponse({"detail": "Origine nepermisă."}, status_code=403))
        # GET endpoints call FlashScore (limited quota): another website must not be able to
        # trigger them through <img>/<script> tags. Browsers mark such requests cross-site;
        # non-browser clients (Excel, scripts) do not send the header and stay allowed.
        if (
            request.url.path.startswith("/api/")
            and request.headers.get("sec-fetch-site") == "cross-site"
            and origin not in DEV_ORIGINS
        ):
            return secure(JSONResponse({"detail": "Origine nepermisă."}, status_code=403))
        return secure(await call_next(request))

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # The SPA expects JSON; the traceback still goes to the server log. This handler runs
        # outside the security middleware, so it adds the headers itself.
        return secure(
            JSONResponse(
                {"detail": "Eroare internă a serverului. Detaliile sunt în jurnalul serverului."},
                status_code=500,
            )
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"detail": "Parametri invalizi. Verifică data, ID-ul și pragul."}, status_code=422
        )

    async def day_fixtures(day, sport="football", refresh=False):
        ttl = past_day_ttl(day, datetime.now(timezone.utc).date())
        found, cached, rejected = await fetch_day(provider, day, sport, refresh=refresh, ttl=ttl)
        store.save_matches(found)
        settled = store.settle(found)
        return found, cached, rejected, settled

    @app.get("/api/health")
    def health():
        matches = store.matches()
        return {
            "status": "ok",
            "version": VERSION,
            "api_configured": bool(settings.api_key),
            "history_matches": sum(
                m.status == "finished" and m.sport == "football" for m in matches
            ),
            "synced_days": len(store.synced_days()),
            "calibrated": False,
            "sports": list(SPORTS),
            "history_by_sport": {
                sport: sum(m.status == "finished" and m.sport == sport for m in matches)
                for sport in SPORTS
            },
        }

    @app.get("/api/sports")
    def sports():
        return {"sports": sport_list()}

    @app.get("/api/matches")
    async def matches(day: date, refresh: bool = False, sport: Sport = "football"):
        found, cached, rejected, settled = await day_fixtures(day, sport, refresh)
        return {
            "matches": [public_match(m) for m in found],
            "cached": cached,
            "rejected": rejected,
            "settled": settled,
            "sport": sport,
            "source": "flashscore",
        }

    @app.get("/api/predictions")
    async def predictions(
        day: date,
        competition: str = "",
        limit: Annotated[int, Query(ge=1, le=400)] = 120,
        offset: Annotated[int, Query(ge=0)] = 0,
        refresh: bool = False,
        demo: bool = False,
        sport: Sport = "football",
    ):
        if demo and sport != "football":
            # The synthetic demo is football only.
            return {
                "day": day,
                "sport": sport,
                "total": 0,
                "items": [],
                "competitions": [],
                "source": "synthetic",
            }
        if demo:
            history, found = demo_data()
            index = HistoryIndex(history)
            items = [board_item(m, analyze(m, index)) for m in found]
            return {
                "day": day,
                "total": len(items),
                "items": items,
                "competitions": catalog(found, demo=True),
                "sport": sport,
                "source": "synthetic",
            }
        found, cached, rejected, _ = await day_fixtures(day, sport, refresh)
        found = [m for m in found if m.status != "unavailable"]
        competitions = catalog(found, demo=True, sport=sport)
        if competition:
            found = [m for m in found if match_competition(m) == competition]
        found.sort(key=priority)
        page = found[offset : offset + limit]
        items = await run_in_threadpool(lambda: [board_item(m, cache.get(m)) for m in page])
        return {
            "day": day,
            "total": len(found),
            "offset": offset,
            "items": items,
            "competitions": competitions,
            "cached": cached,
            "rejected": rejected,
            "sport": sport,
            "source": "flashscore",
        }

    @app.get("/api/competitions")
    async def competitions(
        day: date, demo: bool = False, refresh: bool = False, sport: Sport = "football"
    ):
        if demo:
            _, found = demo_data()
            found = [
                m.model_copy(update={"league": f"Demo League {i + 1}"}) for i, m in enumerate(found)
            ]
            return {"competitions": catalog(found, demo=True), "source": "synthetic"}
        if refresh:
            found, _, _ = await fetch_day(provider, day, sport)
            store.save_matches(found)
        else:
            found = store.matches(sport=sport)
        now = datetime.now(timezone.utc)
        found = [
            m
            for m in found
            if m.status == "scheduled"
            and m.kickoff > now
            and m.kickoff.astimezone(timezone.utc).date() == day
        ]
        return {
            "competitions": catalog(found, sport=sport),
            "sport": sport,
            "source": "flashscore" if refresh else "local",
        }

    async def merge_prices(match, refresh=False):
        """matches/odds best prices -> the stored Match.odds (list-by-date 1/X/2 kept)."""
        prices = await provider.match_odds(match, refresh)
        current = store.match(match.id)
        if prices and current is not None:
            merged = merge_odds(current.odds, prices)
            if merged != current.odds:
                store.save_matches([current.model_copy(update={"odds": merged})])
        return prices

    async def enrich(match, refresh=False):
        """H2H/form history, standings and every quoted market price, for any sport."""
        warnings, standings = [], []
        try:
            history, _, rejected = await provider.head_to_head(match, refresh)
            store.save_matches(history)
            if rejected:
                warnings.append(f"{rejected} rezultate incomplete au fost ignorate.")
            if not history:
                warnings.append("FlashScore nu are istoric pentru aceste echipe.")
            if match.sport != "tennis":
                standings = await provider.standings(match)
        except ProviderError as exc:
            if exc.status == 503:
                raise
            warnings.append(str(exc))
            # Quota or upstream trouble: do not spend another request on prices.
            return warnings, standings
        if not (match.status == "scheduled" and match.kickoff > datetime.now(timezone.utc)):
            # Prices carry no quote time: once the match has started, stored prices stay the
            # pre-match ones, so no in-play price can reach a prediction or a simulation.
            return warnings, standings
        try:
            await merge_prices(match, refresh)
        except ProviderError as exc:
            if exc.status == 503:
                raise
            warnings.append(f"Cotele suplimentare nu au putut fi încărcate: {exc}")
        except Exception:
            # Extra prices are optional: a malformed odds answer never breaks an analysis.
            log.warning("matches/odds failed for %s", match.id, exc_info=True)
        return warnings, standings

    def sport_match(match_id, sport, missing):
        match = store.match(match_id)
        if match is None or (sport and match.sport != sport):
            raise HTTPException(404, missing)
        return match

    @app.post("/api/analyze/{match_id}")
    async def analyze_one(match_id: str, body: AnalysisRequest, sport: AnySport = ""):
        match = sport_match(match_id, sport, "Încarcă mai întâi ziua acestui meci.")
        now = datetime.now(timezone.utc)
        prematch = match.status == "scheduled" and match.kickoff > now
        warnings, standings = [], []
        if body.enrich:
            warnings, standings = await enrich(match, body.refresh)
            # Enrichment may have merged new market prices into the stored fixture.
            match = store.match(match_id) or match
        prediction = await run_in_threadpool(cache.get, match, body.threshold)
        # Only pre-match snapshots enter the prospective ledger; later views are retrospective.
        # The ledger always uses LEDGER_THRESHOLD: a first view at 0.5 must not freeze a 50%
        # pick into the immutable track record.
        saved = False
        if prematch:
            ledger = prediction
            if body.threshold != LEDGER_THRESHOLD:
                ledger = await run_in_threadpool(cache.get, match, LEDGER_THRESHOLD)
            saved = store.snapshot(match, compact(ledger), now)
        return {
            "match": public_match(match),
            "prediction": prediction,
            "saved": saved,
            "retrospective": not prematch,
            "standings": standings,
            "warnings": warnings,
        }

    @app.get("/api/analysis/{match_id}")
    async def local_analysis(match_id: str, threshold: Threshold = 0.85, sport: AnySport = ""):
        match = sport_match(match_id, sport, "Meciul nu există în baza locală.")
        prediction = await run_in_threadpool(cache.get, match, threshold)
        now = datetime.now(timezone.utc)
        return {
            "match": public_match(match),
            "prediction": prediction,
            "saved": False,
            # Same rule as /api/analyze: a scheduled match past kickoff is not pre-match.
            "retrospective": not (match.status == "scheduled" and match.kickoff > now),
            "standings": [],
            "warnings": [],
        }

    @app.post("/api/history/sync", status_code=202)
    async def start_sync(body: SyncRequest):
        return sync.start(body.days, body.sports)

    @app.get("/api/history/sync")
    def sync_status():
        return sync.state

    @app.get("/api/export.xlsx")
    async def export_xlsx(
        day: date, competition: str = "", enriched_only: bool = False, sport: Sport = "football"
    ):
        found, _, _, _ = await day_fixtures(day, sport)
        found = [m for m in found if m.status != "unavailable"]
        if competition:
            found = [m for m in found if match_competition(m) == competition]
        found.sort(key=priority)
        items = await run_in_threadpool(lambda: [(m, cache.get(m)) for m in found[:400]])
        if enriched_only:
            items = [(m, a) for m, a in items if a["grade"] != "D"]
        if not items:
            raise HTTPException(404, "Nu există meciuri pentru export în această zi.")
        if sport == "football":
            content = await run_in_threadpool(build_workbook, day, items)
            filename = f"FootyPreds-{day.isoformat()}.xlsx"
        else:
            content = await run_in_threadpool(build_sport_workbook, day, items, sport)
            filename = f"FootyPreds-{sport}-{day.isoformat()}.xlsx"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/results")
    def results():
        rows = store.predictions()
        return {"metrics": summarize(rows, store.assessment_count()), "rows": rows[:300]}

    @app.post("/api/plans", status_code=202)
    async def create_plan(body: PlanRequest):
        return builder.start(body)

    @app.get("/api/benchmark")
    def benchmark_report():
        path = PACKAGE / "data/benchmark/report.json"
        if not path.exists():
            raise HTTPException(
                404,
                "Rulează python -m footypreds.evaluation.dataset și "
                "python -m footypreds.evaluation.run.",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/benchmark/markdown")
    def benchmark_markdown():
        path = PACKAGE / "docs/BENCHMARK.md"
        if not path.exists():
            raise HTTPException(404, "Raportul strict nu a fost încă generat.")
        return FileResponse(path, media_type="text/markdown", filename="FootyPreds-benchmark.md")

    @app.get("/api/plans")
    def plans():
        settle_plans(store)
        return {"plans": store.plans()}

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: str):
        plan = store.plan(plan_id)
        if plan is None:
            raise HTTPException(404, "Planul nu există.")
        return plan

    @app.post("/api/plans/{plan_id}/refresh")
    async def refresh_plan(plan_id: str):
        if builder.task and not builder.task.done():
            raise HTTPException(409, "Așteaptă terminarea generării înainte de actualizare.")
        plan = store.plan(plan_id)
        if plan is None:
            raise HTTPException(404, "Planul nu există.")
        if not plan["request"]["demo"]:
            for day in plan["days"]:
                ticket_day = date.fromisoformat(day["date"])
                if day.get("ticket") and ticket_day <= datetime.now(timezone.utc).date():
                    found, _, _ = await fetch_day(provider, ticket_day, refresh=True)
                    store.save_matches(found)
                    store.settle(found)
            settle_plans(store)
        return store.plan(plan_id)

    @app.post("/api/backtest")
    def stored_backtest(threshold: Threshold = 0.85):
        # The walk-forward backtest is the football engine's.
        matches = [m for m in store.matches(sport="football") if m.status == "finished"][-3000:]
        if not matches:
            raise HTTPException(400, "Sincronizează istoricul sau importă un CSV cu rezultate.")
        return {**backtest(matches, threshold), "source": "flashscore"}

    @app.post("/api/backtest/csv")
    async def csv_backtest(request: Request, threshold: Threshold = 0.85):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise HTTPException(413, "Fișier prea mare. Limita este 2 MB.")
        try:
            matches = parse_csv(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**await run_in_threadpool(backtest, matches, threshold), "source": "csv"}

    @app.get("/api/demo")
    def demo(threshold: Threshold = 0.85):
        history, fixtures = demo_data()
        index = HistoryIndex(history)
        return {
            "source": "synthetic",
            "matches": [public_match(m) for m in fixtures],
            "analyses": [
                {
                    "match": public_match(m),
                    "prediction": analyze(m, index, threshold),
                    "saved": False,
                    "retrospective": False,
                    "standings": [],
                    "warnings": [],
                }
                for m in fixtures
            ],
        }

    @app.post("/api/demo/backtest")
    def demo_backtest(threshold: Threshold = 0.85):
        history, _ = demo_data()
        return {**backtest(history, threshold), "source": "synthetic"}

    # Shared objects for feature routers (docs/CONTRACTS.md). Feature modules read them
    # from request.app.state and never import create_app.
    app.state.settings = settings
    app.state.provider = provider
    app.state.cache = cache
    app.state.enrich = enrich
    app.state.day_fixtures = day_fixtures
    app.state.ledger_threshold = LEDGER_THRESHOLD
    # /api/img proxy (media.py): upstream transport and disk cache, read on first use.
    app.state.img_transport = image_transport
    app.state.img_cache_dir = settings.database.parent / "img_cache"

    async def excel_day_fixtures(day, refresh=False):
        return await day_fixtures(day, refresh=refresh)

    # excel: the Excel/VBA client (footypreds/excel_api.py) reuses these shared objects.
    app.state.excel_settings = settings
    app.state.excel_provider = provider
    app.state.excel_cache = cache
    app.state.excel_enrich = enrich
    app.state.excel_day_fixtures = excel_day_fixtures  # excel: football, (day, refresh)
    app.state.excel_ledger_threshold = LEDGER_THRESHOLD  # excel: same ledger rule as the web
    app.include_router(excel_router)  # excel: /api/excel/* flat CSV/TSV tables
    # Feature routers: ONE line each, here, before the static mounts at "/".
    app.include_router(live_router)  # live: /api/live, /api/live/{match_id}
    app.include_router(recommend_router)  # recommendations: /api/recommendations*, tickets
    app.include_router(sim_router)  # simulator: /api/simulate*, /api/wallet*
    app.include_router(media_router)  # media: /api/img logo/flag proxy

    @app.get("/")
    def index():
        return FileResponse(PACKAGE / "web/index.html")

    app.mount("/static", StaticFiles(directory=PACKAGE / "web"), name="static")
    app.mount("/", StaticFiles(directory=PACKAGE / "web", html=True), name="frontend")
    return app


def __getattr__(name):
    """`footypreds.api:app` (uvicorn) is built on first access, not at import time.

    Importing this module (tests, scripts/mock_server.py) therefore never opens the real
    database or reads .env.
    """
    if name == "app":
        globals()["app"] = application = create_app()
        return application
    raise AttributeError(name)
