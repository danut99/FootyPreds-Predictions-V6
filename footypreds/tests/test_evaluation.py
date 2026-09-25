from copy import deepcopy
from datetime import timedelta

import pytest

from footypreds.engine import analyze
from footypreds.evaluation.run import blind, evaluate
from footypreds.tests.helpers import fixture, strong_history

PROTOCOL = {
    "history_seasons": ["history"],
    "validation_seasons": ["validation"],
    "test_season": "test",
    "leagues": ["E0"],
    "selection_threshold": 0.85,
    "rich_history_minimum": 20,
    "bootstrap": {"iterations": 100, "seed": 7},
}


def record(match, season, odds=None):
    return {
        "match": match.model_dump(mode="json"),
        "season": season,
        "league_code": "E0",
        "reference_odds": odds or {"1": 1.2, "X": 6, "2": 12, "over25": 1.5, "under25": 2.6},
    }


def records():
    rows = [record(m, "history") for m in strong_history()]
    for i, season in enumerate(("validation", "validation", "test", "test", "test")):
        match = fixture(
            id=f"{season}-{i}",
            kickoff=fixture().kickoff + timedelta(days=i),
            status="finished",
            home_goals=4,
            away_goals=0,
        )
        rows.append(record(match, season))
    return rows


def test_result_fields_are_structurally_unavailable():
    clean = blind(fixture(status="finished", home_goals=5, away_goals=1))
    for name in ("home_goals", "away_goals", "scores", "result", "live_stats"):
        with pytest.raises(AttributeError):
            getattr(clean, name)
    assert not clean.odds
    with pytest.raises(TypeError):
        clean.odds["1"] = 2
    assert dict(blind(fixture(), {"1": 2.0}).odds) == {"1": 2.0}


def test_spy_checks_every_input_has_no_result_or_same_day_history():
    calls = []

    def spy(target, history, threshold, **kwargs):
        assert not hasattr(target, "home_goals")
        # Market variant: 1X2 and over/under 2.5 prices only, never a result.
        assert set(target.odds) <= {"1", "X", "2", "over25", "under25"}
        assert all(m.kickoff.date() < target.kickoff.date() for m in history.rows)
        calls.append((target.id, bool(target.odds)))
        return analyze(target, history, threshold, **kwargs)

    report, _ = evaluate(records(), PROTOCOL, baseline=False, predictor=spy)
    # Each test match is predicted twice: without and with market odds.
    assert calls.count(("test-2", False)) == 1 and calls.count(("test-2", True)) == 1
    assert len(calls) == 6
    assert report["holdout_matches"] == 3


def test_validation_run_never_loads_the_test_season():
    report, rows = evaluate(records(), PROTOCOL, holdout="validation", baseline=False)
    assert {r["id"] for r in rows} == {"validation-0", "validation-1"}
    assert all(r["history_count"] <= 26 for r in rows)
    assert report["holdout_season"] == "validation"


def test_future_result_poisoning_cannot_change_earlier_predictions():
    first = records()
    poisoned = deepcopy(first)
    poisoned[-1]["match"].update(home_goals=0, away_goals=40)
    _, rows1 = evaluate(first, PROTOCOL)
    _, rows2 = evaluate(poisoned, PROTOCOL)
    assert [r["model"] for r in rows1] == [r["model"] for r in rows2]
    assert [r["v7"] for r in rows1] == [r["v7"] for r in rows2]
    assert rows1[-1]["actual"] != rows2[-1]["actual"]


def test_same_day_results_never_reach_each_other():
    source = records()
    source[-1]["match"]["kickoff"] = source[-2]["match"]["kickoff"]
    source[-1]["match"]["home"] = "Other home"
    source[-1]["match"]["away"] = "Other away"
    _, rows = evaluate(source, PROTOCOL)
    assert rows[-1]["history_count"] == rows[-2]["history_count"]


def test_duplicate_and_temporal_overlap_fail_closed():
    source = records()
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate(source + [source[-1]], PROTOCOL)
    source[0]["match"]["kickoff"] = source[-1]["match"]["kickoff"]
    source[0]["match"]["home"] = "Overlap without duplicate"
    with pytest.raises(ValueError, match="overlap"):
        evaluate(source, PROTOCOL)


def test_shuffled_input_is_deterministic():
    source = records()
    assert evaluate(source, PROTOCOL) == evaluate(list(reversed(source)), PROTOCOL)


def test_report_compares_model_market_v7_and_bookmaker():
    report, _ = evaluate(records(), PROTOCOL)
    assert set(report["one_x_two"]) >= {"model", "model_market", "v7", "league_frequency"}
    assert report["same_odds_subset"]["bookmaker"]["count"] == 3
    assert report["over25_vs_bookmaker"]["matches"] == 3
    assert report["selected"]["model_market"]["selected"] >= 1
