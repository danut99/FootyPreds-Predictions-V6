from datetime import datetime, timedelta, timezone

import pytest

from app.demo import demo_data
from app.domain import Match
from app.model import backtest, outcome, predict, score_matrix, summarize, wilson


def fixture(**kwargs):
    data = dict(
        id="fixture",
        kickoff=datetime(2026, 6, 1, tzinfo=timezone.utc),
        league="Test",
        home="Strong",
        away="Weak",
    )
    return Match(**(data | kwargs))


def strong_history():
    target = fixture()
    return [
        fixture(
            id=f"past-{i}",
            kickoff=target.kickoff - timedelta(days=7 * (i + 1)),
            status="finished",
            home_goals=4,
            away_goals=0,
        )
        for i in range(25)
    ]


@pytest.mark.parametrize("home,away", [(0.25, 0.25), (4, 4), (4, 0.25), (1.5, 1.2)])
def test_score_matrix_is_normalized_and_marginals_preserved(home, away):
    cells = score_matrix(home, away)
    assert sum(p for _, _, p in cells) == pytest.approx(1)
    assert sum(h * p for h, _, p in cells) == pytest.approx(home, abs=0.0001)
    assert sum(a * p for _, a, p in cells) == pytest.approx(away, abs=0.0001)
    assert all(p >= 0 for _, _, p in cells)


def test_markets_are_coherent():
    prediction = predict(fixture(), strong_history())
    probs = {m["key"]: m["probability"] for m in prediction["markets"]}
    assert probs["1"] + probs["X"] + probs["2"] == pytest.approx(1)
    assert probs["1X"] == pytest.approx(probs["1"] + probs["X"])
    for left, right in [
        ("over15", "under15"),
        ("over25", "under25"),
        ("over35", "under35"),
        ("btts", "no_btts"),
    ]:
        assert probs[left] + probs[right] == pytest.approx(1)
    assert probs["over15"] > probs["over25"] > probs["over35"]


def test_future_current_and_simultaneous_results_cannot_leak():
    target = fixture()
    baseline = predict(target, strong_history())
    contamination = [
        fixture(
            id=str(i),
            kickoff=target.kickoff + timedelta(hours=i),
            status="finished",
            home_goals=0,
            away_goals=40,
        )
        for i in (-2, 0, 1, 24)
    ]
    assert predict(target, strong_history() + contamination) == baseline


def test_duplicate_history_does_not_inflate_sample():
    history = strong_history()
    assert predict(fixture(), history + history) == predict(fixture(), history)


def test_insufficient_or_stale_history_abstains():
    assert predict(fixture(), strong_history()[:7])["selection"] is None
    stale = [
        m.model_copy(update={"kickoff": m.kickoff - timedelta(days=180)}) for m in strong_history()
    ]
    assert predict(fixture(), stale)["selection"] is None


def test_high_probability_selection_is_not_claimed_calibrated():
    p = predict(fixture(), strong_history())
    assert p["selection"]["probability"] >= 0.85
    assert p["calibrated"] is False
    assert p["selection"] in p["markets"]


def test_other_leagues_excluded():
    wrong = [m.model_copy(update={"league": "Different"}) for m in strong_history()]
    assert predict(fixture(), wrong)["sample"]["home"] == 0


@pytest.mark.parametrize(
    "key,home,away,won",
    [
        ("1X", 1, 1, True),
        ("X2", 2, 1, False),
        ("over25", 2, 1, True),
        ("under35", 2, 2, False),
        ("btts", 0, 1, False),
        ("no_btts", 0, 0, True),
    ],
)
def test_settlement_markets(key, home, away, won):
    assert outcome(key, home, away) is won


def test_metrics_have_no_fake_accuracy_on_empty_sample():
    result = summarize([], 0)
    assert result["accuracy"] is None
    assert result["interval95"] is None
    assert not result["target_supported"]
    assert result["brier"] is None


def test_metrics_pending_denominators_and_brier():
    prediction = {"selection": {"probability": 0.9}}
    rows = [
        {"prediction": prediction, "result": {"won": True}},
        {"prediction": prediction, "result": {"won": False}},
        {"prediction": prediction, "result": None},
    ]
    m = summarize(rows, 10)
    assert m["accuracy"] == 0.5
    assert m["coverage"] == 0.3
    assert m["brier"] == pytest.approx(0.41)
    assert m["pending"] == 1
    assert not m["target_supported"]
    assert wilson(9, 10)[0] < 0.85


def test_walk_forward_order_and_duplicate_invariance():
    history, _ = demo_data()
    sample = history[:72]
    assert backtest(sample) == backtest(list(reversed(sample)) + sample)
    assert backtest(sample)["metrics"]["total_matches"] == 72


def test_backtest_does_not_learn_target_result():
    history = strong_history()
    one = fixture(status="finished", home_goals=4, away_goals=0)
    two = one.model_copy(update={"home_goals": 0, "away_goals": 4})
    first = backtest(history + [one])["rows"][-1]
    second = backtest(history + [two])["rows"][-1]
    assert first["prediction"] == second["prediction"]
