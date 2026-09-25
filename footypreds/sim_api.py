"""Bankroll simulator and virtual wallet endpoints (docs/CONTRACTS.md §10.2).

- GET  /api/simulate/datasets: availability, date range and size of every dataset.
- POST /api/simulate: blind walk-forward bankroll simulation (footypreds/simulator.py),
  including the "ladder" (rollover) strategy and the "recent" dataset (last N days).
- POST /api/simulate/recent/prepare + GET /api/simulate/recent/status: background loader of
  the last N days (one FlashScore day list per day and sport, never a completed day twice).
- /api/wallet...: the local virtual wallet (footypreds/wallet.py), wired through this router.

Tests may point the simulator elsewhere through ``app.state.sim_benchmark_dir`` (datasets),
``app.state.sim_cache_dir`` (prediction cache) and ``app.state.sim_workers`` (processes).
"""

import asyncio
import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from footypreds.evaluation import sim_datasets
from footypreds.provider import ProviderError
from footypreds.simulator import CACHE_DIR, SimulationError, simulate
from footypreds.sports import SPORTS
from footypreds.wallet import router as wallet_router

log = logging.getLogger(__name__)
router = APIRouter(tags=["simulator"])
router.include_router(wallet_router)
# Dataset used when a request names only the sport.
DEFAULT_DATASETS = {"football": "football", "tennis": "tennis", "basketball": "local-basketball"}
# Recent-days loader: days fully in the past keep their list 30 days in the provider cache
# (like the history sync); yesterday can still change and is re-checked.
PAST_DAY_TTL = 30 * 86400
# Uncached FlashScore requests one prepare call may spend (1 request = 1 day of 1 sport).
RECENT_BUDGET = 90
# Days loaded before the window only as form history (not bet).
RECENT_WARMUP = 14
RECENT_MAX_WARMUP = 30


def known_sports(value):
    if value is None:
        return value
    if not value or any(sport not in SPORTS for sport in value):
        raise ValueError("Sport necunoscut.")
    return list(dict.fromkeys(value))


class SimulateRequest(BaseModel):
    dataset: Annotated[str, Field(max_length=40)] = "football"
    sport: str | None = None
    # "recent" dataset: the sports mixed on the tickets and the last N days (1..60).
    sports: Annotated[list[str], Field(max_length=3)] | None = None
    days: int | None = None
    bankroll: float = 1000.0
    # Contract form: flat | percent | kelly (+ target_odds for a daily ticket) | ladder.
    # Long form: singles | ticket | value, with `staking`.
    strategy: str = "flat"
    staking: str | None = None
    mode: str | None = None
    stake: float | None = None
    kelly_fraction: float | None = None
    kelly_cap: float | None = None
    target_odds: float | None = None
    # Ladder: share of the ladder bankroll staked each day, restart after a loss, cash-out.
    reinvest: float | None = None
    restart_on_loss: bool = True
    max_days: int | None = None
    start: date | None = None
    end: date | None = None
    max_bets_per_day: int | None = None
    picks_per_day: int | None = None
    # Accepted for compatibility; the simulation is deterministic and uses no randomness.
    seed: int | None = None

    @field_validator("sports")
    @classmethod
    def valid_sports(cls, value):
        return known_sports(value)


class PrepareRequest(BaseModel):
    days: int = Field(default=sim_datasets.RECENT_DEFAULT_DAYS, ge=1, le=60)
    sports: Annotated[list[str], Field(min_length=1, max_length=3)] = ["football"]
    # Extra past days loaded only as form history for the first days of the window.
    warmup_days: int = Field(default=RECENT_WARMUP, ge=0, le=RECENT_MAX_WARMUP)

    @field_validator("sports")
    @classmethod
    def valid_sports(cls, value):
        return known_sports(value)


def _settings(request):
    state = request.app.state
    return (
        getattr(state, "sim_benchmark_dir", sim_datasets.BENCHMARK_DIR),
        getattr(state, "sim_cache_dir", CACHE_DIR),
        getattr(state, "sim_workers", None),
    )


def _parsed_dir(cache_dir):
    return cache_dir / "datasets" if cache_dir else None


@router.get("/api/simulate/datasets")
async def datasets(request: Request):
    benchmark, cache_dir, _ = _settings(request)
    store = request.app.state.store

    def load(dataset_id):
        return sim_datasets.cached_dataset(dataset_id, store, benchmark, _parsed_dir(cache_dir))

    items = await run_in_threadpool(sim_datasets.availability, store, benchmark, load)
    return {"datasets": items, "disclaimer": "Simulare cu bani virtuali. 18+."}


