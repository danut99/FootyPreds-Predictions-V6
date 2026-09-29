"""tenisPrediction v3: odds join and blend, injury/fatigue features, per-tour engine params,
S-shaped calibration and the high-precision selection profile (tiny synthetic data, no network)."""

import random
from datetime import date, timedelta

import numpy as np
import pytest
from openpyxl import Workbook

from tenisPrediction import benchmark as bm
from tenisPrediction import calibration, engine, odds
from tenisPrediction import model as model_module
from tenisPrediction.model import (
    CACHE_FILE,
    NO_BET,
    SELECTED,
    VERSION,
    TennisModel,
    TennisPredictor,
    data_signature,
)

from .test_compact_tennis import make_row, small_model, swap, synthetic_rows

# --------------------------------------------------------------------------- odds: margin, blend


@pytest.mark.parametrize("method", odds.MARGIN_METHODS)
def test_fair_probability_removes_the_margin_symmetrically(method):
    p = odds.fair_probability(1.5, 2.8, method)
    q = odds.fair_probability(2.8, 1.5, method)
    assert 0.6 < p < 0.7 and abs(p + q - 1.0) < 1e-9
    # no margin: every method is the plain normalisation
    assert abs(odds.fair_probability(2.0, 2.0, method) - 0.5) < 1e-12
    assert abs(odds.fair_probability(4.0, 4 / 3, method) - 0.25) < 1e-9


def test_power_margin_shrinks_the_longshot_more_than_proportional():
    proportional = odds.fair_probability(1.2, 5.0, "proportional")
    power = odds.fair_probability(1.2, 5.0, "power")
    assert power > proportional  # favourite-longshot bias: the favourite gets more
    assert not odds.valid_odds(1.0, 3.0) and not odds.valid_odds(1.2, 1.2)  # 167% book
    assert odds.valid_odds(1.5, 2.7)
    with pytest.raises(ValueError):
        odds.fair_probability(1.5, 2.5, "magic")


def test_blend_is_symmetric_and_fit_recovers_the_weights():
    z = odds.blend_logit(1.0, 0.8, (0.3, 0.7))
    assert abs(z - (0.3 + 0.7 * odds.logit(0.8))) < 1e-12
    assert (
        abs(
            odds.blend_probability(0.7, 0.2, (0.5, 0.5))
            + odds.blend_probability(0.3, 0.8, (0.5, 0.5))
            - 1
        )
        < 1e-12
    )
    assert odds.blend_probability(0.7, None, (0.5, 0.5)) == 0.7
    rng = np.random.default_rng(1)
    z_model = rng.normal(0, 1.2, 20000)
    z_market = 0.8 * z_model + rng.normal(0, 0.5, 20000)
    truth = 0.3 * z_model + 0.8 * z_market
    won = rng.random(20000) < 1 / (1 + np.exp(-truth))
    sign = np.where(won, 1.0, -1.0)
    w_model, w_market = odds.fit_blend_weights(z_model * sign, z_market * sign)
    assert abs(w_model - 0.3) < 0.08 and abs(w_market - 0.8) < 0.1


# --------------------------------------------------------------------------- odds: join


PLAYERS = {
    "S1": "Jannik Sinner",
    "A1": "Carlos Alcaraz",
    "D1": "Alex De Minaur",
    "Z1": "Alexander Zverev",
}


def odds_rows():
    day = date(2023, 3, 6)
    rows = [
        make_row(
            season=2023,
            date=day,
            tourney_id="T1",
            match_num=1,
            round="R16",
            winner_id="S1",
            loser_id="A1",
            winner_name=PLAYERS["S1"],
            loser_name=PLAYERS["A1"],
        ),
        make_row(
            season=2023,
            date=day,
            tourney_id="T1",
            match_num=2,
            round="R16",
            winner_id="D1",
            loser_id="Z1",
            winner_name=PLAYERS["D1"],
            loser_name=PLAYERS["Z1"],
        ),
        # the same pair again three weeks later: a separate event, matched by its own date
        make_row(
            season=2023,
            date=day + timedelta(days=21),
            tourney_id="T2",
            match_num=1,
            round="QF",
            winner_id="A1",
            loser_id="S1",
            winner_name=PLAYERS["A1"],
            loser_name=PLAYERS["S1"],
        ),
    ]
    return rows


