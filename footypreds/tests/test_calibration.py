"""Goals calibration: Platt map, market pool, one coherent rescaled matrix, validation-only fit,
and the recommendation value cap (docs/MODEL.md, "Calibrarea golurilor")."""

import math
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from footypreds import recommend as rc
from footypreds.engine import PARAMS, analyze
from footypreds.engine import markets as mk
from footypreds.evaluation import calibration as cal
from footypreds.sports import validate_analysis
from footypreds.tests.helpers import KICKOFF, fixture, league_history, strong_history

IDENTITY = replace(PARAMS, totals_intercept=0.0, totals_slope=1.0, totals_market_weight=0.0)
ONE_X_TWO = ("1", "X", "2", "1X", "X2", "12")


def probabilities(analysis):
    return {m["key"]: m["probability"] for m in analysis["markets"]}


# --- maps --------------------------------------------------------------------------------------


@pytest.mark.parametrize("intercept, slope", [(0.0556, 0.7241), (-0.4, 0.5), (0.3, 1.6)])
def test_platt_map_is_monotone_and_bounded(intercept, slope):
    grid = [i / 1000 for i in range(0, 1001)]
    values = [mk.platt(p, intercept, slope) for p in grid]
    assert all(0 < v < 1 for v in values)
    assert all(b > a for a, b in zip(values, values[1:], strict=False))


def test_identity_platt_and_pool_leave_the_probability_untouched():
    for p in (0.0, 0.2, 0.5, 0.93, 1.0):
        assert mk.platt(p) == p
        assert mk.pool_binary(p, None, 1.0) == p
        assert mk.pool_binary(p, 0.4, 0.0) == p
    assert mk.pool_binary(0.7, 0.4, 1.0) == 0.4


def test_shrinking_map_pulls_extremes_toward_the_middle():
    assert mk.platt(0.8, 0.0, 0.7) < 0.8 and mk.platt(0.2, 0.0, 0.7) > 0.2
    assert mk.platt(0.5, 0.0, 0.7) == pytest.approx(0.5)


def test_pool_lies_between_model_and_market_and_moves_with_the_weight():
    model, market = 0.7, 0.45
    pooled = [mk.pool_binary(model, market, w / 10) for w in range(11)]
    assert pooled[0] == model and pooled[-1] == market
    assert all(market <= p <= model for p in pooled)
    assert all(b < a for a, b in zip(pooled, pooled[1:], strict=False))


@pytest.mark.parametrize(
    "odds, expected",
    [
        ({"over25": 1.9, "under25": 1.9}, 0.5),
        ({"over25": 1.5, "under25": 2.6}, (1 / 1.5) / (1 / 1.5 + 1 / 2.6)),
        ({"over25": 1.9}, None),
        ({"over25": 1.0, "under25": 20.0}, None),
        ({"over25": 3.0, "under25": 3.0}, None),  # 0.67: not a two-way book
        ({"over25": 1.2, "under25": 1.3}, None),  # overround 1.6
        ({"over25": float("nan"), "under25": 1.9}, None),
        ({"over25": "1.9", "under25": 1.9}, None),
        ({"over25": True, "under25": 1.9}, None),
    ],
)
def test_two_way_probability_removes_the_margin_and_refuses_bad_books(odds, expected):
    value = mk.two_way_probability(odds, "over25", "under25")
    assert value == (None if expected is None else pytest.approx(expected))


# --- one coherent matrix ----------------------------------------------------------------------


def coherent(matrix):
    ft = mk.full_time(matrix)
    assert all(p >= 0 for row in matrix for p in row)
    assert sum(map(sum, matrix)) == pytest.approx(1, abs=1e-12)
    for yes, no in (("over15", "under15"), ("over25", "under25"), ("over35", "under35")):
        assert ft[yes] + ft[no] == pytest.approx(1, abs=1e-12)
    assert ft["over05"] >= ft["over15"] >= ft["over25"] >= ft["over35"] >= ft["over45"]
    assert ft["btts"] + ft["no_btts"] == pytest.approx(1, abs=1e-12)
    assert ft["btts_over25"] <= min(ft["btts"], ft["over25"]) + 1e-12
    assert ft["1X"] == pytest.approx(ft["1"] + ft["X"], abs=1e-12)
    return ft


