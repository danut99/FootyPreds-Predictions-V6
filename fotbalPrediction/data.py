"""football-data.co.uk archive: downloader, tolerant parser and cached loader.

Files
-----
Main leagues (22, with half-time score, match stats and pre-closing + closing odds)::

    https://www.football-data.co.uk/mmz4281/{season}/{code}.csv  ->  raw/main/{season}/{code}.csv

Extra leagues (16 "new" files, one per league, all seasons, full-time score and CLOSING 1X2
only)::

    https://www.football-data.co.uk/new/{CODE}.csv  ->  raw/new/{CODE}.csv

``raw`` is ``footypreds/data/fotbal/raw/`` (git-ignored). ``raw/manifest.json`` records, per
file, the url, sha256, size, HTTP status and ``downloaded_at``. Seasons 0506..2627 are
downloaded: 0506 is the first season with shots, corners and cards in every top league
(England, Scotland Premiership, D1, I1, SP1, F1); the other main leagues only carry stats from
1718, which ``load_rows`` handles per row (missing stats are ``None``).

Rows
----
``MatchRow`` (frozen dataclass) holds one finished match: league, country, tier, season (the
football-data code, e.g. "2324"), date (``datetime.date``), time ("HH:MM" or None), home,
away, full-time and half-time goals, shots, shots on target, corners, fouls, yellow and red
cards, referee and ``odds``. Missing cells are ``None``.

``odds`` maps a source to a dict of market prices keyed with the fotbalPrediction market keys
("1", "X", "2", "over25", "under25", "ah_1_-0.75", "ah_2_+0.75"). Sources:

    avg, max, ps, b365, bfe                    PRE-CLOSING prices (collected Tuesday/Friday)
    avg_closing, max_closing, ps_closing,      CLOSING prices (football-data "C" columns:
    b365_closing, bfe_closing                  AvgCH, MaxCH, PSCH, AHCh ...)

Before 1920 "avg"/"max" fall back to the Betbrain columns (BbAvH, BbAv>2.5, BbAHh +
BbAvAHH ...). Extra leagues only have ``*_closing`` sources. ``is_closing(source)`` tells them
apart. Pinnacle (``ps``) is missing from mid-2526 on: never depend on it.

Parsing pitfalls handled (see the scout notes): UTF-8 BOM, cp1252 files (EC 1617/2021/2122),
2- and 4-digit years, empty trailing header columns, rows without a full-time score
(abandoned games), matches without HT or stats (forfeits), unsorted files, leading/trailing
spaces, tab-separated files, mixed competitions in one extra file (the most frequent League
value of each season is kept), calendar-year seasons (mapped to the July-June football
season of the match date) and team renames (``TEAM_ALIASES``).

Parsed files are cached as pickles in ``footypreds/data/fotbal/cache/`` keyed by the raw
file's size, mtime and ``PARSER_VERSION``.

CLI::

    python -m fotbalPrediction.data --download [--leagues E0,SP1] [--seasons 2223,2324]
    python -m fotbalPrediction.data --download --refresh      # re-fetch the running season
    python -m fotbalPrediction.data --coverage                 # coverage table of local files
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import pickle
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "footypreds" / "data" / "fotbal"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
MANIFEST_NAME = "manifest.json"
PARSER_VERSION = 3

MAIN_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
EXTRA_URL = "https://www.football-data.co.uk/new/{code}.csv"
USER_AGENT = "fotbalPrediction/0.1 (local research; polite)"

# code -> (league name, country, tier inside the country)
MAIN_LEAGUES = {
    "E0": ("Premier League", "Anglia", 1),
    "E1": ("Championship", "Anglia", 2),
    "E2": ("League One", "Anglia", 3),
    "E3": ("League Two", "Anglia", 4),
    "EC": ("National League", "Anglia", 5),
    "SC0": ("Premiership", "Scoția", 1),
    "SC1": ("Championship", "Scoția", 2),
    "SC2": ("League One", "Scoția", 3),
    "SC3": ("League Two", "Scoția", 4),
    "D1": ("Bundesliga", "Germania", 1),
    "D2": ("2. Bundesliga", "Germania", 2),
    "I1": ("Serie A", "Italia", 1),
    "I2": ("Serie B", "Italia", 2),
    "SP1": ("La Liga", "Spania", 1),
    "SP2": ("La Liga 2", "Spania", 2),
    "F1": ("Ligue 1", "Franța", 1),
    "F2": ("Ligue 2", "Franța", 2),
    "N1": ("Eredivisie", "Olanda", 1),
    "B1": ("Jupiler Pro League", "Belgia", 1),
    "P1": ("Primeira Liga", "Portugalia", 1),
    "T1": ("Süper Lig", "Turcia", 1),
    "G1": ("Super League", "Grecia", 1),
}
# code -> (league name, country); every extra file holds CLOSING 1X2 prices only.
EXTRA_LEAGUES = {
    "ARG": ("Liga Profesional", "Argentina"),
    "AUT": ("Bundesliga", "Austria"),
    "BRA": ("Serie A", "Brazilia"),
    "CHN": ("Super League", "China"),
    "DNK": ("Superliga", "Danemarca"),
    "FIN": ("Veikkausliiga", "Finlanda"),
    "IRL": ("Premier Division", "Irlanda"),
    "JPN": ("J1 League", "Japonia"),
    "MEX": ("Liga MX", "Mexic"),
    "NOR": ("Eliteserien", "Norvegia"),
    "POL": ("Ekstraklasa", "Polonia"),
    "ROU": ("Superliga", "România"),
    "RUS": ("Premier League", "Rusia"),
    "SWE": ("Allsvenskan", "Suedia"),
    "SWZ": ("Super League", "Elveția"),
    "USA": ("MLS", "SUA"),
}
ALL_LEAGUES = tuple(MAIN_LEAGUES) + tuple(EXTRA_LEAGUES)

FIRST_SEASON = "0506"
RUNNING_SEASON = "2627"
LOCKED_TEST_SEASON = "2526"
CONFIRM_SEASON = "2425"
TUNE_SEASONS = ("2223", "2324")


def season_code(start_year: int) -> str:
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def season_start(code: str) -> int:
    """First calendar year of a season code ("2324" -> 2023, "9900" -> 1999)."""
    first = int(code[:2])
    return (1900 if first >= 90 else 2000) + first


SEASONS = tuple(season_code(year) for year in range(2005, 2027))

# Plain odds columns are pre-closing; "C" after the bookmaker marks closing prices.
# source -> list of alternatives, each (home, draw, away, over, under, ah_line, ah_home, ah_away)
_ODDS_COLUMNS = {
    "avg": [
        ("AvgH", "AvgD", "AvgA", "Avg>2.5", "Avg<2.5", "AHh", "AvgAHH", "AvgAHA"),
        ("BbAvH", "BbAvD", "BbAvA", "BbAv>2.5", "BbAv<2.5", "BbAHh", "BbAvAHH", "BbAvAHA"),
    ],
    "max": [
        ("MaxH", "MaxD", "MaxA", "Max>2.5", "Max<2.5", "AHh", "MaxAHH", "MaxAHA"),
        ("BbMxH", "BbMxD", "BbMxA", "BbMx>2.5", "BbMx<2.5", "BbAHh", "BbMxAHH", "BbMxAHA"),
    ],
    "ps": [("PSH", "PSD", "PSA", "P>2.5", "P<2.5", "AHh", "PAHH", "PAHA")],
    "b365": [("B365H", "B365D", "B365A", "B365>2.5", "B365<2.5", "AHh", "B365AHH", "B365AHA")],
    "bfe": [("BFEH", "BFED", "BFEA", "BFE>2.5", "BFE<2.5", "AHh", "BFEAHH", "BFEAHA")],
    "avg_closing": [
        ("AvgCH", "AvgCD", "AvgCA", "AvgC>2.5", "AvgC<2.5", "AHCh", "AvgCAHH", "AvgCAHA")
    ],
    "max_closing": [
        ("MaxCH", "MaxCD", "MaxCA", "MaxC>2.5", "MaxC<2.5", "AHCh", "MaxCAHH", "MaxCAHA")
    ],
    "ps_closing": [("PSCH", "PSCD", "PSCA", "PC>2.5", "PC<2.5", "AHCh", "PCAHH", "PCAHA")],
    "b365_closing": [
        ("B365CH", "B365CD", "B365CA", "B365C>2.5", "B365C<2.5", "AHCh", "B365CAHH", "B365CAHA")
    ],
    "bfe_closing": [
        ("BFECH", "BFECD", "BFECA", "BFEC>2.5", "BFEC<2.5", "AHCh", "BFECAHH", "BFECAHA")
    ],
}
ODDS_SOURCES = tuple(_ODDS_COLUMNS)


def is_closing(source: str) -> bool:
    return source.endswith("_closing")


# Same club under different names inside one league file (checked on the scout sample).
TEAM_ALIASES = {
    "ROU": {
        "Din. Bucuresti": "Dinamo Bucuresti",
        "U Craiova": "Univ. Craiova",
        "Viitorul Constanta": "Farul Constanta",
    },
}

_STAT_COLUMNS = {
    "home_shots": "HS",
    "away_shots": "AS",
    "home_sot": "HST",
    "away_sot": "AST",
    "home_corners": "HC",
    "away_corners": "AC",
    "home_fouls": "HF",
    "away_fouls": "AF",
    "home_yellow": "HY",
    "away_yellow": "AY",
    "home_red": "HR",
    "away_red": "AR",
}
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MatchRow:
    """One finished match. Stats and HT goals are None when the file does not have them."""

    league: str
    season: str
    date: date
    home: str
    away: str
    home_goals: int
    away_goals: int
    country: str = ""
    tier: int = 0
    time: str | None = None
    ht_home_goals: int | None = None
    ht_away_goals: int | None = None
    home_shots: int | None = None
    away_shots: int | None = None
    home_sot: int | None = None
    away_sot: int | None = None
    home_corners: int | None = None
    away_corners: int | None = None
    home_fouls: int | None = None
    away_fouls: int | None = None
    home_yellow: int | None = None
    away_yellow: int | None = None
    home_red: int | None = None
    away_red: int | None = None
    referee: str | None = None
    odds: dict = field(default_factory=dict, compare=False, hash=False)
    season_label: str = ""
    extra: bool = False

    @property
    def id(self) -> str:
        return f"{self.league}-{self.date:%Y%m%d}-{self.home}-{self.away}"

    def get(self, name: str, default=None):
        return getattr(self, name, default)

    def __getitem__(self, name: str):
        try:
            return getattr(self, name)
        except AttributeError:
            raise KeyError(name) from None


# --------------------------------------------------------------------------- parsing helpers


def decode(content: bytes) -> str:
    """utf-8 (with or without BOM), then cp1252, then latin-1."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    # A BOM on a file that is not valid UTF-8 survives a cp1252 decode as "ï»¿".
    for bom in ("﻿", "ï»¿"):
        if text.startswith(bom):
            text = text[len(bom) :]
    return text


