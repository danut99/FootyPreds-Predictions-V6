"""Reproducible, score-blind, chronological evaluation.

python -m footypreds.evaluation.run            # locked test season (run once per model version)
python -m footypreds.evaluation.run --validate # validation season, used for tuning
"""

import argparse
import hashlib
import itertools
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from footypreds.config import PACKAGE
from footypreds.domain import Match
from footypreds.engine import PARAMS, VERSION, HistoryIndex, analyze, fit_history, summarize
from footypreds.engine.analyzer import params_dict
from footypreds.engine.markets import FT_MARKETS, outcome
from footypreds.evaluation import baseline_v7
from footypreds.evaluation.dataset import DATA_DIR, load

PROTOCOL = Path(__file__).parent / "protocol.json"


@dataclass(frozen=True)
class BlindFixture:
    """Prediction input has NO result or live-stat attributes; odds only when allowed."""

    id: str
    kickoff: datetime
    league: str
    country: str
    home: str
    away: str
    home_id: str = ""
    away_id: str = ""
    quotes: tuple = field(default=())

    @property
    def odds(self):
        return MappingProxyType(dict(self.quotes))


def blind(match, odds=None):
    fields = {k: getattr(match, k) for k in BlindFixture.__dataclass_fields__ if k != "quotes"}
    return BlindFixture(**fields, quotes=tuple(sorted((odds or {}).items())))


def result_index(match):
    return (
        0
        if match.home_goals > match.away_goals
        else (2 if match.home_goals < match.away_goals else 1)
    )


def class_metrics(rows, field_name):
    rows = [r for r in rows if r.get(field_name) is not None]
    if not rows:
        return {"count": 0, "accuracy": None, "log_loss": None, "brier": None}
    probabilities = [(r[field_name], r["actual"]) for r in rows]
    return {
        "count": len(rows),
        "accuracy": sum(max(range(3), key=lambda i: p[i]) == y for p, y in probabilities)
        / len(rows),
        "log_loss": sum(-math.log(max(1e-15, p[y])) for p, y in probabilities) / len(rows),
        "brier": sum(
            sum((v - (i == y)) ** 2 for i, v in enumerate(p)) / 3 for p, y in probabilities
        )
        / len(rows),
    }


def binary_metrics(observations):
    """observations: (probability of YES, happened)."""
    if not observations:
        return {"count": 0, "accuracy": None, "log_loss": None, "brier": None}
    n = len(observations)
    return {
        "count": n,
        "accuracy": sum((p >= 0.5) == y for p, y in observations) / n,
        "log_loss": sum(-math.log(max(1e-15, p if y else 1 - p)) for p, y in observations) / n,
        "brier": sum((p - y) ** 2 for p, y in observations) / n,
    }


def calibration(observations):
    bins = []
    for index in range(10):
        group = [(p, y) for p, y in observations if min(9, int(p * 10)) == index]
        if group:
            bins.append(
                {
                    "range": f"{index * 10}–{(index + 1) * 10}%",
                    "count": len(group),
                    "predicted": sum(p for p, _ in group) / len(group),
                    "actual": sum(y for _, y in group) / len(group),
                }
            )
    count = len(observations)
    ece = (
        sum(abs(b["predicted"] - b["actual"]) * b["count"] for b in bins) / count if count else None
    )
    return {"bins": bins, "ece": ece}


def cluster_interval(rows, key, iterations=1000, seed=7):
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        if row[key]:
            groups[row["date"]][0] += row[key]["won"]
            groups[row["date"]][1] += 1
    values = list(groups.values())
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample = rng.choices(values, k=len(values))
        estimates.append(sum(w for w, _ in sample) / sum(n for _, n in sample))
    estimates.sort()
    return [
        estimates[int(0.025 * iterations)],
        estimates[min(iterations - 1, int(0.975 * iterations))],
    ]


