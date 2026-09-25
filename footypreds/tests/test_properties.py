"""Property tests for the engine over hundreds of seeded pseudo-random inputs."""

import json
import math
import random
from datetime import timedelta

import pytest

from footypreds.engine import SELECTABLE, HistoryIndex, analyze, with_params
from footypreds.engine import markets as mk
from footypreds.engine.analyzer import blend, grade_of, market_probabilities
from footypreds.engine.ratings import PRIOR_HOME, PRIOR_MU, Ratings, fit
from footypreds.tests.generators import (
    poisson_sample,
    random_league,
    random_odds,
    random_target,
)
from footypreds.tests.helpers import KICKOFF, fixture, result

EPS = 1e-9
OVER_LINES = ("over05", "over15", "over25", "over35", "over45")
UNDER_LINES = ("under05", "under15", "under25", "under35", "under45")
COMBOS = {
    "btts_over25": ("btts", "over25"),
    "1_over15": ("1", "over15"),
    "2_over15": ("2", "over15"),
    "1X_under35": ("1X", "under35"),
    "X2_under35": ("X2", "under35"),
    "home_win_nil": ("1", "no_btts", "home_over05"),
    "away_win_nil": ("2", "no_btts", "away_over05"),
}


def rates(rng, low=0.01, high=8.0):
    return low + rng.random() * (high - low)


def probs(analysis):
    return {m["key"]: m["probability"] for m in analysis["markets"]}


# --- score matrix ------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(4))
def test_score_matrix_is_a_distribution_for_any_rate_and_rho(seed):
    rng = random.Random(seed)
    extreme = [0.0, -0.12, -1, 1, -1e6, 1e6, -37.5, 0.9999, 1.0, -0.5]
    for n in range(100):
        home, away = rates(rng), rates(rng)
        rho = extreme[n % len(extreme)] if n % 3 == 0 else (rng.random() - 0.5) * 40
        matrix = mk.score_matrix(home, away, rho)
        assert len(matrix) == mk.MAX_GOALS + 1 and all(len(r) == mk.MAX_GOALS + 1 for r in matrix)
        cells = [p for *_, p in mk.cells(matrix)]
        assert all(math.isfinite(p) and p >= 0 for p in cells), (home, away, rho)
        assert sum(cells) == pytest.approx(1, abs=1e-9)


def test_score_matrix_boundary_rates_are_valid():
    for home, away in [(0.01, 0.01), (8, 8), (0.01, 8), (8, 0.01), (0.15, 5), (5, 0.15)]:
        for rho in (-1e9, -0.12, 0, 0.5, 1e9):
            matrix = mk.score_matrix(home, away, rho)
            assert sum(map(sum, matrix)) == pytest.approx(1)
            assert min(p for *_, p in mk.cells(matrix)) >= 0


def test_dixon_coles_only_touches_the_four_low_score_cells():
    plain, corrected = mk.score_matrix(1.3, 1.1), mk.score_matrix(1.3, 1.1, -0.12)
    # After renormalization every untouched cell keeps the same relative weight.
    ratio = corrected[2][3] / plain[2][3]
    for h, a, p in mk.cells(plain):
        if (h, a) not in {(0, 0), (0, 1), (1, 0), (1, 1)}:
            assert corrected[h][a] == pytest.approx(p * ratio, rel=1e-9)


# --- full-time markets --------------------------------------------------------------------


