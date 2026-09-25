"""Reproducible, score-blind, chronological evaluation; never tunes on the holdout."""

import hashlib
import itertools
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from app.config import ROOT
from app.domain import Match
from app.model import LABELS, VERSION, outcome, predict, summarize
from evaluation.dataset import DATA_DIR, load


@dataclass(frozen=True)
class BlindFixture:
    """Prediction input has NO result, live-stat, or bookmaker-price attributes."""

    id: str
    kickoff: datetime
    league: str
    country: str
    home: str
    away: str
    home_id: str = ""
    away_id: str = ""

    @property
    def odds(self):
        return MappingProxyType({})


def blind(match):
    return BlindFixture(**{k: getattr(match, k) for k in BlindFixture.__dataclass_fields__})


def result_index(match):
    return (
        0
        if match.home_goals > match.away_goals
        else (2 if match.home_goals < match.away_goals else 1)
    )


def class_metrics(rows, field):
    if not rows:
        return {"count": 0, "accuracy": None, "log_loss": None, "brier": None}
    probabilities = [(r[field], r["actual"]) for r in rows]
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


def cluster_interval(rows, iterations=1000, seed=7):
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        if row["selection"]:
            groups[row["date"]][0] += row["selection"]["won"]
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


def selected_metrics(rows):
    selections = [
        {"prediction": {"selection": r["selection"]}, "result": {"won": r["selection"]["won"]}}
        for r in rows
        if r["selection"]
    ]
    return summarize(selections, len(rows))


