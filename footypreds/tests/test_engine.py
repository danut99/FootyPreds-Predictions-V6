from datetime import timedelta

import pytest

from footypreds.demo import demo_data
from footypreds.engine import HistoryIndex, analyze, backtest, outcome, summarize, with_params
from footypreds.engine import markets as mk
from footypreds.engine.backtest import walk_forward, wilson
from footypreds.engine.ratings import fit
from footypreds.tests.helpers import (
    KICKOFF,
    fixture,
    league_history,
    result,
    strong_history,
)


def probs(analysis):
    return {m["key"]: m["probability"] for m in analysis["markets"]}


# --- score matrix and markets ------------------------------------------------------------


@pytest.mark.parametrize("home,away", [(0.25, 0.25), (3, 3), (3, 0.25), (1.5, 1.2)])
def test_score_matrix_is_normalized_and_preserves_expected_goals(home, away):
    matrix = mk.score_matrix(home, away)
    cells = list(mk.cells(matrix))
    assert sum(p for *_, p in cells) == pytest.approx(1)
    assert sum(h * p for h, _, p in cells) == pytest.approx(home, abs=1e-3)
    assert sum(a * p for _, a, p in cells) == pytest.approx(away, abs=1e-3)


@pytest.mark.parametrize("rho", [-0.1, -5, 5])
def test_dixon_coles_correction_stays_a_valid_distribution(rho):
    matrix = mk.score_matrix(1.4, 1.1, rho)
    assert all(p >= 0 for *_, p in mk.cells(matrix))
    assert sum(map(sum, matrix)) == pytest.approx(1)


def test_negative_rho_raises_low_scoring_draws():
    plain, corrected = mk.score_matrix(1.3, 1.1), mk.score_matrix(1.3, 1.1, -0.1)
    assert corrected[0][0] > plain[0][0] and corrected[1][1] > plain[1][1]
    assert mk.one_x_two(corrected)["X"] > mk.one_x_two(plain)["X"]


def test_full_time_markets_are_coherent():
    p = mk.full_time(mk.score_matrix(1.7, 0.9, -0.06))
    assert p["1"] + p["X"] + p["2"] == pytest.approx(1)
    assert p["1X"] == pytest.approx(p["1"] + p["X"])
    assert p["12"] == pytest.approx(p["1"] + p["2"])
    for over, under in [("over05", "under05"), ("over15", "under15"), ("over25", "under25")]:
        assert p[over] + p[under] == pytest.approx(1)
    assert p["over05"] > p["over15"] > p["over25"] > p["over35"] > p["over45"]
    assert p["btts"] + p["no_btts"] == pytest.approx(1)
    assert p["btts_over25"] <= min(p["btts"], p["over25"]) + 1e-12
    assert p["home_win_nil"] <= p["1"]
    assert p["1_over15"] <= p["1"]


def test_reweight_hits_market_target_and_keeps_shape_inside_each_result():
    matrix = mk.score_matrix(1.5, 1.0)
    target = {"1": 0.3, "X": 0.3, "2": 0.4}
    adjusted = mk.reweight(matrix, target)
    assert mk.one_x_two(adjusted) == pytest.approx(target)
    assert adjusted[2][1] / adjusted[1][0] == pytest.approx(matrix[2][1] / matrix[1][0])


def test_half_time_full_time_is_consistent_with_final_result():
    target = mk.one_x_two(mk.score_matrix(1.6, 1.0, -0.06))
    ht, htft = mk.half_time(1.6, 1.0, target)
    assert sum(htft.values()) == pytest.approx(1)
    for final in mk.RESULTS:
        column = sum(htft[f"{half}/{final}"] for half in mk.RESULTS)
        assert column == pytest.approx(target[final])
    assert ht["ht_1"] + ht["ht_X"] + ht["ht_2"] == pytest.approx(1)
    assert ht["ht_over05"] > ht["ht_over15"]
    # A draw at half-time is more likely than at full time.
    assert ht["ht_X"] > target["X"]


@pytest.mark.parametrize(
    "key,home,away,won",
    [
        ("1X", 1, 1, True),
        ("X2", 2, 1, False),
        ("over25", 2, 1, True),
        ("under35", 2, 2, False),
        ("btts", 0, 1, False),
        ("no_btts", 0, 0, True),
        ("home_win_nil", 2, 0, True),
        ("away_win_nil", 0, 0, False),
        ("btts_over25", 2, 1, True),
        ("1_over15", 1, 0, False),
        ("X2_under35", 2, 2, False),
    ],
)
def test_settlement_markets(key, home, away, won):
    assert outcome(key, home, away) is won


# --- ratings -----------------------------------------------------------------------------


def test_ratings_recover_strength_order_and_home_advantage():
    rows = [(m.home, m.away, m.home_goals, m.away_goals, 1.0) for m in league_history()]
    ratings = fit(rows)
    assert ratings.attack["Strong"] > ratings.attack["Mid"] > ratings.attack["Weak"]
    assert ratings.defence["Weak"] > ratings.defence["Strong"]
    assert ratings.home > 1