def check_full_time(p):
    assert set(p) == set(mk.FT_MARKETS)
    assert all(-EPS <= v <= 1 + EPS for v in p.values()), p
    assert p["1"] + p["X"] + p["2"] == pytest.approx(1)
    assert p["1X"] == pytest.approx(p["1"] + p["X"])
    assert p["X2"] == pytest.approx(p["X"] + p["2"])
    assert p["12"] == pytest.approx(p["1"] + p["2"])
    for over, under in zip(OVER_LINES, UNDER_LINES):
        assert p[over] + p[under] == pytest.approx(1)
    for higher, lower in zip(OVER_LINES, OVER_LINES[1:]):
        assert p[higher] >= p[lower] - EPS
    for lower, higher in zip(UNDER_LINES, UNDER_LINES[1:]):
        assert p[lower] <= p[higher] + EPS
    assert p["btts"] + p["no_btts"] == pytest.approx(1)
    assert p["home_over15"] <= p["home_over05"] + EPS
    assert p["away_over15"] <= p["away_over05"] + EPS
    for combo, parts in COMBOS.items():
        assert p[combo] <= min(p[k] for k in parts) + EPS, combo
    # Every final score belongs to exactly one of 1/X/2 and one of the 0.5 lines.
    assert p["btts"] <= min(p["home_over05"], p["away_over05"]) + EPS


@pytest.mark.parametrize("seed", range(3))
def test_full_time_markets_are_coherent_for_random_matrices(seed):
    rng = random.Random(100 + seed)
    for _ in range(100):
        matrix = mk.score_matrix(rates(rng), rates(rng), (rng.random() - 0.7) * 2)
        check_full_time(mk.full_time(matrix))


def test_full_time_matches_settlement_on_every_cell():
    """The pricing predicates and the settlement predicate are the same function of a score."""
    rng = random.Random(5)
    matrix = mk.score_matrix(1.4, 1.1, -0.12)
    p = mk.full_time(matrix)
    for key in mk.FT_MARKETS:
        brute = sum(q for h, a, q in mk.cells(matrix) if mk.outcome(key, h, a))
        assert brute == pytest.approx(p[key], abs=1e-12)
    for _ in range(200):
        h, a = rng.randint(0, 9), rng.randint(0, 9)
        assert mk.outcome("1X", h, a) == (mk.outcome("1", h, a) or mk.outcome("X", h, a))
        assert mk.outcome("btts", h, a) != mk.outcome("no_btts", h, a)
        assert mk.outcome("over25", h, a) != mk.outcome("under25", h, a)


# --- reweight -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(3))
def test_reweight_reproduces_random_targets_and_keeps_shape(seed):
    rng = random.Random(200 + seed)
    for _ in range(100):
        matrix = mk.score_matrix(rates(rng, 0.15, 5), rates(rng, 0.15, 5), -0.12)
        target = random_target(rng)
        adjusted = mk.reweight(matrix, target)
        assert mk.one_x_two(adjusted) == pytest.approx(target, abs=1e-9)
        assert sum(map(sum, adjusted)) == pytest.approx(1)
        assert min(p for *_, p in mk.cells(adjusted)) >= 0
        # Shape inside a region: 2-1 vs 1-0 are both home wins.
        assert adjusted[2][1] / adjusted[1][0] == pytest.approx(matrix[2][1] / matrix[1][0])
        check_full_time(mk.full_time(adjusted))


def test_reweight_handles_near_zero_regions():
    matrix = mk.score_matrix(8, 0.01)
    assert mk.one_x_two(matrix)["2"] < 1e-3
    target = {"1": 0.2, "X": 0.3, "2": 0.5}
    adjusted = mk.reweight(matrix, target)
    assert all(math.isfinite(p) and p >= 0 for *_, p in mk.cells(adjusted))
    assert mk.one_x_two(adjusted) == pytest.approx(target, abs=1e-9)


def test_reweight_with_an_empty_region_stays_a_probability_distribution():
    # No away-win cell has mass: the region cannot be scaled up to 40%.
    matrix = [[0.5, 0.0], [0.5, 0.0]]
    adjusted = mk.reweight(matrix, {"1": 0.3, "X": 0.3, "2": 0.4})
    assert sum(map(sum, adjusted)) == pytest.approx(1)
    assert min(p for *_, p in mk.cells(adjusted)) >= 0