def evaluate(records, protocol, predictor=predict):
    selected_seasons = set(protocol["history_seasons"] + [protocol["test_season"]])
    parsed = [
        (Match.model_validate(r["match"]), r)
        for r in records
        if r["season"] in selected_seasons and r["league_code"] in protocol["leagues"]
    ]
    if len({m.id for m, _ in parsed}) != len(parsed):
        raise ValueError("Duplicate match IDs; evaluation refused.")
    identities = {(m.kickoff.date(), m.league, m.home, m.away) for m, _ in parsed}
    if len(identities) != len(parsed):
        raise ValueError("Duplicate fixtures with different IDs; evaluation refused.")
    test_dates = [m.kickoff for m, r in parsed if r["season"] == protocol["test_season"]]
    if not test_dates:
        raise ValueError("The holdout is empty.")
    history_dates = [m.kickoff for m, r in parsed if r["season"] != protocol["test_season"]]
    if history_dates and max(history_dates) >= min(test_dates):
        raise ValueError("History and test seasons overlap; evaluation refused.")
    parsed.sort(key=lambda item: (item[0].kickoff, item[0].id))
    history, rows = defaultdict(list), []
    for day, group in itertools.groupby(parsed, key=lambda item: item[0].kickoff.date()):
        batch = list(group)
        for match, raw in batch:
            if raw["season"] != protocol["test_season"]:
                continue
            past = tuple(history[match.league])
            if any(m.kickoff.date() >= day for m in past):
                raise AssertionError("Future or same-day result leaked into history.")
            prediction = predictor(blind(match), past, protocol["selection_threshold"])
            markets = {m["key"]: m["probability"] for m in prediction["markets"]}
            actual = result_index(match)
            counts = [1, 1, 1]
            for previous in past:
                counts[result_index(previous)] += 1
            reference = raw.get("reference_odds", {})
            bookmaker = None
            if set(reference) == {"1", "X", "2"}:
                inverse = [1 / reference[k] for k in ("1", "X", "2")]
                bookmaker = [p / sum(inverse) for p in inverse]
            pick = prediction["selection"]
            selection = (
                {
                    "key": pick["key"],
                    "probability": pick["probability"],
                    "won": outcome(pick["key"], match.home_goals, match.away_goals),
                }
                if pick
                else None
            )
            rows.append(
                {
                    "id": match.id,
                    "date": day.isoformat(),
                    "league": match.league,
                    "home": match.home,
                    "away": match.away,
                    "actual": actual,
                    "home_goals": match.home_goals,
                    "away_goals": match.away_goals,
                    "poisson": [markets[k] for k in ("1", "X", "2")],
                    "frequency": [c / sum(counts) for c in counts],
                    "bookmaker": bookmaker,
                    "markets": markets,
                    "sample": prediction["sample"],
                    "selection": selection,
                    "history_count": len(past),
                    "latest_history": max((m.kickoff.isoformat() for m in past), default=None),
                }
            )
        # Results become available only AFTER all predictions for this date are made.
        for match, _ in batch:
            history[match.league].append(match)
    odds_rows = [r for r in rows if r["bookmaker"] is not None]
    rich = [
        r
        for r in rows
        if min(r["sample"]["home"], r["sample"]["away"]) >= protocol["rich_history_minimum"]
    ]
    per_market = {}
    for key, label in LABELS.items():
        obs = [(r["markets"][key], outcome(key, r["home_goals"], r["away_goals"])) for r in rows]
        chosen = [(p, y) for p, y in obs if p >= protocol["selection_threshold"]]
        per_market[key] = {
            "label": label,
            "count": len(obs),
            "brier": sum((p - y) ** 2 for p, y in obs) / len(obs),
            "over_threshold": len(chosen),
            "over_threshold_accuracy": sum(y for _, y in chosen) / len(chosen) if chosen else None,
            "calibration": calibration(obs),
        }
    summary = {
        "model": VERSION,
        "protocol": protocol,
        "holdout_matches": len(rows),
        "history_matches": len(parsed) - len(rows),
        "dataset_matches": len(parsed),
        "test_start": min(r["date"] for r in rows),
        "test_end": max(r["date"] for r in rows),
        "one_x_two": {
            "poisson": class_metrics(rows, "poisson"),
            "league_frequency": class_metrics(rows, "frequency"),
            "always_home_accuracy": sum(r["actual"] == 0 for r in rows) / len(rows),
        },
        "same_odds_subset": {
            "poisson": class_metrics(odds_rows, "poisson"),
            "bookmaker": class_metrics(odds_rows, "bookmaker"),
        },
        "selected": selected_metrics(rows),
        "selected_cluster_interval95": cluster_interval(
            rows, **{k: protocol["bootstrap"][k] for k in ("iterations", "seed")}
        ),
        "rich_history": {
            "matches": len(rich),
            "minimum_per_team": protocol["rich_history_minimum"],
            "one_x_two": class_metrics(rich, "poisson"),
            "selected": selected_metrics(rich),
        },
        "per_league": {
            league: {
                "one_x_two": class_metrics([r for r in rows if r["league"] == league], "poisson"),
                "selected": selected_metrics([r for r in rows if r["league"] == league]),
            }
            for league in sorted({r["league"] for r in rows})
        },
        "per_market": per_market,
        "audit": {
            "same_day_excluded": True,
            "target_score_not_in_prediction_input": True,
            "odds_not_in_model_input": True,
            "duplicates": 0,
            "threshold_tuned_on_test": False,
        },
    }
    return summary, rows


