"""Estimare „din cote” pentru meciurile pe care modelul de fotbal nu le acoperă.

Orice meci cu cote 1/X/2 (echipe naționale, cupe, ligi fără date football-data) primește toate
piețele de goluri dintr-o singură matrice de scor coerentă cu piața:

1. marja casei se scoate din cotele 1/X/2 (metoda „power”, ca în modelul principal);
2. se caută ratele de goluri (gazde, oaspeți) ale unei matrice Dixon-Coles care dau aceeași
   diferență P(1) − P(2) și aceeași probabilitate de egal ca piața: egalul fixează totalul de
   goluri (multe egaluri = puține goluri). Totalul este tras spre media ``base_total`` cu
   ponderea ``total_weight`` (1.0 = doar piața);
3. matricea este re-ponderată ca 1/X/2 să fie exact cele ale pieței, iar din ea se citesc
   șansa dublă, peste/sub, goluri pe echipă, GG/NG, handicap și scorul exact.

Nu folosește istoric de echipe, deci nu are cornere, cartonașe, șuturi sau pauză. Selecțiile
„selectează” urmează lista înghețată ``market_rule.json``, derivată doar pe sezoanele de reglaj
cu ``python -m fotbalPrediction.market_eval --derive`` (vezi EXPERIMENTS.md, „Estimarea din cote”).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import markets as mk
from .candidates.goals_model import GOAL_KEYS, SIZE, one_x_two, reweight, score_matrix
from .model import MAX_P, NO_BET, SELECT, THRESHOLD, THRESHOLD_HIGH, demargin, display_markets

VERSION = "fotbal-cote-1.0"
RULE_FILE = Path(__file__).with_name("market_rule.json")
RHO = -0.05
# totalul de goluri: base + weight * (totalul dedus din egal − base); alese pe sezoanele de reglaj
BASE_TOTAL = 2.65
TOTAL_WEIGHT = 0.75
TOTALS = np.round(np.arange(1.5, 4.51, 0.05), 2)
SPLITS = np.linspace(0.02, 0.98, 193)


@lru_cache(maxsize=2)
def _table(rho: float) -> tuple[np.ndarray, np.ndarray]:
    """(diff, draw): P(1) − P(2) și P(X) ale matricei brute pe grila (total, împărțire)."""
    diff = np.empty((len(TOTALS), len(SPLITS)))
    draw = np.empty_like(diff)
    for i, total in enumerate(TOTALS):
        for j, split in enumerate(SPLITS):
            p = one_x_two(score_matrix(total * split, total * (1 - split), rho, 0.0, SIZE))
            diff[i, j] = p[0] - p[2]
            draw[i, j] = p[1]
    diff.setflags(write=False)
    draw.setflags(write=False)
    return diff, draw


def implied_rates(
    target,
    base_total: float = BASE_TOTAL,
    total_weight: float = TOTAL_WEIGHT,
    rho: float = RHO,
) -> tuple[float, float]:
    """Ratele de goluri (gazde, oaspeți) care reproduc diferența 1−2 și egalul pieței."""
    diff, draw = _table(rho)
    want = float(target[0] - target[2])
    splits = np.array([np.interp(want, diff[i], SPLITS) for i in range(len(TOTALS))])
    draws = np.array([np.interp(splits[i], SPLITS, draw[i]) for i in range(len(TOTALS))])
    # egalul scade când totalul crește; forțăm monotonia ca inversarea să fie unică
    draws = np.minimum.accumulate(draws)
    implied = float(np.interp(float(target[1]), draws[::-1], TOTALS[::-1]))
    total = base_total + total_weight * (implied - base_total)
    total = float(min(TOTALS[-1], max(TOTALS[0], total)))
    split = float(np.interp(total, TOTALS, splits))
    return total * split, total * (1 - split)


def goal_probabilities(
    odds: Mapping[str, float] | None,
    base_total: float = BASE_TOTAL,
    total_weight: float = TOTAL_WEIGHT,
    rho: float = RHO,
) -> dict[str, float] | None:
    """Probabilitățile tuturor piețelor de goluri din cotele 1/X/2, sau None fără cote valide."""
    if not odds:
        return None
    fair = demargin([odds.get("1"), odds.get("X"), odds.get("2")])
    if fair is None:
        return None
    target = np.array(fair)
    home_rate, away_rate = implied_rates(target, base_total, total_weight, rho)
    matrix = reweight(score_matrix(home_rate, away_rate, rho, 0.0, SIZE), target)
    return mk.probabilities("goals", matrix, GOAL_KEYS)


@lru_cache(maxsize=4)
def _load_rule(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_rule(path: str | Path = RULE_FILE) -> dict:
    """Lista înghețată {"select": [...], "select_high": [...]}; goală dacă fișierul lipsește."""
    if not Path(path).exists():
        return {"select": [], "select_high": []}
    return _load_rule(str(path))


def choose(probs: Mapping[str, float], threshold: float, allow, max_p: float = MAX_P) -> set[str]:
    """O cheie pe grup: cea permisă din banda prag ≤ p ≤ max_p cu p minim (cota cea mai lungă)."""
    best: dict[str, tuple[float, str]] = {}
    for key, p in probs.items():
        market = mk.CATALOGUE[key]
        if key not in allow or not market.selectable or not threshold - 1e-12 <= p <= max_p:
            continue
        if market.group not in best or p < best[market.group][0]:
            best[market.group] = (p, key)
    return {key for _, key in best.values()}


def predict(odds: Mapping[str, float] | None, rule_path: str | Path = RULE_FILE) -> dict | None:
    """Piețele afișabile (formatul ``FootballPredictor.predict``) sau None fără cote 1/X/2."""
    probs = goal_probabilities(odds)
    if probs is None:
        return None
    rule = load_rule(rule_path)
    allow, allow_high = frozenset(rule["select"]), frozenset(rule["select_high"])
    picks = choose(probs, THRESHOLD, allow)
    picks_high = choose(probs, THRESHOLD_HIGH, allow_high)
    markets = []
    for key, p in probs.items():
        market = mk.CATALOGUE[key]
        price = odds.get(key)
        markets.append(
            {
                "key": key,
                "label": market.label,
                "group": market.group,
                "group_label": mk.GROUPS[market.group],
                "stat": market.stat,
                "probability": round(p, 6),
                "fair_odds": round(1.0 / p, 3) if p > 1e-9 else None,
                "odds": round(float(price), 3) if price else None,
                "selectable": market.selectable and (key in allow or key in allow_high),
                "decision": SELECT if key in picks else NO_BET,
                "decision_high": SELECT if key in picks_high else NO_BET,
            }
        )
    return {"markets": display_markets(markets), "version": VERSION}
