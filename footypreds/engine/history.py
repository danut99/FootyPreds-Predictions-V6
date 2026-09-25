"""Indexed access to finished matches, by team, with no leakage past a cutoff."""

import unicodedata
from bisect import bisect_left
from functools import lru_cache


@lru_cache(maxsize=65536)
def canonical(value):
    value = value.split(":", 1)[-1].strip().casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def is_friendly(league):
    return "friendly" in canonical(league) or "club friendly" in canonical(league)


# Kept in line with competitions.YOUTH (which already treats U16 as youth for the board).
YOUTH_TOKENS = (
    "u15",
    "u16",
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u23",
    "youth",
    "primavera",
    "junior",
)


def is_youth(league):
    name = canonical(league)
    return any(token in name for token in YOUTH_TOKENS)


class HistoryIndex:
    """Finished matches, de-duplicated by id, sorted by kickoff, indexed by team name and ID."""

    def __init__(self, matches=()):
        self.rows = []
        self.times = []
        self.by_name = {}
        self.by_id = {}
        self.ids = set()
        self.extend(matches)

    def extend(self, matches):
        fresh = [
            m
            for m in {m.id: m for m in matches if m.status == "finished"}.values()
            if m.id not in self.ids and m.home_goals is not None and m.away_goals is not None
        ]
        if not fresh:
            return
        # Order is (kickoff, id): a result tied on kickoff with the last row may still sort
        # before it, so the fast path needs the full key to be strictly greater.
        last = self.rows[-1] if self.rows else None
        appended = last is None or min((m.kickoff, m.id) for m in fresh) > (last.kickoff, last.id)
        self.rows.extend(fresh)
        self.ids.update(m.id for m in fresh)
        if not appended:
            self.rows.sort(key=lambda m: (m.kickoff, m.id))
        else:
            fresh.sort(key=lambda m: (m.kickoff, m.id))
            self.rows[-len(fresh) :] = fresh
        self.times = [m.kickoff for m in self.rows]
        self.by_name, self.by_id = {}, {}
        for position, match in enumerate(self.rows):
            for name, team_id in ((match.home, match.home_id), (match.away, match.away_id)):
                self.by_name.setdefault(canonical(name), []).append(position)
                if team_id:
                    self.by_id.setdefault(team_id, []).append(position)

    def before(self, cutoff, oldest=None):
        start = 0 if oldest is None else bisect_left(self.times, oldest)
        return self.rows[start : bisect_left(self.times, cutoff)]

    def team(self, name, team_id="", cutoff=None, oldest=None, exclude=None):
        """Team matches (oldest first), matched by ID when available, otherwise by name."""
        positions = set(self.by_name.get(canonical(name), ()))
        if team_id:
            positions.update(self.by_id.get(team_id, ()))
        output = []
        key = canonical(name)
        for position in sorted(positions):
            match = self.rows[position]
            if cutoff is not None and match.kickoff >= cutoff:
                break
            if oldest is not None and match.kickoff < oldest:
                continue
            if exclude and match.id == exclude:
                continue
            side = side_of(match, key, team_id)
            if side:
                output.append((match, side))
        return output


def side_of(match, key, team_id=""):
    if team_id and match.home_id == team_id:
        return "home"
    if team_id and match.away_id == team_id:
        return "away"
    # A known, different ID means a namesake from another country or division.
    if canonical(match.home) == key and not (team_id and match.home_id):
        return "home"
    if canonical(match.away) == key and not (team_id and match.away_id):
        return "away"
    return None
