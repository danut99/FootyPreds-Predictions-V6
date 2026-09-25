"""Offline FootyPreds: the REAL app with a fake FlashScore, fake crests and synthetic datasets.

    .\\.venv\\Scripts\\python.exe footypreds/scripts/mock_server.py --port 8765 [--seed 7]
        [--today 2026-09-26]

What it serves (nothing reaches the network, the real database is never opened):
- `create_app` with a temporary SQLite database (deleted on exit) and an `httpx.MockTransport`
  that answers every FlashScore endpoint from the captured payloads in
  `footypreds/tests/fixtures/flashscore/`: day lists (football is built from the captured live
  teams plus a small Premier League day), live lists, H2H, matches/odds, standings and stats.
- Day lists are re-timestamped around "now": today's games start after now (so the board, the
  recommendations and the ticket generator have upcoming games), live games started ~50 min ago,
  past days are FINISHED with deterministic results and their pre-match 1X2 odds (history sync
  and the simulator's "recent" days), future days keep the captured time of day. Pairings rotate
  from day to day and match ids get a `-pN` / `-nN` suffix outside the fixture day.
- Every team of the day lists gets a short synthetic history, so analyses reach grades A-C.
- /api/img answers from a fake image transport: small PNG crests and flags generated from the
  URL, so the UI shows images offline.
- /api/simulate uses a synthetic benchmark (`football`) plus two extra football-data style
  leagues (`football-plus`) ending yesterday.

--today sets the fixture day (default: today, UTC); --seed changes the synthetic data.
Ctrl+C stops the server and removes the temporary folder.
"""

import argparse
import asyncio
import copy
import csv
import hashlib
import io
import json
import math
import random
import re
import shutil
import socket
import struct
import sys
import tempfile
import zlib
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from footypreds.config import PACKAGE, Settings  # noqa: E402
from footypreds.domain import IMAGE_HOSTS  # noqa: E402
from footypreds.provider import FlashScore, normalize_matches  # noqa: E402

FIXTURES = PACKAGE / "tests" / "fixtures" / "flashscore"
SPORT_IDS = {"1": "football", "2": "tennis", "3": "basketball"}
# The captured lists and H2H rows were recorded around this day.
CAPTURE_DAY = date(2026, 9, 26)
# Fixture-day matches whose matches/odds (and H2H) answers are captured payloads.
CAPTURED_ODDS = {"mockfb0": "football", "KnR6QDo1": "tennis", "dU94shVA": "basketball"}
CAPTURED_H2H = {"KnR6QDo1": "tennis", "KMHepeEM": "basketball"}
LOGO_ROOT = "https://static.flashscore.com/res/image/data/"
PREMIER_LEAGUE = [
    ("Arsenal", "Chelsea"),
    ("Liverpool", "Everton"),
    ("Leeds", "Burnley"),
    ("Tottenham", "Newcastle"),
    ("Aston Villa", "Brighton"),
]
HISTORY_GAMES = 10
LIVE_AGE = timedelta(minutes=50)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def digest(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:12], 16)


def team_name(team):
    if isinstance(team, list):
        return "/".join(str(p.get("name")) for p in team)
    return str(team.get("name"))