def write_workbook(directory, tour="atp", year=2023):
    book = Workbook()
    sheet = book.active
    sheet.append(["Date", "Winner", "Loser", "AvgW", "AvgL", "PSW", "PSL"])
    # the first match, priced with Alcaraz as the (tennis-data) winner column: prices must
    # follow the player, not the column
    sheet.append([date(2023, 3, 8), "Sinner J.", "Alcaraz C.", 1.6, 2.4, 1.62, 2.45])
    sheet.append([date(2023, 3, 8), "Zverev A.", "De Minaur A.", 1.9, 1.95, None, None])
    sheet.append([date(2023, 3, 30), "Alcaraz C.", "Sinner J.", 2.1, 1.75, 2.15, 1.78])
    path = directory / f"{tour}_{year}.xlsx"
    book.save(path)
    return path


def test_odds_join_orients_prices_by_player_and_context(tmp_path):
    rows = odds_rows()
    write_workbook(tmp_path)
    joined, stats = odds.attach_odds(rows, "avg", directory=tmp_path)
    assert stats["atp"]["matched"] == 3 and stats["conflicts"] == 0
    first, second, third = joined
    assert isinstance(first, bm.OddsRow) and first["winner_odds"] == 1.6
    assert first["loser_odds"] == 2.4 and first.get("winner_odds") == 1.6
    # tennis-data listed Zverev first, but De Minaur won in TML: the 1.95 follows De Minaur
    assert second["winner_odds"] == 1.95 and second["loser_odds"] == 1.9
    assert third["winner_odds"] == 2.1 and third["loser_odds"] == 1.75
    ctx, winner_first = bm.make_context(first)
    assert (ctx.first_odds, ctx.second_odds) == ((1.6, 2.4) if winner_first else (2.4, 1.6))
    assert bm.make_context(rows[0])[0].first_odds is None  # plain rows carry no prices
    # a book without prices falls back to the average; "ps" alone for the second match is None
    only_ps, _ = odds.attach_odds(rows, "ps", directory=tmp_path, fallback=False)
    assert only_ps[0]["winner_odds"] == 1.62 and not isinstance(only_ps[1], bm.OddsRow)


def test_odds_join_keeps_each_meeting_on_its_own_record(tmp_path):
    """Regression: a pair meeting in consecutive weeks (both inside the date window) gets one
    record per row, never the later meeting's prices on the earlier row; a pair meeting twice
    in ONE event (round robin, then the final) is left unpriced instead of guessed."""
    monday = date(2023, 10, 2)
    rows = [
        make_row(
            season=2023,
            date=monday,
            tourney_id="A",
            match_num=1,
            round="QF",
            winner_id="S1",
            loser_id="A1",
            winner_name=PLAYERS["S1"],
            loser_name=PLAYERS["A1"],
        ),
        make_row(
            season=2023,
            date=monday + timedelta(days=7),
            tourney_id="B",
            match_num=1,
            round="R16",
            winner_id="A1",
            loser_id="S1",
            winner_name=PLAYERS["A1"],
            loser_name=PLAYERS["S1"],
        ),
        make_row(
            season=2023,
            date=monday + timedelta(days=42),
            tourney_id="F",
            match_num=1,
            round="RR",
            winner_id="D1",
            loser_id="Z1",
            winner_name=PLAYERS["D1"],
            loser_name=PLAYERS["Z1"],
        ),
        make_row(
            season=2023,
            date=monday + timedelta(days=42),
            tourney_id="F",
            match_num=9,
            round="F",
            winner_id="Z1",
            loser_id="D1",
            winner_name=PLAYERS["Z1"],
            loser_name=PLAYERS["D1"],
        ),
    ]
    book = Workbook()
    sheet = book.active
    sheet.append(["Date", "Winner", "Loser", "AvgW", "AvgL"])
    sheet.append([monday + timedelta(days=4), "Sinner J.", "Alcaraz C.", 1.5, 2.6])
    sheet.append([monday + timedelta(days=9), "Alcaraz C.", "Sinner J.", 1.3, 3.5])
    sheet.append([monday + timedelta(days=44), "De Minaur A.", "Zverev A.", 3.0, 1.4])
    sheet.append([monday + timedelta(days=48), "Zverev A.", "De Minaur A.", 1.35, 3.2])
    book.save(tmp_path / "atp_2023.xlsx")
    joined, stats = odds.attach_odds(rows, "avg", directory=tmp_path)
    first, second, robin, final = joined
    assert (first["winner_odds"], first["loser_odds"]) == (1.5, 2.6)
    assert (second["winner_odds"], second["loser_odds"]) == (1.3, 3.5)
    assert not isinstance(robin, bm.OddsRow) and not isinstance(final, bm.OddsRow)
    assert stats["atp"]["matched"] == 2 and stats["atp"]["ambiguous"] == 2
    assert stats["conflicts"] == 0


