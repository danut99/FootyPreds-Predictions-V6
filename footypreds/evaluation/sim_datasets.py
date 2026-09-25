"""Historical datasets for the bankroll simulator (finished matches with real pre-match odds).

Datasets:
- ``football``: the locked football-data.co.uk benchmark (5 top leagues, market-average odds).
- ``football-plus``: the benchmark plus 11 more public football-data.co.uk leagues and the
  current season, downloaded with ``python -m footypreds.evaluation.sim_datasets --download``
  into ``footypreds/data/benchmark/sim/`` (git-ignored).
- ``tennis``: tennis-data.co.uk archives via ``footypreds.evaluation.tennis_data`` (optional).
- ``local-<sport>``: finished matches stored by the app that carry pre-match odds.
- ``recent``: the last N days of the local store for the chosen sports (all of them mixed on
  one ticket), loaded day by day with ``POST /api/simulate/recent/prepare`` (``sim_api.py``).
  Every stored finished match is history; the bettable ones are the priced finished matches of
  the window, at most ``ANALYSIS_LIMIT`` per sport and day in the board order, exactly like the
  daily recommendations.

Every loader returns a ``Dataset``: all finished matches (the walk-forward history) split into
independent ``groups`` (a football-data league never sees another league's teams), and the
subset that can be bet (``bettable``: matches with a real price).
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cached_property

import httpx

from footypreds.config import DATA
from footypreds.domain import Match

BENCHMARK_DIR = DATA / "benchmark"
SIM_DIR = BENCHMARK_DIR / "sim"
TENNIS_DIR = BENCHMARK_DIR / "tennis"
PARSED_DIR = DATA / "sim_cache" / "datasets"
FD_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"

TOP5 = {
    "E0": ("Premier League", "Anglia"),
    "SP1": ("La Liga", "Spania"),
    "D1": ("Bundesliga", "Germania"),
    "I1": ("Serie A", "Italia"),
    "F1": ("Ligue 1", "Franța"),
}
EXTRA = {
    "E1": ("Championship", "Anglia"),
    "SP2": ("La Liga 2", "Spania"),
    "I2": ("Serie B", "Italia"),
    "D2": ("2. Bundesliga", "Germania"),
    "F2": ("Ligue 2", "Franța"),
    "N1": ("Eredivisie", "Olanda"),
    "B1": ("Jupiler Pro League", "Belgia"),
    "P1": ("Primeira Liga", "Portugalia"),
    "T1": ("Süper Lig", "Turcia"),
    "G1": ("Super League", "Grecia"),
    "SC0": ("Premiership", "Scoția"),
}
BENCHMARK_SEASONS = ("2122", "2223", "2324", "2425", "2526")
EXTRA_SEASONS = BENCHMARK_SEASONS + ("2627",)
# Columns of the market-average prices (football-data.co.uk notes.txt: Avg* are the pre-closing
# averages collected with the fixtures; the closing averages are the AvgC* columns). The app
# itself compares best prices across bookmakers, so these carry a larger margin.
ODDS_COLUMNS = (
    ("1", "AvgH"),
    ("X", "AvgD"),
    ("2", "AvgA"),
    ("over25", "Avg>2.5"),
    ("under25", "Avg<2.5"),
)


@dataclass
class Dataset:
    """Finished matches (sorted by kickoff, id) of one sport, grouped into independent histories."""

    id: str
    sport: str
    label: str
    source: str
    groups: dict = field(default_factory=dict)  # group -> [Match] (sorted)
    periods: dict = field(default_factory=dict)  # match id -> period label (season / year)
    warmup_days: int = 0
    hint: str = ""
    # Football-data leagues refit team ratings daily per group, exactly like the benchmark.
    fit_ratings: bool = False
    # Multi-sport datasets ("recent"): the sport of each group (default: `sport`).
    group_sports: dict = field(default_factory=dict)
    # Only these match ids may be bet (None: every priced match).
    targets: frozenset | None = None
    # Fixed (first, last) simulable days, e.g. the last N days of "recent".
    window: tuple | None = None

    @property
    def sports(self):
        found = {self.sport_of(group) for group in self.groups}
        return [s for s in SPORT_ORDER if s in found] or [self.sport]

    def sport_of(self, group):
        return self.group_sports.get(group, self.sport)

    def is_target(self, match):
        return has_price(match) and (self.targets is None or match.id in self.targets)

    @cached_property
    def matches(self):
        rows = [m for group in self.groups.values() for m in group]
        return sorted(rows, key=lambda m: (m.kickoff, m.id))

    @cached_property
    def bettable(self):
        rows = [m for m in self.matches if self.is_target(m)]
        if self.window:
            first, last = self.window
            rows = [m for m in rows if first <= m.kickoff.date() <= last]
        return rows

    def period_of(self, match):
        return self.periods.get(match.id) or str(match.kickoff.year)

    def bounds(self):
        """(first simulable day, last day) or (None, None) when empty."""
        bettable = self.bettable
        if not bettable:
            return None, None
        if self.window:
            return self.window
        first = self.matches[0].kickoff.date()
        start = max(bettable[0].kickoff.date(), first + timedelta(days=self.warmup_days))
        return start, bettable[-1].kickoff.date()

    def describe(self):
        start, end = self.bounds()
        output = {
            "id": self.id,
            "sport": self.sport,
            "label": self.label,
            "matches": len(self.matches),
            "bettable": len(self.bettable) if start else 0,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "source": self.source,
            "available": bool(start and end and start <= end),
            "hint": self.hint,
        }
        if self.group_sports:
            output["sports"] = self.sports
        return output


SPORT_ORDER = ("football", "basketball", "tennis")
# The "recent" dataset: at most this many past days, and the default window.
RECENT_MAX_DAYS = 60
RECENT_DEFAULT_DAYS = 10
RECENT_HINT = (
    "Încarcă ultimele zile: în Simulator cu butonul „Pregătește datele”, în Excel cu "
    "„Rulează simularea” pe setul recent, sau sincronizează istoricul din aplicație."
)


def utcnow():
    """Current time; tests replace it to freeze the clock of the "recent" dataset."""
    return datetime.now(timezone.utc)


def result_keys(sport):
    return ("1", "X", "2") if sport == "football" else ("1", "2")


def has_price(match):
    """A match can be bet when it has at least the result prices of its sport."""
    return all(match.odds.get(k) for k in result_keys(match.sport))


def list_prices_only(match):
    """The match with only the day list's result prices (1/X/2 or 1/2).

    Extended prices (totals, handicaps...) come from matches/odds, which carries no quote time:
    they may have been loaded after kickoff, so a blind simulation never sees them.
    """
    keys = result_keys(match.sport)
    odds = {k: v for k, v in match.odds.items() if k in keys}
    return match if odds == match.odds else match.model_copy(update={"odds": odds})


def _decode(content):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Codare CSV necunoscută.")


def parse_csv(content, league, season, today=None):
    """football-data.co.uk CSV -> [{"match", "season", "league_code", "reference_odds"}].

    Same ids and fields as ``evaluation.dataset.parse_archive``, for any league code and for a
    season in progress (rows without a final score, or dated today or later, are skipped).
    """
    names = TOP5 | EXTRA
    if league not in names:
        raise ValueError(f"Ligă necunoscută: {league}")
    today = today or datetime.now(timezone.utc).date()
    reader = csv.DictReader(io.StringIO(_decode(content)))
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError(f"Schema CSV invalidă: {league}/{season}")
    records, seen = [], set()
    for row in reader:
        raw_date = (row.get("Date") or "").strip()
        home, away = (row.get("HomeTeam") or "").strip(), (row.get("AwayTeam") or "").strip()
        if not raw_date or not home or not away or home == away:
            continue
        if not (row.get("FTHG") or "").strip() or not (row.get("FTAG") or "").strip():
            continue
        fmt = "%d/%m/%Y" if len(raw_date.split("/")[-1]) == 4 else "%d/%m/%y"
        try:
            kickoff = datetime.strptime(raw_date, fmt).replace(tzinfo=timezone.utc)
            home_goals, away_goals = int(float(row["FTHG"])), int(float(row["FTAG"]))
        except ValueError:
            continue
        if kickoff.date() >= today:
            continue
        match_id = f"fd-{league}-{kickoff.date()}-{home}-{away}"
        if match_id in seen:
            continue
        seen.add(match_id)
        odds = {}
        for key, column in ODDS_COLUMNS:
            try:
                value = float(row.get(column) or 0)
            except ValueError:
                continue
            if 1 < value < 1001:
                odds[key] = value
        match = Match(
            id=match_id,
            kickoff=kickoff,
            league=names[league][0],
            country=names[league][1],
            home=home,
            away=away,
            status="finished",
            home_goals=home_goals,
            away_goals=away_goals,
            source="football-data.co.uk",
        )
        records.append(
            {
                "match": match.model_dump(mode="json"),
                "season": season,
                "league_code": league,
                "reference_odds": odds,
            }
        )
    return records


def _football_dataset(dataset_id, label, records, hint):
    groups, periods = {}, {}
    for record in records:
        match = Match.model_validate(record["match"])
        odds = {k: v for k, v in record.get("reference_odds", {}).items() if 1 < v < 1001}
        match = match.model_copy(update={"odds": odds})
        code = record["league_code"]
        groups.setdefault(code, {})[match.id] = match
        periods[match.id] = record["season"]
    return Dataset(
        id=dataset_id,
        sport="football",
        label=label,
        source="football-data.co.uk (cote medii de piață)",
        groups={
            code: sorted(rows.values(), key=lambda m: (m.kickoff, m.id))
            for code, rows in sorted(groups.items())
        },
        periods=periods,
        # The first season only warms the ratings up: bets start one season later.
        warmup_days=330,
        hint=hint,
        fit_ratings=True,
    )


def benchmark_records(directory=BENCHMARK_DIR):
    from footypreds.evaluation.dataset import load

    records, _ = load(directory)
    return records


def load_football(directory=BENCHMARK_DIR):
    records = benchmark_records(directory)
    return _football_dataset(
        "football",
        "Fotbal – 5 ligi de top (2021–2026)",
        records,
        "python -m footypreds.evaluation.dataset",
    )


def load_extra_records(directory=SIM_DIR, today=None):
    raw = directory / "raw"
    records = []
    for path in sorted(raw.glob("*.csv")) if raw.exists() else ():
        match = re.fullmatch(r"(\d{4})-([A-Z0-9]+)\.csv", path.name)
        if not match or match.group(2) not in TOP5 | EXTRA:
            continue
        records.extend(parse_csv(path.read_bytes(), match.group(2), match.group(1), today))
    return records


def load_football_plus(benchmark=BENCHMARK_DIR, directory=SIM_DIR, today=None):
    extra = load_extra_records(directory, today)
    if not extra:
        raise FileNotFoundError("Ligile suplimentare nu sunt descărcate.")
    records = {r["match"]["id"]: r for r in benchmark_records(benchmark)}
    for record in extra:
        records.setdefault(record["match"]["id"], record)
    return _football_dataset(
        "football-plus",
        "Fotbal – 16 ligi europene (2021–azi)",
        list(records.values()),
        "python -m footypreds.evaluation.sim_datasets --download",
    )


def load_tennis(directory=TENNIS_DIR):
    try:
        from footypreds.evaluation.tennis_data import load_tennis_matches
    except ImportError as error:
        raise FileNotFoundError("Modulul de date pentru tenis nu este instalat.") from error
    raw = directory / "raw"
    if not raw.exists() or not any(raw.glob("*.xlsx")):
        raise FileNotFoundError("Arhivele tennis-data.co.uk nu sunt descărcate.")
    matches = [
        m
        for m in load_tennis_matches(directory=raw)
        if m.sport == "tennis" and m.status == "finished" and m.home_goals is not None
    ]
    if not matches:
        raise FileNotFoundError("Arhivele tennis-data.co.uk nu conțin meciuri.")
    unique = sorted({m.id: m for m in matches}.values(), key=lambda m: (m.kickoff, m.id))
    first, last = unique[0].kickoff.year, unique[-1].kickoff.year
    return Dataset(
        id="tennis",
        sport="tennis",
        label=f"Tenis ATP/WTA ({first}–{last})" if first != last else f"Tenis ATP/WTA ({first})",
        source="tennis-data.co.uk (cote medii de piață)",
        groups={"all": unique},
        # Months: a new year of results only recomputes the months it adds.
        periods={m.id: m.kickoff.strftime("%Y-%m") for m in unique},
        warmup_days=120,
        hint=HINTS["tennis"],
    )


def load_local(store, sport):
    """Finished matches stored by the app; only those with stored pre-match prices are bet."""
    matches = [
        m
        for m in store.matches(sport=sport)
        if m.status == "finished" and m.home_goals is not None and m.away_goals is not None
    ]
    label = {"football": "Fotbal", "basketball": "Baschet", "tennis": "Tenis"}[sport]
    return Dataset(
        id=f"local-{sport}",
        sport=sport,
        label=f"{label} – istoricul local FlashScore",
        source="baza locală (cote FlashScore salvate înainte de meci)",
        groups={"all": sorted(matches, key=lambda m: (m.kickoff, m.id))},
        # Periods are months: a new sync only recomputes the recent ones.
        periods={m.id: m.kickoff.strftime("%Y-%m") for m in matches},
        hint="Sincronizează istoricul din aplicație (Istoric → Sincronizare).",
    )


def analysis_limit():
    """Fixtures analysed per sport and day by the recommendations (same cap here)."""
    try:
        from footypreds.recommend import ANALYSIS_LIMIT
    except ImportError:
        return 150
    return ANALYSIS_LIMIT


def load_recent(store, sports=SPORT_ORDER, today=None, days=RECENT_MAX_DAYS):
    """The last `days` days (today excluded) of the local store for `sports`, blind-ready.

    Every stored finished match of those sports before today is history (one independent
    group per sport). The bettable matches are the priced ones of the window, at most
    ``ANALYSIS_LIMIT`` per sport and day, chosen in the board order (``competitions.priority``)
    like the daily recommendations: the ranking uses pre-match fields only (competition,
    prices), never a result. Postponed, cancelled or abandoned matches ("unavailable") with a
    price stay candidates and settle as void, as they would have in the app: dropping them would
    pick the day's pool with post-kickoff knowledge. Only the list's result prices are kept
    (``list_prices_only``). Periods are ISO weeks inside the window (a new day only recomputes
    its week) and months before it.
    """
    from footypreds.competitions import priority

    if store is None:
        raise FileNotFoundError("Baza locală nu este disponibilă.")
    sports = [s for s in SPORT_ORDER if s in set(sports or ())]
    if not sports:
        raise ValueError("Alege cel puțin un sport.")
    today = today or utcnow().date()
    days = max(1, min(RECENT_MAX_DAYS, int(days)))
    first, last = today - timedelta(days=days), today - timedelta(days=1)
    limit = analysis_limit()
    groups, periods, targets = {}, {}, set()
    for sport in sports:
        rows = sorted(
            (
                list_prices_only(m)
                for m in store.matches(sport=sport)
                if m.kickoff.date() < today
                and (
                    (
                        m.status == "finished"
                        and m.home_goals is not None
                        and m.away_goals is not None
                    )
                    # Called off inside the window: a void candidate, never history.
                    or (m.status == "unavailable" and first <= m.kickoff.date() and has_price(m))
                )
            ),
            key=lambda m: (m.kickoff, m.id),
        )
        by_day = {}
        for match in rows:
            day = match.kickoff.date()
            if first <= day <= last:
                periods[match.id] = day.strftime("%G-W%V")
                if has_price(match):
                    by_day.setdefault(day, []).append(match)
            else:
                periods[match.id] = match.kickoff.strftime("%Y-%m")
        for candidates in by_day.values():
            candidates.sort(key=lambda m: (priority(m), m.kickoff, m.id))
            targets.update(m.id for m in candidates[:limit])
        groups[sport] = rows
    labels = {"football": "fotbal", "basketball": "baschet", "tennis": "tenis"}
    return Dataset(
        id="recent",
        sport=sports[0] if len(sports) == 1 else "multi",
        label=f"Ultimele {days} zile – baza locală ({', '.join(labels[s] for s in sports)})",
        source="baza locală (rezultate și cote 1X2 FlashScore de dinainte de meci)",
        groups=groups,
        periods=periods,
        hint=RECENT_HINT,
        group_sports={sport: sport for sport in sports},
        targets=frozenset(targets),
        window=(first, last),
    )


_RECENT_MEMO = {}


def recent_dataset(store, sports=SPORT_ORDER, today=None, days=RECENT_MAX_DAYS):
    """load_recent, memoized per store write version, sports, day and window."""
    today = today or utcnow().date()
    key = (id(store), getattr(store, "version", 0), tuple(sports), today, days)
    if key not in _RECENT_MEMO:
        dataset = load_recent(store, sports, today, days)
        # A few windows at once (the datasets list, the status and a run); drop stale versions.
        for old in [k for k in _RECENT_MEMO if k[:2] != key[:2] or len(_RECENT_MEMO) >= 6]:
            del _RECENT_MEMO[old]
        _RECENT_MEMO[key] = dataset
    return _RECENT_MEMO[key]


DATASET_IDS = (
    "football",
    "football-plus",
    "tennis",
    "local-football",
    "local-basketball",
    "local-tennis",
    "recent",
)
UNAVAILABLE = {
    "football": ("football", "Fotbal – 5 ligi de top", "football-data.co.uk"),
    "football-plus": ("football", "Fotbal – 16 ligi europene", "football-data.co.uk"),
    "tennis": ("tennis", "Tenis ATP/WTA", "tennis-data.co.uk"),
    "local-football": ("football", "Fotbal – istoricul local FlashScore", "baza locală"),
    "local-basketball": ("basketball", "Baschet – istoricul local FlashScore", "baza locală"),
    "local-tennis": ("tennis", "Tenis – istoricul local FlashScore", "baza locală"),
    "recent": ("multi", "Ultimele zile – baza locală", "baza locală"),
}
HINTS = {
    "football": "python -m footypreds.evaluation.dataset",
    "football-plus": "python -m footypreds.evaluation.sim_datasets --download",
    "tennis": "python -m footypreds.evaluation.tennis_eval --download",
    "recent": RECENT_HINT,
}


def load_dataset(dataset_id, store=None, benchmark=BENCHMARK_DIR, today=None):
    """Dataset by id; raises FileNotFoundError when its files or stored matches are missing."""
    if dataset_id == "football":
        return load_football(benchmark)
    if dataset_id == "football-plus":
        return load_football_plus(benchmark, benchmark / "sim", today)
    if dataset_id == "tennis":
        return load_tennis(benchmark / "tennis")
    if dataset_id.startswith("local-") and dataset_id[6:] in ("football", "basketball", "tennis"):
        if store is None:
            raise FileNotFoundError("Baza locală nu este disponibilă.")
        return load_local(store, dataset_id[6:])
    if dataset_id == "recent":
        return load_recent(store, SPORT_ORDER, today)
    raise KeyError(dataset_id)


_MEMO = {}


def _signature(dataset_id, store, benchmark):
    """What a dataset depends on: file sizes/mtimes, or the store and its write version."""
    if dataset_id.startswith("local-"):
        return ("store", id(store), getattr(store, "version", 0))
    paths = [benchmark / "matches.jsonl", benchmark / "manifest.json"]
    if dataset_id == "football-plus":
        paths += sorted((benchmark / "sim" / "raw").glob("*.csv"))
    if dataset_id == "tennis":
        paths = sorted((benchmark / "tennis" / "raw").glob("*.xlsx"))
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists())


def _read_parsed(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        groups = {
            group: [Match.model_validate_json(row) for row in rows]
            for group, rows in payload["groups"].items()
        }
        return Dataset(**payload["meta"], groups=groups, periods=payload["periods"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_parsed(path, dataset):
    meta = {
        "id": dataset.id,
        "sport": dataset.sport,
        "label": dataset.label,
        "source": dataset.source,
        "warmup_days": dataset.warmup_days,
        "hint": dataset.hint,
        "fit_ratings": dataset.fit_ratings,
    }
    payload = {
        "meta": meta,
        "periods": dataset.periods,
        "groups": {g: [m.model_dump_json() for m in rows] for g, rows in dataset.groups.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob(f"{dataset.id}-*.json"):
            old.unlink(missing_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass  # Only speed is lost.


def cached_dataset(dataset_id, store=None, benchmark=BENCHMARK_DIR, cache_dir=PARSED_DIR):
    """load_dataset with an in-memory cache that follows file and store changes.

    File datasets are also kept parsed on disk (``data/sim_cache/datasets``): reading 60 000
    tennis rows back is several times faster than parsing the xlsx workbooks again.
    """
    if dataset_id == "recent":
        return recent_dataset(store, SPORT_ORDER)
    signature = _signature(dataset_id, store, benchmark)
    key = (dataset_id, str(benchmark), signature)
    if key in _MEMO:
        return _MEMO[key]
    dataset, path = None, None
    if not dataset_id.startswith("local-") and signature and cache_dir:
        digest = hashlib.sha256(json.dumps([str(benchmark), signature]).encode()).hexdigest()
        path = cache_dir / f"{dataset_id}-{digest[:16]}.json"
        dataset = _read_parsed(path) if path.exists() else None
    if dataset is None:
        dataset = load_dataset(dataset_id, store, benchmark)
        if path:
            _write_parsed(path, dataset)
    for old in [k for k in _MEMO if k[:2] == key[:2]]:
        del _MEMO[old]
    _MEMO[key] = dataset
    return dataset


def unavailable(dataset_id, reason=""):
    sport, label, source = UNAVAILABLE[dataset_id]
    hint = HINTS.get(dataset_id, "Sincronizează istoricul din aplicație.")
    return {
        "id": dataset_id,
        "sport": sport,
        "label": label,
        "matches": 0,
        "bettable": 0,
        "start": None,
        "end": None,
        "source": source,
        "available": False,
        "hint": f"{reason} {hint}".strip() if reason else hint,
    }


def availability(store=None, benchmark=BENCHMARK_DIR, loader=None):
    """[describe()] for every dataset, including the unavailable ones (with a hint)."""
    loader = loader or (lambda dataset_id: cached_dataset(dataset_id, store, benchmark))
    output = []
    for dataset_id in DATASET_IDS:
        try:
            output.append(loader(dataset_id).describe())
        except (FileNotFoundError, ValueError, KeyError, OSError) as error:
            output.append(unavailable(dataset_id, reason_of(error)))
    return output


def reason_of(error):
    """Romanian reason for a dataset that cannot load, never an OS message or a local path."""
    if isinstance(error, ValueError) or (
        isinstance(error, FileNotFoundError) and error.filename is None and str(error)
    ):
        return str(error)
    return "Fișierele setului de date nu sunt descărcate."


# --- download (network; never called by tests) -------------------------------------------


async def download(directory=SIM_DIR, leagues=None, seasons=EXTRA_SEASONS, refresh_current=True):
    """Download extra football-data.co.uk leagues (and the current season of all 16)."""
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    wanted = []
    for season in seasons:
        codes = leagues or (list(EXTRA) + (list(TOP5) if season not in BENCHMARK_SEASONS else []))
        wanted.extend((season, code) for code in codes)
    files = []
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        for season, code in wanted:
            path = raw / f"{season}-{code}.csv"
            current = season == seasons[-1] and refresh_current
            if not path.exists() or current:
                response = await client.get(FD_URL.format(season=season, league=code))
                if response.status_code == 404:
                    print(f"{season}/{code}: lipsește", flush=True)
                    continue
                response.raise_for_status()
                parse_csv(response.content, code, season)  # validate before caching
                path.write_bytes(response.content)
                await asyncio.sleep(0.15)
            content = path.read_bytes()
            count = len(parse_csv(content, code, season))
            files.append(
                {
                    "url": FD_URL.format(season=season, league=code),
                    "file": path.name,
                    "matches": count,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            print(f"{season}/{code}: {count} meciuri", flush=True)
    manifest = {
        "source": "https://www.football-data.co.uk/data.php",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Date istorice pentru simulatorul de bankroll.")
    parser.add_argument("--download", action="store_true", help="descarcă ligile suplimentare")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.download:
        manifest = asyncio.run(download())
        print(f"{len(manifest['files'])} fișiere în {SIM_DIR / 'raw'}")
    for item in availability():
        print(json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    main()
