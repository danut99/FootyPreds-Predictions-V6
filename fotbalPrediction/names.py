"""Potrivirea numelor FlashScore (ligă, echipe) cu codurile și numele football-data.co.uk.

Regula de bază: NICIODATĂ nu ghicim. O potrivire ambiguă întoarce None, iar meciul este afișat
„fără model” (sau cu probabilitățile modelului de bază FootyPreds, marcate clar). Un nume greșit
ar da ratingurile altei echipe, deci ambiguitatea e mai sigură decât o potrivire forțată.

Ordinea pentru echipe: (1) ``team_overrides.json`` (mic, versionat, editabil de mână),
(2) potrivire exactă după normalizare, (3) aceleași cuvinte, (4) conținere unică,
(5) ``difflib`` cu scor >= 0.85 și avans >= 0.08 față de al doilea candidat.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path

OVERRIDES_FILE = Path(__file__).with_name("team_overrides.json")

# (țară FlashScore, ligă FlashScore fără fază) normalizate -> cod football-data
LEAGUE_MAP = {
    ("england", "premierleague"): "E0",
    ("england", "championship"): "E1",
    ("england", "leagueone"): "E2",
    ("england", "leaguetwo"): "E3",
    ("england", "nationalleague"): "EC",
    ("scotland", "premiership"): "SC0",
    ("scotland", "championship"): "SC1",
    ("scotland", "leagueone"): "SC2",
    ("scotland", "leaguetwo"): "SC3",
    ("germany", "bundesliga"): "D1",
    ("germany", "2bundesliga"): "D2",
    ("italy", "seriea"): "I1",
    ("italy", "serieb"): "I2",
    ("spain", "laliga"): "SP1",
    ("spain", "laliga2"): "SP2",
    ("spain", "laligahypermotion"): "SP2",
    ("france", "ligue1"): "F1",
    ("france", "ligue2"): "F2",
    ("netherlands", "eredivisie"): "N1",
    ("belgium", "jupilerproleague"): "B1",
    ("belgium", "firstdivisiona"): "B1",
    ("portugal", "ligaportugal"): "P1",
    ("portugal", "primeiraliga"): "P1",
    ("turkey", "superlig"): "T1",
    ("greece", "superleague"): "G1",
    ("greece", "superleague1"): "G1",
}
# Echipe de tineret, feminine, rezerve: nu există în football-data.
REJECT_TOKENS = frozenset(
    {"u17", "u18", "u19", "u20", "u21", "u23", "women", "w", "ii", "reserves"}
)
DROP_TOKENS = frozenset(
    {
        "fc",
        "afc",
        "cf",
        "sc",
        "ac",
        "as",
        "ssc",
        "us",
        "cd",
        "ud",
        "rc",
        "rcd",
        "sd",
        "sv",
        "vfb",
        "vfl",
        "tsg",
        "fsv",
        "1",
        "club",
        "calcio",
        "the",
    }
)
EXPAND = {
    "utd": "united",
    "man": "manchester",
    "atl": "atletico",
    "ath": "athletic",
    "st": "saint",
    "sp": "sporting",
    "nottm": "nottingham",
    "rvs": "rovers",
    "weds": "wednesday",
    "wed": "wednesday",
}
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(c for c in value if not unicodedata.combining(c)).replace("ß", "ss")


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _plain(value))


def league_code(league: str | None, country: str | None = "") -> str | None:
    """Codul football-data al unei ligi FlashScore („ENGLAND: Premier League - ...”) sau None."""
    text = str(league or "")
    prefix, separator, name = text.partition(":")
    if not separator:
        prefix, name = country or "", text
    base = name.split(" - ", 1)[0]
    return LEAGUE_MAP.get((_squash(prefix or country or ""), _squash(base)))


def tokens(name: str) -> list[str]:
    value = _SUFFIX.sub("", _plain(name)).replace("&", " and ").replace("'", "")
    words = _NON_WORD.sub(" ", value.replace(".", " ").replace("-", " ")).split()
    return [EXPAND.get(word, word) for word in words]


def normalize_team(name: str) -> str:
    """Nume normalizat: fără diacritice și punctuație, fără prefixe de club (FC, AC, SV...)."""
    words = [word for word in tokens(name) if word not in DROP_TOKENS]
    return " ".join(words or tokens(name))


def is_rejected(name: str) -> bool:
    """Echipe de tineret, feminine sau secunde, care nu există în football-data."""
    words = tokens(name)
    return bool(set(words) & REJECT_TOKENS) or (len(words) > 1 and words[-1] in {"2", "b"})


def load_overrides(path: str | Path = OVERRIDES_FILE) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        code: {normalize_team(k): v for k, v in names.items()}
        for code, names in raw.items()
        if isinstance(names, dict) and not code.startswith("_")
    }


class TeamResolver:
    """Rezolvă numele FlashScore în numele football-data dintr-o ligă (sau din țara ei)."""

    def __init__(
        self,
        teams: Mapping[str, Iterable[str]],
        country_of: Mapping[str, str],
        overrides: Mapping[str, Mapping[str, str]] | None = None,
    ):
        self.teams = {code: sorted(set(names)) for code, names in teams.items()}
        self.country_of = dict(country_of)
        self.overrides = {code: dict(v) for code, v in (overrides or {}).items()}
        self._normal = {
            code: {normalize_team(name): name for name in names}
            for code, names in self.teams.items()
        }

    def _pool(self, code: str) -> list[dict[str, str]]:
        country = self.country_of.get(code)
        others = [
            self._normal[other]
            for other in self.teams
            if other != code and country and self.country_of.get(other) == country
        ]
        return [self._normal.get(code, {}), *others]

    def resolve(self, code: str | None, name: str) -> str | None:
        if not code or not name or is_rejected(name):
            return None
        key = normalize_team(name)
        override = self.overrides.get(code, {}).get(key)
        if override:
            return override
        for pool in self._pool(code):
            found = self._match(key, pool)
            if found is not None:
                return found
        return None

    @staticmethod
    def _match(key: str, pool: Mapping[str, str]) -> str | None:
        if not pool:
            return None
        if key in pool:
            return pool[key]
        words = set(key.split())
        same = [real for norm, real in pool.items() if set(norm.split()) == words]
        if len(same) == 1:
            return same[0]
        contained = [
            real
            for norm, real in pool.items()
            if f" {key} " in f" {norm} " or f" {norm} " in f" {key} "
        ]
        if len(contained) == 1:
            return contained[0]
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, key, norm).ratio(), real)
                for norm, real in pool.items()
            ),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.85:
            if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08:
                return scored[0][1]
        return None

    def search(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        key = normalize_team(query)
        found = []
        for code, pool in self._normal.items():
            for norm, real in pool.items():
                if not key or key in norm:
                    found.append({"team": real, "league": code})
        found.sort(key=lambda item: (item["team"], item["league"]))
        return found[:limit]