def clean_name(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("’", "'").replace("‘", "'").replace("`", "'").replace("â€™", "'")
    return _SPACES.sub(" ", value).strip()


def parse_date(value: str | None) -> date | None:
    """dd/mm/yy or dd/mm/yyyy (chosen by the length of the year part)."""
    if not value:
        return None
    parts = value.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        day, month, year = int(parts[0]), int(parts[1]), parts[2].strip()
        if len(year) == 2:
            full = 2000 + int(year) if int(year) < 70 else 1900 + int(year)
        elif len(year) == 4:
            full = int(year)
        else:
            return None
        return date(full, month, day)
    except ValueError:
        return None


def parse_time(value: str | None) -> str | None:
    found = _TIME.match((value or "").strip())
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_count(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number < 0 or number != int(number):
        return None
    return int(number)


def parse_price(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if 1.0 < number < 1000.0 else None


def parse_line(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    # Quarter-step handicap lines only.
    if number != number or abs(number) > 10 or abs(number * 4 - round(number * 4)) > 1e-9:
        return None
    return round(number * 4) / 4


def ah_keys(line: float) -> tuple[str, str]:
    """Home/away market keys of a football-data AH line (the size given to the HOME team)."""
    from footypreds.sports.keys import handicap

    return handicap("1", line), handicap("2", -line)


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    first = text.split("\n", 1)[0]
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration:
        return [], []
    return header, [row for row in reader if any(cell.strip() for cell in row)]


def _index(header: list[str]) -> dict[str, int]:
    """Column name -> position; empty header cells are ignored, the first duplicate wins."""
    index: dict[str, int] = {}
    for position, name in enumerate(header):
        if name and name not in index:
            index[name] = position
    return index


def _cell(row: list[str], index: dict[str, int], name: str) -> str | None:
    position = index.get(name)
    if position is None or position >= len(row):
        return None
    value = row[position].strip()
    return value or None


def parse_odds(row: list[str], index: dict[str, int], sources=ODDS_SOURCES) -> dict:
    odds: dict[str, dict] = {}
    for source in sources:
        prices: dict[str, float] = {}
        for columns in _ODDS_COLUMNS[source]:
            home, draw, away, over, under, line, ah_home, ah_away = columns
            if not any(name in index for name in columns):
                continue
            for key, column in (("1", home), ("X", draw), ("2", away)):
                value = parse_price(_cell(row, index, column))
                if value and key not in prices:
                    prices[key] = value
            for key, column in (("over25", over), ("under25", under)):
                value = parse_price(_cell(row, index, column))
                if value and key not in prices:
                    prices[key] = value
            size = parse_line(_cell(row, index, line))
            if size is not None and not any(k.startswith("ah_") for k in prices):
                home_key, away_key = ah_keys(size)
                for key, column in ((home_key, ah_home), (away_key, ah_away)):
                    value = parse_price(_cell(row, index, column))
                    if value:
                        prices[key] = value
        if prices:
            odds[source] = prices
    return odds


def _alias(league: str, name: str) -> str:
    return TEAM_ALIASES.get(league, {}).get(name, name)


def parse_main(content: bytes, league: str, season: str) -> list[MatchRow]:
    """Rows of one mmz4281 file; rows without a full-time score are skipped."""
    header, body = _rows(decode(content))
    index = _index(header)
    name, country, tier = MAIN_LEAGUES.get(league, (league, "", 0))
    rows = []
    for raw in body:
        day = parse_date(_cell(raw, index, "Date"))
        home = _alias(league, clean_name(_cell(raw, index, "HomeTeam") or _cell(raw, index, "HT")))
        away = _alias(league, clean_name(_cell(raw, index, "AwayTeam") or _cell(raw, index, "AT")))
        home_goals = parse_count(_cell(raw, index, "FTHG") or _cell(raw, index, "HG"))
        away_goals = parse_count(_cell(raw, index, "FTAG") or _cell(raw, index, "AG"))
        if day is None or not home or not away or home_goals is None or away_goals is None:
            continue
        stats = {
            key: parse_count(_cell(raw, index, column)) for key, column in _STAT_COLUMNS.items()
        }
        referee = clean_name(_cell(raw, index, "Referee")) or None
        rows.append(
            MatchRow(
                league=league,
                season=season,
                date=day,
                home=home,
                away=away,
                home_goals=home_goals,
                away_goals=away_goals,
                country=country,
                tier=tier,
                time=parse_time(_cell(raw, index, "Time")),
                ht_home_goals=parse_count(_cell(raw, index, "HTHG")),
                ht_away_goals=parse_count(_cell(raw, index, "HTAG")),
                referee=referee,
                odds=parse_odds(raw, index),
                season_label=season,
                **stats,
            )
        )
    return _dedupe(rows)


def _extra_season(label: str, day: date) -> str:
    """ "2012/2013" -> "1213"; a calendar-year label -> the July-June season of the date."""
    found = re.match(r"^(\d{4})\s*/\s*(\d{4})$", label)
    if found:
        return season_code(int(found.group(1)))
    return season_code(day.year if day.month >= 7 else day.year - 1)


def parse_extra(content: bytes, league: str) -> list[MatchRow]:
    """Rows of one new/{CODE}.csv file (all seasons), CLOSING 1X2 odds only."""
    header, body = _rows(decode(content))
    index = _index(header)
    name, country = EXTRA_LEAGUES.get(league, (league, ""))
    # The file mixes competitions (e.g. SWZ Challenge League play-offs): per season keep the
    # most frequent League value.
    per_season: dict[str, Counter] = {}
    for raw in body:
        label = clean_name(_cell(raw, index, "Season"))
        per_season.setdefault(label, Counter())[clean_name(_cell(raw, index, "League"))] += 1
    main_competition = {
        label: counts.most_common(1)[0][0] for label, counts in per_season.items() if counts
    }
    rows = []
    for raw in body:
        label = clean_name(_cell(raw, index, "Season"))
        if clean_name(_cell(raw, index, "League")) != main_competition.get(label):
            continue
        day = parse_date(_cell(raw, index, "Date"))
        home = _alias(league, clean_name(_cell(raw, index, "Home")))
        away = _alias(league, clean_name(_cell(raw, index, "Away")))
        home_goals = parse_count(_cell(raw, index, "HG"))
        away_goals = parse_count(_cell(raw, index, "AG"))
        if day is None or not home or not away or home_goals is None or away_goals is None:
            continue
        rows.append(
            MatchRow(
                league=league,
                season=_extra_season(label, day),
                date=day,
                home=home,
                away=away,
                home_goals=home_goals,
                away_goals=away_goals,
                country=country,
                tier=1,
                time=parse_time(_cell(raw, index, "Time")),
                odds=parse_odds(raw, index, [s for s in ODDS_SOURCES if is_closing(s)]),
                season_label=label,
                extra=True,
            )
        )
    return _dedupe(rows)


def sort_key(row: MatchRow) -> tuple:
    return (row.date, row.time or "", row.league, row.home, row.away)


def _dedupe(rows: list[MatchRow]) -> list[MatchRow]:
    seen, output = set(), []
    for row in sorted(rows, key=sort_key):
        identity = (row.league, row.date, row.home, row.away)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(row)
    return output


# --------------------------------------------------------------------------- files and cache


def main_path(raw_dir: Path, season: str, league: str) -> Path:
    return Path(raw_dir) / "main" / season / f"{league}.csv"


def extra_path(raw_dir: Path, league: str) -> Path:
    return Path(raw_dir) / "new" / f"{league}.csv"


def _cached_parse(path: Path, cache_dir: Path | None, parse) -> list[MatchRow]:
    stat = path.stat()
    stamp = (PARSER_VERSION, stat.st_size, stat.st_mtime_ns)
    cache_file = None
    if cache_dir is not None:
        relative = path.parent.name + "-" + path.stem
        if path.parent.parent.name == "main":
            relative = "main-" + relative
        cache_file = Path(cache_dir) / f"{relative}.pkl"
        if cache_file.exists():
            try:
                with cache_file.open("rb") as handle:
                    saved_stamp, rows = pickle.load(handle)
                if saved_stamp == stamp:
                    return rows
            except Exception:  # noqa: BLE001 - a broken cache is simply rebuilt
                pass
    rows = parse(path.read_bytes())
    if cache_file is not None:
        # Several benchmark processes may parse the same file at once: unique temporary name,
        # and a lost race simply leaves the other process's (identical) cache in place.
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                pickle.dump((stamp, rows), handle, protocol=pickle.HIGHEST_PROTOCOL)
            temporary.replace(cache_file)
        except OSError:
            temporary.unlink(missing_ok=True)
    return rows


def season_range(first: str, last: str) -> list[str]:
    start, end = season_start(first), season_start(last)
    return [season_code(year) for year in range(start, end + 1)]


def load_rows(
    leagues: Iterable[str] = tuple(MAIN_LEAGUES),
    first_season: str = FIRST_SEASON,
    last_season: str = RUNNING_SEASON,
    *,
    raw_dir: str | Path = RAW_DIR,
    cache_dir: str | Path | None = CACHE_DIR,
) -> list[MatchRow]:
    """All local rows of `leagues` between two seasons, sorted by (date, time, league, home).

    Missing files are skipped silently (``coverage()`` reports them).
    """
    raw_dir = Path(raw_dir)
    cache = Path(cache_dir) if cache_dir is not None else None
    seasons = set(season_range(first_season, last_season))
    rows: list[MatchRow] = []
    for league in dict.fromkeys(leagues):
        if league in MAIN_LEAGUES:
            for season in sorted(seasons):
                path = main_path(raw_dir, season, league)
                if path.exists():
                    rows.extend(
                        _cached_parse(
                            path,
                            cache,
                            lambda content, s=season, lg=league: parse_main(content, lg, s),
                        )
                    )
        elif league in EXTRA_LEAGUES:
            path = extra_path(raw_dir, league)
            if path.exists():
                parsed = _cached_parse(
                    path, cache, lambda content, lg=league: parse_extra(content, lg)
                )
                rows.extend(row for row in parsed if row.season in seasons)
        else:
            raise ValueError(f"Ligă necunoscută: {league}")
    rows.sort(key=sort_key)
    return rows


# --------------------------------------------------------------------------- download


def _manifest_path(raw_dir: Path) -> Path:
    return Path(raw_dir) / MANIFEST_NAME


def read_manifest(raw_dir: str | Path = RAW_DIR) -> dict:
    path = _manifest_path(Path(raw_dir))
    if not path.exists():
        return {"files": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}


def _write_manifest(raw_dir: Path, manifest: dict) -> None:
    path = _manifest_path(raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def looks_like_csv(content: bytes) -> bool:
    head = decode(content[:400]).lstrip()
    return head.startswith(("Div", "Country")) and ("," in head or "\t" in head)


def planned_files(
    leagues: Iterable[str], seasons: Iterable[str], raw_dir: Path
) -> list[tuple[str, Path, str]]:
    """(url, local path, season or "all") of every file to fetch."""
    plan = []
    seasons = list(seasons)
    for league in dict.fromkeys(leagues):
        if league in MAIN_LEAGUES:
            for season in seasons:
                plan.append(
                    (
                        MAIN_URL.format(season=season, code=league),
                        main_path(raw_dir, season, league),
                        season,
                    )
                )
        elif league in EXTRA_LEAGUES:
            plan.append((EXTRA_URL.format(code=league), extra_path(raw_dir, league), "all"))
        else:
            raise ValueError(f"Ligă necunoscută: {league}")
    return plan


def download(
    leagues: Iterable[str] = ALL_LEAGUES,
    seasons: Iterable[str] = SEASONS,
    *,
    raw_dir: str | Path = RAW_DIR,
    refresh: bool = False,
    delay: float = 1.0,
    client=None,
    log=print,
    sleep=time.sleep,
) -> dict:
    """Fetch missing files (and, with ``refresh``, the running season and the extra files).

    Returns counts {"downloaded", "skipped", "missing", "failed"}. Every fetched file is
    recorded in the manifest with its url, sha256 and download time.
    """
    import httpx

    raw_dir = Path(raw_dir)
    manifest = read_manifest(raw_dir)
    files = manifest.setdefault("files", {})
    own_client = client is None
    if own_client:
        client = httpx.Client(
            follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT}
        )
    counts = Counter()
    first_request = True
    try:
        for url, path, season in planned_files(leagues, seasons, raw_dir):
            relative = path.relative_to(raw_dir).as_posix()
            stale = refresh and season in (RUNNING_SEASON, "all")
            if path.exists() and not stale:
                counts["skipped"] += 1
                continue
            if files.get(relative, {}).get("status") == 404 and not stale:
                counts["missing"] += 1
                continue
            if not first_request and delay > 0:
                sleep(delay)
            first_request = False
            try:
                response = client.get(url)
            except httpx.HTTPError as error:
                counts["failed"] += 1
                log(f"eșec {relative}: {type(error).__name__}")
                continue
            entry = {
                "url": url,
                "status": response.status_code,
                "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if response.status_code == 404:
                files[relative] = entry
                counts["missing"] += 1
                log(f"lipsă {relative}")
                continue
            content = response.content
            if response.status_code != 200 or not looks_like_csv(content):
                counts["failed"] += 1
                log(f"eșec {relative}: HTTP {response.status_code}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            temporary.write_bytes(content)
            temporary.replace(path)
            entry.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
            files[relative] = entry
            counts["downloaded"] += 1
            log(f"descărcat {relative} ({len(content) // 1024} KB)")
            if counts["downloaded"] % 20 == 0:
                _write_manifest(raw_dir, manifest)
    finally:
        _write_manifest(raw_dir, manifest)
        if own_client:
            client.close()
    return dict(counts)


# --------------------------------------------------------------------------- coverage


def coverage(rows: Iterable[MatchRow]) -> dict[str, dict[str, dict]]:
    """league -> season -> {n, ht, sot, corners, cards, referee, avg, avg_closing} fill rates."""
    table: dict[str, dict[str, Counter]] = {}
    for row in rows:
        counts = table.setdefault(row.league, {}).setdefault(row.season, Counter())
        counts["n"] += 1
        counts["ht"] += row.ht_home_goals is not None and row.ht_away_goals is not None
        counts["sot"] += row.home_sot is not None and row.away_sot is not None
        counts["corners"] += row.home_corners is not None and row.away_corners is not None
        counts["cards"] += all(
            value is not None
            for value in (row.home_yellow, row.away_yellow, row.home_red, row.away_red)
        )
        counts["referee"] += bool(row.referee)
        for source in ("avg", "avg_closing", "ps"):
            prices = row.odds.get(source, {})
            counts[source] += all(key in prices for key in ("1", "X", "2"))
            counts[f"{source}_ou"] += "over25" in prices
            counts[f"{source}_ah"] += any(key.startswith("ah_1_") for key in prices)
    output: dict[str, dict[str, dict]] = {}
    for league, seasons in table.items():
        for season, counts in sorted(seasons.items()):
            n = counts["n"]
            output.setdefault(league, {})[season] = {
                "n": n,
                **{key: round(value / n, 3) for key, value in counts.items() if key != "n"},
            }
    return output


def format_coverage(table: dict[str, dict[str, dict]], seasons: Iterable[str]) -> str:
    seasons = list(seasons)
    lines = [
        "ligă  " + " ".join(f"{s:>16}" for s in seasons),
        "      " + " ".join(f"{'n ht sot cor card ref':>16}"[:16] for _ in seasons),
    ]
    for league in sorted(table, key=lambda code: ALL_LEAGUES.index(code)):
        cells = []
        for season in seasons:
            m = table[league].get(season)
            if not m:
                cells.append(f"{'-':>16}")
                continue
            flags = "".join(
                "Y" if m.get(key, 0) >= 0.9 else ("p" if m.get(key, 0) > 0 else ".")
                for key in ("ht", "sot", "corners", "cards", "referee", "avg", "avg_closing")
            )
            cells.append(f"{m['n']:>5} {flags:>10}")
        lines.append(f"{league:<6}" + " ".join(cells))
    lines.append("flags: ht sot corners cards referee avg(pre-closing 1X2) avg_closing")
    return "\n".join(lines)


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fotbalPrediction.data",
        description="Descarcă și verifică arhiva football-data.co.uk pentru FotbalPrediction.",
    )
    parser.add_argument("--download", action="store_true", help="descarcă fișierele lipsă")
    parser.add_argument(
        "--refresh", action="store_true", help=f"redescarcă sezonul curent {RUNNING_SEASON}"
    )
    parser.add_argument("--leagues", help="coduri separate prin virgulă (implicit: toate)")
    parser.add_argument("--seasons", help="sezoane separate prin virgulă (implicit 0506..2627)")
    parser.add_argument("--delay", type=float, default=1.0, help="pauză între cereri (secunde)")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--coverage", action="store_true", help="tabel de acoperire local")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    leagues = _split(args.leagues) or list(ALL_LEAGUES)
    seasons = _split(args.seasons) or list(SEASONS)
    unknown = [code for code in leagues if code not in ALL_LEAGUES]
    if unknown:
        print(f"Eroare: ligi necunoscute {unknown}", file=sys.stderr)
        return 2
    if args.download or args.refresh:
        counts = download(
            leagues, seasons, raw_dir=args.raw_dir, refresh=args.refresh, delay=args.delay
        )
        print(f"gata: {counts}")
    if args.coverage or not (args.download or args.refresh):
        rows = load_rows(
            leagues,
            min(seasons, key=season_start),
            max(seasons, key=season_start),
            raw_dir=args.raw_dir,
            cache_dir=args.cache_dir,
        )
        print(format_coverage(coverage(rows), seasons))
        print(f"rânduri: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