@pytest.mark.parametrize("home, away, rho", [(1.6, 1.1, -0.12), (0.7, 2.4, -0.12), (2.9, 0.4, 0)])
@pytest.mark.parametrize("target", [0.25, 0.45, 0.62, 0.8])
def test_fit_total_hits_the_target_and_keeps_1x2(home, away, rho, target):
    base = mk.score_matrix(home, away, rho)
    one_x_two = mk.one_x_two(base)
    scale, matrix = mk.fit_total(home, away, rho, one_x_two, target)
    assert scale > 0
    ft = coherent(matrix)
    assert ft["over25"] == pytest.approx(target, abs=1e-8)
    for key in ("1", "X", "2"):
        assert ft[key] == pytest.approx(one_x_two[key], abs=1e-12)


def test_more_goals_move_every_goals_line_the_same_way():
    base = mk.score_matrix(1.4, 1.2, -0.12)
    target = mk.one_x_two(base)
    low = mk.full_time(mk.fit_total(1.4, 1.2, -0.12, target, 0.4)[1])
    high = mk.full_time(mk.fit_total(1.4, 1.2, -0.12, target, 0.6)[1])
    for key in ("over05", "over15", "over25", "over35", "over45", "btts", "home_over05"):
        assert high[key] > low[key], key


def test_fit_total_is_monotone_in_the_target_and_clamps_the_impossible():
    target = mk.one_x_two(mk.score_matrix(1.5, 1.0, -0.12))
    scales = [mk.fit_total(1.5, 1.0, -0.12, target, t / 20)[0] for t in range(4, 17)]
    assert all(b > a for a, b in zip(scales, scales[1:], strict=False))
    assert mk.fit_total(1.5, 1.0, -0.12, target, 0.0)[0] == pytest.approx(0.25)
    assert mk.fit_total(1.5, 1.0, -0.12, target, 1.0)[0] == pytest.approx(4.0)


# --- analyzer -----------------------------------------------------------------------------------


def test_identity_parameters_reproduce_the_uncalibrated_model():
    history = league_history()
    plain = analyze(fixture(odds={"1": 1.7, "X": 3.9, "2": 4.8}), history, params=IDENTITY)
    totals = plain["components"]["totals"]
    assert totals["scale"] == 1.0 and totals["over25"] == totals["model_over25"]
    p = probabilities(plain)
    # The pre-calibration pipeline: Dixon-Coles matrix reweighted to the blended 1X2.
    matrix = mk.score_matrix(plain["expected"]["home"], plain["expected"]["away"], PARAMS.rho)
    matrix = mk.reweight(matrix, {k: p[k] for k in ("1", "X", "2")})
    assert mk.over_probability(matrix) == pytest.approx(p["over25"], abs=1e-12)
    assert mk.full_time(matrix)["btts"] == pytest.approx(p["btts"], abs=1e-12)


def test_totals_step_never_moves_1x2_with_or_without_prices():
    history = league_history()
    for odds in ({}, {"1": 1.7, "X": 3.9, "2": 4.8}):
        raw = probabilities(analyze(fixture(odds=odds), history, params=IDENTITY))
        for extra in ({}, {"over25": 2.3, "under25": 1.6}, {"over25": 1.35, "under25": 3.2}):
            calibrated = probabilities(analyze(fixture(odds=odds | extra), history))
            for key in ONE_X_TWO:
                assert calibrated[key] == pytest.approx(raw[key], abs=1e-12), (odds, extra, key)