def test_default_tour_params_keep_the_v2_dynamics_for_wta():
    """The WTA override did not confirm on 2024 and was dropped: only ATP is tuned."""
    assert set(model_module.DEFAULT_TOUR_PARAMS) == {"atp"}
    rows = synthetic_rows(seasons=(2019, 2020)) + synthetic_rows(
        seed=9, seasons=(2019, 2020), tour="wta"
    )
    rows.sort(key=lambda r: (r["date"], r["tour"], r["match_num"]))
    default = TennisModel(health_features=()).engine
    v2 = TennisModel(tour_params={}, health_features=()).engine
    for row in rows:
        default.update(row, row["date"].toordinal())
        v2.update(row, row["date"].toordinal())
    atp = [r for r in rows if r["tour"] == "atp"][-1]
    wta = [r for r in rows if r["tour"] == "wta"][-1]
    for row, same in ((wta, True), (atp, False)):
        winner, loser, match = model_module.row_sides(row, row["date"].toordinal())
        d_default, _ = default.features(winner, loser, match, store=False)
        d_v2, _ = v2.features(winner, loser, match, store=False)
        assert np.allclose(d_default, d_v2) is same


def test_benchmark_without_odds_is_untouched_and_with_odds_reports_the_join(tmp_path):
    rows = odds_rows()
    write_workbook(tmp_path)

    class Market:
        def predict(self, ctx):
            if ctx.first_odds is None:
                return 0.5
            return odds.fair_probability(ctx.first_odds, ctx.second_odds, "power")

        def update(self, row):
            pass

    plain = bm.run_benchmark(Market, [2023], ("atp",), 2023, rows=rows)
    assert "odds" not in plain["meta"]
    assert all(
        r["p"] == 0.5
        for r in bm.run_benchmark(Market, [2023], ("atp",), 2023, rows=rows, return_records=True)[
            "records"
        ]
    )
    priced = bm.run_benchmark(
        Market,
        [2023],
        ("atp",),
        2023,
        rows=rows,
        odds="avg",
        odds_dir=tmp_path,
        return_records=True,
    )
    assert priced["meta"]["odds"]["source"] == "avg"
    assert priced["meta"]["odds"]["groups"]["atp/2023"]["with_odds"] == 3
    assert all(r["p"] != 0.5 for r in priced["records"])
    with pytest.raises(ValueError):
        odds.attach_odds(rows, "bookie", directory=tmp_path)


# --------------------------------------------------------------------------- odds: model


def priced_rows(seed=7, players=30):
    """Synthetic seasons where the "market" knows the hidden strength better than the model."""
    rows = synthetic_rows(seed=seed, players=players, seasons=(2019, 2020, 2021, 2022))
    rng = random.Random(seed)
    # the same first draws as synthetic_rows: the true strengths behind the results
    strength = {f"P{i}": rng.gauss(0, 1.0) for i in range(players)}
    noise = random.Random(seed + 1)
    out = []
    for row in rows:
        gap = 1.5 * (strength[row["winner_key"]] - strength[row["loser_key"]])
        p = 1 / (1 + np.exp(-(gap + noise.gauss(0, 0.3))))
        p = min(0.9, max(0.1, p))  # both prices stay above 1.0 with the 6% margin
        out.append(bm.OddsRow(row._values, 1 / (1.06 * p), 1 / (1.06 * (1 - p))))
    return out