def test_ratings_shrink_small_samples_towards_average():
    ratings = fit([("A", "B", 5, 0, 1.0)], prior=4)
    assert 1 < ratings.attack["A"] < 5 / ratings.mu
    assert fit([]).attack == {}


# --- history ------------------------------------------------------------------------------


def test_history_index_is_sorted_deduplicated_and_respects_cutoff():
    rows = strong_history()
    index = HistoryIndex(rows + list(reversed(rows)))
    assert len(index.rows) == len(rows)
    assert index.rows == sorted(rows, key=lambda m: (m.kickoff, m.id))
    cutoff = KICKOFF - timedelta(days=30)
    assert all(m.kickoff < cutoff for m, _ in index.team("Strong", cutoff=cutoff))


def test_namesake_with_another_team_id_is_not_mixed_in():
    own = result("own", 10, "Nacional", "X", 2, 0, home_id="uy")
    other = result("other", 12, "Nacional", "Y", 0, 5, home_id="py")
    unknown = result("h2h", 14, "Nacional", "Z", 1, 1)  # h2h rows carry no IDs
    rows = HistoryIndex([own, other, unknown]).team("Nacional", "uy")
    assert {m.id for m, _ in rows} == {"own", "h2h"}


# --- analyzer -----------------------------------------------------------------------------


def test_analysis_markets_are_coherent_and_complete():
    analysis = analyze(fixture(), strong_history())
    p = probs(analysis)
    assert p["1"] + p["X"] + p["2"] == pytest.approx(1)
    assert p["1"] > 0.6
    assert {t["category"] for t in analysis["tips"]} >= {"Rezultat final", "Goluri", "Scor corect"}
    assert len(analysis["score_grid"]) == 6
    assert sum(analysis["goal_distribution"]) == pytest.approx(1)
    assert sum(item["probability"] for item in analysis["htft"]) == pytest.approx(1)
    assert analysis["summary"].startswith("Modelul favorizează Strong")


def test_future_current_and_simultaneous_results_cannot_leak():
    baseline = analyze(fixture(), strong_history())
    contamination = [
        fixture(
            id=f"leak-{hours}",
            kickoff=KICKOFF + timedelta(hours=hours),
            status="finished",
            home_goals=0,
            away_goals=9,
        )
        for hours in (-2, 0, 1, 24)
    ]
    assert analyze(fixture(), strong_history() + contamination) == baseline


def test_duplicate_history_and_order_do_not_change_the_analysis():
    history = strong_history()
    assert analyze(fixture(), history + list(reversed(history))) == analyze(fixture(), history)


def test_cup_match_uses_league_and_other_competition_form():
    """V7 refused cup games without 8 results in the SAME cup; V8 uses every competition."""
    history = [
        result(f"s{i}", 4 * (i + 1), "Strong", f"Club{i}", 3, 0, league="SPAIN: LaLiga")
        for i in range(10)
    ] + [
        result(f"w{i}", 4 * (i + 1) + 1, f"Side{i}", "Weak", 3, 0, league="SPAIN: Segunda")
        for i in range(10)
    ]
    analysis = analyze(fixture(league="SPAIN: Copa del Rey"), history, 0.6)
    assert analysis["sample"] == {"home": 10, "away": 10, "league": 20, "h2h": 0}
    assert analysis["grade"] in ("A", "B")
    assert analysis["quality"] == "sufficient"
    assert probs(analysis)["1"] > 0.6
    assert analysis["form"]["home"]["sequence"] == "WWWWW"
    assert analysis["form"]["away"]["sequence"] == "LLLLL"


def test_no_history_still_predicts_but_never_selects():
    analysis = analyze(fixture(), [])
    assert analysis["grade"] == "D" and analysis["quality"] == "insufficient"
    assert analysis["selection"] is None
    assert probs(analysis)["1"] + probs(analysis)["X"] + probs(analysis)["2"] == pytest.approx(1)
    assert "insuficiente" in analysis["summary"]


def test_stale_history_lowers_confidence():
    fresh = analyze(fixture(), strong_history())
    stale = [
        m.model_copy(update={"kickoff": m.kickoff - timedelta(days=400)}) for m in strong_history()
    ]
    old = analyze(fixture(), stale)
    assert old["confidence"] < fresh["confidence"]
    assert old["grade"] in ("C", "D")


def test_market_blend_moves_towards_bookmaker_and_rejects_broken_books():
    history = strong_history()
    plain = probs(analyze(fixture(), history))
    market = probs(analyze(fixture(odds={"1": 3.0, "X": 3.2, "2": 2.4}), history))
    assert market["1"] < plain["1"] and market["2"] > plain["2"]
    ignored = with_params(market_weight=0.0)
    assert probs(analyze(fixture(odds={"1": 3.0, "X": 3.2, "2": 2.4}), history, params=ignored))[
        "1"
    ] == pytest.approx(plain["1"])
    broken = probs(analyze(fixture(odds={"1": 1.1, "X": 1.1, "2": 1.1}), history))
    assert broken["1"] == pytest.approx(plain["1"])