def test_over_under_price_sets_over25_and_the_whole_matrix_follows():
    history = league_history()
    odds = {"1": 1.7, "X": 3.9, "2": 4.8, "over25": 2.3, "under25": 1.6}
    analysis = validate_analysis(analyze(fixture(odds=odds), history))
    p = probabilities(analysis)
    market = mk.two_way_probability(odds, "over25", "under25")
    # Tuned weight 1.0: with a usable price, P(over 2.5) is the margin-free market probability.
    assert PARAMS.totals_market_weight == 1.0
    assert p["over25"] == pytest.approx(market, abs=1e-8)
    assert p["over25"] + p["under25"] == pytest.approx(1)
    assert p["over15"] >= p["over25"] >= p["over35"]
    totals = analysis["components"]["totals"]
    assert totals["market_over25"] == pytest.approx(market) and totals["market_weight"] == 1.0
    # The score grid, the goal distribution and the extended totals come from the same matrix.
    distribution = analysis["goal_distribution"]
    assert sum(distribution[3:]) == pytest.approx(p["over25"], abs=1e-9)
    by_key = {m["key"]: m for m in analysis["markets"]}
    assert by_key["over25"]["probability"] == pytest.approx(p["over25"])
    # Expected goals are the rescaled rates that produced the matrix.
    raw = analyze(fixture(odds=odds), history, params=IDENTITY)
    scale = totals["scale"]
    assert analysis["expected"]["home"] == pytest.approx(raw["expected"]["home"] * scale)
    assert analysis["expected_goals"] == analysis["expected"]


def test_without_a_price_the_platt_map_decides_over25():
    history = league_history()
    odds = {"1": 1.7, "X": 3.9, "2": 4.8}
    raw = probabilities(analyze(fixture(odds=odds), history, params=IDENTITY))["over25"]
    p = probabilities(analyze(fixture(odds=odds), history))["over25"]
    assert p == pytest.approx(mk.platt(raw, PARAMS.totals_intercept, PARAMS.totals_slope))
    # The fitted slope is below 1: the raw goal totals were too extreme on validation.
    assert 0 < PARAMS.totals_slope < 1
    assert abs(p - 0.5) < abs(raw - 0.5) + 0.02


def test_unusable_over_under_prices_are_ignored():
    history = league_history()
    base = probabilities(analyze(fixture(odds={"1": 1.7, "X": 3.9, "2": 4.8}), history))
    for bad in ({"over25": 1.9}, {"over25": 1.0, "under25": 9.0}, {"over25": 3, "under25": 3}):
        odds = {"1": 1.7, "X": 3.9, "2": 4.8} | bad
        assert probabilities(analyze(fixture(odds=odds), history)) == pytest.approx(base)


def test_calibration_uses_no_result_after_the_cutoff():
    """Poisoning results at/after kickoff - 3h changes nothing, with or without prices."""
    history = strong_history()
    odds = {"1": 1.3, "X": 5.0, "2": 9.0, "over25": 1.7, "under25": 2.1}
    clean = analyze(fixture(odds=odds), history)
    poison = [
        fixture(
            id=f"late-{i}",
            kickoff=KICKOFF - timedelta(hours=h),
            status="finished",
            home_goals=0,
            away_goals=7,
        )
        for i, h in enumerate((0, 1, 2.9))
    ] + [
        fixture(
            id="after",
            kickoff=KICKOFF + timedelta(days=1),
            status="finished",
            home_goals=0,
            away_goals=9,
        )
    ]
    dirty = analyze(fixture(odds=odds), history + poison)
    assert probabilities(dirty) == probabilities(clean)
    assert dirty["components"]["totals"] == clean["components"]["totals"]


# --- fitting (validation only) ------------------------------------------------------------------


