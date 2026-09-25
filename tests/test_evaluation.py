from copy import deepcopy
from datetime import timedelta

import pytest

from app.model import predict
from evaluation.run import blind, evaluate
from tests.test_model import fixture, strong_history

PROTOCOL = {
    "history_seasons": ["history"],
    "test_season": "test",
    "leagues": ["E0"],
    "selection_threshold": 0.85,
    "rich_history_minimum": 20,
    "bootstrap": {"iterations": 100, "seed": 7},
}


def records():
    history = [
        {"match": m.model_dump(mode="json"), "season": "history", "league_code": "E0"}
        for m in strong_history()
    ]
    for i in range(3):
        match = fixture(
            id=f"test-{i}",
            kickoff=fixture().kickoff + timedelta(days=i),
            status="finished",
            home_goals=4,
            away_goals=0,
        )
        history.append(
            {
                "match": match.model_dump(mode="json"),
                "season": "test",
                "league_code": "E0",
                "reference_odds": {"1": 1.2, "X": 6, "2": 12},
            }
        )
    return history


def test_result_fields_are_structurally_unavailable():
    clean = blind(fixture(status="finished", home_goals=5, away_goals=1))
    for field in ("home_goals", "away_goals", "scores", "result", "live_stats"):
        with pytest.raises(AttributeError):
            getattr(clean, field)
    assert not clean.odds
    with pytest.raises(TypeError):
        clean.odds["1"] = 2


def test_spy_checks_every_input_has_no_result_or_same_day_history():
    calls = []

    def spy(target, history, threshold):
        assert not hasattr(target, "home_goals")
        assert not target.odds
        assert all(m.kickoff.date() < target.kickoff.date() for m in history)
        calls.append(target.id)
        return predict(target, history, threshold)

    report, _ = evaluate(records(), PROTOCOL, spy)
    assert len(calls) == 3
    assert report["rich_history"]["matches"] == 3
    assert report["history_matches"] == 25


def test_future_result_poisoning_cannot_change_earlier_predictions():
    first = records()
    poisoned = deepcopy(first)
    poisoned[-1]["match"].update(home_goals=0, away_goals=40)
    _, rows1 = evaluate(first, PROTOCOL)
    _, rows2 = evaluate(poisoned, PROTOCOL)
    assert [r["poisson"] for r in rows1] == [r["poisson"] for r in rows2]
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
