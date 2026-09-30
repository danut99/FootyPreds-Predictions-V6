"""Validarea estimării „din cote” (``market_model``) pe date football-data.co.uk.

    python -m fotbalPrediction.market_eval --totals     # alege ponderea totalului (2223, 2324)
    python -m fotbalPrediction.market_eval --derive     # derivă market_rule.json (2223, 2324)
    python -m fotbalPrediction.market_eval --report     # regula înghețată pe 2223, 2324
    python -m fotbalPrediction.market_eval --confirm    # o singură dată: 2425
    python -m fotbalPrediction.market_eval --locked-test  # o singură dată: 2526

Două seturi, ambele tratate ca „ligi necunoscute” (estimatorul nu vede niciun istoric):

- ``main``: cele 22 de ligi principale cu cotele 1/X/2 Bet365 de dinaintea închiderii (o singură
  casă, cu marjă: cel mai aproape de cotele din lista FlashScore);
- ``extra``: cele 16 ligi extra (România, Brazilia, SUA...) cu media cotelor de ÎNCHIDERE,
  singurele disponibile acolo. Modelul principal nu se antrenează pe aceste ligi.

Aceleași criterii ca ``tune_rule.py``: o cheie intră în listă pentru ținta T doar dacă, în
fiecare set, are cel puțin ``MIN_PICKS`` selecții, acuratețe ≥ T, supra-încredere ≤ 0.03 și
niciun sezon cu ≥ ``MIN_SEASON_PICKS`` selecții sub T − 0.02.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict

from . import data
from . import market_model as mm
from . import markets as mk
from .model import MAX_P, THRESHOLD, THRESHOLD_HIGH
from .tune_rule import MAX_OVERCONFIDENCE, MIN_PICKS, MIN_SEASON_PICKS, SEASON_SLACK, TARGETS

TUNE = data.TUNE_SEASONS
SETS = {
    "main": (tuple(data.MAIN_LEAGUES), ("b365", "avg")),
    "extra": (tuple(data.EXTRA_LEAGUES), ("avg_closing",)),
}
TOTAL_KEYS = ("over15", "over25", "over35", "btts", "home_over05", "away_over05", "under45")
GOAL_MARKETS = tuple(
    key
    for key in mk.keys_of("goals")
    if mk.CATALOGUE[key].selectable and mk.CATALOGUE[key].group != "cs"
)


def book_of(row, sources) -> dict | None:
    """Cotele 1/X/2 ale primei surse disponibile."""
    for source in sources:
        book = row.odds.get(source)
        if book and all(book.get(key) for key in ("1", "X", "2")):
            return book
    return None


def load(name: str, seasons) -> list:
    leagues, sources = SETS[name]
    rows = data.load_rows(leagues, min(seasons), max(seasons))
    output = []
    for row in rows:
        if row.season not in seasons:
            continue
        book = book_of(row, sources)
        if book is not None:
            output.append((row, book))
    return output


def real_price(key: str, book: dict) -> float | None:
    """Cota reală: 1/X/2 direct; șansa dublă ca pariu împărțit pe cele două rezultate."""
    if key in ("1", "X", "2"):
        return book[key]
    pair = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}.get(key)
    if pair is None:
        return None
    return 1.0 / (1.0 / book[pair[0]] + 1.0 / book[pair[1]])


# --------------------------------------------------------------------------- total weight


def totals(seasons=TUNE) -> dict:
    """Log-loss mediu pe piețele de goluri pentru câteva ponderi ale totalului dedus din egal."""
    weights = (0.0, 0.5, 0.75, 1.0)
    output = {}
    for name in SETS:
        loss = {w: defaultdict(lambda: [0.0, 0]) for w in weights}
        for row, book in load(name, seasons):
            for weight in weights:
                probs = mm.goal_probabilities(book, total_weight=weight)
                if probs is None:
                    continue
                for key in TOTAL_KEYS:
                    result = mk.outcome(key, row)
                    if result is None:
                        continue
                    p = min(1 - 1e-9, max(1e-9, probs[key]))
                    loss[weight][key][0] -= math.log(p if result == 1.0 else 1 - p)
                    loss[weight][key][1] += 1
        output[name] = {
            str(weight): {key: round(v[0] / v[1], 5) for key, v in keys.items() if v[1]}
            for weight, keys in loss.items()
        }
    return output


# --------------------------------------------------------------------------- derive


def band_stats(pairs, threshold: float) -> dict[str, dict]:
    """Per cheie: toate selecțiile decontate din banda prag ≤ p ≤ MAX_P (fără limita pe grup)."""
    stats: dict[str, dict] = {}
    for row, book in pairs:
        probs = mm.goal_probabilities(book)
        if probs is None:
            continue
        for key in GOAL_MARKETS:
            p = probs.get(key)
            if p is None or not threshold - 1e-12 <= p <= MAX_P:
                continue
            result = mk.outcome(key, row)
            if result is None:
                continue
            entry = stats.setdefault(key, {"n": 0, "won": 0.0, "p": 0.0, "seasons": {}})
            season = entry["seasons"].setdefault(row.season, [0, 0.0])
            entry["n"] += 1
            entry["won"] += result
            entry["p"] += p
            season[0] += 1
            season[1] += result
    return {
        key: {
            "n": entry["n"],
            "accuracy": entry["won"] / entry["n"],
            "mean_p": entry["p"] / entry["n"],
            "seasons": {
                season: {"n": n, "accuracy": won / n}
                for season, (n, won) in entry["seasons"].items()
            },
        }
        for key, entry in stats.items()
    }


def passes(stats: dict | None, target: float) -> bool:
    if not stats or stats["n"] < MIN_PICKS or stats["accuracy"] < target:
        return False
    if stats["mean_p"] - stats["accuracy"] > MAX_OVERCONFIDENCE:
        return False
    return all(
        s["n"] < MIN_SEASON_PICKS or s["accuracy"] >= target - SEASON_SLACK
        for s in stats["seasons"].values()
    )


def derive(seasons=TUNE) -> dict:
    sets = {name: load(name, seasons) for name in SETS}
    rule: dict = {"stats": {}}
    for flag, target in TARGETS.items():
        per_set = {name: band_stats(pairs, target) for name, pairs in sets.items()}
        keys = set().union(*(set(stats) for stats in per_set.values()))
        rule[flag] = sorted(
            (key for key in keys if all(passes(s.get(key), target) for s in per_set.values())),
            key=mk.KEYS.index,
        )
        rule["stats"][flag] = per_set
    return {
        "version": mm.VERSION,
        "derived_on": list(seasons),
        "sets": {
            name: {
                "leagues": len(SETS[name][0]),
                "odds": list(SETS[name][1]),
                "matches": len(sets[name]),
            }
            for name in SETS
        },
        "criteria": {
            "band": [TARGETS["select"], MAX_P],
            "band_high": [TARGETS["select_high"], MAX_P],
            "min_picks": MIN_PICKS,
            "min_season_picks": MIN_SEASON_PICKS,
            "season_slack": SEASON_SLACK,
            "max_overconfidence": MAX_OVERCONFIDENCE,
            "pick": "o cheie pe grup și meci: p minim din listă",
        },
        "total": {"base": mm.BASE_TOTAL, "weight": mm.TOTAL_WEIGHT, "rho": mm.RHO},
        "select": rule["select"],
        "select_high": rule["select_high"],
        "stats": rule["stats"],
    }


# --------------------------------------------------------------------------- report


def report(seasons, rule_path=mm.RULE_FILE) -> dict:
    """Regula înghețată (o selecție pe grup): acuratețe / acoperire / cotă corectă / ROI."""
    rule = mm.load_rule(rule_path)
    allow = {False: frozenset(rule["select"]), True: frozenset(rule["select_high"])}
    output = {}
    for name in SETS:
        pairs = load(name, seasons)
        groups: dict = {}
        loss = defaultdict(lambda: [0.0, 0])
        x12 = [0.0, 0, 0]
        for row, book in pairs:
            probs = mm.goal_probabilities(book)
            if probs is None:
                continue
            outcome = (
                0
                if row.home_goals > row.away_goals
                else (1 if row.home_goals == row.away_goals else 2)
            )
            triple = [probs["1"], probs["X"], probs["2"]]
            x12[0] -= math.log(max(1e-9, triple[outcome]))
            x12[1] += 1
            x12[2] += int(max(range(3), key=triple.__getitem__) == outcome)
            for key in TOTAL_KEYS:
                result = mk.outcome(key, row)
                if result is not None:
                    p = min(1 - 1e-9, max(1e-9, probs[key]))
                    loss[key][0] -= math.log(p if result == 1.0 else 1 - p)
                    loss[key][1] += 1
            for high, threshold in ((False, THRESHOLD), (True, THRESHOLD_HIGH)):
                any_pick = False
                for key in mm.choose(probs, threshold, allow[high]):
                    result = mk.outcome(key, row)
                    if result is None:
                        continue
                    any_pick = True
                    for group in (mk.CATALOGUE[key].group, "all"):
                        cell = groups.setdefault(
                            (group, high),
                            {
                                "n": 0,
                                "won": 0.0,
                                "fair": 0.0,
                                "matches": 0,
                                "bets": 0,
                                "profit": 0.0,
                            },
                        )
                        cell["n"] += 1
                        cell["won"] += result
                        cell["fair"] += 1.0 / probs[key]
                        if group != "all":
                            cell["matches"] += 1
                        price = real_price(key, book)
                        if price is not None:
                            cell["bets"] += 1
                            cell["profit"] += mk.profit(*mk.settle(key, row), price)
                if any_pick:
                    groups[("all", high)]["matches"] += 1
        matches = x12[1]
        output[name] = {
            "matches": matches,
            "x12": {"log_loss": round(x12[0] / matches, 4), "accuracy": round(x12[2] / matches, 4)},
            "log_loss": {key: round(v[0] / v[1], 4) for key, v in loss.items() if v[1]},
            "groups": {
                f"{group}{'_high' if high else ''}": {
                    "picks": cell["n"],
                    "accuracy": round(cell["won"] / cell["n"], 4),
                    "coverage": round(cell["matches"] / matches, 4),
                    "fair_odds": round(cell["fair"] / cell["n"], 3),
                    "roi": round(cell["profit"] / cell["bets"], 4) if cell["bets"] else None,
                    "bets": cell["bets"],
                }
                for (group, high), cell in sorted(groups.items())
            },
        }
    return output


def show(result: dict) -> None:
    for name, part in result.items():
        print(
            f"\n== {name}: {part['matches']} meciuri · 1X2 log-loss {part['x12']['log_loss']} "
            f"acuratețe {part['x12']['accuracy']:.1%}"
        )
        print("   log-loss:", ", ".join(f"{k} {v}" for k, v in part["log_loss"].items()))
        print(f"   {'grup':<16}{'selecții':>9}{'acurat.':>9}{'acop.':>8}{'cotă':>7}{'ROI':>9}")
        for group, cell in part["groups"].items():
            roi = "" if cell["roi"] is None else f"{cell['roi']:+.1%} @{cell['bets']}"
            print(
                f"   {group:<16}{cell['picks']:>9}{cell['accuracy']:>9.1%}"
                f"{cell['coverage']:>8.1%}{cell['fair_odds']:>7.2f}  {roi}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fotbalPrediction.market_eval")
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("totals", "derive", "report", "confirm", "locked-test"):
        mode.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.totals:
        result = totals()
        print(json.dumps(result, indent=1))
    elif args.derive:
        result = derive()
        text = json.dumps(result, ensure_ascii=False, indent=1) + "\n"
        mm.RULE_FILE.write_text(text, encoding="utf-8", newline="\n")
        mm._load_rule.cache_clear()
        print(f"select ({len(result['select'])}): {', '.join(result['select'])}")
        print(f"select_high ({len(result['select_high'])}): {', '.join(result['select_high'])}")
    else:
        if args.locked_test:
            print("ATENȚIE: test blocat 2526 — se rulează o singură dată pe versiune.")
            seasons = (data.LOCKED_TEST_SEASON,)
        elif args.confirm:
            seasons = (data.CONFIRM_SEASON,)
        else:
            seasons = TUNE
        result = report(seasons)
        show(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