def test_reweight_to_its_own_1x2_is_identity():
    matrix = mk.score_matrix(1.7, 0.8, -0.12)
    adjusted = mk.reweight(matrix, mk.one_x_two(matrix))
    for (_, _, p), (_, _, q) in zip(mk.cells(matrix), mk.cells(adjusted)):
        assert q == pytest.approx(p, rel=1e-12)


# --- half-time ----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(3))
def test_half_time_is_consistent_with_full_time_target(seed):
    rng = random.Random(300 + seed)
    for _ in range(60):
        home, away = rates(rng, 0.15, 5), rates(rng, 0.15, 5)
        target = random_target(rng)
        ht, htft = mk.half_time(home, away, target)
        assert set(htft) == {f"{a}/{b}" for a in mk.RESULTS for b in mk.RESULTS}
        assert all(0 <= v <= 1 + EPS for v in htft.values())
        assert sum(htft.values()) == pytest.approx(1)
        for final in mk.RESULTS:
            column = sum(htft[f"{half}/{final}"] for half in mk.RESULTS)
            assert column == pytest.approx(target[final], abs=1e-9)
        assert ht["ht_1"] + ht["ht_X"] + ht["ht_2"] == pytest.approx(1)
        assert 0 <= ht["ht_over15"] <= ht["ht_over05"] <= 1
        for key in ("ht_1", "ht_X", "ht_2"):
            row = sum(htft[f"{key[-1]}/{final}"] for final in mk.RESULTS)
            assert ht[key] == pytest.approx(row)


def test_half_time_with_an_impossible_final_result():
    ht, htft = mk.half_time(1.2, 1.0, {"1": 0.5, "X": 0.5, "2": 0.0})
    assert all(htft[f"{half}/2"] == 0 for half in mk.RESULTS)
    assert sum(htft.values()) == pytest.approx(1)
    assert ht["ht_1"] + ht["ht_X"] + ht["ht_2"] == pytest.approx(1)


def test_display_tables_are_consistent():
    rng = random.Random(4)
    for _ in range(40):
        matrix = mk.score_matrix(rates(rng, 0.15, 5), rates(rng, 0.15, 5), -0.12)
        scores = mk.correct_scores(matrix, 10)
        values = [s["probability"] for s in scores]
        assert values == sorted(values, reverse=True) and len(scores) == 10
        assert sum(values) <= 1 + EPS
        assert sum(mk.goal_distribution(matrix)) == pytest.approx(1)
        grid = mk.score_grid(matrix)
        assert len(grid) == 6 and all(len(row) == 6 for row in grid)
        assert sum(map(sum, grid)) <= 1 + EPS


# --- ratings ------------------------------------------------------------------------------


def league_rows(rng, teams=8, matches=150, weights=True):
    names = [f"T{i}" for i in range(teams)]
    rows = []
    for _ in range(matches):
        h, a = rng.sample(names, 2)
        w = 0.05 + rng.random() if weights else 1.0
        rows.append((h, a, poisson_sample(rng, 1.5), poisson_sample(rng, 1.1), w))
    return rows


def assert_sane(ratings):
    for value in (ratings.mu, ratings.home, *ratings.attack.values(), *ratings.defence.values()):
        assert math.isfinite(value) and value > 0


@pytest.mark.parametrize("seed", range(6))
def test_ratings_fit_random_leagues_is_finite_positive_and_converged(seed):
    rng = random.Random(400 + seed)
    rows = league_rows(rng, rng.randint(2, 12), rng.randint(1, 250))
    ratings = fit(rows)
    assert_sane(ratings)
    assert ratings.matches == len(rows)
    assert ratings.total_weight == pytest.approx(sum(r[4] for r in rows))
    # attack/defence/mu share a scale that only the weak priors pin down, so compare the
    # identifiable quantity: expected goals for every pairing after 40 vs 2000 iterations.
    longer = fit(rows, iterations=2000, tolerance=1e-14)
    for home in ratings.attack:
        for away in ratings.attack:
            if home != away:
                assert ratings.expected(home, away) == pytest.approx(
                    longer.expected(home, away), rel=2e-3
                )