def pmf(lam, k):
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson(rng, lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def price(p, margin=1.06):
    return round(min(50.0, max(1.02, 1 / (max(p, 1e-6) * margin))), 2)


# --- images ---------------------------------------------------------------------------------


def png(width, height, pixel):
    """A tiny RGBA PNG encoder (no Pillow needed)."""
    raw = b"".join(
        b"\x00" + bytes(c for x in range(width) for c in pixel(x, y)) for y in range(height)
    )

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def colour(seed):
    return (40 + seed % 180, 40 + (seed >> 8) % 180, 40 + (seed >> 16) % 180)


def crest(url):
    """Round two-colour crest (teams, leagues) or a three-stripe flag (flagcdn)."""
    seed = digest(url)
    first, second = colour(seed), colour(seed >> 24)
    if "flagcdn.com" in url:
        third = colour(seed >> 12)
        return png(40, 28, lambda x, y: (*(first, second, third)[min(2, y * 3 // 28)], 255))

    def pixel(x, y):
        dx, dy = x - 15.5, y - 15.5
        radius = math.hypot(dx, dy)
        if radius > 15.5:
            return (0, 0, 0, 0)
        if radius > 12.5:
            return (*second, 255)
        return (*(first if (x + y) % 16 < 8 else second), 255) if abs(dx) < 4 else (*first, 255)

    return png(32, 32, pixel)


def image_handler(request):
    """Fake static.flashscore.com / flagcdn.com: a generated PNG for any image path."""
    if request.url.scheme != "https" or request.url.host not in IMAGE_HOSTS:
        return httpx.Response(404)
    if not request.url.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return httpx.Response(404)
    return httpx.Response(
        200, content=crest(str(request.url)), headers={"content-type": "image/png"}
    )


# --- synthetic results and prices -----------------------------------------------------------


class World:
    """Deterministic team strengths, histories and results for one seed."""

    def __init__(self, seed):
        self.seed = seed

    def strength(self, name):
        return 0.6 + (digest(self.seed, "strength", name) % 1000) / 1000 * 1.2

    def rates(self, home, away):
        sh, sa = self.strength(home), self.strength(away)
        return 1.45 * sh / sa**0.6, 1.1 * sa / sh**0.6

    def home_win(self, sport, home, away):
        sh, sa = self.strength(home), self.strength(away)
        if sport == "tennis":
            return sh**3 / (sh**3 + sa**3)
        return 1 / (1 + math.exp(-(4 * (sh - sa) + 0.25)))

    def list_odds(self, sport, home, away):
        if sport == "football":
            lam_home, lam_away = self.rates(home, away)
            grid = football_grid(lam_home, lam_away)
            return {
                "1": price(sum(p for h, a, p in grid if h > a), 1.05),
                "X": price(sum(p for h, a, p in grid if h == a), 1.05),
                "2": price(sum(p for h, a, p in grid if h < a), 1.05),
            }
        p = self.home_win(sport, home, away)
        return {"1": price(p, 1.05), "X": None, "2": price(1 - p, 1.05)}

    def result(self, sport, home, away, rng, odds=None):
        """(home score, away score): goals, points or sets."""
        if sport == "football":
            lam_home, lam_away = self.rates(home, away)
            return min(9, poisson(rng, lam_home)), min(9, poisson(rng, lam_away))
        p = self.home_win(sport, home, away)
        if odds and odds.get("1") and odds.get("2"):
            p = (1 / odds["1"]) / (1 / odds["1"] + 1 / odds["2"])
        won = rng.random() < p
        if sport == "tennis":
            lost_sets = 1 if rng.random() < 0.4 else 0
            return (2, lost_sets) if won else (lost_sets, 2)
        total = int(rng.gauss(160, 14))
        margin = 1 + int(abs(rng.gauss(0, 10)))
        winner, loser = (total + margin) // 2, (total - margin) // 2
        return (winner, loser) if won else (loser, winner)


def football_grid(lam_home, lam_away):
    return [(h, a, pmf(lam_home, h) * pmf(lam_away, a)) for h in range(11) for a in range(11)]


def odds_row(pid, value, **extra):
    row = {
        "eventParticipantId": pid,
        "value": f"{value:.2f}",
        "active": True,
        "handicap": None,
        "selection": None,
        "winner": None,
        "score": None,
        "bothTeamsToScore": None,
    }
    return row | extra


def line_row(value, line, selection):
    return odds_row(None, value, handicap={"value": f"{line:.1f}"}, selection=selection)


def synthetic_odds(world, sport, row):
    """matches/odds payload (two bookmakers) consistent with the list 1X2 prices."""
    home, away = team_name(row["home_team"]), team_name(row["away_team"])
    pids = (participant(row["home_team"]), participant(row["away_team"]))
    listed = row.get("odds") or {}
    if not listed.get("1") or not listed.get("2") or not all(pids):
        return []
    groups = []
    if sport == "football":
        lam_home, lam_away = world.rates(home, away)
        grid = football_grid(lam_home, lam_away)
        p1 = sum(p for h, a, p in grid if h > a)
        px = sum(p for h, a, p in grid if h == a)
        p2 = 1 - p1 - px
        groups.append(
            ("HOME_DRAW_AWAY", [(pids[0], p1), (None, px), (pids[1], p2)], {}),
        )
        groups.append(
            ("DOUBLE_CHANCE", [(pids[0], p1 + px), (None, p1 + p2), (pids[1], px + p2)], {}),
        )
        groups.append(("DRAW_NO_BET", [(pids[0], p1 / (p1 + p2)), (pids[1], p2 / (p1 + p2))], {}))
        lines = []
        for line in (0.5, 1.5, 2.5, 3.5, 4.5):
            over = sum(p for h, a, p in grid if h + a > line)
            lines += [(line, "OVER", over), (line, "UNDER", 1 - over)]
        groups.append(("OVER_UNDER", lines, {"lines": True}))
        both = (1 - math.exp(-lam_home)) * (1 - math.exp(-lam_away))
        groups.append(("BOTH_TEAMS_TO_SCORE", [(True, both), (False, 1 - both)], {"btts": True}))
        scope = "FULL_TIME"
    elif sport == "basketball":
        p = (1 / listed["1"]) / (1 / listed["1"] + 1 / listed["2"])
        groups.append(("HOME_AWAY", [(pids[0], p), (pids[1], 1 - p)], {}))
        mean = 150.5 + digest("total", home, away) % 30
        lines = []
        for line in (mean - 6, mean, mean + 6):
            over = 1 - 0.5 * (1 + math.erf((line - mean) / (14 * math.sqrt(2))))
            lines += [(line, "OVER", over), (line, "UNDER", 1 - over)]
        groups.append(("OVER_UNDER", lines, {"lines": True}))
        scope = "FULL_TIME_OVER_TIME"
    else:
        p = (1 / listed["1"]) / (1 / listed["1"] + 1 / listed["2"])
        groups.append(("HOME_AWAY", [(pids[0], p), (pids[1], 1 - p)], {}))
        low, high = 0.0, 1.0
        for _ in range(40):  # per-set probability s with s^2 (3 - 2s) = p
            s = (low + high) / 2
            low, high = (s, high) if s * s * (3 - 2 * s) < p else (low, s)
        scores = {"2:0": s * s, "2:1": 2 * s * s * (1 - s), "1:2": 2 * s * (1 - s) ** 2}
        scores["0:2"] = (1 - s) ** 2
        groups.append(("CORRECT_SCORE", list(scores.items()), {"scores": True}))
        scope = "FULL_TIME"
    books = []
    for name, factor in (("MockBet", 1.0), ("DemoBet", 0.985)):
        odds = []
        for kind, rows, flags in groups:
            out = []
            for first, *rest in rows:
                if flags.get("lines"):
                    line, selection, p = first, rest[0], rest[1]
                    out.append(line_row(price(p) * factor, line, selection))
                elif flags.get("btts"):
                    out.append(odds_row(None, price(rest[0]) * factor, bothTeamsToScore=first))
                elif flags.get("scores"):
                    out.append(odds_row(None, price(rest[0], 1.1) * factor, score=first))
                else:
                    out.append(odds_row(first, price(rest[0]) * factor))
            odds.append({"bettingType": kind, "bettingScope": scope, "odds": out})
        books.append({"name": name, "odds": odds})
    return books


def participant(team):
    if isinstance(team, list):
        return (team[0] or {}).get("event_participant_id") if team else None
    return team.get("event_participant_id")


def rewrite_participants(payload, home_pid, away_pid):
    """Captured odds for another fixture: its two participant ids become this match's."""
    order = []
    for book in payload:
        for group in book.get("odds", []):
            for row in group.get("odds", []):
                pid = row.get("eventParticipantId")
                if pid and pid not in order:
                    order.append(pid)
    if len(order) != 2 or not home_pid or not away_pid:
        return payload
    mapping = {order[0]: home_pid, order[1]: away_pid}
    text = json.dumps(payload)
    for old, new in mapping.items():
        text = text.replace(f'"{old}"', f'"__{new}__"')
    for new in mapping.values():
        text = text.replace(f'"__{new}__"', f'"{new}"')
    return json.loads(text)


# --- fixture lists -------------------------------------------------------------------------


def football_groups(world):
    """A Premier League day plus the captured live football teams, re-paired as a day list."""
    base = datetime.combine(CAPTURE_DAY, time(11, 0), timezone.utc).timestamp()
    rows = []
    for n, (home, away) in enumerate(PREMIER_LEAGUE):
        home_team = {"team_id": f"t-{slug(home)}", "name": home}
        away_team = {"team_id": f"t-{slug(away)}", "name": away}
        home_team["small_image_path"] = f"{LOGO_ROOT}mock-{slug(home)}.png"
        away_team["small_image_path"] = f"{LOGO_ROOT}mock-{slug(away)}.png"
        pids = ("QsL3TXzh", "CbJBRB54") if n == 0 else (f"p{n}h{slug(home)}", f"p{n}a{slug(away)}")
        home_team["event_participant_id"], away_team["event_participant_id"] = pids
        rows.append(
            {
                "match_id": f"mockfb{n}",
                "timestamp": base + 5400 * n,
                "home_team": home_team,
                "away_team": away_team,
            }
        )
    groups = [
        {
            "name": "ENGLAND: Premier League",
            "country_name": "England",
            "image_path": f"{LOGO_ROOT}mock-premier-league.png",
            "matches": rows,
        }
    ]
    for group in load("live_football.json"):
        matches = []
        for n, row in enumerate(group.get("matches", [])):
            matches.append(
                {
                    "match_id": f"s{row['match_id']}",
                    "timestamp": base + 1800 * (n + len(groups)) + 900 * len(matches),
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                }
            )
        if matches:
            groups.append({k: v for k, v in group.items() if k != "matches"} | {"matches": matches})
    for group in groups:
        for row in group["matches"]:
            row["odds"] = world.list_odds(
                "football", team_name(row["home_team"]), team_name(row["away_team"])
            )
    return groups


def captured_groups(sport):
    return load(f"list_{sport}.json")


def paired(group, shift):
    """The group's rows with away sides rotated by `shift` (1-game groups swap sides)."""
    rows = group["matches"]
    if not shift or not rows:
        return [copy.deepcopy(r) for r in rows]
    if len(rows) == 1:
        row = copy.deepcopy(rows[0])
        if shift % 2:
            row["home_team"], row["away_team"] = row["away_team"], row["home_team"]
        return [row]
    output = []
    for n, row in enumerate(rows):
        row = copy.deepcopy(row)
        row["away_team"] = copy.deepcopy(rows[(n + shift) % len(rows)]["away_team"])
        output.append(row)
    return output


def suffix(offset):
    if offset == 0:
        return ""
    return f"-p{-offset}" if offset < 0 else f"-n{offset}"


def blank_status(started=False, finished=False):
    return {
        "stage": "Finished" if finished else None,
        "is_cancelled": False,
        "is_postponed": False,
        "is_started": started or finished,
        "is_in_progress": started and not finished,
        "is_finished": finished,
        "is_finished_after_extra_time": False,
        "is_finished_after_penalties": False,
        "live_time": None,
        "live_minute": None,
        "winner": None,
        "final_winner": None,
    }


class FakeFlashScore:
    """httpx.MockTransport handler serving every FlashScore endpoint the app calls."""

    def __init__(self, world, today, now):
        self.world, self.today, self.now = world, today, now
        self.calls = []
        self.rows = {}  # match id -> (sport, list row) of every served fixture
        self.pools = {}
        self.bases = {
            "football": football_groups(world),
            "basketball": captured_groups("basketball"),
            "tennis": captured_groups("tennis"),
        }
        self.shift = (today - CAPTURE_DAY).days * 86400

    # Day lists ------------------------------------------------------------------------------

    def day_groups(self, sport, day):
        offset = (day - self.today).days
        groups = []
        for group in self.bases[sport]:
            rows = []
            for row in paired(group, abs(offset)):
                row["match_id"] = f"{row['match_id']}{suffix(offset)}"
                home, away = team_name(row["home_team"]), team_name(row["away_team"])
                if offset and row.get("odds") and any(row["odds"].values()):
                    # Another day, another pairing: prices follow the new pairing.
                    row["odds"] = self.world.list_odds(sport, home, away)
                rows.append(row)
            groups.append({k: v for k, v in group.items() if k != "matches"} | {"matches": rows})
        self.retime(groups, day, sport)
        for group in groups:
            for row in group["matches"]:
                self.rows[row["match_id"]] = (sport, row)
        return groups

    def retime(self, groups, day, sport):
        """Scheduled after now (today), finished with a result (past) or as captured (future)."""
        rows = [row for group in groups for row in group["matches"]]
        midnight = datetime.combine(day, time(0), timezone.utc)
        real_today = self.now.date()
        if day == real_today:
            end = midnight + timedelta(hours=23, minutes=50)
            room = max(end - self.now, timedelta(0))
            start = self.now + min(timedelta(minutes=30), room / 4)
            end = max(end, start + timedelta(minutes=10))
            stamps = sorted({float(r["timestamp"]) for r in rows})
            low, high = stamps[0], stamps[-1]
            for row in rows:
                share = (float(row["timestamp"]) - low) / (high - low) if high > low else 0
                row["timestamp"] = (start + (end - start) * share).timestamp()
                row["match_status"] = blank_status()
                row["scores"] = {"home": None, "away": None}
            return
        for row in rows:
            seconds = float(row["timestamp"]) % 86400
            row["timestamp"] = midnight.timestamp() + seconds
            if day < real_today:
                rng = random.Random(digest(self.world.seed, "result", row["match_id"]))
                home, away = team_name(row["home_team"]), team_name(row["away_team"])
                score = self.world.result(sport, home, away, rng, row.get("odds"))
                row["match_status"] = blank_status(finished=True)
                row["scores"] = {"home": score[0], "away": score[1]}
            else:
                row["match_status"] = blank_status()
                row["scores"] = {"home": None, "away": None}

    # History ----------------------------------------------------------------------------------

    def pool(self, sport):
        """{team name: (team object, its group)} of the sport's base day list."""
        if sport not in self.pools:
            teams = {}
            for group in self.bases[sport]:
                for row in group["matches"]:
                    for side in ("home_team", "away_team"):
                        teams.setdefault(team_name(row[side]), (row[side], group))
            self.pools[sport] = teams
        return self.pools[sport]

    def history_rows(self, sport, name):
        """Payload rows of `name`'s last results, all well before the recent days."""
        teams = self.pool(sport)
        team, group = teams[name]
        doubles = isinstance(team, list)
        rivals = sorted(n for n, (t, _) in teams.items() if isinstance(t, list) == doubles)
        rivals = [n for n in rivals if n != name] or [f"{name} II"]
        rng = random.Random(digest(self.world.seed, "history", sport, name))
        tournament, surface = tournament_of(sport, group)
        rows = []
        for i in range(HISTORY_GAMES):
            rival = rivals[rng.randrange(len(rivals))]
            home, away = (name, rival) if i % 2 == 0 else (rival, name)
            home_team = teams.get(home, ({"name": home}, None))[0]
            away_team = teams.get(away, ({"name": away}, None))[0]
            kickoff = datetime.combine(self.today, time(18, 0), timezone.utc) - timedelta(
                days=61 + 4 * i + digest(name, i) % 3
            )
            score = self.world.result(sport, home, away, rng)
            row = {
                "match_id": f"h{digest(self.world.seed, sport, name, i) % 10**10:010d}",
                "timestamp": kickoff.timestamp(),
                "status": "FINISHED",
                "tournament_name": tournament,
                "home_team": history_team(home_team),
                "away_team": history_team(away_team),
                "scores": {"home": str(score[0]), "away": str(score[1])},
            }
            if surface:
                row["surface"] = surface
            rows.append(row)
        return rows

    def seed_history(self):
        """Finished Match rows of every team of every sport (for the analyses' grades)."""
        output = []
        for sport in self.bases:
            payload = [row for name in self.pool(sport) for row in self.history_rows(sport, name)]
            matches, _ = normalize_matches(payload, results=True, sport=sport)
            output.extend(matches)
        return output

    # Endpoints ------------------------------------------------------------------------------

    def __call__(self, request):
        path, params = request.url.path, dict(request.url.params)
        self.calls.append((path, params))
        sport = SPORT_IDS.get(params.get("sport_id", "1"), "football")
        if path.endswith("list-by-date"):
            day = date.fromisoformat(params["date"])
            return httpx.Response(200, json=self.day_groups(sport, day))
        if path.endswith("matches/live"):
            return httpx.Response(200, json=self.live(sport))
        match_id = params.get("match_id", "")
        if path.endswith("matches/h2h"):
            return httpx.Response(200, json=self.h2h(match_id))
        if path.endswith("matches/odds"):
            return httpx.Response(200, json=self.odds(match_id))
        if path.endswith("matches/standings"):
            return httpx.Response(200, json=[])
        if path.endswith("match/stats"):
            return httpx.Response(200, json=load("stats_live_football.json"))
        return httpx.Response(200, json=[])

    def live(self, sport):
        groups = load(f"live_{sport}.json")
        started = (self.now - LIVE_AGE).timestamp()
        for group in groups:
            for row in group.get("matches", []):
                row["timestamp"] = started
        return groups

    def h2h(self, match_id):
        if match_id in CAPTURED_H2H:
            rows = load(f"h2h_{CAPTURED_H2H[match_id]}.json")
            for row in rows:
                row["timestamp"] = float(row["timestamp"]) + self.shift
            return rows
        found = self.rows.get(match_id)
        if found is None:
            return []
        sport, row = found
        names = (team_name(row["home_team"]), team_name(row["away_team"]))
        return [r for name in names for r in self.history_rows(sport, name)]

    def odds(self, match_id):
        found = self.rows.get(match_id)
        if found is None:
            return []
        sport, row = found
        if CAPTURED_ODDS.get(match_id) == sport:
            payload = load(f"odds_{sport}.json")
            return rewrite_participants(
                payload, participant(row["home_team"]), participant(row["away_team"])
            )
        return synthetic_odds(self.world, sport, row)


def tournament_of(sport, group):
    name = str(group.get("name") or "Liga")
    text = name.split(": ", 1)[-1]
    if sport != "tennis":
        return text, None
    parts = [p.strip() for p in text.split(",")]
    surface = parts[1].split(" ")[0] if len(parts) > 1 else None
    return re.sub(r"\s*\(.*\)$", "", parts[0]), surface


def history_team(team):
    if isinstance(team, list):
        return [{"name": p.get("name"), "image_path": p.get("small_image_path")} for p in team]
    return {"name": team.get("name"), "image_path": team.get("small_image_path")}


# --- simulator datasets --------------------------------------------------------------------

BENCH_LEAGUES = {
    "E0": ["Arsenal", "Chelsea", "Liverpool", "Everton", "Leeds", "Burnley"],
    "SP1": ["Sevilla", "Valencia", "Betis", "Girona", "Osasuna", "Celta"],
}
EXTRA_LEAGUES = {
    "N1": ["Ajax", "PSV", "Feyenoord", "Utrecht", "Twente", "Heerenveen"],
    "P1": ["Benfica", "Porto", "Sporting", "Braga", "Guimaraes", "Boavista"],
}
WEEKS = 70
WEEKDAYS = {"E0": 5, "SP1": 6, "N1": 4, "P1": 0}  # Saturday, Sunday, Friday, Monday


def season_of(day):
    first = day.year if day.month >= 7 else day.year - 1
    return f"{first % 100:02d}{(first + 1) % 100:02d}"


def league_games(world, code, teams, today):
    """Weekly round robin over the WEEKS before `today`: [(day, home, away, h, a, odds)]."""
    rng = random.Random(digest(world.seed, "league", code))
    last = today - timedelta(days=1)
    last -= timedelta(days=(last.weekday() - WEEKDAYS[code]) % 7)
    games = []
    for week in range(WEEKS):
        day = last - timedelta(days=7 * (WEEKS - 1 - week))
        rest = teams[1:]
        shift = week % len(rest)
        order = [teams[0], *rest[shift:], *rest[:shift]]
        for i in range(len(teams) // 2):
            home, away = order[i], order[-1 - i]
            if week % 2:
                home, away = away, home
            lam_home, lam_away = world.rates(home, away)
            grid = football_grid(lam_home, lam_away)
            over = sum(p for h, a, p in grid if h + a > 2.5)
            odds = world.list_odds("football", home, away) | {
                "over25": price(over, 1.05),
                "under25": price(1 - over, 1.05),
            }
            score = (min(9, poisson(rng, lam_home)), min(9, poisson(rng, lam_away)))
            games.append((day, home, away, *score, odds))
    return games


def write_datasets(world, directory, today):
    """`football` (matches.jsonl + manifest) and `football-plus` (sim/raw CSVs) datasets."""
    from footypreds.evaluation.sim_datasets import TOP5

    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for code, teams in BENCH_LEAGUES.items():
        league, country = TOP5[code]
        for day, home, away, hg, ag, odds in league_games(world, code, teams, today):
            kickoff = datetime.combine(day, time(0), timezone.utc)
            match = {
                "id": f"fd-{code}-{day}-{home}-{away}",
                "kickoff": kickoff.isoformat(),
                "league": league,
                "country": country,
                "home": home,
                "away": away,
                "status": "finished",
                "home_goals": hg,
                "away_goals": ag,
                "source": "football-data.co.uk",
            }
            records.append(
                {
                    "match": match,
                    "season": season_of(day),
                    "league_code": code,
                    "reference_odds": odds,
                }
            )
    content = ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")
    (directory / "matches.jsonl").write_bytes(content)
    manifest = {"dataset_sha256": hashlib.sha256(content).hexdigest(), "source": "mock"}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    raw = directory / "sim" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    columns = ["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    columns += ["AvgH", "AvgD", "AvgA", "Avg>2.5", "Avg<2.5"]
    for code, teams in EXTRA_LEAGUES.items():
        by_season = {}
        for day, home, away, hg, ag, odds in league_games(world, code, teams, today):
            by_season.setdefault(season_of(day), []).append(
                [code, day.strftime("%d/%m/%Y"), "18:00", home, away, hg, ag]
                + [odds["1"], odds["X"], odds["2"], odds["over25"], odds["under25"]]
            )
        for season, rows in by_season.items():
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows(rows)
            (raw / f"{season}-{code}.csv").write_text(buffer.getvalue(), encoding="utf-8")
    return directory


# --- the app -------------------------------------------------------------------------------


class _Unthrottled(FlashScore):
    """The fake answers instantly: no 0.35 s pause between requests (this instance only)."""

    last_request = property(lambda self: float("-inf"), lambda self, value: None)


def build_app(workdir, seed=7, today=None, now=None):
    """The real FastAPI app wired to the fakes; everything lives under `workdir`."""
    from footypreds.api import create_app

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    world = World(seed)
    fake = FakeFlashScore(world, today, now)
    settings = Settings(api_key="mock-key", database=workdir / "mock.sqlite3")
    app = create_app(settings, httpx.MockTransport(fake), httpx.MockTransport(image_handler))
    app.state.provider.__class__ = _Unthrottled
    app.state.img_cache_dir = workdir / "img_cache"
    app.state.sim_benchmark_dir = write_datasets(world, workdir / "bench", now.date())
    app.state.sim_cache_dir = workdir / "sim-cache"
    app.state.sim_workers = 1
    app.state.mock = fake
    app.state.store.save_matches(fake.seed_history())
    return app


def port_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


async def serve(server, url):
    """Run uvicorn; print the URL once it listens. Returns the exit code."""
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if server.started:
        print(
            f"FootyPreds mock: {url} (date fictive, fără rețea; Ctrl+C pentru oprire)", flush=True
        )
    try:
        await task
    except SystemExit as exc:  # uvicorn exits when it cannot bind or start
        return exc.code or 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="FootyPreds offline, cu date FlashScore fictive.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    import uvicorn

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a cp1252 console must not crash the banner
    if not port_free(args.host, args.port):
        print(
            f"Portul {args.port} este ocupat (poate rulează deja un server). "
            "Alege altul cu --port.",
            file=sys.stderr,
        )
        return 1
    workdir = Path(tempfile.mkdtemp(prefix="footypreds-mock-"))
    code = 0
    try:
        app = build_app(workdir, seed=args.seed, today=args.today)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        code = asyncio.run(serve(uvicorn.Server(config), f"http://{args.host}:{args.port}"))
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