def selected_metrics(rows, key):
    selections = [
        {"prediction": {"selection": r[key]}, "result": {"won": r[key]["won"]}}
        for r in rows
        if r[key]
    ]
    return summarize(selections, len(rows))


# Prices the market variant may see: 1X2 (blend) and over/under 2.5 (goals pool), the two
# markets the football-data.co.uk averages carry. Never the score.
MARKET_KEYS = ("1", "X", "2", "over25", "under25")


def market_odds(reference):
    return {k: v for k, v in reference.items() if k in MARKET_KEYS}


def normalized(reference, keys):
    if not all(reference.get(k) for k in keys):
        return None
    inverse = [1 / reference[k] for k in keys]
    return [p / sum(inverse) for p in inverse]


def selection_of(prediction, match):
    pick = prediction["selection"]
    if not pick:
        return None
    return {
        "key": pick["key"],
        "probability": pick["probability"],
        "won": outcome(pick["key"], match.home_goals, match.away_goals),
    }


def split(records, protocol, holdout):
    """History = every season before the holdout; later seasons are never loaded."""
    order = protocol["history_seasons"] + protocol["validation_seasons"] + [protocol["test_season"]]
    allowed = set(order[: order.index(holdout) + 1])
    parsed = [
        (Match.model_validate(r["match"]), r)
        for r in records
        if r["season"] in allowed and r["league_code"] in protocol["leagues"]
    ]
    if len({m.id for m, _ in parsed}) != len(parsed):
        raise ValueError("Duplicate match IDs; evaluation refused.")
    identities = {(m.kickoff.date(), m.league, m.home, m.away) for m, _ in parsed}
    if len(identities) != len(parsed):
        raise ValueError("Duplicate fixtures with different IDs; evaluation refused.")
    test_dates = [m.kickoff for m, r in parsed if r["season"] == holdout]
    if not test_dates:
        raise ValueError("The holdout is empty.")
    history_dates = [m.kickoff for m, r in parsed if r["season"] != holdout]
    if history_dates and max(history_dates) >= min(test_dates):
        raise ValueError("History and test seasons overlap; evaluation refused.")
    parsed.sort(key=lambda item: (item[0].kickoff, item[0].id))
    return parsed


def evaluate(
    records, protocol, *, holdout=None, params=PARAMS, baseline=True, market=True, predictor=analyze
):
    holdout = holdout or protocol["test_season"]
    parsed = split(records, protocol, holdout)
    threshold = protocol["selection_threshold"]
    history, ratings, rows = defaultdict(HistoryIndex), {}, []
    for day, group in itertools.groupby(parsed, key=lambda item: item[0].kickoff.date()):
        batch = list(group)
        start = datetime.combine(day, time.min, timezone.utc)
        for league in {m.league for m, raw in batch if raw["season"] == holdout}:
            if any(m.kickoff.date() >= day for m in history[league].rows):
                raise AssertionError("Future or same-day result leaked into history.")
            # Same horizon as the analyzer: nothing within cutoff_hours of the day's first kickoff.
            horizon = start - timedelta(hours=params.cutoff_hours)
            past = history[league].before(horizon)
            ratings[league] = fit_history(past, start, params, init=ratings.get(league))
        for match, raw in batch:
            if raw["season"] != holdout:
                continue
            index = history[match.league]
            reference = raw.get("reference_odds", {})
            model = predictor(
                blind(match), index, threshold, params=params, ratings=ratings[match.league]
            )
            with_market = (
                predictor(
                    blind(match, market_odds(reference)),
                    index,
                    threshold,
                    params=params,
                    ratings=ratings[match.league],
                )
                if market
                else model
            )
            legacy = (
                baseline_v7.predict(blind(match), tuple(index.rows), threshold)
                if baseline
                else None
            )
            counts = [1, 1, 1]
            for previous in index.rows:
                counts[result_index(previous)] += 1

            def probs(prediction):
                return {m["key"]: m["probability"] for m in prediction["markets"]}

            model_p, market_p = probs(model), probs(with_market)
            legacy_p = probs(legacy) if legacy else None
            rows.append(
                {
                    "id": match.id,
                    "date": day.isoformat(),
                    "league": match.league,
                    "home": match.home,
                    "away": match.away,
                    "actual": result_index(match),
                    "home_goals": match.home_goals,
                    "away_goals": match.away_goals,
                    "model": [model_p[k] for k in ("1", "X", "2")],
                    "model_market": [market_p[k] for k in ("1", "X", "2")],
                    "v7": [legacy_p[k] for k in ("1", "X", "2")] if legacy_p else None,
                    "frequency": [c / sum(counts) for c in counts],
                    "bookmaker": normalized(reference, ("1", "X", "2")),
                    "bookmaker_over25": (normalized(reference, ("over25", "under25")) or [None])[0],
                    "markets": model_p,
                    "markets_market": market_p,
                    # Goals-calibration internals (raw and calibrated P(over 2.5), market pool).
                    "totals": model.get("components", {}).get("totals"),
                    "totals_market": with_market.get("components", {}).get("totals"),
                    "v7_markets": legacy_p,
                    "sample": model["sample"],
                    "grade": model["grade"],
                    "selection": selection_of(model, match),
                    "selection_market": selection_of(with_market, match),
                    "selection_v7": selection_of(legacy, match) if legacy else None,
                    "history_count": len(index.rows),
                }
            )
        # Results become available only AFTER all predictions for this date are made.
        for match, _ in batch:
            history[match.league].extend([match])
    return summarize_rows(rows, protocol, holdout, params), rows