@pytest.mark.parametrize("seed", range(5))
def test_ratings_are_invariant_to_row_order(seed):
    rng = random.Random(500 + seed)
    rows = league_rows(rng)
    shuffled = rows[:]
    rng.shuffle(shuffled)
    one, two = fit(rows), fit(shuffled)
    assert one.mu == pytest.approx(two.mu, rel=1e-9)
    assert one.home == pytest.approx(two.home, rel=1e-9)
    for team in one.attack:
        assert one.attack[team] == pytest.approx(two.attack[team], rel=1e-9)
        assert one.defence[team] == pytest.approx(two.defence[team], rel=1e-9)


def test_rows_without_positive_weight_are_ignored():
    rng = random.Random(9)
    rows = league_rows(rng)
    noise = [("T0", "T1", 9, 0, 0.0), ("T2", "Ghost", 7, 7, -3.0)]
    one, two = fit(rows), fit(rows + noise)
    assert "Ghost" not in two.attack
    assert one.attack == pytest.approx(two.attack)
    assert fit(noise).attack == {}


def test_weight_scale_controls_shrinkage_monotonically():
    rng = random.Random(10)
    rows = league_rows(rng, 6, 120, weights=False)

    def spread(scale):
        ratings = fit([(h, a, hg, ag, w * scale) for h, a, hg, ag, w in rows])
        # Ratio, not difference: the common scale of attack ratings is not identifiable.
        values = list(ratings.attack.values())
        return math.log(max(values) / min(values))

    spreads = [spread(scale) for scale in (1e-4, 0.01, 0.1, 1, 10, 100)]
    assert spreads == sorted(spreads)
    # A negligible total weight means "no information": every team stays at the prior.
    assert spreads[0] < 1e-3


def test_ratings_recover_planted_strength_order_on_large_samples():
    rng = random.Random(11)
    attack = {f"P{i}": v for i, v in enumerate((0.5, 0.75, 1.0, 1.35, 1.8, 2.4))}
    defence = {f"P{i}": v for i, v in enumerate((2.0, 1.5, 1.1, 0.85, 0.65, 0.5))}
    rows = []
    for _ in range(3000):
        h, a = rng.sample(sorted(attack), 2)
        rows.append(
            (
                h,
                a,
                poisson_sample(rng, 1.3 * 1.25 * attack[h] * defence[a]),
                poisson_sample(rng, 1.3 * attack[a] * defence[h]),
                1.0,
            )
        )
    ratings = fit(rows)
    teams = sorted(attack)
    assert sorted(teams, key=ratings.attack.get) == sorted(teams, key=attack.get)
    assert sorted(teams, key=ratings.defence.get) == sorted(teams, key=defence.get)
    assert ratings.home == pytest.approx(1.25, rel=0.1)


def test_all_goalless_league_and_two_team_league_stay_finite():
    rows = [(f"Z{i % 5}", f"Z{(i + 1) % 5}", 0, 0, 1.0) for i in range(200)]
    ratings = fit(rows)
    assert_sane(ratings)
    assert ratings.mu < PRIOR_MU
    home, away = ratings.expected("Z0", "Z1")
    assert 0 < home < 0.5 and 0 < away < 0.5
    pair = fit([("A", "B", 3, 1, 1.0)] * 50)
    assert_sane(pair)
    assert pair.attack["A"] > pair.attack["B"]


def test_empty_fit_is_the_prior_and_unknown_teams_are_average():
    ratings = fit([])
    assert (ratings.mu, ratings.home, ratings.matches) == (PRIOR_MU, PRIOR_HOME, 0)
    assert ratings.expected("x", "y") == pytest.approx((PRIOR_MU * PRIOR_HOME, PRIOR_MU))
    warm = fit([("A", "B", 1, 0, 1.0)], init=Ratings(attack={"Old": 2.0}, defence={"Old": 0.5}))
    assert warm.attack["Old"] == 2.0