def test_model_blend_is_symmetric_and_pure_without_prices():
    rows = priced_rows()
    blended = small_model(odds_weights=(0.4, 0.6))
    pure = small_model()
    for row in rows:
        blended.update(row)
        pure.update(row)
    assert blended.market_weights("atp") == (0.4, 0.6)
    for row in rows[-40:]:
        ctx, _ = bm.make_context(row)
        p = blended.predict(ctx)
        assert abs(p + blended.predict(swap(ctx)) - 1) < 1e-12
        z_market = odds.logit(odds.fair_probability(ctx.first_odds, ctx.second_odds, "power"))
        z_model = odds.logit(pure.predict(ctx))
        assert abs(p - engine.sigmoid(0.4 * z_model + 0.6 * z_market)) < 1e-9
        stripped = bm.make_context(bm.Row(row._values))[0]
        assert blended.predict(stripped) == pure.predict(stripped)  # no price: pure model
    # the selection window followed the blended probability
    assert len(blended.windows["atp"]) > 0 and blended.thresholds["atp"] != pure.thresholds["atp"]


def test_online_blend_weights_come_from_earlier_seasons_only():
    rows = priced_rows()
    fitted = small_model(odds_weights="fit", odds_fit_min=100)
    seen = {}
    for row in rows:
        if row["season"] not in seen:
            seen[row["season"]] = dict(fitted.odds_coef)
        fitted.update(row)
    assert seen[2019] == {} and seen[2020] == {}  # nothing out of sample yet
    assert "atp" in seen[2022] and "all" in seen[2022]
    w_model, w_market = seen[2022]["atp"]
    assert w_market > w_model > -0.5  # the informed price dominates the blend
    # pairs stored for the fit are out-of-sample rows only (after the first coefficient fit)
    assert len(fitted.odds_pairs["atp"]) == sum(1 for r in rows if r["season"] >= 2020)
    fitted.finalize()  # refits on everything seen, then drops the pairs
    assert fitted.odds_pairs == {} and fitted.market_weights("atp") is not None
    with pytest.raises(ValueError):
        TennisModel(odds_weights="magic")


# --------------------------------------------------------------------------- health features


def test_approx_match_day_places_rounds_before_the_final():
    monday = date(2024, 3, 4).toordinal()
    sunday = engine.approx_match_day(monday, "F", "A", 32)
    assert date.fromordinal(int(sunday)).weekday() == 6 and sunday - monday == 6
    assert engine.approx_match_day(monday, "SF", "A", 32) == sunday - 1
    assert engine.approx_match_day(monday, "R32", "A", 32) >= monday
    assert engine.approx_match_day(monday, "Q1", "A", 32) == monday - 3
    assert engine.approx_match_day(monday, None, "A", 32) == monday  # app: real match day
    slam_final = engine.approx_match_day(monday, "F", "G", 128)
    assert slam_final - monday >= 13 and date.fromordinal(int(slam_final)).weekday() == 6


def health_engine():
    return engine.FeatureEngine(health_features="all")


def played(day, winner, loser, **fields):
    return make_row(
        date=day,
        winner_id=winner,
        loser_id=loser,
        tourney_id=fields.pop("tourney_id", str(day)),
        **fields,
    )


