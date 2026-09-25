"""Walk-forward/backtest leakage checks and metric edge cases."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from footypreds.domain import Match
from footypreds.engine import backtest, summarize, walk_forward, wilson
from footypreds.engine.backtest import CALIBRATION_BANDS
from footypreds.tests.generators import poisson_sample

START = datetime(2025, 8, 1, tzinfo=timezone.utc)


def season(seed, days=70, teams=8, per_day=2):
    """Matches spread over many days, never within 3 hours of midnight UTC."""
    rng = random.Random(seed)
    names = [f"Side{n}" for n in range(teams)]
    rows = []
    for day in range(days):
        for slot in range(per_day):
            home, away = rng.sample(names, 2)
            rows.append(
                Match(
                    id=f"s{seed}-{day}-{slot}",
                    kickoff=START + timedelta(days=day, hours=12 + 3 * slot),
                    league="Test",
                    home=home,
                    away=away,
                    status="finished",
                    home_goals=poisson_sample(rng, 1.5),
                    away_goals=poisson_sample(rng, 1.1),
                    odds={"1": 2.2, "X": 3.3, "2": 3.4} if rng.random() < 0.5 else {},
                )
            )
    return rows


def predictions(matches, **kwargs):
    return {match.id: prediction for match, prediction in walk_forward(matches, **kwargs)}


@pytest.mark.parametrize("seed", range(4))
def test_poisoning_same_day_and_future_results_never_changes_earlier_predictions(seed):
    rng = random.Random(seed)
    rows = season(seed)
    baseline = predictions(rows)
    day = START.date() + timedelta(days=rng.randint(20, 60))
    poisoned = [
        m.model_copy(update={"home_goals": rng.randint(0, 12), "away_goals": rng.randint(0, 12)})
        if m.kickoff.date() >= day
        else m
        for m in rows
    ]
    extra = [
        Match(
            id=f"future-{n}",
            kickoff=datetime.combine(day, datetime.min.time(), timezone.utc)
            + timedelta(days=n % 5, hours=14),
            league="Test",
            home=f"Side{n % 8}",
            away=f"Side{(n + 1) % 8}",
            status="finished",
            home_goals=9,
            away_goals=0,
        )
        for n in range(20)
    ]
    changed = predictions(poisoned + extra)
    for match in rows:
        if match.kickoff.date() <= day:
            assert changed[match.id] == baseline[match.id], match.id


@pytest.mark.parametrize("seed", range(3))
def test_input_order_duplicates_and_unfinished_rows_do_not_matter(seed):
    rng = random.Random(100 + seed)
    rows = season(100 + seed, days=40)
    baseline = predictions(rows)
    shuffled = rows[:]
    rng.shuffle(shuffled)
    noise = [
        m.model_copy(
            update={
                "id": f"{m.id}-sched",
                "status": "scheduled",
                "home_goals": None,
                "away_goals": None,
            }
        )
        for m in rows[::5]
    ]
    assert predictions(shuffled + rows[: len(rows) // 3] + noise) == baseline
    assert list(predictions(shuffled)) == [
        m.id for m in sorted(rows, key=lambda m: (m.kickoff, m.id))
    ]


def test_the_target_score_and_its_odds_flag_are_respected():
    rows = season(7, days=30)
    for match, prediction in walk_forward(rows, keep_odds=False):
        assert prediction["components"]["market_1x2"] is None
    target = rows[-1]
    flipped = [
        m if m.id != target.id else m.model_copy(update={"home_goals": 0, "away_goals": 9})
        for m in rows
    ]
    assert predictions(rows)[target.id] == predictions(flipped)[target.id]


def test_results_inside_the_three_hour_window_across_midnight_do_not_leak():
    rows = season(11, days=40)
    midnight = datetime.combine(
        START.date() + timedelta(days=40), datetime.min.time(), timezone.utc
    )
    late = Match(
        id="late",
        kickoff=midnight - timedelta(minutes=30),
        league="Test",
        home="Side2",
        away="Side3",
        status="finished",
        home_goals=1,
        away_goals=1,
    )
    target = Match(
        id="target",
        kickoff=midnight + timedelta(minutes=30),
        league="Test",
        home="Side0",
        away="Side1",
        status="finished",
        home_goals=2,
        away_goals=0,
    )
    first = predictions(rows + [late, target])["target"]
    poisoned = late.model_copy(update={"home_goals": 9, "away_goals": 9})
    second = predictions(rows + [poisoned, target])["target"]
    assert second == first


def test_backtest_rows_and_metrics_are_consistent():
    rows = season(21, days=60)
    report = backtest(rows, threshold=0.6)
    metrics = report["metrics"]
    assert metrics["total_matches"] == len(rows)
    assert metrics["selected"] >= metrics["settled"] == metrics["selected"]
    assert metrics["pending"] == 0
    assert 0 <= metrics["sufficient_history"] <= len(rows)
    if metrics["settled"]:
        low, high = metrics["interval95"]
        assert low <= metrics["accuracy"] <= high
    by_id = {m.id: m for m in rows}
    for row in report["rows"]:
        match = by_id[row["match"]["id"]]
        assert row["prediction"]["selection"]["probability"] >= 0.6
        assert row["result"]["score"] == f"{match.home_goals}-{match.away_goals}"
        assert row["prediction"]["grade"] in ("A", "B", "C")
    assert len(report["rows"]) <= 100


def test_backtest_of_nothing():
    report = backtest([])
    assert report["metrics"]["total_matches"] == 0 and report["rows"] == []
    assert report["metrics"]["accuracy"] is None and report["metrics"]["coverage"] == 0


# --- wilson / summarize -------------------------------------------------------------------


def test_wilson_interval_properties():
    rng = random.Random(3)
    assert wilson(0, 0) is None
    for _ in range(500):
        count = rng.randint(1, 2000)
        wins = rng.randint(0, count)
        low, high = wilson(wins, count)
        assert 0 <= low <= wins / count <= high <= 1
    assert wilson(0, 10)[0] == 0 and wilson(10, 10)[1] == 1
    assert wilson(0, 10)[1] > 0 and wilson(10, 10)[0] < 1
    widths = [wilson(n // 2, n)[1] - wilson(n // 2, n)[0] for n in (10, 100, 1000, 10000)]
    assert widths == sorted(widths, reverse=True)


def row(probability, won):
    return {
        "prediction": {"selection": {"probability": probability}},
        "result": None if won is None else {"won": won},
    }


def test_summarize_calibration_band_edges():
    rows = [row(p, True) for p in (0.0, 0.4999, 0.5, 0.5999, 0.6, 0.8999, 0.9, 0.99, 1.0)]
    metrics = summarize(rows, 20)
    counts = {band["range"]: band["count"] for band in metrics["calibration"]}
    assert counts == {"0%–50%": 2, "50%–60%": 2, "60%–70%": 1, "80%–90%": 1, "90%–100%": 3}
    assert sum(counts.values()) == metrics["settled"] == 9
    assert len(CALIBRATION_BANDS) == 6


def test_summarize_pending_rows_never_enter_accuracy_or_brier():
    rows = [row(0.9, True), row(0.9, None), row(0.9, None)]
    metrics = summarize(rows, 3)
    assert (metrics["selected"], metrics["settled"], metrics["pending"]) == (3, 1, 2)
    assert metrics["accuracy"] == 1 and metrics["brier"] == pytest.approx(0.01)
    assert metrics["coverage"] == 1
    only_pending = summarize([row(0.9, None)], 0)
    assert only_pending["accuracy"] is None and only_pending["brier"] is None
    assert only_pending["coverage"] == 0 and only_pending["calibration"] == []


@pytest.mark.parametrize("count,supported", [(99, False), (100, True)])
def test_target_support_needs_a_hundred_settled_rows(count, supported):
    metrics = summarize([row(0.9, True) for _ in range(count)], count)
    assert metrics["target_supported"] is supported


def test_target_support_needs_the_lower_bound_above_85_percent():
    rows = [row(0.9, n % 10 != 0) for n in range(100)]  # 90% accuracy, n=100
    metrics = summarize(rows, 100)
    assert metrics["accuracy"] == 0.9
    assert metrics["interval95"][0] < 0.85 and not metrics["target_supported"]
    # The same 90% on 400 settled rows is enough evidence.
    assert summarize([row(0.9, n % 10 != 0) for n in range(400)], 400)["target_supported"]
