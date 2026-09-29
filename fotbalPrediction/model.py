"""Modelul de producție fotbalPrediction (v1): compunere din candidații auditați.

Ce intră (toți auditați fără leakage, reproduși exact și mai buni decât baseline-ul pe 2223/2324,
confirmați o dată pe 2425; detalii în ``EXPERIMENTS.md``):

- ``goals_model`` (candidat): matricea de scor pentru TOATE piețele de goluri și de pauză
  (1X2, șansă dublă, total 0.5-4.5, goluri pe echipă, GG/NG, DNB, handicap asiatic -2.5..+2.5
  inclusiv liniile sfert, scor exact, pauză 1X2 și total la pauză).
- ``corners_cards`` (candidat): cornere, cartonașe, puncte cartonașe (galben 1, roșu 2) și
  șuturi pe poartă, cu propria regulă de selecție înghețată pe 2223+2324.
- ``odds_blend`` (idee): cu cote reale, 1X2 și peste/sub 2.5 se combină cu piața fără marjă
  (metoda „power”) printr-un amestec geometric/logit cu pondere FIXĂ 0.9 pe piață
  (``PRODUCTION_ODDS_WEIGHTS``); ponderile învățate (~ -0.1 / 1.1) nu se livrează, fiindcă
  amplifică zgomotul cotelor FlashScore.

Neintegrate (motivele în EXPERIMENTS.md): ``rating_stack`` (câștig mic pe 1X2 fără cote, zero
cu cote, de 5 ori mai lent, stivă sklearn) și ``selector`` (≈33 selecții pe meci; regula
de mai jos atinge aceeași țintă cu o selecție pe grup de piață).

Regula de selecție (pe piață; ``selection_rule.json``, derivată de ``tune_rule.py`` NUMAI pe
2223+2324, înghețată înainte de confirmarea 2425):

- piețe de goluri și pauză: cheia trebuie să fie în lista permisă (``select`` pentru ținta 80%,
  ``select_high`` pentru 85%), ``prag <= p <= 0.93`` (peste 0.93 cota corectă e sub ~1.08,
  deci „pariul” e trivial), ambele echipe cu cel puțin 5 meciuri de ligă în ultimii 730 de zile
  (sau rating cunoscut din liga anterioară) și cel mult O SELECȚIE PE GRUP DE PIAȚĂ și meci:
  cheia eligibilă cu cea mai mică probabilitate (cota corectă cea mai lungă care trece pragul);
- cornere / cartonașe / șuturi: regula înghețată a candidatului ``corners_cards`` (aceeași
  idee: listă permisă, 0.80 <= p <= 0.93, o selecție pe grup și meci).

Ținta ≥80% / ≥85% se raportează la nivel de GRUP de piață, cu acoperirea și cota corectă medie
a selecțiilor. Selecțiile au cote scurte; nicio selecție cu preț real nu a avut ROI pozitiv
(vezi EXPERIMENTS.md): acuratețea mare NU înseamnă profit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import pickle
import threading
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from . import data
from . import markets as mk
from .benchmark import MatchContext
from .candidates.corners_cards import CornersCardsModel
from .candidates.goals_model import GoalsModel

VERSION = "fotbal-1.0"
SELECT = "selectează"
NO_BET = "fără pariu"
THRESHOLD = 0.80
THRESHOLD_HIGH = 0.85
MAX_P = 0.93
# (1X2, peste/sub 2.5): ponderea pieței fără marjă în amestec (odds_blend: pool fix 0.9).
PRODUCTION_ODDS_WEIGHTS = (0.9, 0.9)
DEMARGIN = "power"
# O carte de cote e acceptată doar cu o marjă plauzibilă (cotele „best of” pot coborî sub 1).
MIN_BOOK, MAX_BOOK = 0.97, 1.25
GOAL_STATS = ("goals", "ht_goals")
RULE_FILE = Path(__file__).with_name("selection_rule.json")
STORE_DIR = data.ROOT / "footypreds" / "data" / "fotbalPrediction"
# Pe fiecare grup de piață răspunsul către pagină păstrează doar cele mai probabile chei.
DISPLAY_PER_GROUP = 6
# Sursele care definesc modelul antrenat: o modificare a lor invalidează pickle-ul chiar fără
# schimbarea lui VERSION (app.py, train.py, names.py nu intră în starea antrenată).
MODEL_SOURCES = (
    "model.py",
    "markets.py",
    "data.py",
    "benchmark.py",
    "candidates/goals_model.py",
    "candidates/corners_cards.py",
)
LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- odds


def demargin(prices, method: str = DEMARGIN) -> list[float] | None:
    """Probabilități fără marjă dintr-un set exclusiv de cote zecimale (sau None).

    ``power``: q_i ** k cu suma 1 (Newton pe k), q_i = 1 / cotă; ``proportional``: q_i / S.
    Refuză cote lipsă, invalide sau o carte cu marjă neplauzibilă (S în afara 0.97..1.25).
    """
    try:
        q = [1.0 / float(price) for price in prices]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if len(q) < 2 or any(not (0.0 < v < 1.0) or not math.isfinite(v) for v in q):
        return None
    total = sum(q)
    if not MIN_BOOK <= total <= MAX_BOOK:
        return None
    if method == "proportional" or abs(total - 1.0) < 1e-12:
        return [v / total for v in q]
    if method != "power":
        raise ValueError(f"Metodă necunoscută: {method}")
    logs = [math.log(v) for v in q]
    k = 1.0
    for _ in range(40):
        terms = [math.exp(k * lv) for lv in logs]
        f = sum(terms) - 1.0
        if abs(f) < 1e-13:
            break
        k -= f / sum(lv * t for lv, t in zip(logs, terms))
    p = [math.exp(k * lv) for lv in logs]
    s = sum(p)
    return [v / s for v in p]


def fair_prices(odds: Mapping[str, float] | None, method: str = DEMARGIN) -> dict[str, float]:
    """Cote fără marjă (1/p) pentru 1X2 și peste/sub 2.5; restul cheilor sunt ignorate."""
    if not odds:
        return {}
    output: dict[str, float] = {}
    for keys in (("1", "X", "2"), ("over25", "under25")):
        probs = demargin([odds.get(k) for k in keys], method)
        if probs is not None:
            output.update({k: 1.0 / p for k, p in zip(keys, probs)})
    return output


# --------------------------------------------------------------------------- rule


@lru_cache(maxsize=4)
def _load_rule(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_rule(path: str | Path = RULE_FILE) -> dict:
    """Regula înghețată: {"select": [chei], "select_high": [chei], ...}."""
    return _load_rule(str(path))


# --------------------------------------------------------------------------- model


class FootballModel:
    """Modelul de benchmark și producție (protocolul ``fotbalPrediction.benchmark``).

    ``rule="frozen"`` folosește ``selection_rule.json``; ``rule="derive"`` (doar pentru
    ``tune_rule.py``) selectează ORICE cheie de goluri din banda ``prag <= p <= max_p`` care
    trece pragul de experiență, ca să se poată deriva lista permisă.
    """

    def __init__(
        self,
        market_weight: float = PRODUCTION_ODDS_WEIGHTS[0],
        totals_market_weight: float = PRODUCTION_ODDS_WEIGHTS[1],
        demargin_method: str = DEMARGIN,
        rule: str = "frozen",
        rule_path: str | None = None,
        threshold: float = THRESHOLD,
        threshold_high: float = THRESHOLD_HIGH,
        max_p: float = MAX_P,
        count_params: dict | None = None,
        with_counts: bool = True,
        **goal_params,
    ):
        if rule not in ("frozen", "derive"):
            raise ValueError(f"Regulă necunoscută: {rule}")
        self.goals = GoalsModel(
            market_weight=market_weight, totals_market_weight=totals_market_weight, **goal_params
        )
        self.counts = CornersCardsModel(**(count_params or {}))
        self.with_counts = with_counts
        self.demargin_method = demargin_method
        self.rule = rule
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.max_p = max_p
        self.allow: frozenset[str] = frozenset()
        self.allow_high: frozenset[str] = frozenset()
        if rule == "frozen":
            frozen = load_rule(rule_path or RULE_FILE)
            self.allow = frozenset(frozen["select"])
            self.allow_high = frozenset(frozen["select_high"])
        self._chosen: dict[str, tuple[set[str], set[str]]] = {}

    # ------------------------------------------------------------------ protocol

    def _context(self, ctx):
        """Contextul cu cotele 1X2 și peste/sub 2.5 transformate în cote fără marjă."""
        if not ctx.odds:
            return ctx
        fair = fair_prices(ctx.odds, self.demargin_method)
        return dataclasses.replace(ctx, odds=MappingProxyType(fair) if fair else None)

    def predict(self, ctx) -> dict[str, float]:
        ctx = self._context(ctx)
        output = self.goals.predict(ctx)
        if self.with_counts:
            output.update(self.counts.predict(ctx))
        if self.rule == "frozen":
            self._chosen[ctx.match_id] = (
                self._choose(ctx, output, self.threshold, self.allow),
                self._choose(ctx, output, self.threshold_high, self.allow_high),
            )
        return output

    def update(self, row) -> None:
        self._chosen.pop(row.id, None)
        self.goals.update(row)
        if self.with_counts:
            self.counts.update(row)

    def forget(self, match_id: str) -> None:
        """Șterge starea per meci a unei predicții de producție (nu va primi update)."""
        self._chosen.pop(match_id, None)
        self.goals._sample.pop(match_id, None)
        self.counts._sample.pop(match_id, None)
        self.counts._chosen.pop(match_id, None)

    # ------------------------------------------------------------------ selection

    def _eligible(self, ctx, key: str, p: float, threshold: float) -> bool:
        return threshold - 1e-12 <= p <= self.max_p and self.goals._allowed(ctx, key, p)

    def _choose(self, ctx, output: dict[str, float], threshold: float, allow) -> set[str]:
        """O cheie de goluri/pauză pe grup: cea eligibilă cu p minim (cota cea mai lungă)."""
        best: dict[str, tuple[float, str]] = {}
        for key, p in output.items():
            market = mk.CATALOGUE[key]
            if market.stat not in GOAL_STATS or not market.selectable or key not in allow:
                continue
            if not self._eligible(ctx, key, p, threshold):
                continue
            if market.group not in best or p < best[market.group][0]:
                best[market.group] = (p, key)
        return {key for _, key in best.values()}

    def _goal_rule(self, ctx, key: str, p: float, high: bool) -> bool:
        if self.rule == "derive":
            return self._eligible(ctx, key, p, self.threshold_high if high else self.threshold)
        return key in self._chosen.get(ctx.match_id, (set(), set()))[1 if high else 0]

    def select(self, ctx, key: str, p: float) -> bool:
        if mk.CATALOGUE[key].stat in GOAL_STATS:
            return self._goal_rule(ctx, key, p, False)
        return bool(self.counts.select(ctx, key, p))

    def select_high(self, ctx, key: str, p: float) -> bool:
        if mk.CATALOGUE[key].stat in GOAL_STATS:
            return self._goal_rule(ctx, key, p, True)
        return bool(self.counts.select_high(ctx, key, p))

    def selectable(self, key: str) -> bool:
        """Cheia poate fi selectată vreodată de regula înghețată (standard sau strictă)."""
        market = mk.CATALOGUE[key]
        if not market.selectable:
            return False
        if market.stat in GOAL_STATS:
            return key in self.allow or key in self.allow_high
        allowed = self.counts.allowed
        return market.group in self.counts.select_groups and (allowed is None or key in allowed)


def factory(**params) -> FootballModel:
    return FootballModel(**params)


def benchmark_factory(**params) -> FootballModel:
    """Alias folosit în documentație: ``--model fotbalPrediction.model:benchmark_factory``."""
    return FootballModel(**params)


# --------------------------------------------------------------------------- production


def season_of(day: date) -> str:
    """Sezonul football-data (iulie-iunie) al unei zile."""
    return data.season_code(day.year if day.month >= 7 else day.year - 1)


def settle(key: str, score: tuple[int, int] | None, row=None) -> bool | None:
    """Rezultatul unei piețe: True câștigat (și pe jumătate), False pierdut, None nedecontat.

    Golurile se decontează din scorul final FlashScore; pauza, cornerele, cartonașele și
    șuturile pe poartă doar din rândul football-data al meciului (``row``), altfel None.
    """
    market = mk.CATALOGUE[key]
    if market.stat == "goals" and score is not None:
        pair = score
    elif row is not None:
        pair = mk.stat_pair(row, market.stat)
    else:
        pair = None
    if pair is None:
        return None
    outcome = mk.outcome_of(*market.settle_pair(*pair))
    return None if outcome is None else outcome == 1.0


def data_signature(raw_dir: str | Path = data.RAW_DIR) -> str:
    """Amprenta fișierelor CSV principale (cale relativă, mărime, mtime)."""
    raw = Path(raw_dir)
    digest = hashlib.sha256()
    for path in sorted(raw.glob("main/*/*.csv")):
        stat = path.stat()
        name = path.relative_to(raw).as_posix()
        digest.update(f"{name}|{stat.st_size}|{stat.st_mtime_ns};".encode())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def code_signature() -> str:
    """Amprenta codului modelului (``MODEL_SOURCES``); citită o dată pe proces."""
    digest = hashlib.sha256()
    base = Path(__file__).parent
    for name in MODEL_SOURCES:
        digest.update(name.encode())
        digest.update((base / name).read_bytes())
    return digest.hexdigest()


def cache_key(params: Mapping, raw_dir: str | Path = data.RAW_DIR, rule_path=RULE_FILE) -> str:
    """VERSION + codul modelului + parametri + regula înghețată + fișierele de date."""
    digest = hashlib.sha256()
    digest.update(VERSION.encode())
    digest.update(code_signature().encode())
    digest.update(json.dumps(dict(params), sort_keys=True, default=str).encode())
    digest.update(Path(rule_path).read_bytes())
    digest.update(data_signature(raw_dir).encode())
    return digest.hexdigest()[:20]


def predictor_key(
    raw_dir: str | Path = data.RAW_DIR,
    params: Mapping | None = None,
    first_season: str = data.FIRST_SEASON,
) -> str:
    """Cheia pickle-ului pe care ``FootballPredictor.load_or_train`` l-ar folosi acum."""
    return cache_key({**dict(params or {}), "first_season": first_season}, raw_dir)


class FootballPredictor:
    """Modelul antrenat pe toate datele locale (inclusiv sezonul curent parțial) + potrivirea
    numelor FlashScore. Predicțiile modifică cache-uri interne, deci rulează sub un lacăt.
    """

    def __init__(self, model: FootballModel, rows: Iterable, key: str = ""):
        rows = list(rows)
        self.model = model
        self.key = key
        self.version = VERSION
        self.trained_through: date | None = max((r.date for r in rows), default=None)
        recent = self.trained_through - timedelta(days=500) if self.trained_through else None
        teams: dict[str, set[str]] = {}
        self._rows: dict[tuple[str, date], list] = {}
        for row in rows:
            if recent is not None and row.date < recent:
                continue
            teams.setdefault(row.league, set()).update((row.home, row.away))
            self._rows.setdefault((row.league, row.date), []).append(row)
        self.teams = {code: sorted(names) for code, names in teams.items()}
        self._lock = threading.Lock()
        self._resolver = None

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_lock", None)
        state["_resolver"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()

    @property
    def resolver(self):
        """Rezolvitorul se construiește la cerere, ca modificările din overrides să conteze."""
        if self._resolver is None:
            from .names import TeamResolver, load_overrides

            country_of = {code: info[1] for code, info in data.MAIN_LEAGUES.items()}
            self._resolver = TeamResolver(self.teams, country_of, load_overrides())
        return self._resolver

    @property
    def team_count(self) -> int:
        return len({name for names in self.teams.values() for name in names})

    @property
    def league_count(self) -> int:
        return len(self.teams)

    # ------------------------------------------------------------------ training

    @classmethod
    def train(cls, rows: Iterable, params: Mapping | None = None, key: str = ""):
        rows = sorted(rows, key=data.sort_key)
        model = FootballModel(**dict(params or {}))
        for row in rows:
            model.update(row)
        return cls(model, rows, key)

    @classmethod
    def load_or_train(
        cls,
        raw_dir: str | Path = data.RAW_DIR,
        cache_dir: str | Path | None = data.CACHE_DIR,
        store_dir: str | Path = STORE_DIR,
        params: Mapping | None = None,
        first_season: str = data.FIRST_SEASON,
    ) -> FootballPredictor:
        """Încarcă pickle-ul cu aceeași cheie sau reantrenează pe toate ligile principale."""
        params = dict(params or {})
        key = predictor_key(raw_dir, params, first_season)
        store = Path(store_dir)
        path = store / f"model-{key}.pkl"
        if path.exists():
            try:
                with path.open("rb") as handle:
                    predictor = pickle.load(handle)
                if isinstance(predictor, cls) and predictor.key == key:
                    return predictor
            except Exception:  # noqa: BLE001 - a corrupt cache is simply rebuilt
                LOG.warning("Cache-ul modelului fotbalPrediction nu poate fi citit; reantrenez.")
        rows = data.load_rows(
            tuple(data.MAIN_LEAGUES),
            first_season,
            data.RUNNING_SEASON,
            raw_dir=raw_dir,
            cache_dir=cache_dir,
        )
        if not rows:
            raise FileNotFoundError(
                "Nu există date football-data locale: rulează "
                "`python -m fotbalPrediction.data --download`."
            )
        predictor = cls.train(rows, params, key)
        store.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            pickle.dump(predictor, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)
        for old in store.glob("model-*.pkl"):
            if old != path:
                old.unlink(missing_ok=True)
        return predictor

    # ------------------------------------------------------------------ names

    def resolve(self, league: str | None, country: str | None, home: str, away: str) -> dict:
        from .names import league_code

        code = league_code(league, country)
        if code is None:
            return {"code": None, "home": None, "away": None, "reason": "ligă fără model"}
        found_home = self.resolver.resolve(code, home)
        found_away = self.resolver.resolve(code, away)
        reason = None
        if found_home is None or found_away is None:
            missing = [n for n, f in ((home, found_home), (away, found_away)) if f is None]
            reason = "echipă nerecunoscută: " + ", ".join(missing)
        elif found_home == found_away:
            reason = "nume ambigue"
        if reason:
            found_home = found_away = None
        return {"code": code, "home": found_home, "away": found_away, "reason": reason}

    def search(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        return self.resolver.search(query, limit)

    def find_row(self, code: str, day: date, home: str, away: str):
        """Rândul football-data al meciului (data ±1 zi, fus orar), dacă e deja descărcat."""
        for delta in (0, -1, 1):
            for row in self._rows.get((code, day + timedelta(days=delta)), ()):
                if row.home == home and row.away == away:
                    return row
        return None

    # ------------------------------------------------------------------ prediction

    def context(
        self,
        code: str,
        home: str,
        away: str,
        day: date,
        odds: Mapping[str, float] | None = None,
        match_id: str | None = None,
    ) -> MatchContext:
        _, country, tier = data.MAIN_LEAGUES[code]
        prices = {k: float(v) for k, v in (odds or {}).items() if k in mk.CATALOGUE and v}
        return MatchContext(
            match_id=match_id or f"{code}-{day:%Y%m%d}-{home}-{away}",
            league=code,
            country=country,
            tier=tier,
            season=season_of(day),
            date=day,
            time=None,
            home=home,
            away=away,
            referee=None,
            odds=MappingProxyType(prices) if prices else None,
            odds_source="flashscore" if prices else None,
            odds_closing=False,
        )

    def predict(
        self,
        code: str,
        home: str,
        away: str,
        day: date,
        odds: Mapping[str, float] | None = None,
        match_id: str | None = None,
        full: bool = False,
    ) -> dict:
        """Piețele (toate sau cele afișabile) cu probabilitate, cotă corectă și decizii."""
        ctx = self.context(code, home, away, day, odds, match_id)
        with self._lock:
            probs = self.model.predict(ctx)
            chosen = {
                key: (self.model.select(ctx, key, p), self.model.select_high(ctx, key, p))
                for key, p in probs.items()
            }
            self.model.forget(ctx.match_id)
        blended = bool(fair_prices(ctx.odds))
        markets = []
        for key, p in probs.items():
            market = mk.CATALOGUE[key]
            pick, pick_high = chosen[key]
            price = ctx.odds.get(key) if ctx.odds else None
            markets.append(
                {
                    "key": key,
                    "label": market.label,
                    "group": market.group,
                    "group_label": mk.GROUPS[market.group],
                    "stat": market.stat,
                    "probability": round(p, 6),
                    "fair_odds": round(1.0 / p, 3) if p > 1e-9 else None,
                    "odds": round(price, 3) if price else None,
                    "selectable": self.model.selectable(key),
                    "decision": SELECT if pick else NO_BET,
                    "decision_high": SELECT if pick_high else NO_BET,
                }
            )
        if not full:
            markets = display_markets(markets)
        return {
            "league": code,
            "home": home,
            "away": away,
            "day": day.isoformat(),
            "retro": self.trained_through is not None and day <= self.trained_through,
            "odds_blend": blended,
            "markets": markets,
        }


def display_markets(markets: list[dict]) -> list[dict]:
    """Pentru pagină: 1X2 mereu, orice cheie selectată și primele chei probabile pe grup."""
    keep: dict[str, dict] = {}
    by_group: dict[str, list[dict]] = {}
    for market in markets:
        if market["group"] == "1x2" or SELECT in (market["decision"], market["decision_high"]):
            keep[market["key"]] = market
        elif 0.5 <= market["probability"] <= 0.97 or market["group"] == "cs":
            by_group.setdefault(market["group"], []).append(market)
    for group, items in by_group.items():
        items.sort(key=lambda m: -m["probability"])
        limit = 3 if group == "cs" else DISPLAY_PER_GROUP
        for market in items[:limit]:
            keep.setdefault(market["key"], market)
    order = {key: index for index, key in enumerate(mk.KEYS)}
    return sorted(keep.values(), key=lambda m: order[m["key"]])