def test_health_state_tracks_retirements_walkovers_load_and_comebacks():
    eng = health_engine()
    d0 = date(2024, 1, 8)
    # A retires against B, then gives a walkover a week later
    eng.update(played(d0, "B", "A", score="6-3 2-1 RET"), d0.toordinal())
    d1 = d0 + timedelta(days=7)
    eng.update(played(d1, "C", "A", score="W/O", is_walkover=True), d1.toordinal())
    d2 = d1 + timedelta(days=7)
    names = eng.health
    row = engine.approx_match_day(d2.toordinal(), "R32", "A", 32)
    a = dict(
        zip(
            names,
            engine.health_vector(eng.health_state["A"], d2.toordinal(), row, ("m", "x"), names),
        )
    )
    b = dict(
        zip(
            names,
            engine.health_vector(eng.health_state["B"], d2.toordinal(), row, ("m", "x"), names),
        )
    )
    assert a["exit_14"] == 1.0 and a["exit_30"] == 1.0 and a["wo_60"] == 1.0
    assert b["exit_14"] == 0.0 and b["wo_60"] == 0.0
    # load: a long match earlier in the same event counts within 72 h, an old one does not
    eng2 = health_engine()
    d = date(2024, 5, 6)
    eng2.update(
        played(d, "P", "Q", round="R16", minutes=190, tourney_id="EV", score="7-6 6-7 7-6"),
        d.toordinal(),
    )
    now = engine.approx_match_day(d.toordinal(), "QF", "A", 32)
    p = dict(
        zip(
            names,
            engine.health_vector(eng2.health_state["P"], d.toordinal(), now, ("m", "EV"), names),
        )
    )
    assert p["load_72"] == 1.9 and p["prev_long"] == 1.0 and p["comeback"] == 0.0
    # comeback: 100 days away, then the first match back and the second one
    d3 = d + timedelta(days=100)
    first_back = dict(
        zip(
            names,
            engine.health_vector(
                eng2.health_state["P"], d3.toordinal(), float(d3.toordinal()), ("m", "Z"), names
            ),
        )
    )
    assert first_back["comeback"] == 1.0 and first_back["comeback_away"] > 0
    eng2.update(played(d3, "P", "R", tourney_id="Z"), d3.toordinal())
    second_back = dict(
        zip(
            names,
            engine.health_vector(
                eng2.health_state["P"], d3.toordinal() + 1, d3.toordinal() + 1.0, ("m", "Z"), names
            ),
        )
    )
    assert second_back["comeback"] == 0.5 and 0.9 < second_back["comeback_time"] <= 1.0
    with pytest.raises(ValueError):
        engine.FeatureEngine(health_features="comeback,nonsense")


def test_health_features_keep_antisymmetry_and_purity():
    rows = synthetic_rows(seasons=(2019, 2020))
    eng = engine.FeatureEngine(
        health_features=("comeback", "comeback_away", "comeback_time", "exit_14")
    )
    for row in rows[:-20]:
        eng.update(row, row["date"].toordinal())
    assert len(eng.d_names) == len(engine.FeatureEngine.D_NAMES) + 4
    states = len(eng.health_state)
    for row in rows[-20:]:
        winner, loser, match = model_module.row_sides(row, row["date"].toordinal())
        d, c = eng.features(winner, loser, match, store=False)
        d2, c2 = eng.features(loser, winner, match, store=False)
        assert len(d) == len(eng.d_names)
        assert np.allclose(np.array(d) + np.array(d2), 0.0) and c == c2
    assert len(eng.health_state) == states  # store=False never creates state


# --------------------------------------------------------------------------- per-tour params


def test_tour_params_change_one_group_only():
    rows = synthetic_rows(seasons=(2019, 2020)) + synthetic_rows(
        seed=9, seasons=(2019, 2020), tour="wta"
    )
    rows.sort(key=lambda r: (r["date"], r["tour"], r["match_num"]))
    base = engine.FeatureEngine()
    tuned = engine.FeatureEngine(tour_params={"wta": {"k_shape": 0.6, "form_decay": 0.8}})
    for row in rows:
        base.update(row, row["date"].toordinal())
        tuned.update(row, row["date"].toordinal())
    atp = [r for r in rows if r["tour"] == "atp"][-1]
    wta = [r for r in rows if r["tour"] == "wta"][-1]
    for row, same in ((atp, True), (wta, False)):
        winner, loser, match = model_module.row_sides(row, row["date"].toordinal())
        d_base, _ = base.features(winner, loser, match, store=False)
        d_tuned, _ = tuned.features(winner, loser, match, store=False)
        assert np.allclose(d_base, d_tuned) is same
    assert tuned.k_base == base.k_base  # non-overridden values stay
    with pytest.raises(ValueError):
        engine.FeatureEngine(tour_params={"itf": {"k_shape": 0.5}})
    with pytest.raises(ValueError):
        engine.FeatureEngine(tour_params={"wta": {"gravity": 9.8}})


# --------------------------------------------------------------------------- calibration


