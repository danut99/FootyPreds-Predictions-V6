"""Local analytical slips with real quoted odds; no bookmaker transactions."""

import asyncio
import itertools
import math
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from footypreds.competitions import match_competition, priority
from footypreds.demo import demo_data
from footypreds.engine import VERSION, HistoryIndex, analyze, canonical
from footypreds.provider import ProviderError
from footypreds.recommend import MIN_VALUE, ODDS_WINDOW, eligible_legs, optimize
from footypreds.sports import SPORTS, analyze_match
from footypreds.sports.legs import ticket_status
from footypreds.sports.settle import settle

ENRICH_BUDGET = 8
ANALYSIS_LIMIT = 24


class PlanRequest(BaseModel):
    mode: Literal["week", "custom"] = "week"
    start_date: date
    target_odds: float = Field(default=2, ge=1.2, le=100)
    max_legs: int = Field(default=3, ge=1, le=5)
    # Deprecated and ignored: the optimizer picks the likeliest legs for the target odds by
    # itself. Still accepted (same bounds) so older clients keep working.
    min_probability: float | None = Field(default=None, ge=0.35, le=0.9)
    diverse_leagues: bool = True
    demo: bool = False
    competitions: list[str] = Field(default_factory=list, max_length=30)
    sports: list[str] = Field(default_factory=lambda: ["football"], min_length=1, max_length=3)

    @model_validator(mode="after")
    def future_window(self):
        today = datetime.now(timezone.utc).date()
        if not today <= self.start_date <= today + timedelta(days=30):
            raise ValueError("Alege o dată în următoarele 30 zile (UTC).")
        if any(sport not in SPORTS for sport in self.sports):
            raise ValueError("Sport necunoscut.")
        self.sports = [sport for sport in SPORTS if sport in self.sports]
        self.competitions = list(dict.fromkeys(self.competitions))
        if any(not value or len(value) > 200 or "|" not in value for value in self.competitions):
            raise ValueError("Identificator de competiție invalid.")
        if len(self.competitions) == 1:
            self.diverse_leagues = False
        return self


def choose_ticket(candidates, target, max_legs, diverse=True):
    """Find quoted odds within ±10% target; never invent a target-matching quote."""
    best, best_rank = None, None
    # Bound the combinatorial search (C(28, 5) ≈ 98k) by keeping the most likely legs.
    candidates = sorted(candidates, key=lambda leg: leg["probability"], reverse=True)[:28]
    for size in range(1, max_legs + 1):
        for legs in itertools.combinations(candidates, size):
            if len({leg["match_id"] for leg in legs}) != size:
                continue
            if (
                diverse
                and len({leg.get("competition_id", canonical(leg["league"])) for leg in legs})
                != size
            ):
                continue
            teams = [canonical(team) for leg in legs for team in (leg["home"], leg["away"])]
            if len(set(teams)) != size * 2:
                continue
            odds = math.prod(leg["odds"] for leg in legs)
            if not target * 0.9 <= odds <= target * 1.1:
                continue
            probability = math.prod(leg["probability"] for leg in legs)
            rank = (-abs(math.log(odds / target)), probability, -size)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = {
                    "legs": list(legs),
                    "total_odds": odds,
                    "estimated_probability": probability,
                    "target_odds": target,
                    "probability_assumption": "Independență aproximativă; model necalibrat.",
                    "status": "pending",
                    "version": VERSION,
                }
    return best


def candidates_for(match, prediction, quoted_at, now=None, min_value=True):
    """Plan legs of one fixture: the shared eligible legs (pre-match, grade A-C, real price in
    the leg odds band, no clearly negative value) plus the fields the plan UI shows."""
    if prediction["quality"] != "sufficient":
        return []
    legs = eligible_legs(match, prediction, now, MIN_VALUE if min_value else None)
    return [item | {"league": match.league, "quoted_at": quoted_at} for item in legs]


def competition_of(leg):
    return leg.get("competition_id") or canonical(leg["league"])


