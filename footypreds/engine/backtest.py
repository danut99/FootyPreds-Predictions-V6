"""Walk-forward evaluation and ledger metrics.

Matches are processed one calendar day (UTC) at a time: every prediction for a day is
made before ANY result of that day enters the history, and the target score is removed
from the fixture passed to the analyzer.
"""

import itertools
import math
from datetime import datetime, time, timedelta, timezone

from footypreds.engine.analyzer import PARAMS, VERSION, analyze, fit_history
from footypreds.engine.history import HistoryIndex
from footypreds.engine.markets import outcome


def wilson(wins, count):
    if not count:
        return None
    z, rate = 1.96, wins / count
    denominator = 1 + z * z / count
    middle = (rate + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count**2)) / denominator
    return [max(0, middle - margin), min(1, middle + margin)]


CALIBRATION_BANDS = ((0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.000001))


def summarize(rows, total):
    """One selection per fixture. Only settled rows enter accuracy and Brier.

    A void result (won None: retirement, walkover, push) is settled but neither a win nor a
    loss, so it stays out of accuracy, Brier and calibration.
    """
    closed = [r for r in rows if r["result"] is not None]
    settled = [r for r in closed if r["result"].get("won") is not None]
    count = len(settled)
    wins = sum(r["result"]["won"] for r in settled)
    interval = wilson(wins, count)
    bins = []
    for lower, upper in CALIBRATION_BANDS:
        group = [r for r in settled if lower <= r["prediction"]["selection"]["probability"] < upper]
        if group:
            bins.append(
                {
                    "range": f"{lower:.0%}–{min(upper, 1):.0%}",
                    "count": len(group),
                    "predicted": sum(r["prediction"]["selection"]["probability"] for r in group)
                    / len(group),
                    "actual": sum(r["result"]["won"] for r in group) / len(group),
                }
            )
    return {
        "total_matches": total,
        "selected": len(rows),
        "settled": count,
        "wins": wins,
        "pending": len(rows) - len(closed),
        "void": len(closed) - count,
        "coverage": len(rows) / total if total else 0,
        "accuracy": wins / count if count else None,
        "interval95": interval,
        "brier": sum(
            (r["prediction"]["selection"]["probability"] - r["result"]["won"]) ** 2 for r in settled
        )
        / count
        if count
        else None,
        "target_supported": bool(count >= 100 and interval and interval[0] >= 0.85),
        "calibration": bins,
    }


def hide_result(match):
    return match.model_copy(update={"home_goals": None, "away_goals": None, "status": "scheduled"})


def walk_forward(matches, threshold=0.85, params=PARAMS, keep_odds=True):
    """Yield (match, prediction) in chronological order, with daily re-fitted ratings."""
    ordered = sorted(
        {m.id: m for m in matches if m.status == "finished"}.values(),
        key=lambda m: (m.kickoff, m.id),
    )
    index, ratings = HistoryIndex(), None
    for day, group in itertools.groupby(ordered, key=lambda m: m.kickoff.date()):
        batch = list(group)
        start = datetime.combine(day, time.min, timezone.utc)
        # The shared ratings serve every kickoff of the day, so they may only use results
        # the analyzer itself could see for the earliest one (kickoff - cutoff_hours):
        # a 23:30 result must not shape a 00:30 prediction.
        horizon = start - timedelta(hours=params.cutoff_hours)
        visible = index.before(horizon, start - timedelta(days=params.max_days))
        ratings = fit_history(visible, start, params, init=ratings) if visible else None
        for match in batch:
            fixture = hide_result(match)
            if not keep_odds:
                fixture = fixture.model_copy(update={"odds": {}})
            yield match, analyze(fixture, index, threshold, params=params, ratings=ratings)
        index.extend(batch)


def backtest(matches, threshold=0.85, params=PARAMS):
    rows, total, sufficient = [], 0, 0
    for match, prediction in walk_forward(matches, threshold, params, keep_odds=False):
        total += 1
        sufficient += prediction["quality"] == "sufficient"
        pick = prediction["selection"]
        if pick:
            rows.append(
                {
                    "match": match.model_dump(mode="json"),
                    "prediction": compact(prediction),
                    "result": {
                        "won": outcome(pick["key"], match.home_goals, match.away_goals),
                        "score": f"{match.home_goals}-{match.away_goals}",
                    },
                }
            )
    return {
        "metrics": {**summarize(rows, total), "sufficient_history": sufficient},
        "rows": rows[-100:],
        "threshold": threshold,
        "version": VERSION,
        "method": "walk-forward pe zile; rezultatele din aceeași zi nu sunt vizibile",
        "warning": "Evaluare retrospectivă, nu dovadă prospectivă. Nu optimiza pragul pe test.",
    }


def compact(prediction):
    """The ledger needs the pick and headline numbers, not every display table."""
    keep = ("version", "threshold", "sample", "quality", "grade", "confidence")
    return {k: prediction[k] for k in keep} | {
        # Football keeps expected goals and top scores; other sports have "expected" only.
        "sport": prediction.get("sport", "football"),
        "expected_goals": prediction.get("expected_goals", prediction.get("expected")),
        "selection": prediction["selection"],
        "reason": prediction["reason"],
        "scores": prediction.get("scores", [])[:3],
    }