def test_calibrators_are_antisymmetric_monotone_and_recover_the_curve():
    rng = np.random.default_rng(2)
    z = rng.normal(0, 1.5, 30000)
    bent = 0.8 * z + 0.05 * z * np.abs(z)
    won = rng.random(30000) < 1 / (1 + np.exp(-bent))
    z_winner_first = np.where(won, 1.0, -1.0) * z
    a, b = calibration.fit("s2", z_winner_first)
    assert abs(a - 0.8) < 0.08 and abs(b - 0.05) < 0.04
    (temperature,) = calibration.fit("t", z_winner_first)
    assert 0.8 < temperature < 1.0
    for mode, params in (
        ("s2", (a, b)),
        ("t", (temperature,)),
        ("iso", calibration.fit("iso", z_winner_first)),
    ):
        grid = np.linspace(-6, 6, 121)
        out = calibration.apply(mode, params, grid)
        assert np.allclose(out, -out[::-1])  # antisymmetric
        assert np.all(np.diff(out) >= -1e-12)  # monotone
        assert calibration.apply(mode, params, 0.0) == 0.0
    flat = calibration.apply("s2", (1.0, -0.2), np.array([1.0, 2.5, 4.0]))
    assert flat[1] == flat[2] == 1.25  # flattened past the peak, never reversed
    assert calibration.apply("none", None, 1.7) == 1.7
    with pytest.raises(ValueError):
        TennisModel(calib_mode="spline")


def test_calibrated_model_changes_only_the_displayed_probability():
    rows = synthetic_rows()
    raw = small_model()
    calibrated = small_model(calib_mode="s2", calib_window=800, calib_step=50, calib_min=200)
    for row in rows:
        raw.update(row)
        calibrated.update(row)
    assert "atp" in calibrated.calib and calibrated.calib["atp"][0] > 0
    assert calibrated.thresholds == raw.thresholds  # the window keeps the raw confidence
    assert calibrated.windows["atp"][-1] == raw.windows["atp"][-1]
    raw_threshold = raw.threshold_for("atp")
    shown = calibrated.display_threshold("atp")
    assert (
        abs(shown - engine.sigmoid(calibrated.calibrate("atp", odds.logit(raw_threshold)))) < 1e-12
    )
    for row in rows[-60:]:
        ctx, _ = bm.make_context(row)
        p_raw, p_cal = raw.predict(ctx), calibrated.predict(ctx)
        assert abs(p_cal + calibrated.predict(swap(ctx)) - 1) < 1e-12
        assert (p_cal >= 0.5) == (p_raw >= 0.5)
        # selection is the same decision on both scales
        assert calibrated.select(ctx, p_cal) == raw.select(ctx, p_raw)


def test_high_precision_profile_is_stricter():
    rows = synthetic_rows()
    standard = small_model(select_target=0.75, select_target_high=0.85)
    strict = small_model(select_target=0.75, select_target_high=0.85, select_profile="high")
    for row in rows:
        standard.update(row)
        strict.update(row)
    assert standard.thresholds_high["atp"] >= standard.thresholds["atp"]
    assert strict.threshold_for("atp") == standard.threshold_for("atp")
    assert strict.display_threshold("atp", high=True) == standard.threshold_for("atp", high=True)
    chosen_standard = chosen_strict = 0
    for row in rows[-200:]:
        ctx, _ = bm.make_context(row)
        p = standard.predict(ctx)
        chosen_standard += standard.select(ctx, p)
        chosen_strict += strict.select(ctx, p)
        assert not (strict.select(ctx, p) and not standard.select(ctx, p))
    assert chosen_strict <= chosen_standard
    with pytest.raises(ValueError):
        TennisModel(select_profile="loose")


# --------------------------------------------------------------------------- predictor and app