def report_markdown(report):
    def pct(value):
        return "N/A" if value is None else f"{value:.1%}"

    selected = report["selected"]
    lines = [
        "# V7 — evaluare strictă pe date reale",
        "",
        f"Model: `{report['model']}`. Generat: {report['generated_at']}.",
        "",
        f"Dataset: {report['dataset_matches']} meciuri; istoric inițial: "
        f"{report['history_matches']}; holdout: {report['holdout_matches']}.",
        f"Perioadă de test: {report['test_start']} — {report['test_end']}.",
        "",
        "Sursă: [Football-Data](https://www.football-data.co.uk/data.php). "
        "Scorurile testate nu sunt prezente în obiectul transmis modelului; "
        "toate rezultatele din aceeași zi sunt excluse din istoric.",
        "",
        "## Predicții 1X2 — toate meciurile din test",
        "",
        "| Model | Meciuri | Acuratețe | Log loss ↓ | Brier/3 ↓ |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metric in report["one_x_two"].items():
        if isinstance(metric, dict):
            lines.append(
                f"| {name} | {metric['count']} | {pct(metric['accuracy'])} | "
                f"{metric['log_loss']:.4f} | {metric['brier']:.4f} |"
            )
    lines += [
        "",
        "Referință suplimentară pe același subset cu cote:",
        "",
        "| Model | Meciuri | Acuratețe | Log loss ↓ |",
        "|---|---:|---:|---:|",
    ]
    for name, metric in report["same_odds_subset"].items():
        if metric["count"]:
            lines.append(
                f"| {name} | {metric['count']} | {pct(metric['accuracy'])} | "
                f"{metric['log_loss']:.4f} |"
            )
    lines += [
        "",
        "## Selecții la prag fix de 85%",
        "",
        f"**{selected['wins']}/{selected['settled']} reușite "
        f"({pct(selected['accuracy'])}), acoperire {pct(selected['coverage'])}.**",
        "",
        f"Wilson 95%: {selected['interval95']}. "
        f"Bootstrap pe zile 95%: {report['selected_cluster_interval95']}.",
        "",
        "Selecțiile provin din piețe diferite; acest procent nu reprezintă acuratețea 1X2.",
        "",
        "## Pe ligi",
        "",
        "| Ligă | Meciuri | 1X2 | Selecții | Acuratețe selecții |",
        "|---|---:|---:|---:|---:|",
    ]
    for league, metric in report["per_league"].items():
        p, s = metric["one_x_two"], metric["selected"]
        lines.append(
            f"| {league} | {p['count']} | {pct(p['accuracy'])} | "
            f"{s['selected']} | {pct(s['accuracy'])} |"
        )
    rich = report["rich_history"]
    lines += [
        "",
        "## Istoric bogat",
        "",
        f"{rich['matches']} meciuri au minimum {rich['minimum_per_team']} observații "
        f"pentru fiecare echipă. 1X2: {pct(rich['one_x_two']['accuracy'])}; "
        f"selecții: {pct(rich['selected']['accuracy'])}, "
        f"acoperire {pct(rich['selected']['coverage'])}.",
        "",
        "## Limite și reproductibilitate",
        "",
        "- Model necalibrat; parametrii și pragul au rămas fixați înainte de test.",
        "- Nu ajusta modelul pe acest holdout după consultarea raportului. "
        "O versiune nouă necesită un test ulterior neatins.",
        "- Cotele sunt numai referință externă: ora lor exactă de capturare nu este garantată.",
        "- Bootstrap-ul pe zile tratează corelația din aceeași zi, nu toate dependențele "
        "dintre echipe de-a lungul sezonului.",
        "- Rezultatele pe piețe sunt diagnostice multiple, nu motive pentru alegerea "
        "retroactivă a celei mai bune piețe.",
        f"- SHA256 dataset: `{report['dataset_sha256']}`.",
        f"- SHA256 protocol: `{report['protocol_sha256']}`.",
        "- Predicțiile individuale și proveniența fișierelor sunt păstrate în `data/benchmark/`.",
    ]
    return "\n".join(lines) + "\n"


def main():
    protocol_path = Path(__file__).parent / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    records, manifest = load()
    print(f"Evaluating {len(records)} real historical matches; no network calls.", flush=True)
    report, rows = evaluate(records, protocol)
    report.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dataset_sha256=manifest["dataset_sha256"],
        protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        model_sha256=hashlib.sha256((ROOT / "app/model.py").read_bytes()).hexdigest(),
    )
    (DATA_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (DATA_DIR / "predictions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    (ROOT / "docs/BENCHMARK.md").write_text(report_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in ("holdout_matches", "one_x_two", "selected", "rich_history")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
