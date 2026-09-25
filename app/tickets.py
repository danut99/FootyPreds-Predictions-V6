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

from app.competitions import match_competition, predict_competition, priority
from app.demo import demo_data
from app.model import VERSION, canonical, outcome, predict
from app.provider import ProviderError


class PlanRequest(BaseModel):
    mode: Literal["week", "custom"] = "week"
    start_date: date
    target_odds: float = Field(default=2, ge=1.2, le=100)
    max_legs: int = Field(default=3, ge=1, le=5)
    min_probability: float = Field(default=0.55, ge=0.35, le=0.9)
    diverse_leagues: bool = True
    demo: bool = False
    competitions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def future_window(self):
        today = datetime.now(timezone.utc).date()
        if not today <= self.start_date <= today + timedelta(days=30):
            raise ValueError("Alege o dată în următoarele 30 zile (UTC).")
        self.competitions = list(dict.fromkeys(self.competitions))
        if any(not value or len(value) > 200 or "|" not in value for value in self.competitions):
            raise ValueError("Identificator de competiție invalid.")
        if len(self.competitions) == 1:
            self.diverse_leagues = False
        return self


def choose_ticket(candidates, target, max_legs, diverse=True):
    """Find quoted odds within ±10% target; never invent a target-matching quote."""
    best, best_rank = None, None
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


def candidates_for(match, prediction, minimum, quoted_at):
    if prediction["quality"] != "sufficient":
        return []
    return [
        {
            "match_id": match.id,
            "home": match.home,
            "away": match.away,
            "league": match.league,
            "competition_id": match_competition(match),
            "kickoff": match.kickoff.isoformat(),
            "key": market["key"],
            "label": market["label"],
            "odds": market["odds"],
            "probability": market["probability"],
            "quoted_at": quoted_at,
            "status": "pending",
        }
        for market in prediction["markets"]
        if market["odds"] is not None and market["probability"] >= minimum
    ]


