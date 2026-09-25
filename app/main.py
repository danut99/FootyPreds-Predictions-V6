import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.competitions import catalog
from app.config import ROOT, Settings
from app.demo import demo_data
from app.domain import Match
from app.model import VERSION, backtest, predict, summarize
from app.provider import FlashScore, ProviderError
from app.store import Store
from app.tickets import PlanBuilder, PlanRequest, settle_plans

Threshold = Annotated[float, Query(ge=0.5, le=0.99)]
DEV_ORIGINS = [
    f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5500, 5501)
]


class AnalysisRequest(BaseModel):
    threshold: float = Field(default=0.85, ge=0.5, le=0.99)
    enrich: bool = True


def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
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


def create_app(settings=None, transport=None):
    settings = settings or Settings.load()
    store = Store(settings.database)
    provider = FlashScore(settings, store, transport)
    builder = PlanBuilder(store, provider)

    @asynccontextmanager
    async def lifespan(app):
        builder.recover_interrupted()
        yield
        await builder.close()
        await provider.client.aclose()

    app = FastAPI(title="FootyPreds V7", version="7.0.0", lifespan=lifespan)
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
                return JSONResponse({"detail": "Origine nepermisă."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"detail": "Parametri invalizi. Verifică data, ID-ul și pragul."}, status_code=422
        )

    @app.get("/api/health")
    def health():
        matches = store.matches()
        return {
            "status": "ok",
            "version": VERSION,
            "api_configured": bool(settings.api_key),
            "history_matches": sum(m.status == "finished" for m in matches),
            "calibrated": False,
        }

    @app.get("/api/matches")
    async def matches(day: date, refresh: bool = False):
        found, cached, rejected = await provider.fixtures(day, refresh)
        store.save_matches(found)
        settled = store.settle(found)
        return {
            "matches": found,
            "cached": cached,
            "rejected": rejected,
            "settled": settled,
            "source": "flashscore",
        }

    @app.get("/api/competitions")
    async def competitions(day: date, demo: bool = False, refresh: bool = False):
        if demo:
            _, found = demo_data()
            found = [
                m.model_copy(update={"league": f"Demo League {i + 1}"}) for i, m in enumerate(found)
            ]
            return {"competitions": catalog(found, demo=True), "source": "synthetic"}
        if refresh:
            found, _, _ = await provider.fixtures(day)
            store.save_matches(found)
        else:
            found = store.matches()
        now = datetime.now(timezone.utc)
        found = [
            m
            for m in found
            if m.status == "scheduled"
            and m.kickoff > now
            and m.kickoff.astimezone(timezone.utc).date() == day
        ]
        return {"competitions": catalog(found), "source": "flashscore" if refresh else "local"}

    @app.post("/api/analyze/{match_id}")
    async def analyze(match_id: str, body: AnalysisRequest):
        match = store.match(match_id)
        if match is None:
            raise HTTPException(404, "Încarcă mai întâi ziua acestui meci.")
        now = datetime.now(timezone.utc)
        if match.status != "scheduled" or match.kickoff <= now:
            raise HTTPException(409, "Predicțiile sunt disponibile numai înainte de start.")
        warnings = []
        if body.enrich:
            try:
                history, warnings = await provider.history(match)
                store.save_matches(history)
            except ProviderError as exc:
                warnings.append(str(exc))
        prediction = await run_in_threadpool(predict, match, store.matches(), body.threshold)
        saved = store.snapshot(match, prediction, datetime.now(timezone.utc))
        return {"match": match, "prediction": prediction, "saved": saved, "warnings": warnings}

    @app.get("/api/results")
    def results():
        rows = store.predictions()
        return {"metrics": summarize(rows, store.assessment_count()), "rows": rows[:200]}

    @app.post("/api/plans", status_code=202)
    async def create_plan(body: PlanRequest):
        return builder.start(body)

    @app.get("/api/benchmark")
    def benchmark_report():
        path = ROOT / "data/benchmark/report.json"
        if not path.exists():
            raise HTTPException(
                404, "Rulează python -m evaluation.dataset și python -m evaluation.run."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/benchmark/markdown")
    def benchmark_markdown():
        path = ROOT / "docs/BENCHMARK.md"
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
                    found, _, _ = await provider.fixtures(ticket_day, refresh=True)
                    store.save_matches(found)
                    store.settle(found)
            settle_plans(store)
        return store.plan(plan_id)

    @app.post("/api/backtest")
    def stored_backtest(threshold: Threshold = 0.85):
        matches = [m for m in store.matches() if m.status == "finished"][-3000:]
        if not matches:
            raise HTTPException(400, "Încarcă zile istorice sau importă un CSV cu rezultate.")
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
        return {
            "source": "synthetic",
            "matches": fixtures,
            "analyses": [
                {
                    "match": m,
                    "prediction": predict(m, history, threshold),
                    "saved": False,
                    "warnings": [],
                }
                for m in fixtures
            ],
        }

    @app.post("/api/demo/backtest")
    def demo_backtest(threshold: Threshold = 0.85):
        history, _ = demo_data()
        return {**backtest(history, threshold), "source": "synthetic"}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="frontend")
    return app


app = create_app()