# --- analyzer -----------------------------------------------------------------------------


def random_case(seed):
    rng = random.Random(seed)
    teams = [f"Club{i}" for i in range(rng.randint(3, 9))]
    history = random_league(rng, teams, rng.randint(0, 140), days=rng.choice((60, 400, 900)))
    home, away = rng.sample(teams, 2)
    odds = random_odds(rng) if rng.random() < 0.5 else {}
    return rng, history, fixture(home=home, away=away, odds=odds)


def check_analysis(analysis, threshold):
    json.dumps(analysis)  # JSON-ready
    for market in analysis["markets"]:
        assert 0 <= market["probability"] <= 1 + EPS, market
        assert math.isfinite(market["probability"])
        if market["fair_odds"] is not None:
            assert market["fair_odds"] >= 1 - EPS
    check_full_time({k: v for k, v in probs(analysis).items() if k in mk.FT_MARKETS})
    goals = analysis["expected_goals"]
    assert 0.15 <= goals["home"] <= 5 and 0.15 <= goals["away"] <= 5
    assert isinstance(analysis["confidence"], int) and 0 <= analysis["confidence"] <= 100
    assert analysis["grade"] == grade_of(analysis["confidence"])
    assert (analysis["quality"] == "sufficient") == (analysis["grade"] != "D")
    assert sum(item["probability"] for item in analysis["htft"]) == pytest.approx(1)
    assert sum(analysis["goal_distribution"]) == pytest.approx(1)
    selectable = [m for m in analysis["markets"] if m["selectable"]]
    assert {m["key"] for m in selectable} == set(SELECTABLE)
    best = max(m["probability"] for m in selectable)
    pick = analysis["selection"]
    if pick is not None:
        assert pick["key"] in SELECTABLE and pick["selectable"]
        assert pick["probability"] >= threshold
        assert pick["probability"] == best
        assert analysis["grade"] in ("A", "B", "C")
    elif analysis["grade"] != "D":
        assert best < threshold
    for tip in analysis["tips"]:
        assert 0 <= tip["probability"] <= 1 + EPS