def synthetic(n, seed, intercept=0.06, slope=0.7):
    """Raw forecasts that are too extreme: the truth is platt(raw, intercept, slope)."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        raw = min(0.9, max(0.1, rng.gauss(0.5, 0.13)))
        truth = mk.platt(raw, intercept, slope)
        rows.append((raw, rng.random() < truth))
    return rows


def test_fit_platt_recovers_the_true_map():
    a, b = cal.fit_platt(synthetic(20000, 1))
    assert a == pytest.approx(0.06, abs=0.05) and b == pytest.approx(0.7, abs=0.06)


def test_fit_platt_degenerate_inputs_fall_back_to_identity():
    assert cal.fit_platt([]) == (0.0, 1.0)
    assert cal.fit_platt([(0.6, True)] * 50) == (0.0, 1.0)
    # Forecasts that are anti-correlated with the outcome would need a negative slope.
    rng = random.Random(3)
    reversed_rows = [(p, rng.random() < 1 - p) for p in (rng.random() for _ in range(3000))]
    assert cal.fit_platt(reversed_rows) == (0.0, 1.0)


def test_calibration_fitted_on_one_half_improves_the_other_half():
    """Regression: the fitted map lowers log loss and ECE out of sample (deterministic)."""
    fit_half, test_half = synthetic(6000, 11), synthetic(6000, 12)
    a, b = cal.fit_platt(fit_half)
    calibrated = [(mk.platt(p, a, b), y) for p, y in test_half]
    before, after = cal.reliability(test_half), cal.reliability(calibrated)
    assert after["log_loss"] < before["log_loss"] - 0.002
    assert after["ece"] < before["ece"] * 0.6

    # The extreme bands were overconfident and are pulled back toward the observed rate.
    def worst_gap(summary):
        return max(
            abs(band["predicted"] - band["actual"])
            for band in summary["bands"]
            if band["count"] >= 200
        )

    assert worst_gap(after) < worst_gap(before)


def rows_from(observations, market=None):
    rows = []
    for i, (raw, over) in enumerate(observations):
        goals = (2, 1) if over else (1, 0)
        rows.append(
            {
                "id": f"r{i}",
                "home_goals": goals[0],
                "away_goals": goals[1],
                "totals_market": {
                    "model_over25": raw,
                    "over25": raw,
                    "market_over25": None if market is None else market[i],
                },
            }
        )
    return rows


def test_fit_totals_prefers_the_market_when_it_is_the_better_forecast():
    rng = random.Random(5)
    truth = [min(0.85, max(0.15, rng.gauss(0.5, 0.1))) for _ in range(4000)]
    outcomes = [rng.random() < t for t in truth]
    noisy = [min(0.95, max(0.05, t + rng.gauss(0, 0.15))) for t in truth]
    rows = rows_from(list(zip(noisy, outcomes, strict=True)), market=truth)
    fitted = cal.fit_totals(rows)
    assert fitted["totals_market_weight"] >= 0.8
    assert 0 < fitted["totals_slope"] < 1


def test_fit_totals_refuses_rows_already_calibrated():
    rows = rows_from([(0.6, True)] * 30)
    rows[0]["totals_market"]["over25"] = 0.55
    with pytest.raises(ValueError):
        cal.fit_totals(rows)


def test_reliability_bands_and_ece():
    summary = cal.reliability([(0.15, False), (0.15, True), (0.95, True), (1.0, True)])
    assert [band["count"] for band in summary["bands"]] == [2, 2]
    assert summary["bands"][0]["actual"] == 0.5 and summary["ece"] == pytest.approx(
        (2 * 0.35 + 2 * 0.025) / 4
    )


VALIDATION_PROTOCOL = {
    "history_seasons": ["history"],
    "validation_seasons": ["validation"],
    "test_season": "test",
    "leagues": ["E0"],
    "selection_threshold": 0.85,
    "rich_history_minimum": 20,
    "bootstrap": {"iterations": 50, "seed": 7},
}


def season_records(test_goals=(4, 0)):
    def record(match, season):
        return {
            "match": match.model_dump(mode="json"),
            "season": season,
            "league_code": "E0",
            "reference_odds": {"1": 1.3, "X": 5, "2": 9, "over25": 1.7, "under25": 2.1},
        }

    rows = [record(m, "history") for m in strong_history()]
    for i, season in enumerate(("validation",) * 3 + ("test",) * 3):
        goals = test_goals if season == "test" else (i % 3, 1)
        match = fixture(
            id=f"{season}-{i}",
            kickoff=KICKOFF + timedelta(days=i),
            status="finished",
            home_goals=goals[0],
            away_goals=goals[1],
        )
        rows.append(record(match, season))
    return rows


def test_fit_uses_the_validation_season_only():
    records = season_records()
    rows, _, _ = cal.validation_rows(VALIDATION_PROTOCOL, params=IDENTITY, records=records)
    assert {r["id"] for r in rows} == {"validation-0", "validation-1", "validation-2"}
    fitted = cal.tune_totals(VALIDATION_PROTOCOL, records=records)
    # Poisoning every test-season result cannot change what is fitted.
    poisoned = cal.tune_totals(VALIDATION_PROTOCOL, records=season_records(test_goals=(0, 9)))
    assert poisoned == fitted
    with pytest.raises(ValueError):
        cal.validation_rows(VALIDATION_PROTOCOL, holdout="test", records=records)


def test_value_cap_is_the_validation_roi_maximum():
    legs = [(0.97, True, 2.0)] * 5 + [(0.97, False, 2.0)] * 5  # value <= 1.05: ROI 0
    legs += [(1.2, False, 2.0)] * 8 + [(1.2, True, 2.0)] * 2  # value 1.2: ROI -0.6
    cap, scores = cal.fit_value_cap(legs)
    assert cap == 1.0 and scores["1.0"] == 0.0 and scores["inf"] < 0
    assert cal.fit_value_cap([]) == (None, {})


# --- recommendation value cap ---------------------------------------------------------------------


def test_eligible_legs_skip_strong_disagreements_with_the_market():
    from footypreds.sports import analyze_match
    from footypreds.tests.test_recommend import NOW, basketball

    match = basketball()
    analysis = analyze_match(match, []) | {"grade": "B", "quality": "sufficient"}
    capped = rc.eligible_legs(match, analysis, NOW)
    uncapped = rc.eligible_legs(match, analysis, NOW, max_value=None)
    assert rc.MAX_VALUE == 1.05
    assert all(rc.MIN_VALUE <= x["probability"] * x["odds"] <= rc.MAX_VALUE for x in capped)
    removed = [x for x in uncapped if x["probability"] * x["odds"] > rc.MAX_VALUE]
    assert [x["key"] for x in capped] == [x["key"] for x in uncapped if x not in removed]


def test_eligible_legs_value_cap_on_a_constructed_analysis():
    match = fixture(kickoff=KICKOFF + timedelta(days=30), odds={"1": 2.0, "X": 3.4, "2": 4.0})

    def analysis(p_home):
        rows = []
        for key, p in (("1", p_home), ("X", 0.25), ("2", 0.2)):
            rows.append(
                {
                    "key": key,
                    "label": key,
                    "group": "Rezultat final",
                    "probability": p,
                    "fair_odds": 1 / p,
                    "odds": match.odds[key],
                    "ev": p * match.odds[key] - 1,
                    "selectable": True,
                }
            )
        return {"grade": "A", "confidence": 80, "markets": rows, "insights": [], "summary": ""}

    now = KICKOFF
    fair = [x["key"] for x in rc.eligible_legs(match, analysis(0.51), now)]
    greedy = [x["key"] for x in rc.eligible_legs(match, analysis(0.6), now)]
    assert "1" in fair and "1" not in greedy
    assert "1" in [x["key"] for x in rc.eligible_legs(match, analysis(0.6), now, max_value=None)]
    assert math.isclose(0.6 * 2.0, 1.2)