def test_market_value_uses_quoted_odds():
    analysis = analyze(fixture(odds={"1": 2.5, "X": 3.4, "2": 3.1}), strong_history())
    home = next(m for m in analysis["markets"] if m["key"] == "1")
    assert home["ev"] == pytest.approx(home["probability"] * 2.5 - 1)


def test_recent_form_raises_expected_goals_when_weighted():
    old = [result(f"o{i}", 100 + 7 * i, "Strong", f"T{i}", 1, 1) for i in range(15)]
    surge = [result(f"n{i}", 2 + 3 * i, "Strong", f"U{i}", 4, 0) for i in range(5)]
    history = old + surge
    flat = analyze(fixture(), history, params=with_params(form_weight=0.0))
    weighted = analyze(fixture(), history, params=with_params(form_weight=0.5))
    assert weighted["expected_goals"]["home"] > flat["expected_goals"]["home"]
    assert weighted["form"]["home"]["last5"]["wins"] == 5


def test_form_streaks_splits_and_head_to_head():
    history = [
        result("a", 3, "Strong", "Weak", 2, 1),
        result("b", 10, "Weak", "Strong", 0, 0),
        result("c", 17, "Strong", "Other", 1, 0),
        result("d", 24, "Other", "Strong", 3, 1),
    ]
    analysis = analyze(fixture(), history)
    form = analysis["form"]["home"]
    assert form["sequence"] == "WDWL"
    assert form["streaks"]["unbeaten"] == 3 and form["streaks"]["wins"] == 1
    assert form["home10"]["played"] == 2 and form["away10"]["played"] == 2
    assert form["days_since_last"] == 3
    h2h = analysis["h2h"]
    assert (h2h["played"], h2h["home_wins"], h2h["draws"], h2h["away_wins"]) == (2, 1, 1, 0)


def test_friendlies_count_less_and_youth_games_are_ignored():
    league = [result(f"l{i}", 7 * (i + 1), "Strong", f"T{i}", 1, 1) for i in range(10)]
    friendlies = [
        result(f"f{i}", 7 * i + 3, "Strong", f"F{i}", 6, 0, league="WORLD: Club Friendly")
        for i in range(10)
    ]
    youth = [
        result(f"y{i}", 7 * i + 4, "Strong", f"Y{i}", 9, 0, league="SPAIN: Primavera U19")
        for i in range(10)
    ]
    base = analyze(fixture(), league)["expected_goals"]["home"]
    full = analyze(fixture(), league + friendlies, params=with_params(friendly_weight=1.0))
    half = analyze(fixture(), league + friendlies)
    assert base < half["expected_goals"]["home"] < full["expected_goals"]["home"]
    assert analyze(fixture(), league + youth)["expected_goals"] == pytest.approx(
        analyze(fixture(), league)["expected_goals"]
    )


def test_selection_is_a_ledger_market_above_threshold():
    analysis = analyze(fixture(), strong_history(), 0.85)
    pick = analysis["selection"]
    assert pick["selectable"] and pick["probability"] >= 0.85
    assert pick["key"] in mk.SELECTABLE
    assert analysis["calibrated"] is False
    assert analyze(fixture(), strong_history(), 0.9999)["selection"] is None


def test_insights_describe_form():
    notes = analyze(fixture(), strong_history())["insights"]
    assert any("neînvinsă" in note for note in notes)
    assert any("Meciuri directe" in note for note in notes)


# --- walk-forward -------------------------------------------------------------------------


def test_walk_forward_never_sees_same_day_or_target_results():
    history = strong_history()
    one = result("target", 0, "Strong", "Weak", 4, 0)
    two = one.model_copy(update={"home_goals": 0, "away_goals": 7})
    same_day = result("same-day", 0, "Strong", "Other", 0, 9)
    first = {m.id: p for m, p in walk_forward(history + [one, same_day])}
    second = {m.id: p for m, p in walk_forward(history + [two, same_day])}
    assert first["target"] == second["target"]
    assert first["target"]["sample"]["home"] == 25


def test_backtest_is_order_and_duplicate_invariant():
    history, _ = demo_data()
    sample = history[:72]
    assert backtest(sample) == backtest(list(reversed(sample)) + sample)
    assert backtest(sample)["metrics"]["total_matches"] == 72


def test_metrics_have_no_fake_accuracy_on_empty_sample():
    metrics = summarize([], 0)
    assert metrics["accuracy"] is None and metrics["interval95"] is None
    assert metrics["brier"] is None and not metrics["target_supported"]


def test_metrics_pending_denominators_and_brier():
    prediction = {"selection": {"probability": 0.9}}
    rows = [
        {"prediction": prediction, "result": {"won": True}},
        {"prediction": prediction, "result": {"won": False}},
        {"prediction": prediction, "result": None},
    ]
    metrics = summarize(rows, 10)
    assert metrics["accuracy"] == 0.5 and metrics["coverage"] == 0.3
    assert metrics["brier"] == pytest.approx(0.41) and metrics["pending"] == 1
    assert wilson(9, 10)[0] < 0.85