def goal_observations(rows, field_name, key):
    return [
        (r[field_name][key], outcome(key, r["home_goals"], r["away_goals"]))
        for r in rows
        if r.get(field_name)
    ]


CALIBRATION_KEYS = ("over15", "over25", "over35", "btts")


def goals_calibration(rows):
    """Reliability of the goals markets per variant, and the bookmaker on over/under 2.5."""
    from footypreds.evaluation.calibration import reliability

    output = {
        name: {
            key: reliability(goal_observations(rows, field_name, key)) for key in CALIBRATION_KEYS
        }
        for name, field_name in (("model", "markets"), ("model_market", "markets_market"))
    }
    priced = [r for r in rows if r.get("bookmaker_over25") is not None]
    output["over25_same_rows"] = {
        "matches": len(priced),
        "model": reliability(goal_observations(priced, "markets", "over25")),
        "model_market": reliability(goal_observations(priced, "markets_market", "over25")),
        "bookmaker": reliability(
            [(r["bookmaker_over25"], r["home_goals"] + r["away_goals"] > 2) for r in priced]
        ),
    }
    return output


def summarize_rows(rows, protocol, holdout, params):
    threshold = protocol["selection_threshold"]
    odds_rows = [r for r in rows if r["bookmaker"] is not None]
    ou_rows = [r for r in rows if r["bookmaker_over25"] is not None]
    rich = [
        r
        for r in rows
        if min(r["sample"]["home"], r["sample"]["away"]) >= protocol["rich_history_minimum"]
    ]
    per_market = {}
    for key, (label, group, _) in FT_MARKETS.items():
        obs = goal_observations(rows, "markets_market", key)
        chosen = [(p, y) for p, y in obs if p >= threshold]
        per_market[key] = {
            "label": label,
            "group": group,
            "count": len(obs),
            "brier": sum((p - y) ** 2 for p, y in obs) / len(obs),
            "over_threshold": len(chosen),
            "over_threshold_accuracy": sum(y for _, y in chosen) / len(chosen) if chosen else None,
            "calibration": calibration(obs),
        }
    variants = {"model": "markets", "model_market": "markets_market", "v7": "v7_markets"}
    goals = {
        name: {
            "over25": binary_metrics(goal_observations(rows, field_name, "over25")),
            "btts": binary_metrics(goal_observations(rows, field_name, "btts")),
        }
        for name, field_name in variants.items()
    }
    bootstrap = {k: protocol["bootstrap"][k] for k in ("iterations", "seed")}
    return {
        "model": VERSION,
        "params": params_dict(params),
        "holdout_season": holdout,
        "protocol": protocol,
        "holdout_matches": len(rows),
        "test_start": min(r["date"] for r in rows),
        "test_end": max(r["date"] for r in rows),
        "one_x_two": {
            "model": class_metrics(rows, "model"),
            "model_market": class_metrics(rows, "model_market"),
            "v7": class_metrics(rows, "v7"),
            "league_frequency": class_metrics(rows, "frequency"),
            "always_home_accuracy": sum(r["actual"] == 0 for r in rows) / len(rows),
        },
        "same_odds_subset": {
            "model": class_metrics(odds_rows, "model"),
            "model_market": class_metrics(odds_rows, "model_market"),
            "v7": class_metrics(odds_rows, "v7"),
            "bookmaker": class_metrics(odds_rows, "bookmaker"),
        },
        "goals": goals,
        "over25_vs_bookmaker": {
            "matches": len(ou_rows),
            "model": binary_metrics(goal_observations(ou_rows, "markets", "over25")),
            "model_market": binary_metrics(goal_observations(ou_rows, "markets_market", "over25")),
            "bookmaker": binary_metrics(
                [(r["bookmaker_over25"], r["home_goals"] + r["away_goals"] > 2) for r in ou_rows]
            ),
        },
        "selected": {
            "model": selected_metrics(rows, "selection"),
            "model_market": selected_metrics(rows, "selection_market"),
            "v7": selected_metrics(rows, "selection_v7"),
        },
        "selected_cluster_interval95": {
            "model_market": cluster_interval(rows, "selection_market", **bootstrap),
            "v7": cluster_interval(rows, "selection_v7", **bootstrap),
        },
        "rich_history": {
            "matches": len(rich),
            "minimum_per_team": protocol["rich_history_minimum"],
            "one_x_two": class_metrics(rich, "model_market"),
            "selected": selected_metrics(rich, "selection_market"),
        },
        "per_league": {
            league: {
                "one_x_two": class_metrics(
                    [r for r in rows if r["league"] == league], "model_market"
                ),
                "v7": class_metrics([r for r in rows if r["league"] == league], "v7"),
                "selected": selected_metrics(
                    [r for r in rows if r["league"] == league], "selection_market"
                ),
            }
            for league in sorted({r["league"] for r in rows})
        },
        "per_market": per_market,
        "goals_calibration": goals_calibration(rows),
        "audit": {
            "same_day_excluded": True,
            "target_score_not_in_prediction_input": True,
            "later_seasons_not_loaded": True,
            "odds_only_in_market_variant": True,
            "duplicates": 0,
            "tuned_on": protocol["validation_seasons"],
        },
    }


