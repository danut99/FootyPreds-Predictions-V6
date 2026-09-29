"""Derivează lista de piețe de goluri/pauză selectabile NUMAI din sezoanele de tuning.

    python -m fotbalPrediction.tune_rule [--first-season 0506] [--out selection_rule.json]

Rulează modelul de producție cu ``rule="derive"`` (orice cheie de goluri din banda
``prag <= p <= 0.93`` care trece pragul de experiență) pe 2223 și 2324, fără cote și cu cotele
pre-închidere ``avg``, și păstrează o cheie pentru ținta T (0.80 / 0.85) numai dacă, în AMBELE
variante:

- are cel puțin ``MIN_PICKS`` selecții decontate cumulat pe cele două sezoane;
- acuratețea cumulată >= T;
- supra-încrederea (p mediu - acuratețe) <= ``MAX_OVERCONFIDENCE``;
- niciun sezon cu cel puțin ``MIN_SEASON_PICKS`` selecții nu coboară sub T - ``SEASON_SLACK``.

Aceleași criterii ca lista înghețată a candidatului corners_cards. Sezonul 2425 (confirmare),
2526 (test blocat) și 2627 (curent) nu se încarcă niciodată aici.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import benchmark as bench
from . import data
from . import markets as mk
from .model import MAX_P, RULE_FILE, VERSION, FootballModel

TUNE = data.TUNE_SEASONS
VARIANTS = (None, "avg")
MIN_PICKS = 30
MIN_SEASON_PICKS = 20
MAX_OVERCONFIDENCE = 0.03
SEASON_SLACK = 0.02
TARGETS = {"select": 0.80, "select_high": 0.85}


def key_stats(records, flag: str) -> dict[str, dict]:
    """Per key: selections (flag) that settled, pooled and per season."""
    arr = records.arrays()
    chosen = arr["sel"] if flag == "select" else arr["sel_high"]
    settled = chosen & (arr["y"] >= 0)
    output = {}
    for index in np.unique(arr["key"][settled]):
        key = records.keys[index]
        mask = settled & (arr["key"] == index)
        y, p = arr["y"][mask], arr["p"][mask]
        seasons = {}
        for s_index, season in enumerate(arr["seasons"]):
            part = mask & (arr["season"] == s_index)
            n = int(part.sum())
            seasons[season] = {
                "n": n,
                "accuracy": float(arr["y"][part].mean()) if n else None,
            }
        output[key] = {
            "n": int(mask.sum()),
            "accuracy": float(y.mean()),
            "mean_p": float(p.mean()),
            "seasons": seasons,
        }
    return output


def passes(stats: dict | None, target: float) -> bool:
    if not stats or stats["n"] < MIN_PICKS or stats["accuracy"] < target:
        return False
    if stats["mean_p"] - stats["accuracy"] > MAX_OVERCONFIDENCE:
        return False
    return all(
        s["n"] < MIN_SEASON_PICKS or s["accuracy"] >= target - SEASON_SLACK
        for s in stats["seasons"].values()
    )


def derive(runs: dict[str, object]) -> dict:
    """{"select": [...], "select_high": [...], "stats": {...}} from records of every variant."""
    rule: dict = {"stats": {}}
    for flag, target in TARGETS.items():
        per_variant = {name: key_stats(records, flag) for name, records in runs.items()}
        keys = set().union(*(set(stats) for stats in per_variant.values()))
        allowed = sorted(
            key
            for key in keys
            if all(passes(stats.get(key), target) for stats in per_variant.values())
        )
        rule[flag] = sorted(allowed, key=mk.KEYS.index)
        rule["stats"][flag] = {
            name: {key: stats[key] for key in sorted(stats, key=mk.KEYS.index)}
            for name, stats in per_variant.items()
        }
    return rule


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fotbalPrediction.tune_rule")
    parser.add_argument("--first-season", default=data.FIRST_SEASON)
    parser.add_argument("--out", type=Path, default=RULE_FILE)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    rows = data.load_rows(tuple(data.MAIN_LEAGUES), args.first_season, TUNE[-1])
    runs = {}
    for odds in VARIANTS:
        name = odds or "none"
        result = bench.run_benchmark(
            lambda: FootballModel(rule="derive", with_counts=False),
            TUNE,
            rows=rows,
            first_season=args.first_season,
            markets="goals,ht_goals",
            odds=odds,
            return_records=True,
        )
        runs[name] = result["records"]
        print(f"{name}: {result['meta']['matches_evaluated']} meciuri", flush=True)
    rule = derive(runs)
    payload = {
        "version": VERSION,
        "derived_on": list(TUNE),
        "variants": [odds or "none" for odds in VARIANTS],
        "criteria": {
            "band": [TARGETS["select"], MAX_P],
            "band_high": [TARGETS["select_high"], MAX_P],
            "min_picks": MIN_PICKS,
            "min_season_picks": MIN_SEASON_PICKS,
            "season_slack": SEASON_SLACK,
            "max_overconfidence": MAX_OVERCONFIDENCE,
            "pick": "o cheie pe grup și meci: p minim din listă",
        },
        "select": rule["select"],
        "select_high": rule["select_high"],
        "stats": rule["stats"],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"select ({len(rule['select'])}): {', '.join(rule['select'])}")
    print(f"select_high ({len(rule['select_high'])}): {', '.join(rule['select_high'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