def _recent(body, store):
    """(dataset, last_days) of a "recent" request; HTTPException when it cannot run."""
    sports = body.sports or ([body.sport] if body.sport in SPORTS else ["football"])
    if body.sport and body.sport not in sports:
        raise HTTPException(422, f"Sportul {body.sport} nu este printre sporturile alese.")
    days = sim_datasets.RECENT_DEFAULT_DAYS if body.days is None else body.days
    if not 1 <= days <= sim_datasets.RECENT_MAX_DAYS:
        raise HTTPException(422, "Numărul de zile recente trebuie să fie între 1 și 60.")
    today = sim_datasets.utcnow().date()
    dataset = sim_datasets.recent_dataset(store, sports, today, days)
    if not dataset.describe()["available"]:
        raise HTTPException(
            404,
            f"Nu există meciuri terminate cu cote în ultimele {days} zile pentru sporturile "
            "alese. " + sim_datasets.RECENT_HINT,
        )
    return dataset, days


@router.post("/api/simulate")
async def run_simulation(request: Request, body: SimulateRequest):
    benchmark, cache_dir, workers = _settings(request)
    if "dataset" not in body.model_fields_set and body.sport in DEFAULT_DATASETS:
        body = body.model_copy(update={"dataset": DEFAULT_DATASETS[body.sport]})
    if body.dataset not in sim_datasets.DATASET_IDS:
        raise HTTPException(422, "Set de date necunoscut. Vezi /api/simulate/datasets.")
    store = request.app.state.store
    last_days = body.days
    if body.dataset == "recent":
        dataset, last_days = await run_in_threadpool(_recent, body, store)
    else:
        try:
            dataset = await run_in_threadpool(
                sim_datasets.cached_dataset,
                body.dataset,
                store,
                benchmark,
                _parsed_dir(cache_dir),
            )
        except (FileNotFoundError, ValueError, OSError) as error:
            hint = sim_datasets.unavailable(body.dataset)["hint"]
            reason = sim_datasets.reason_of(error)
            raise HTTPException(
                404, f"Setul de date nu este disponibil: {reason} {hint}"
            ) from error
        if not dataset.describe()["available"]:
            raise HTTPException(
                404,
                "Setul de date nu este disponibil: nu are meciuri terminate cu cote. "
                + sim_datasets.unavailable(body.dataset)["hint"],
            )
        if body.sport and body.sport != dataset.sport:
            raise HTTPException(422, f"Setul de date {body.dataset} conține doar {dataset.sport}.")
    stake = body.stake
    if stake is None and body.kelly_fraction is not None:
        stake = body.kelly_fraction
    per_day = body.max_bets_per_day or body.picks_per_day or 3
    options = {"kelly_cap": body.kelly_cap} if body.kelly_cap is not None else {}
    try:
        result = await run_in_threadpool(
            lambda: simulate(
                dataset,
                bankroll=body.bankroll,
                strategy=body.strategy,
                staking=body.staking,
                mode=body.mode,
                stake=stake,
                target_odds=body.target_odds,
                max_bets_per_day=per_day,
                start=body.start,
                end=body.end,
                cache_dir=cache_dir,
                workers=workers,
                reinvest=body.reinvest,
                restart_on_loss=body.restart_on_loss,
                max_days=body.max_days,
                last_days=last_days,
                **options,
            )
        )
    except SimulationError as error:
        raise HTTPException(422, str(error)) from error
    return result | {"seed": body.seed}


# --- recent days loader --------------------------------------------------------------------


async def fetch_day(provider, day, sport, **kwargs):
    """provider.fixtures for one sport; football calls keep the historical signature."""
    if sport != "football":
        kwargs["sport"] = sport
    return await provider.fixtures(day, **kwargs)