def plan_ticket(candidates, target, max_legs, diverse=True):
    """The likeliest ticket within [0.93, 1.12] x target (recommend.optimize), or None.

    Replaces the old "closest odds above a minimum probability" rule: there is no probability
    threshold, the optimizer maximizes the combined probability for the requested odds.
    """
    legs = optimize(candidates, target, max_legs, extra=competition_of if diverse else None)
    if not legs:
        return None
    total = math.prod(leg["odds"] for leg in legs)
    probability = math.prod(leg["probability"] for leg in legs)
    return {
        "legs": [dict(leg) for leg in legs],
        "total_odds": total,
        "estimated_probability": probability,
        "probability": probability,
        "ev": probability * total - 1,
        "target_odds": target,
        "window": [target * ODDS_WINDOW[0], target * ODDS_WINDOW[1]],
        "probability_assumption": "Independență aproximativă; model necalibrat.",
        "status": "pending",
        "version": VERSION,
    }


def unavailable_reason(day, request):
    details = day.get("diagnostics", {})
    low, high = request.target_odds * ODDS_WINDOW[0], request.target_odds * ODDS_WINDOW[1]
    if details and not details["upcoming"]:
        return "Nu există meciuri viitoare în această zi (UTC). Alege altă dată."
    if details and not details["in_competitions"]:
        return (
            "Competițiile selectate nu au meciuri viitoare în această zi. "
            "Alege altă dată sau alte competiții."
        )
    if details and not details["with_odds"]:
        return "Meciurile din competițiile alese nu au cote disponibile în API."
    if details.get("possible_without_diversity") and request.diverse_leagues:
        return (
            "Există o combinație potrivită în aceeași competiție. "
            "Debifează «Ligi diferite» și generează din nou."
        )
    if not day["candidates"]:
        if details.get("insufficient_history") == day["analyzed"] and day["analyzed"]:
            return (
                "Date insuficiente sau vechi pentru echipele acestor meciuri, chiar după "
                "analiza FlashScore (formă din toate competițiile și H2H)."
            )
        return (
            "Nicio selecție cu cote reale nu are o valoare acceptabilă în această zi. "
            "Schimbă data, sporturile sau competițiile."
        )
    return (
        f"Selecții eligibile: {day['candidates']} din {day['analyzed']} meciuri analizate, "
        f"dar nicio combinație în intervalul {low:.2f}–{high:.2f}. "
        "Modifică ținta, numărul maxim de selecții sau competițiile."
    )


def settle_plans(store):
    for plan in store.plans():
        if plan["request"]["demo"] or plan["status"] == "generating":
            continue
        changed = False
        for day in plan["days"]:
            ticket = day.get("ticket")
            if not ticket:
                continue
            for leg in ticket["legs"]:
                match = store.match(leg["match_id"])
                if match and match.status == "finished" and leg["status"] == "pending":
                    won = settle(
                        match.sport,
                        leg["key"],
                        match.home_goals,
                        match.away_goals,
                        match.finish_type or match.status,
                    )
                    leg["status"] = "void" if won is None else "won" if won else "lost"
                    leg["score"] = f"{match.home_goals}-{match.away_goals}"
                    changed = True
            # A ticket whose every leg is void is "void" (stake returned), not "won".
            ticket["status"] = ticket_status([leg["status"] for leg in ticket["legs"]])
        if changed:
            store.save_plan(plan)