@pytest.mark.parametrize("seed", range(40))
def test_analysis_is_bounded_deterministic_and_order_invariant(seed):
    rng, history, match = random_case(seed)
    threshold = rng.choice((0.5, 0.6, 0.75, 0.85, 0.99))
    analysis = analyze(match, history, threshold)
    check_analysis(analysis, threshold)
    shuffled = history[:]
    rng.shuffle(shuffled)
    assert analyze(match, shuffled + history[: len(history) // 2], threshold) == analysis


@pytest.mark.parametrize("seed", range(25))
def test_results_at_or_after_the_cutoff_never_change_the_analysis(seed):
    rng, history, match = random_case(1000 + seed)
    baseline = analyze(match, history)
    teams = sorted({m.home for m in history} | {m.away for m in history} | {match.home, match.away})
    cutoff = match.kickoff - timedelta(hours=3)
    poison = []
    for n in range(30):
        home, away = rng.sample(teams, 2) if n % 3 else (match.home, match.away)
        when = cutoff + timedelta(minutes=rng.choice((0, 1, 60, 179, 180, 181, 60 * 24 * 30)))
        poison.append(
            result(f"poison{n}", 0, home, away, rng.randint(0, 12), rng.randint(0, 12)).model_copy(
                update={"kickoff": when}
            )
        )
    # The fixture itself reported as finished, with the same ID, must not leak either.
    poison.append(
        match.model_copy(update={"status": "finished", "home_goals": 0, "away_goals": 11})
    )
    assert analyze(match, history + poison) == baseline


def test_result_just_before_the_cutoff_is_visible():
    match = fixture()
    visible = result("edge", 0, "Strong", "Weak", 5, 0).model_copy(
        update={"kickoff": KICKOFF - timedelta(hours=3, seconds=1)}
    )
    assert analyze(match, [visible])["sample"]["home"] == 1
    at_cutoff = visible.model_copy(update={"kickoff": KICKOFF - timedelta(hours=3)})
    assert analyze(match, [at_cutoff])["sample"]["home"] == 0


@pytest.mark.parametrize("seed", range(10))
def test_unrelated_results_do_not_change_the_analysis(seed):
    rng, history, match = random_case(2000 + seed)
    baseline = analyze(match, history)
    stranger = [
        result(f"x{n}", 1 + rng.random() * 300, f"Far{n % 4}", f"Away{n % 5}", 9, 0)
        for n in range(40)
    ]
    assert analyze(match, history + stranger) == baseline


@pytest.mark.parametrize("seed", range(10))
def test_second_hop_results_move_ratings_but_not_form_or_h2h(seed):
    rng = random.Random(2100 + seed)
    history = [
        result(f"h{n}", 3 + n * 5, "Strong", f"Opp{n % 4}", rng.randint(0, 3), rng.randint(0, 3))
        for n in range(12)
    ] + [
        result(f"a{n}", 4 + n * 5, f"Opp{n % 4}", "Weak", rng.randint(0, 3), rng.randint(0, 3))
        for n in range(12)
    ]
    match = fixture()
    baseline = analyze(match, history)
    # Opponents' other results (2nd hop) are part of the rating fit and nothing else.
    extra = [result(f"o{n}", 2 + n, f"Opp{n % 4}", f"Else{n}", 7, 0) for n in range(8)]
    changed = analyze(match, history + extra)
    for key in ("form", "h2h"):
        assert changed[key] == baseline[key]
    assert changed["sample"]["home"] == baseline["sample"]["home"]
    assert changed["sample"]["away"] == baseline["sample"]["away"]
    assert changed["components"]["fit_matches"] == baseline["components"]["fit_matches"] + 8


@pytest.mark.parametrize("seed", range(8))
def test_confidence_never_increases_as_data_gets_older(seed):
    rng, history, match = random_case(3000 + seed)
    previous = None
    for shift in (0, 20, 60, 150, 300, 500, 800):
        aged = [
            m.model_copy(update={"kickoff": m.kickoff - timedelta(days=shift)}) for m in history
        ]
        confidence = analyze(match, aged)["confidence"]
        if previous is not None:
            assert confidence <= previous, (shift, confidence, previous)
        previous = confidence


def test_selection_threshold_is_monotone():
    history = random_league(random.Random(1), ["Strong", "Weak", "Mid", "Low"], 150, days=200)
    match = fixture()
    picks = [analyze(match, history, t)["selection"] for t in (0.5, 0.6, 0.7, 0.8, 0.9, 0.99)]
    seen_none = False
    for pick in picks:
        if pick is None:
            seen_none = True
        else:
            assert not seen_none, "a higher threshold selected while a lower one abstained"


def test_grade_d_never_selects_even_at_minimum_threshold():
    match = fixture(odds={"1": 1.05, "X": 15, "2": 40})
    analysis = analyze(match, [], 0.5)
    assert analysis["grade"] == "D" and analysis["selection"] is None
    assert max(m["probability"] for m in analysis["markets"] if m["selectable"]) > 0.5


# --- market blend -------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_market_weight_limits(seed):
    rng, history, match = random_case(4000 + seed)
    odds = random_odds(rng)
    priced = match.model_copy(update={"odds": odds})
    market = market_probabilities(priced.odds)
    if market is None:
        pytest.skip("random book outside the usable overround")
    model_only = analyze(priced, history, params=with_params(market_weight=0.0))
    assert model_only["components"]["market_weight"] == 0
    assert model_only["components"]["model_1x2"] == pytest.approx(
        {k: probs(model_only)[k] for k in "1X2"}
    )
    market_only = analyze(priced, history, params=with_params(market_weight=1.0))
    assert {k: probs(market_only)[k] for k in "1X2"} == pytest.approx(market)
    default = analyze(priced, history)
    assert default["components"]["market_1x2"] == pytest.approx(market)


def test_invalid_or_partial_books_are_ignored():
    history = random_league(random.Random(3), ["Strong", "Weak", "A", "B"], 80)
    plain = analyze(fixture(), history)
    books = [
        {"1": 2.0, "X": 3.0},  # partial
        {"1": 2.0, "2": 3.0},
        {"1": 1.1, "X": 1.1, "2": 1.1},  # absurd overround
        {"1": 10.0, "X": 10.0, "2": 10.0},  # negative margin
        {"1": float("nan"), "X": 3.0, "2": 3.0},
        {"1": float("inf"), "X": 3.0, "2": 3.0},
        {"1": 0.5, "X": 3.0, "2": 3.0},
        {"1": 5000, "X": 1.5, "2": 3.0},
    ]
    for odds in books:
        analysis = analyze(fixture(odds=odds), history)
        assert analysis["components"]["market_1x2"] is None, odds
        assert analysis["components"]["market_weight"] == 0
        assert probs(analysis)["1"] == pytest.approx(probs(plain)["1"])


def test_blend_is_a_normalized_geometric_pool():
    rng = random.Random(12)
    for _ in range(200):
        model, market = random_target(rng), random_target(rng)
        weight = rng.random()
        pooled = blend(model, market, weight)
        assert sum(pooled.values()) == pytest.approx(1)
        assert blend(model, market, 0) == pytest.approx(model)
        assert blend(model, market, 1) == pytest.approx(market)
        # The favourite of both sources stays the favourite of the pool.
        if max(model, key=model.get) == max(market, key=market.get):
            assert max(pooled, key=pooled.get) == max(model, key=model.get)


# --- history index ------------------------------------------------------------------------


def tied_results(seed):
    """Many results sharing kickoff times (every Saturday 15:00 kicks off together)."""
    rng = random.Random(seed)
    rows = []
    for n in range(80):
        home, away = rng.sample(["Strong", "Weak", "Mid", "Low", "Top"], 2)
        rows.append(
            result(
                f"id{rng.randrange(10**6):06}-{n}",
                rng.randint(1, 6) * 7,
                home,
                away,
                rng.randint(0, 4),
                rng.randint(0, 4),
            )
        )
    return rows


@pytest.mark.parametrize("seed", range(3))
def test_history_index_batch_build_is_sorted_and_order_free(seed):
    rows = tied_results(seed)
    index = HistoryIndex(rows)
    assert [m.id for m in index.rows] == [
        m.id for m in sorted(rows, key=lambda m: (m.kickoff, m.id))
    ]
    shuffled = rows[:]
    random.Random(seed).shuffle(shuffled)
    assert [m.id for m in HistoryIndex(shuffled).rows] == [m.id for m in index.rows]


@pytest.mark.parametrize("seed", range(3))
def test_history_index_incremental_build_equals_batch_build(seed):
    rows = tied_results(seed)
    batch = HistoryIndex(rows)
    incremental = HistoryIndex()
    # Chronological feeding, one result at a time; ties arrive in descending id order.
    by_kickoff = {}
    for match in rows:
        by_kickoff.setdefault(match.kickoff, []).append(match)
    for kickoff in sorted(by_kickoff):
        for match in sorted(by_kickoff[kickoff], key=lambda m: m.id, reverse=True):
            incremental.extend([match])
    assert [m.id for m in incremental.rows] == [m.id for m in batch.rows]
    assert analyze(fixture(), incremental) == analyze(fixture(), batch)


def test_team_names_that_differ_only_by_case_or_accents_do_not_crash():
    history = random_league(random.Random(8), ["Dinamo", "Rapid", "Steaua", "Otelul"], 60)
    for home, away in (("Dinamo", "DINAMO"), ("Oțelul", "Otelul")):
        analysis = analyze(fixture(home=home, away=away), history)
        check_analysis(analysis, 0.85)