class RecentLoader:
    """Loads the finished results (with pre-match 1X2 prices) of the last N days.

    One FlashScore day list per day and sport (``provider.fixtures``). A day at least two days
    old is marked synced after loading and is never requested again (shared with the history
    sync); yesterday is re-checked through the provider cache. One prepare call spends at most
    ``budget`` uncached requests, newest days first; the rest waits for the next call.
    """

    def __init__(self, store, provider, budget=RECENT_BUDGET):
        self.store, self.provider, self.budget = store, provider, budget
        self.task = None
        self.request = {"days": 0, "sports": [], "warmup_days": 0}
        self.state = self.idle()

    @staticmethod
    def idle():
        return {
            "status": "idle",
            "done": 0,
            "total": 0,
            "days_loaded": 0,
            "days_total": 0,
            "matches": 0,
            "loaded_matches": 0,
            "requests": 0,
            "message": "",
        }

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def window(self, days, today):
        return [today - timedelta(days=n) for n in range(1, days + 1)]

    def plan(self, days, sports, warmup, today):
        """[(sport, day)] still to load, newest first: the window, then the warm-up days."""
        synced = {sport: self.store.synced_days(sport) for sport in sports}
        return [
            (sport, day)
            for day in self.window(days + warmup, today)
            for sport in sports
            if day.isoformat() not in synced[sport]
        ]

    def coverage(self, days, sports, today):
        """(days loaded, days in the window, bettable matches) for (days, sports)."""
        loaded = 0
        for sport in sports:
            synced = self.store.synced_days(sport)
            for day in self.window(days, today):
                stored = day.isoformat() in synced or (
                    day == today - timedelta(days=1)
                    and any(m.status == "finished" for m in self.store.matches_on(day, sport))
                )
                loaded += stored
        matches = 0
        if sports:
            try:
                dataset = sim_datasets.recent_dataset(self.store, sports, today, days)
                matches = len(dataset.bettable)
            except (FileNotFoundError, ValueError):
                matches = 0
        return loaded, days * len(sports), matches

    def status(self, days=None, sports=None):
        """The loader state; with (days, sports) the coverage of that window instead."""
        state = dict(self.state)
        days = days or self.request["days"]
        sports = sports or self.request["sports"]
        if days and sports:
            today = sim_datasets.utcnow().date()
            loaded, total, matches = self.coverage(days, sports, today)
            state.update(days_loaded=loaded, days_total=total, matches=matches)
        state.update(
            days=days or 0,
            sports=list(sports or []),
            warmup_days=self.request["warmup_days"],
            budget=self.budget,
        )
        return state

    def start(self, days, sports, warmup=RECENT_WARMUP):
        if self.running:
            return self.status()
        today = sim_datasets.utcnow().date()
        self.request = {"days": days, "sports": list(sports), "warmup_days": warmup}
        pending = self.plan(days, sports, warmup, today)
        self.state = self.idle() | {
            "status": "running" if pending else "done",
            "total": len(pending),
            "message": "Pornesc încărcarea zilelor…"
            if pending
            else "Zilele cerute sunt deja încărcate.",
        }
        if pending:
            self.task = asyncio.create_task(self.run(pending, today))
        return self.status()

    async def run(self, pending, today):
        requests = 0
        try:
            for sport, day in pending:
                if requests >= self.budget:
                    left = len(pending) - self.state["done"]
                    self.state.update(
                        status="partial",
                        message=f"Am atins limita de {self.budget} cereri FlashScore pentru o "
                        f"pregătire; mai sunt {left} zile de încărcat. Apasă din nou pentru "
                        "restul.",
                    )
                    return
                label = SPORTS[sport]["label"].lower()
                self.state["message"] = f"Rezultate {label} {day.isoformat()}…"
                ttl = PAST_DAY_TTL if day < today - timedelta(days=1) else None
                matches, cached, _ = await fetch_day(self.provider, day, sport, ttl=ttl)
                if not cached:
                    requests += 1
                    self.state["requests"] = requests
                finished = [m for m in matches if m.status == "finished"]
                await run_in_threadpool(self.save, matches)
                # Yesterday can still receive late results; re-check it next time.
                if day <= today - timedelta(days=2):
                    self.store.mark_synced(day, len(finished), sport)
                self.state["done"] += 1
                self.state["loaded_matches"] += len(finished)
            self.state.update(status="done", message="Ultimele zile sunt încărcate.")
        except ProviderError as exc:
            self.state.update(status="failed", message=str(exc))
        except asyncio.CancelledError:
            self.state.update(status="interrupted", message="Încărcare întreruptă.")
            raise
        except Exception:
            # Anything else (e.g. a locked database) must not leave the UI polling "running".
            log.exception("Recent days loader failed")
            self.state.update(
                status="failed",
                message="Încărcarea a eșuat. Zilele deja salvate rămân; reîncearcă.",
            )

    def save(self, matches):
        self.store.save_matches(matches)
        self.store.settle(matches)

    async def wait(self):
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


def loader_of(app):
    loader = getattr(app.state, "recent_loader", None)
    if loader is None:
        loader = RecentLoader(app.state.store, app.state.provider)
        app.state.recent_loader = loader
    return loader


@router.post("/api/simulate/recent/prepare", status_code=202)
async def prepare_recent(request: Request, body: PrepareRequest):
    return loader_of(request.app).start(body.days, body.sports, body.warmup_days)


@router.get("/api/simulate/recent/status")
async def recent_status(
    request: Request,
    days: Annotated[int | None, Query(ge=1, le=60)] = None,
    sports: Annotated[str | None, Query(max_length=60)] = None,
):
    chosen = None
    if sports:
        chosen = [s.strip() for s in sports.split(",") if s.strip()]
        if not chosen or any(s not in SPORTS for s in chosen):
            raise HTTPException(422, "Sport necunoscut.")
        chosen = list(dict.fromkeys(chosen))
    loader = loader_of(request.app)
    return await run_in_threadpool(loader.status, days, chosen)
