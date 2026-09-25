import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from pydantic import ValidationError

from footypreds.domain import Match, image_url
from footypreds.sports import SPORTS
from footypreds.sports.odds import parse_odds

HOST = "flashscore4.p.rapidapi.com"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# matches/live changes every few seconds; a short cache still protects the quota.
LIVE_TTL = 30


class ProviderError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def mapping(value, name):
    """A nested object of the payload; None/missing is empty, any other type is malformed."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{name} is not an object")
    return value


# Status texts of a game still being played. Finished variants ("FINISHED", "AET",
# "After Penalties") contain none of them.
IN_PLAY_WORDS = ("live", "half", "progress")


# Image fields of a team/player, best first. "smaill_image_path" is a typo of the live feed.
LOGO_FIELDS = ("small_image_path", "smaill_image_path", "image_path")


def logo_of(item):
    """First allowed image URL of a team, player or tournament group (domain.image_url)."""
    for name in LOGO_FIELDS:
        url = image_url(item.get(name))
        if url:
            return url
    return None


def team_of(value, name):
    """A side of the fixture; tennis doubles list two players under one participant id."""
    if isinstance(value, list):
        players = [mapping(player, name) for player in value]
        if not players or not all(p.get("name") for p in players):
            raise KeyError(name)
        return {
            "name": "/".join(str(p["name"]) for p in players),
            "team_id": "/".join(str(p.get("team_id") or "") for p in players).strip("/"),
            "event_participant_id": players[0].get("event_participant_id") or "",
            # Doubles: the first player's flag that is allowed.
            "small_image_path": next((logo_of(p) for p in players if logo_of(p)), None),
        }
    return mapping(value, name)


def finish_type_of(raw_status, state):
    """How a finished game ended: "", "aet", "penalties", "retired" or "walkover"."""
    text = f"{raw_status} {str(state.get('stage') or '').lower()}"
    if "walkover" in text:
        return "walkover"
    if "retire" in text:
        return "retired"
    if state.get("is_finished_after_penalties") or "penalt" in text:
        return "penalties"
    if state.get("is_finished_after_extra_time") or "extra_time" in text or "aet" in text.split():
        return "aet"
    return ""


def period_of(sport, stage):
    """Short period code of a live stage text, e.g. "2H", "HT", "Q3", "OT", "S2"."""
    text = str(stage or "").strip().lower()
    if not text:
        return ""
    if "half time" in text or text == "halftime":
        return "HT"
    if "break" in text or "pause" in text:
        return "BREAK"
    if "interrupt" in text:
        return "INT"
    if "overtime" in text or "extra time" in text:
        return "OT" if sport == "basketball" else "ET"
    if "penalt" in text:
        return "PEN"
    ordinal = next((n for n in range(1, 6) if text.startswith(str(n))), None)
    if "quarter" in text and ordinal:
        return f"Q{ordinal}"
    if "half" in text and ordinal:
        return f"{ordinal}H"
    if text.startswith("set "):
        number = text[4:].strip()
        return f"S{number}" if number.isdigit() else ""
    return ""


def live_info(sport, state, home, away):
    """Match.live of an in-play game: stage, clock, minute, period and red cards."""
    minute = state.get("live_minute")
    info = {
        "stage": str(state.get("stage") or ""),
        "clock": str(state.get("live_time") or ""),
        "minute": int(minute)
        if isinstance(minute, (int, float)) and not isinstance(minute, bool) and minute >= 0
        else None,
        "period": period_of(sport, state.get("stage")),
    }
    cards = (home.get("red_cards"), away.get("red_cards"))
    if sport == "football" and all(
        isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in cards
    ):
        info["red_cards"] = {"home": cards[0], "away": cards[1]}
    return info


def normalize_matches(payload, *, results=False, sport="football"):
    """Parse tournament groups and H2H rows; never infer missing goals as zero."""
    output, rejected = {}, 0
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("results", payload.get("matches")))
    if not isinstance(payload, list):
        raise ProviderError("FlashScore a returnat un format necunoscut.")
    # "-" is the feed's placeholder: a fixture without a score yet. In a results feed it is an
    # incomplete row and stays rejected by validation.
    placeholders = ("",) if results else ("", "-")

    def visit(rows, league="", country="", league_logo=None):
        nonlocal rejected
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            if isinstance(row.get("matches"), list):
                # null name/country_name must not replace the parent's values with None.
                visit(
                    row["matches"],
                    row.get("name") or league,
                    row.get("country_name") or country,
                    logo_of(row) or league_logo,
                )
                continue
            try:
                home = team_of(row["home_team"], "home_team") or None
                away = team_of(row["away_team"], "away_team") or None
                if home is None or away is None:
                    raise KeyError("team")
                state = mapping(row.get("match_status"), "match_status")
                scores = mapping(row.get("scores"), "scores")
                hg = scores.get("home", home.get("score"))
                ag = scores.get("away", away.get("score"))
                status = "scheduled"
                raw_status = str(row.get("status", "")).lower()
                # An explicit in-play status text ("LIVE", "1st Half", "Half Time") means the
                # score is partial, even in a results feed.
                playing = any(s in raw_status for s in IN_PLAY_WORDS)
                # Results/H2H feeds: two scores make a result (their match_status flags are not
                # reliable; tests/test_api.py::test_results_parser_zero_is_valid).
                scored = results and hg is not None and ag is not None and not playing
                if state.get("is_finished") or scored:
                    status = "finished"
                elif state.get("is_started") or state.get("is_in_progress") or playing:
                    status = "live"
                # Unknown/abandoned/postponed states must not be treated as pre-match.
                if any(s in raw_status for s in ("postpon", "cancel", "abandon", "award")):
                    status = "unavailable"
                finish = finish_type_of(raw_status, state)
                if finish == "walkover" and any(v is None or v in placeholders for v in (hg, ag)):
                    # A walkover has no score and says nothing about form: not an error. In a
                    # fixtures feed the game will not be played.
                    if results:
                        continue
                    status, hg, ag = "unavailable", None, None
                elif status != "finished":
                    finish = ""
                league_name = row.get("tournament_name") or league or "Unknown"
                surface = row.get("surface")
                if sport == "tennis" and isinstance(surface, str) and surface.strip():
                    # H2H rows name only the tournament; keep FlashScore's ", surface" suffix.
                    if not league_name.lower().endswith(surface.strip().lower()):
                        league_name = f"{league_name}, {surface.strip().lower()}"
                odds = {}
                quoted = row.get("odds")
                # Malformed odds only cost the prices, not the fixture.
                for k, v in (quoted if isinstance(quoted, dict) else {}).items():
                    try:
                        odds[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
                match = Match(
                    id=str(row["match_id"]),
                    # fromtimestamp() raises OSError on Windows before 1970 (old H2H games).
                    kickoff=EPOCH + timedelta(seconds=float(row["timestamp"])),
                    league=league_name,
                    country=country,
                    home=home["name"],
                    away=away["name"],
                    home_id=home.get("team_id") or "",
                    away_id=away.get("team_id") or "",
                    status=status,
                    home_goals=None if hg in placeholders else hg,
                    away_goals=None if ag in placeholders else ag,
                    odds=odds,
                    sport=sport,
                    home_participant_id=str(home.get("event_participant_id") or ""),
                    away_participant_id=str(away.get("event_participant_id") or ""),
                    live=live_info(sport, state, home, away) if status == "live" else {},
                    finish_type=finish,
                    home_logo=logo_of(home),
                    away_logo=logo_of(away),
                    league_logo=league_logo,
                )
                output[match.id] = match
            except (
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
                OSError,
                ValidationError,
            ):
                rejected += 1

    visit(payload)
    return list(output.values()), rejected


def parse_standings(payload):
    rows = []
    for position, row in enumerate(payload if isinstance(payload, list) else [], 1):
        try:
            scored, conceded = (int(x) for x in str(row.get("goals", "0:0")).split(":", 1))
            rows.append(
                {
                    "position": position,
                    "team_id": row.get("team_id") or "",
                    "name": row["name"],
                    "played": int(row.get("matches_played") or 0),
                    "wins": int(row.get("wins") or 0),
                    "draws": int(row.get("draws") or 0),
                    "losses": int(row.get("losses") or 0),
                    "scored": scored,
                    "conceded": conceded,
                    "points": int(row.get("points") or 0),
                }
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            # e.g. group headers or strings instead of row objects.
            continue
    return rows


def stat_number(value):
    """First number of a stat value: 0.84, "29%" -> 29.0, "71% (5/7)" -> 71.0; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value or ""))
    return float(found.group(1)) if found else None