class PlanBuilder:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.task = None

    def recover_interrupted(self):
        # A previous process cannot continue its job after restart.
        for plan in self.store.plans():
            if plan["status"] == "generating":
                plan.update(status="interrupted", message="Generarea a fost întreruptă de restart.")
                for day in plan["days"]:
                    if day["status"] in ("waiting", "analyzing"):
                        day.update(status="interrupted", reason=plan["message"])
                self.store.save_plan(plan)

    def start(self, request):
        if self.task is not None and not self.task.done():
            raise ProviderError("O generare rulează deja. Așteaptă finalizarea ei.", 409)
        count = 7 if request.mode == "week" else 1
        plan = {
            "id": uuid.uuid4().hex,
            "created": time.time(),
            "status": "generating",
            "request": request.model_dump(mode="json"),
            "progress": 0,
            "message": "Pregătim calendarul…",
            "warnings": [],
            "source": "synthetic" if request.demo else "flashscore",
            "days": [
                {
                    "date": (request.start_date + timedelta(days=i)).isoformat(),
                    "status": "waiting",
                    "ticket": None,
                }
                for i in range(count)
            ],
        }
        self.store.save_plan(plan)
        self.task = asyncio.create_task(self.generate(plan, request))
        return plan

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def generate(self, plan, request):
        try:
            for index, day in enumerate(plan["days"]):
                plan["message"] = f"Ziua {index + 1}: căutăm meciuri și cote disponibile…"
                day["status"] = "analyzing"
                self.store.save_plan(plan)
                if request.demo:
                    candidates, analyzed = self.demo_candidates(date.fromisoformat(day["date"]))
                    if request.competitions:
                        candidates = [
                            c for c in candidates if c["competition_id"] in request.competitions
                        ]
                else:
                    candidates, analyzed = await self.real_candidates(day, plan, request)
                now = datetime.now(timezone.utc)
                # Long API requests must not produce a ticket after a selection started.
                candidates = [c for c in candidates if datetime.fromisoformat(c["kickoff"]) > now]
                ticket = await run_in_threadpool(
                    plan_ticket,
                    candidates,
                    request.target_odds,
                    request.max_legs,
                    request.diverse_leagues,
                )
                day.update(
                    ticket=ticket,
                    status="ready" if ticket else "unavailable",
                    analyzed=analyzed,
                    candidates=len(candidates),
                )
                if ticket is None:
                    day["reason"] = unavailable_reason(day, request)
                plan["progress"] = index + 1
                self.store.save_plan(plan)
            ready = sum(day["ticket"] is not None for day in plan["days"])
            plan.update(
                status="ready" if ready == len(plan["days"]) else "partial",
                message=(
                    f"{ready}/{len(plan['days'])} bilete generate."
                    if ready
                    else "Analiză încheiată: niciun bilet eligibil. Vezi explicațiile de mai jos."
                ),
            )
        except asyncio.CancelledError:
            plan.update(
                status="interrupted", message="Generare întreruptă. Biletele existente rămân."
            )
            raise
        except ProviderError as exc:
            plan.update(status="failed", message=str(exc))
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Ticket generation failed: %s", plan["id"])
            plan.update(
                status="failed", message="Generarea a eșuat. Biletele deja create rămân salvate."
            )
        finally:
            if plan["status"] in ("failed", "interrupted"):
                for day in plan["days"]:
                    if day["status"] in ("waiting", "analyzing"):
                        day.update(status=plan["status"], reason=plan["message"])
            self.store.save_plan(plan)

    async def real_candidates(self, day, plan, request):
        target_day = date.fromisoformat(day["date"])
        matches, rejected = [], 0
        for sport in request.sports:
            # Football keeps the historical call form (older providers and test fakes).
            kwargs = {} if sport == "football" else {"sport": sport}
            found, _, dropped = await self.provider.fixtures(target_day, **kwargs)
            self.store.save_matches(found)
            matches += found
            rejected += dropped
        now = datetime.now(timezone.utc)
        future = [
            m
            for m in matches
            if m.status == "scheduled"
            and m.kickoff > now
            and m.kickoff.astimezone(timezone.utc).date() == target_day
        ]
        filtered = [
            m
            for m in future
            if not request.competitions or match_competition(m) in request.competitions
        ]
        pool = sorted([m for m in filtered if m.odds], key=priority)
        # Spread the analysis window across competitions, rather than the first kickoff times.
        groups = {}
        for match in pool:
            groups.setdefault(match_competition(match), []).append(match)
        chosen = []
        for position in range(max((len(group) for group in groups.values()), default=0)):
            for group in groups.values():
                if position < len(group):
                    chosen.append(group[position])
        chosen = chosen[:ANALYSIS_LIMIT]
        diagnostics = {
            "upcoming": len(future),
            "in_competitions": len(filtered),
            "with_odds": len(pool),
            "analysis_limit": ANALYSIS_LIMIT,
            "history_enriched": 0,
            "insufficient_history": 0,
            "below_threshold": 0,
            "history_errors": 0,
        }
        day["diagnostics"] = diagnostics
        if rejected:
            plan["warnings"].append(f"{day['date']}: {rejected} meciuri incomplete ignorate.")

        def analyse_all():
            # One history per sport: team names repeat across sports.
            indexes = {
                sport: HistoryIndex(self.store.matches(sport=sport))
                for sport in {m.sport for m in chosen}
            }
            return {m.id: analyze_match(m, indexes[m.sport]) for m in chosen}

        def collect_candidates():
            stamp = datetime.now(timezone.utc).isoformat()
            return [c for match in chosen for c in candidates_for(match, analyses[match.id], stamp)]

        def search():
            return run_in_threadpool(
                plan_ticket,
                collect_candidates(),
                request.target_odds,
                request.max_legs,
                request.diverse_leagues,
            )

        analyses = await run_in_threadpool(analyse_all)
        possible = await search()
        # One H2H request gives both teams' form in every competition; stop once a ticket exists.
        for index, match in enumerate(chosen):
            if possible or diagnostics["history_enriched"] >= ENRICH_BUDGET:
                break
            if analyses[match.id]["grade"] in ("A", "B"):
                continue
            plan["message"] = f"{day['date']} · analiză {index + 1}/{len(chosen)} · {match.home}"
            self.store.save_plan(plan)
            diagnostics["history_enriched"] += 1
            try:
                history, _, _ = await self.provider.head_to_head(match)
            except ProviderError as exc:
                if exc.status in (429, 503):
                    raise
                diagnostics["history_errors"] += 1
                plan["warnings"].append(f"Istoricul pentru {match.home} nu este disponibil.")
                continue
            self.store.save_matches(history)
            analyses = await run_in_threadpool(analyse_all)
            possible = await search()
        eligible = {m.id: bool(candidates_for(m, analyses[m.id], "")) for m in chosen}
        diagnostics["match_details"] = [
            {
                "sport": m.sport,
                "home": m.home,
                "away": m.away,
                "sample": analyses[m.id]["sample"],
                "quality": analyses[m.id]["quality"],
                "grade": analyses[m.id]["grade"],
                "eligible": eligible[m.id],
            }
            for m in chosen
        ]
        diagnostics["insufficient_history"] = sum(
            p["quality"] != "sufficient" for p in analyses.values()
        )
        # Kept for older clients: enough data, but no leg with a real price in the odds band
        # and an acceptable value.
        diagnostics["below_threshold"] = sum(
            analyses[m.id]["quality"] == "sufficient" and not eligible[m.id] for m in chosen
        )
        candidates = collect_candidates()
        diagnostics["eligible_selections"] = len(candidates)
        if candidates and possible is None:
            alternative = await run_in_threadpool(
                plan_ticket, candidates, request.target_odds, request.max_legs, False
            )
            diagnostics["possible_without_diversity"] = alternative is not None
        return candidates, len(chosen)

    @staticmethod
    def demo_candidates(day, minimum=None):
        """Synthetic legs for the demo plan (`minimum` is ignored, kept for old callers)."""
        history, fixtures = demo_data()
        candidates = []
        for i, match in enumerate(fixtures):
            when = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(
                hours=23, minutes=55
            )
            shift = when - match.kickoff
            match = match.model_copy(update={"kickoff": when, "id": f"demo-{day}-{i}"})
            past = [m.model_copy(update={"kickoff": m.kickoff + shift}) for m in history]
            prediction = analyze(match, past)
            # Explicit synthetic quotes for UI tests; never used as real bookmaker odds.
            for market in prediction["markets"]:
                if market["key"] in ("1", "X", "2", "1X", "X2", "over15", "under35"):
                    market["odds"] = round(max(1.05, 0.95 / market["probability"]), 2)
                    market["ev"] = market["probability"] * market["odds"] - 1
            # Separate fictional leagues to demonstrate diversity without real data.
            match = match.model_copy(update={"league": f"Demo League {i + 1}"})
            stamp = datetime.now(timezone.utc).isoformat()
            # Synthetic prices carry no value information: no value filter.
            candidates.extend(candidates_for(match, prediction, stamp, min_value=False))
        return candidates, len(fixtures)