def test_predictor_blends_the_market_and_keeps_pick_consistent():
    predictor = TennisPredictor(small_model(calib_mode="s2", calib_min=200, calib_step=50))
    predictor.fit_rows(synthetic_rows())
    when = date(2022, 1, 10)
    alone = predictor.predict("Player P0", "Player P1", "Hard", when=when, market_weights=None)
    blended = predictor.predict(
        "Player P0",
        "Player P1",
        "Hard",
        when=when,
        market_probability=0.05,
        market_weights=(0.25, 0.70),
    )
    assert alone.market_probability_1 is None and alone.probability_1 == alone.model_probability_1
    assert blended.model_probability_1 == alone.probability_1
    assert blended.probability_1 < alone.probability_1  # pulled towards the 5% price
    z_model = predictor._evaluate(
        "Player P0", "Player P1", "Hard", None, None, None, None, None, when, None
    )[0]
    assert (
        abs(alone.probability_1 - engine.sigmoid(predictor.model.calibrate("atp", z_model))) < 1e-12
    )
    # the blend acts on the raw model logit and the result is calibrated once
    expected = engine.sigmoid(
        predictor.model.calibrate("atp", 0.25 * z_model + 0.7 * odds.logit(0.05))
    )
    assert abs(blended.probability_1 - expected) < 1e-9
    assert blended.winner == ("Player P0" if blended.probability_1 >= 0.5 else "Player P1")
    body = blended.as_dict()
    assert {
        "decision_high",
        "threshold_high",
        "model_probability_1",
        "market_probability_1",
    } <= set(body)
    assert body["decision_high"] in {SELECTED, NO_BET}
    if body["decision"] == SELECTED:
        assert max(body["probability_1"], body["probability_2"]) >= body["threshold"] - 1e-4
    if body["decision_high"] == SELECTED:
        assert body["decision"] == SELECTED
    # decisions are never given for unknown players
    unknown = predictor.predict("Nobody X.", "Player P1", market_probability=0.9)
    assert unknown.decision == NO_BET and unknown.decision_high == NO_BET


def test_api_match_prices_enter_the_blend():
    from datetime import UTC, datetime

    from footypreds.domain import Match

    predictor = TennisPredictor(small_model())
    predictor.fit_rows(synthetic_rows())
    match = Match(
        id="m1",
        kickoff=datetime(2022, 1, 10, 12, tzinfo=UTC),
        league="ATP - SINGLES: Test (X), hard",
        home="Player P0",
        away="Player P1",
        status="scheduled",
        odds={"1": 1.5, "2": 2.7},
        source="test",
        sport="tennis",
    )
    payload = predictor.predict_api_match(match)
    assert payload["market_probability_1"] == round(model_module.market_probability_of(1.5, 2.7), 4)
    assert payload["known"] is True and payload["match_id"] == "m1"
    match_no_odds = match.model_copy(update={"odds": {}})
    assert predictor.predict_api_match(match_no_odds)["market_probability_1"] is None
    assert model_module.market_probability_of("x", 2.0) is None


def test_cache_key_includes_the_model_version_and_odds_files(tmp_path, monkeypatch):
    data = tmp_path / "tml"
    data.mkdir()
    (data / "2023.csv").write_text("x", encoding="utf-8")
    tours = ("atp",)
    plain = data_signature(data, 2023, 2023, tours, {})
    assert plain == data_signature(data, 2023, 2023, tours, {})
    assert plain != data_signature(data, 2023, 2023, tours, {"odds_weights": (0.25, 0.7)})
    odds_dir = tmp_path / "odds"
    odds_dir.mkdir()
    with_dir = data_signature(data, 2023, 2023, tours, {}, odds="avg", odds_dir=odds_dir)
    assert with_dir != plain
    write_workbook(odds_dir)
    assert data_signature(data, 2023, 2023, tours, {}, odds="avg", odds_dir=odds_dir) != with_dir
    monkeypatch.setattr(model_module, "VERSION", VERSION + "-next")
    assert data_signature(data, 2023, 2023, tours, {}) != plain
    assert CACHE_FILE == "model_v3.pkl" and VERSION.startswith("tenisPrediction-3")


def test_benchmark_reports_the_high_precision_rule_beside_select():
    rows = synthetic_rows()
    result = bm.run_benchmark(small_model, [2021], ("atp",), 2019, rows=rows, return_records=True)
    metrics = result["metrics"]["atp/2021"]
    assert result["meta"]["has_select_high"] is True
    assert (
        "select_high" in metrics
        and metrics["select_high"]["coverage"] <= metrics["select"]["coverage"]
    )
    assert all(not r["selected_high"] or r["selected"] for r in result["records"])
    assert "select_high" in bm.format_table(result["metrics"])
    plain = bm.run_benchmark(
        lambda: type("M", (), {"predict": lambda s, c: 0.6, "update": lambda s, r: None})(),
        [2021],
        ("atp",),
        2019,
        rows=rows,
    )
    assert (
        plain["meta"]["has_select_high"] is False
        and "select_high" not in plain["metrics"]["atp/2021"]
    )