def parse_stats(payload):
    """matches/match/stats -> {period: [{name, home, away, home_value, away_value}]}.

    Periods are the payload keys ("match", "1st-half", ...); a repeated stat name keeps its
    first row. Malformed rows are skipped.
    """
    output = {}
    for period, rows in payload.items() if isinstance(payload, dict) else []:
        if not isinstance(rows, list):
            continue
        seen, stats = set(), []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            if row["name"] in seen:
                continue
            seen.add(row["name"])
            home, away = row.get("home_team"), row.get("away_team")
            stats.append(
                {
                    "name": row["name"],
                    "home": home,
                    "away": away,
                    "home_value": stat_number(home),
                    "away_value": stat_number(away),
                }
            )
        output[str(period)] = stats
    return output


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

    async def get(self, endpoint, params, refresh=False, ttl=None):
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
            self.store.put_cache(key, payload, ttl or self.settings.cache_ttl)
            return payload, False

    async def fixtures(self, day, sport="football", refresh=False, ttl=None):
        payload, cached = await self.get(
            "matches/list-by-date",
            {"date": day.isoformat(), "sport_id": SPORTS[sport]["id"]},
            refresh,
            ttl,
        )
        matches, rejected = normalize_matches(payload, sport=sport)
        return matches, cached, rejected

    async def live(self, sport="football", refresh=False):
        """In-play games of `sport` (status "live", Match.live filled), cached LIVE_TTL s."""
        payload, cached = await self.get(
            "matches/live", {"sport_id": SPORTS[sport]["id"]}, refresh, LIVE_TTL
        )
        if isinstance(payload, list) and not payload:
            return [], cached, 0
        matches, rejected = normalize_matches(payload, sport=sport)
        return [m for m in matches if m.status == "live"], cached, rejected

    async def match_stats(self, match_id, refresh=False):
        """Live/final statistics {period: [stat rows]} (parse_stats), cached LIVE_TTL s."""
        payload, _ = await self.get(
            "matches/match/stats", {"match_id": match_id}, refresh, LIVE_TTL
        )
        return parse_stats(payload)

    async def match_odds(self, match, refresh=False):
        """{market_key: {"best", "avg", "books"}} from matches/odds, for the match's sport."""
        payload, _ = await self.get(
            "matches/odds", {"match_id": match.id}, refresh, self.settings.history_ttl
        )
        sets = 3
        if match.sport == "tennis":
            from footypreds.sports.tennis import best_of

            sets = best_of(match)
        return parse_odds(
            payload, match.sport, match.home_participant_id, match.away_participant_id, sets
        )

    async def head_to_head(self, match, refresh=False):
        """ONE request: both teams' recent results in every competition, plus mutual games."""
        payload, cached = await self.get(
            "matches/h2h", {"match_id": match.id}, refresh, self.settings.history_ttl
        )
        if isinstance(payload, list) and not payload:
            return [], cached, 0
        rows, rejected = normalize_matches(payload, results=True, sport=match.sport)
        # Only completed games before this fixture can be history.
        rows = [r for r in rows if r.status == "finished" and r.kickoff < match.kickoff]
        return rows, cached, rejected

    async def standings(self, match):
        try:
            payload, _ = await self.get(
                "matches/standings",
                {"match_id": match.id, "type": "overall"},
                ttl=self.settings.history_ttl,
            )
        except ProviderError as exc:
            if exc.status in (429, 503):
                raise
            return []
        return parse_standings(payload)

    async def history(self, match, *, start_page=1, pages=1):
        # Team results have stable team IDs and a well-defined completed-results endpoint.
        rows, warnings = [], []
        for team_id in (match.home_id, match.away_id):
            if team_id:
                seen = set()
                for page in range(start_page, start_page + pages):
                    payload, _ = await self.get("teams/results", {"team_id": team_id, "page": page})
                    parsed, rejected = normalize_matches(payload, results=True, sport=match.sport)
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
