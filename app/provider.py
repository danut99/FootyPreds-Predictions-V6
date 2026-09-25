import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import ValidationError

from app.domain import Match

HOST = "flashscore4.p.rapidapi.com"


class ProviderError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def normalize_matches(payload, *, results=False):
    """Parse tournament groups and H2H rows; never infer missing goals as zero."""
    output, rejected = {}, 0
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("results", payload.get("matches")))
    if not isinstance(payload, list):
        raise ProviderError("FlashScore a returnat un format necunoscut.")

    def visit(rows, league="", country=""):
        nonlocal rejected
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            if isinstance(row.get("matches"), list):
                visit(row["matches"], row.get("name", league), row.get("country_name", country))
                continue
            try:
                home, away = row["home_team"], row["away_team"]
                state = row.get("match_status") or {}
                scores = row.get("scores") or {}
                hg = scores.get("home", home.get("score"))
                ag = scores.get("away", away.get("score"))
                status = "scheduled"
                if state.get("is_finished") or (results and hg is not None and ag is not None):
                    status = "finished"
                elif state.get("is_started") or state.get("is_in_progress"):
                    status = "live"
                # Unknown/abandoned/postponed states must not be treated as pre-match.
                raw_status = str(row.get("status", "")).lower()
                if any(s in raw_status for s in ("postpon", "cancel", "abandon", "award")):
                    status = "unavailable"
                odds = {}
                for k, v in (row.get("odds") or {}).items():
                    try:
                        odds[k] = float(v)
                    except (TypeError, ValueError):
                        continue
                match = Match(
                    id=str(row["match_id"]),
                    kickoff=datetime.fromtimestamp(float(row["timestamp"]), timezone.utc),
                    league=row.get("tournament_name") or league or "Unknown",
                    country=country,
                    home=home["name"],
                    away=away["name"],
                    home_id=home.get("team_id") or "",
                    away_id=away.get("team_id") or "",
                    status=status,
                    home_goals=hg if hg != "" else None,
                    away_goals=ag if ag != "" else None,
                    odds=odds,
                )
                output[match.id] = match
            except (KeyError, TypeError, ValueError, OverflowError, ValidationError):
                rejected += 1

    visit(payload)
    return list(output.values()), rejected


class FlashScore:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store = settings, store
        self.client = httpx.AsyncClient(
            base_url=f"https://{HOST}/api/flashscore/v2/",
            headers={
                "accept": "application/json",
                "x-rapidapi-host": HOST,
                "x-rapidapi-key": settings.api_key,
            },
            timeout=httpx.Timeout(25),
            follow_redirects=False,
            transport=transport,
        )
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def get(self, endpoint, params, refresh=False):
        if not self.settings.api_key:
            raise ProviderError(
                "Adaugă RAPIDAPI_KEY în fișierul .env și repornește aplicația.", 503
            )
        key = endpoint + json.dumps(params, sort_keys=True)
        async with self.lock:
            cached = self.store.get_cache(key)
            if cached is not None and not refresh:
                return cached, True
            await asyncio.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            try:
                self.last_request = time.monotonic()
                response = await self.client.get(endpoint, params=params)
            except httpx.RequestError as exc:
                raise ProviderError("Conexiunea FlashScore a eșuat. Încearcă din nou.") from exc
            if response.status_code == 429:
                raise ProviderError("Limita RapidAPI a fost atinsă. Reîncearcă mai târziu.", 429)
            if response.status_code in (401, 403):
                raise ProviderError("Cheia RapidAPI sau abonamentul FlashScore nu este valid.", 503)
            if response.status_code != 200:
                raise ProviderError(f"FlashScore nu este disponibil (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError("FlashScore nu a returnat JSON valid.") from exc
            if isinstance(payload, dict) and (payload.get("error") or payload.get("message")):
                raise ProviderError("FlashScore a refuzat cererea. Verifică abonamentul API.")
            self.store.put_cache(key, payload, self.settings.cache_ttl)
            return payload, False

    async def fixtures(self, day, refresh=False):
        payload, cached = await self.get(
            "matches/list-by-date",
            {"date": day.isoformat(), "sport_id": 1},
            refresh,
        )
        matches, rejected = normalize_matches(payload)
        return matches, cached, rejected

    async def history(self, match, *, start_page=1, pages=1):
        # Team results have stable team IDs and a well-defined completed-results endpoint.
        rows, warnings = [], []
        for team_id in (match.home_id, match.away_id):
            if team_id:
                seen = set()
                for page in range(start_page, start_page + pages):
                    payload, _ = await self.get("teams/results", {"team_id": team_id, "page": page})
                    parsed, rejected = normalize_matches(payload, results=True)
                    fresh = [row for row in parsed if row.id not in seen]
                    if rejected:
                        warnings.append(f"{rejected} rezultate incomplete au fost ignorate.")
                    if not fresh:
                        break
                    rows.extend(fresh)
                    seen.update(row.id for row in fresh)
                    if max(row.kickoff for row in fresh) < match.kickoff - timedelta(days=730):
                        break
        return list({r.id: r for r in rows}.values()), warnings