def report_markdown(report):
    def pct(value):
        return "N/A" if value is None else f"{value:.1%}"

    def num(value):
        return "N/A" if value is None else f"{value:.4f}"

    names = {
        "model": "V8 model (fără cote)",
        "model_market": "V8 model + piață (1X2, peste/sub 2.5)",
        "v7": "V7 (vechi)",
        "league_frequency": "Frecvența ligii",
        "bookmaker": "Case de pariuri",
    }
    lines = [
        f"# Benchmark {report['model']} — sezonul {report['holdout_season']}",
        "",
        f"Generat: {report['generated_at']}. Meciuri evaluate: {report['holdout_matches']} "
        f"({report['test_start']} — {report['test_end']}).",
        "",
        "Sursă: [Football-Data](https://www.football-data.co.uk/data.php), 5 ligi de top. "
        "Scorul meciului evaluat nu există în obiectul transmis modelului; rezultatele din "
        "aceeași zi sunt invizibile. Parametrii au fost aleși numai pe sezonul de validare "
        f"{', '.join(report['protocol']['validation_seasons'])}.",
        "",
    ]
    calibration_protocol = report["protocol"].get("calibration")
    if calibration_protocol:
        lines += [
            "Calibrarea golurilor (hartă Platt, pondere peste/sub 2.5, plafonul de valoare al "
            "recomandărilor) a fost potrivită numai pe predicțiile sezonului de validare "
            f"{', '.join(calibration_protocol['seasons'])} din "
            f"{len(calibration_protocol['leagues'])} ligi (`python -m footypreds.evaluation.tune "
            "--totals`). Problema totalurilor (8.0 prea încrezător la peste/sub 2.5) a fost "
            "observată întâi pe sezonul de test 2025-26: rezultatul de mai jos confirmă o "
            "corecție motivată de test, cu parametri potriviți numai pe validare.",
            "",
            "## 1X2 — toate meciurile",
            "",
            "| Model | Meciuri | Acuratețe | Log loss ↓ | Brier/3 ↓ |",
            "|---|---:|---:|---:|---:|",
        ]
    for name, metric in report["one_x_two"].items():
        if isinstance(metric, dict) and metric["count"]:
            lines.append(
                f"| {names.get(name, name)} | {metric['count']} | {pct(metric['accuracy'])} | "
                f"{num(metric['log_loss'])} | {num(metric['brier'])} |"
            )
    lines += ["", "Același subset, cu cotele caselor:", "", "| Model | Acuratețe | Log loss ↓ |"]
    lines.append("|---|---:|---:|")
    for name, metric in report["same_odds_subset"].items():
        if metric["count"]:
            lines.append(
                f"| {names.get(name, name)} | {pct(metric['accuracy'])} | "
                f"{num(metric['log_loss'])} |"
            )
    lines += [
        "",
        "## Goluri",
        "",
        "| Model | Peste 2.5: acuratețe | Peste 2.5: log loss ↓ | GG: acuratețe | GG: log loss ↓ |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metric in report["goals"].items():
        if metric["over25"]["count"]:
            lines.append(
                f"| {names.get(name, name)} | {pct(metric['over25']['accuracy'])} | "
                f"{num(metric['over25']['log_loss'])} | {pct(metric['btts']['accuracy'])} | "
                f"{num(metric['btts']['log_loss'])} |"
            )
    ou = report["over25_vs_bookmaker"]
    if ou["matches"]:
        lines += [
            "",
            f"Peste/sub 2.5 față de case ({ou['matches']} meciuri): "
            f"model {pct(ou['model']['accuracy'])} / log loss {num(ou['model']['log_loss'])}; "
            "model+piață "
            f"{pct(ou['model_market']['accuracy'])} / {num(ou['model_market']['log_loss'])}; "
            f"case {pct(ou['bookmaker']['accuracy'])} / {num(ou['bookmaker']['log_loss'])}.",
        ]
    calibration_table = report.get("goals_calibration")
    if calibration_table:
        lines += [
            "",
            "## Calibrarea piețelor de goluri",
            "",
            "Fără cote, P(peste 2.5) trece prin harta Platt potrivită pe sezonul de validare; cu "
            "cotă peste/sub 2.5, probabilitatea este cea a pieței fără marjă (pondere validată "
            f"{report['params'].get('totals_market_weight', 0):g}). Ambele rate de goluri se "
            "rescalează ca toată matricea de scoruri să fie de acord; 1X2 nu se schimbă.",
            "",
            "| Piață | Variantă | Prezis (medie) | Observat | Log loss ↓ | ECE ↓ |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for key in CALIBRATION_KEYS:
            for name in ("model", "model_market"):
                metric = calibration_table[name][key]
                if metric["count"]:
                    lines.append(
                        f"| {FT_MARKETS[key][0]} | {names[name]} | {pct(metric['mean_predicted'])} "
                        f"| {pct(metric['mean_actual'])} | {num(metric['log_loss'])} | "
                        f"{num(metric['ece'])} |"
                    )
        same = calibration_table["over25_same_rows"]
        if same["matches"]:
            lines += [
                "",
                f"Peste 2.5 pe benzi de probabilitate ({same['matches']} meciuri cu cote "
                "peste/sub; prezis / observat):",
                "",
                "| Bandă | V8 fără cote | V8 + piață | Case de pariuri |",
                "|---|---|---|---|",
            ]
            bands = {}
            for name in ("model", "model_market", "bookmaker"):
                for band in same[name]["bands"]:
                    bands.setdefault(band["range"], {})[name] = band
            for label in sorted(bands):
                cells = []
                for name in ("model", "model_market", "bookmaker"):
                    band = bands[label].get(name)
                    cells.append(
                        f"{band['count']}: {pct(band['predicted'])} / {pct(band['actual'])}"
                        if band
                        else "—"
                    )
                lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"## Selecții la prag fix {report['protocol']['selection_threshold']:.0%}",
        "",
        "| Model | Selecții | Reușite | Acuratețe | Acoperire | Wilson 95% |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, metric in report["selected"].items():
        interval = metric["interval95"]
        lines.append(
            f"| {names.get(name, name)} | {metric['settled']} | {metric['wins']} | "
            f"{pct(metric['accuracy'])} | {pct(metric['coverage'])} | "
            f"{'N/A' if not interval else f'{interval[0]:.1%}–{interval[1]:.1%}'} |"
        )
    lines += [
        "",
        "Selecțiile provin din piețe diferite (șansă dublă, goluri, GG); procentul nu este "
        "acuratețea 1X2 și nu este rezultatul unor bilete combinate.",
        "",
        "## Pe ligi (V8 model + piață)",
        "",
        "| Ligă | Meciuri | 1X2 V8 | 1X2 V7 | Selecții | Acuratețe selecții |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for league, metric in report["per_league"].items():
        p, s, old = metric["one_x_two"], metric["selected"], metric["v7"]
        lines.append(
            f"| {league} | {p['count']} | {pct(p['accuracy'])} | {pct(old['accuracy'])} | "
            f"{s['selected']} | {pct(s['accuracy'])} |"
        )
    lines += [
        "",
        "## Parametri",
        "",
        "```json",
        json.dumps(report["params"], indent=2),
        "```",
        "",
        "## Limite",
        "",
        "- 1X2 nu este calibrat separat (amestec cu piața); piețele de goluri sunt calibrate pe "
        "sezonul de validare (docs/MODEL.md). Tabelele complete sunt în `report.json`.",
        "- Cotele de referință sunt medii de piață; momentul exact al capturării nu este garantat.",
        "- Bootstrap-ul pe zile tratează corelația din aceeași zi, nu toate dependențele.",
        "- Nicio evaluare istorică nu garantează rezultate viitoare.",
        f"- SHA256 dataset: `{report['dataset_sha256']}`; protocol: `{report['protocol_sha256']}`.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true", help="evaluate the validation season")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    records, manifest = load()
    holdout = protocol["validation_seasons"][-1] if args.validate else protocol["test_season"]
    print(f"Evaluating season {holdout}; no network calls.", flush=True)
    report, rows = evaluate(records, protocol, holdout=holdout)
    engine = PACKAGE / "engine"
    report.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dataset_sha256=manifest["dataset_sha256"],
        protocol_sha256=hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        engine_sha256=hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(engine.glob("*.py")))
        ).hexdigest(),
    )
    suffix = "-validation" if args.validate else ""
    (DATA_DIR / f"report{suffix}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (DATA_DIR / f"predictions{suffix}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    if not args.validate:
        (PACKAGE / "docs/BENCHMARK.md").write_text(report_markdown(report), encoding="utf-8")
    keys = ("holdout_matches", "one_x_two", "goals", "selected")
    print(json.dumps({k: report[k] for k in keys}, indent=2))


if __name__ == "__main__":
    main()
