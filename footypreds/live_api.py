"""Live endpoints: in-play games and what can be bet on them now (docs/CONTRACTS.md §10.2).

GET /api/live?sport=football|basketball|tennis   every live game with in-play markets
GET /api/live/{match_id}?sport=...              one live game plus its live statistics

The list response is cached in memory for LIVE_TTL seconds per sport (the provider also caches
matches/live for LIVE_TTL), and live statistics for STATS_TTL seconds per match, so repeated
requests inside those windows never reach FlashScore.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from starlette.concurrency import run_in_threadpool

from footypreds.competitions import priority
from footypreds.live import DISCLAIMER, ODDS_NOTE, live_item
from footypreds.provider import LIVE_TTL, ProviderError
from footypreds.sports import SPORT_PATTERN

router = APIRouter(prefix="/api", tags=["live"])
Sport = Annotated[str, Query(pattern=SPORT_PATTERN)]
MatchId = Annotated[str, Path(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]
RESPONSE_TTL = LIVE_TTL
STATS_TTL = 60
# Pre-match analyses are CPU work: only the first games (board priority) get one per request;
# the others use their pre-match prices.
ANALYSIS_BUDGET = 40
log = logging.getLogger(__name__)


def clock():
    """Monotonic seconds; tests replace it to move time forward."""
    return time.monotonic()


class TTLCache:
    def __init__(self):
        self.items = {}

    def get(self, key):
        found = self.items.get(key)
        if found is None or found[0] <= clock():
            self.items.pop(key, None)
            return None
        return found[1]

    def put(self, key, value, ttl):
        self.items[key] = (clock() + ttl, value)
        if len(self.items) > 500:
            now = clock()
            self.items = {k: v for k, v in self.items.items() if v[0] > now}


def caches(app):
    """Per-app caches kept on app.state (created on first use)."""
    found = getattr(app.state, "live_caches", None)
    if found is None:
        found = {"list": TTLCache(), "stats": TTLCache()}
        app.state.live_caches = found
    return found


def with_stored(app, match):
    """(live match with the stored pre-match prices added, pre-match analysis or None)."""
    stored = app.state.store.match(match.id)
    if stored is None or stored.sport != match.sport:
        return match, None
    # The live feed's 1/X/2 are the list-by-date prices; enrichment may have added more.
    update = {"odds": {**stored.odds, **match.odds}}
    # Crests/flags of the stored fixture when the live row has none.
    for field in ("home_logo", "away_logo", "league_logo"):
        if not getattr(match, field) and getattr(stored, field):
            update[field] = getattr(stored, field)
    match = match.model_copy(update=update)
    return match, stored


def prematch_analysis(app, stored):
    """The stored fixture's pre-match analysis (never sees the live score), or None."""
    if stored is None:
        return None
    fixture = stored
    if stored.status != "scheduled":
        fixture = stored.model_copy(
            update={
                "status": "scheduled",
                "home_goals": None,
                "away_goals": None,
                "live": {},
                "finish_type": "",
            }
        )
    try:
        return app.state.cache.get(fixture, getattr(app.state, "ledger_threshold", 0.85))
    except Exception:  # an analysis failure must not hide the live game
        log.exception("Pre-match analysis failed for %s", stored.id)
        return None


def build_items(app, matches, budget=ANALYSIS_BUDGET):
    items = []
    for n, match in enumerate(sorted(matches, key=priority)):
        try:
            match, stored = with_stored(app, match)
            analysis = prematch_analysis(app, stored) if n < budget else None
            items.append(live_item(match, analysis))
        except Exception:  # one malformed game must not break the whole list
            log.exception("Live analysis failed for %s", match.id)
    return items


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@router.get("/live")
async def live_list(request: Request, sport: Sport = "football", refresh: bool = False):
    app = request.app
    cache = caches(app)["list"]
    if not refresh:
        found = cache.get(sport)
        if found is not None:
            return {**found, "cached": True}
    matches, provider_cached, rejected = await app.state.provider.live(sport, refresh=refresh)
    items = await run_in_threadpool(build_items, app, matches)
    updated = now_iso()
    body = {
        "sport": sport,
        "cached": False,
        "provider_cached": provider_cached,
        "updated_at": updated,
        "updated": updated,
        "count": len(items),
        "rejected": rejected,
        "matches": items,
        "odds_note": ODDS_NOTE,
        "disclaimer": DISCLAIMER,
        "ttl": RESPONSE_TTL,
    }
    cache.put(sport, body, RESPONSE_TTL)
    return body


async def live_stats(app, match_id, refresh=False):
    """({period: rows}, warning or None); cached STATS_TTL s per match."""
    cache = caches(app)["stats"]
    if not refresh:
        found = cache.get(match_id)
        if found is not None:
            return found, None
    try:
        stats = await app.state.provider.match_stats(match_id, refresh=refresh)
    except ProviderError as exc:
        if exc.status in (429, 503):
            raise
        return {}, "Statisticile live nu sunt disponibile acum."
    cache.put(match_id, stats, STATS_TTL)
    return stats, None


@router.get("/live/{match_id}")
async def live_detail(
    request: Request, match_id: MatchId, sport: Sport = "football", refresh: bool = False
):
    app = request.app
    matches, _, _ = await app.state.provider.live(sport, refresh=refresh)
    match = next((m for m in matches if m.id == match_id), None)
    if match is None:
        raise HTTPException(404, "Meciul nu este live acum.")
    stats, warning = await live_stats(app, match_id, refresh)

    def compute():
        current, stored = with_stored(app, match)
        return live_item(current, prematch_analysis(app, stored), stats)

    item = await run_in_threadpool(compute)
    if warning:
        item["notes"].insert(0, warning)
    updated = now_iso()
    return {
        **item,
        "stats": stats,
        "updated_at": updated,
        "updated": updated,
        "disclaimer": DISCLAIMER,
    }