def unavailable_reason(day, request):
    details = day.get("diagnostics", {})
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
                "Istoric insuficient în competiția selectată: minimum 8 rezultate per echipă "
                "în ultimii 2 ani, inclusiv unul în ultimele 90 zile. "
                "Rezultatele din alte ligi sau cupe nu sunt incluse în acest model."
            )
        return (
            "Nicio selecție cu cote disponibile nu trece pragul ales. "
            "Schimbă data, competițiile sau pragul."
        )
    return (
        f"Selecții eligibile: {day['candidates']} din {day['analyzed']} meciuri analizate, "
        f"dar nicio combinație în intervalul {request.target_odds * 0.9:.2f}"
        f"–{request.target_odds * 1.1:.2f}. "
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
                    leg["status"] = (
                        "won" if outcome(leg["key"], match.home_goals, match.away_goals) else "lost"
                    )
                    leg["score"] = f"{match.home_goals}-{match.away_goals}"
                    changed = True
            statuses = [leg["status"] for leg in ticket["legs"]]
            ticket["status"] = (
                "lost"
                if "lost" in statuses
                else "won"
                if all(s == "won" for s in statuses)
                else "pending"
            )
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
                    candidates, analyzed = self.demo_candidates(
                        date.fromisoformat(day["date"]), request.min_probability
                    )
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
                    choose_ticket,
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
        matches, _, rejected = await self.provider.fixtures(target_day)
        self.store.save_matches(matches)
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
        chosen = chosen[:24]
        diagnostics = {
            "upcoming": len(future),
            "in_competitions": len(filtered),
            "with_odds": len(pool),
            "analysis_limit": 24,
            "history_enriched": 0,
            "insufficient_history": 0,
            "below_threshold": 0,
            "history_errors": 0,
            "deep_history_matches": 0,
        }
        day["diagnostics"] = diagnostics
        analyses = {}
        stored = self.store.matches()
        for match in chosen:
            analyses[match.id] = await run_in_threadpool(predict_competition, match, stored)
        if rejected:
            plan["warnings"].append(f"{day['date']}: {rejected} meciuri incomplete ignorate.")

        def collect_candidates():
            stamp = datetime.now(timezone.utc).isoformat()
            return [
                c
                for match in chosen
                for c in candidates_for(match, analyses[match.id], request.min_probability, stamp)
            ]

        candidates = collect_candidates()
        already_possible = await run_in_threadpool(
            choose_ticket,
            candidates,
            request.target_odds,
            request.max_legs,
            request.diverse_leagues,
        )
        for index, match in enumerate(chosen):
            if already_possible or diagnostics["history_enriched"] >= 8:
                break
            if analyses[match.id]["quality"] == "sufficient":
                continue
            plan["message"] = f"{day['date']} · analiză {index + 1}/{len(chosen)} · {match.home}"
            self.store.save_plan(plan)
            diagnostics["history_enriched"] += 1
            try:
                history, warnings = await self.provider.history(match)
            except ProviderError as exc:
                if exc.status in (429, 503):
                    raise
                diagnostics["history_errors"] += 1
                plan["warnings"].append(f"Istoricul pentru {match.home} nu este disponibil.")
                continue
            self.store.save_matches(history)
            plan["warnings"].extend(warnings)
            analyses[match.id] = await run_in_threadpool(
                predict_competition, match, self.store.matches()
            )
            candidates = collect_candidates()
            already_possible = await run_in_threadpool(
                choose_ticket,
                candidates,
                request.target_odds,
                request.max_legs,
                request.diverse_leagues,
            )
        # A cup's recent team-results page can contain mostly domestic-league games.
        # Deepen the two closest-to-eligible fixtures, with at most eight extra requests.
        if already_possible is None and request.competitions:
            stored = self.store.matches()
            for match in chosen:
                analyses[match.id] = await run_in_threadpool(predict_competition, match, stored)
            missing = sorted(
                [m for m in chosen if analyses[m.id]["quality"] != "sufficient"],
                key=lambda m: min(
                    analyses[m.id]["sample"]["home"], analyses[m.id]["sample"]["away"]
                ),
                reverse=True,
            )
            for match in missing[:2]:
                # Old results cannot repair the lack of any recent observation.
                if (
                    min(analyses[match.id]["sample"]["home"], analyses[match.id]["sample"]["away"])
                    >= 8
                ):
                    continue
                diagnostics["deep_history_matches"] += 1
                plan["message"] = f"Istoric extins · {match.home} – {match.away} · paginile 2–3"
                self.store.save_plan(plan)
                try:
                    history, warnings = await self.provider.history(match, start_page=2, pages=2)
                except ProviderError as exc:
                    if exc.status in (429, 503):
                        raise
                    diagnostics["history_errors"] += 1
                    plan["warnings"].append(
                        f"Istoricul extins pentru {match.home} nu este disponibil."
                    )
                    continue
                self.store.save_matches(history)
                plan["warnings"].extend(warnings)
                stored = self.store.matches()
                for item in chosen:
                    analyses[item.id] = await run_in_threadpool(predict_competition, item, stored)
                already_possible = await run_in_threadpool(
                    choose_ticket,
                    collect_candidates(),
                    request.target_odds,
                    request.max_legs,
                    request.diverse_leagues,
                )
                if already_possible:
                    break
        # Every fixture must see the final shared history, including results fetched for opponents.
        stored = self.store.matches()
        for match in chosen:
            analyses[match.id] = await run_in_threadpool(predict_competition, match, stored)
        diagnostics["match_details"] = [
            {
                "home": m.home,
                "away": m.away,
                "sample": analyses[m.id]["sample"],
                "quality": analyses[m.id]["quality"],
                "eligible": bool(candidates_for(m, analyses[m.id], request.min_probability, "")),
            }
            for m in chosen
        ]
        diagnostics["insufficient_history"] = sum(
            p["quality"] != "sufficient" for p in analyses.values()
        )
        diagnostics["below_threshold"] = sum(
            p["quality"] == "sufficient"
            and not candidates_for(match, p, request.min_probability, "")
            for match in chosen
            for p in [analyses[match.id]]
        )
        candidates = collect_candidates()
        diagnostics["eligible_selections"] = len(candidates)
        if candidates and already_possible is None:
            alternative = await run_in_threadpool(
                choose_ticket, candidates, request.target_odds, request.max_legs, False
            )
            diagnostics["possible_without_diversity"] = alternative is not None
        return candidates, len(chosen)

    @staticmethod
    def demo_candidates(day, minimum):
        history, fixtures = demo_data()
        candidates = []
        for i, match in enumerate(fixtures):
            when = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(
                hours=23, minutes=55
            )
            shift = when - match.kickoff
            match = match.model_copy(update={"kickoff": when, "id": f"demo-{day}-{i}"})
            past = [m.model_copy(update={"kickoff": m.kickoff + shift}) for m in history]
            prediction = predict(match, past)
            # Explicit synthetic quotes for UI tests; never used as real bookmaker odds.
            for market in prediction["markets"]:
                if market["key"] in ("1", "X", "2", "1X", "X2", "over15", "under35"):
                    market["odds"] = round(max(1.05, 0.95 / market["probability"]), 2)
            # Separate fictional leagues to demonstrate diversity without real data.
            match = match.model_copy(update={"league": f"Demo League {i + 1}"})
            candidates.extend(
                candidates_for(match, prediction, minimum, datetime.now(timezone.utc).isoformat())
            )
        return candidates, len(fixtures)
