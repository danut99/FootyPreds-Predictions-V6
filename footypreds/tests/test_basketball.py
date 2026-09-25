"""Basketball analyzer: shape, distribution invariants, market blend, settlement, no leakage."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from footypreds.domain import Match
from footypreds.engine import HistoryIndex
from footypreds.provider import normalize_matches
from footypreds.sports import analyze_match, main_markets, validate_analysis
from footypreds.sports import basketball as bb
from footypreds.sports.keys import handicap, over, parse
from footypreds.sports.odds import parse_odds
from footypreds.sports.settle import is_settleable, settle

KICKOFF = datetime(2026, 6, 1, 18, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def game(match_id, when, home, away, hs, as_, league="USA: NBA", **extra):
    kickoff = when if isinstance(when, datetime) else KICKOFF - timedelta(days=when)
    return Match(
        id=match_id,
        kickoff=kickoff,
        league=league,
        home=home,
        away=away,
        status="finished",
        home_goals=hs,
        away_goals=as_,
        sport="basketball",
        **extra,
    )


def target(home="Alpha", away="Beta", league="USA: NBA", **extra):
    data = dict(id="t", kickoff=KICKOFF, league=league, home=home, away=away, sport="basketball")
    return Match(**(data | extra))


def league(days=240, seed=11, league_name="USA: NBA", teams=None, level=220.0):
    """Deterministic synthetic league: true offence/defence per team, home edge 3 points.

    Alpha at home to Beta: true margin 3 + (4 + 2) - (-3 - 2) = 14, total level + 1.
    """
    rng = random.Random(seed)
    teams = list(teams or ["Alpha", "Beta", "Gamma", "Delta", "Omega", "Sigma", "Kappa", "Zeta"])
    strength = {t: (rng.gauss(0, 4), rng.gauss(0, 4)) for t in teams}
    # (offence, defence) in points above average; a high defence value concedes more.
    strength["Alpha"], strength["Beta"] = (4.0, -2.0), (-3.0, 2.0)
    rows = []
    for day in range(2, days, 2):
        rng.shuffle(teams)
        for i in range(0, len(teams) - 1, 2):
            h, a = teams[i], teams[i + 1]
            (oh, dh), (oa, da) = strength[h], strength[a]
            margin = 3 + (oh + da) - (oa + dh) + rng.gauss(0, 12)
            total = level + oh + da + oa + dh + rng.gauss(0, 17)
            hs, as_ = round((total + margin) / 2), round((total - margin) / 2)
            if hs == as_:
                hs += 1
            rows.append(game(f"{league_name}-{day}-{i}", day, h, a, hs, as_, league_name))
    return rows


def by_key(analysis):
    return {m["key"]: m for m in analysis["markets"]}


def nbl():
    rows, _ = normalize_matches(load("h2h_basketball.json"), results=True, sport="basketball")
    listed, _ = normalize_matches(load("list_basketball.json"), sport="basketball")
    fixture_ = next(m for m in listed if m.id == "dU94shVA")
    return fixture_, rows


# ---------------------------------------------------------------- shape and real payloads


def test_real_flashscore_rows_give_a_valid_confident_analysis():
    fixture_, rows = nbl()
    assert (fixture_.home, fixture_.away) == ("Perth", "New Zealand Breakers")
    analysis = validate_analysis(analyze_match(fixture_, rows))
    assert analysis["sport"] == "basketball" and analysis["version"] == bb.VERSION
    assert analysis["grade"] in ("A", "B", "C")
    assert analysis["sample"]["home"] > 30 and analysis["sample"]["away"] > 30
    assert analysis["sample"]["h2h"] == analysis["h2h"]["played"] > 0
    assert analysis["expected"]["minutes"] == 40
    assert 150 < analysis["expected"]["total"] < 210
    home_form = analysis["form"]["home"]
    assert home_form["last"][0]["competition"] == "NBL" and len(home_form["sequence"]) == 5
    assert {"margin_avg5", "streak", "rest_days", "back_to_back"} <= set(home_form)
    assert "Perth" in analysis["summary"] and "puncte" in analysis["summary"]
    assert any("a câștigat" in note and "din ultimele" in note for note in analysis["insights"])
    assert any("media de puncte" in note for note in analysis["insights"])
    # Only 1/2 are quoted in the date list: they get a price and an EV, nothing else does.
    priced = {m["key"] for m in analysis["markets"] if m["odds"] is not None}
    assert priced == {"1", "2"}
    assert analysis["components"]["market_weight"] == pytest.approx(0.85)


def test_real_odds_payload_feeds_every_quoted_market():
    fixture_, rows = nbl()
    parsed = parse_odds(load("odds_basketball.json"), "basketball", "tM1zGJSk", "EZarEcc2")
    odds = {key: value["best"] for key, value in parsed.items()}
    analysis = validate_analysis(analyze_match(fixture_.model_copy(update={"odds": odds}), rows))
    markets = by_key(analysis)
    for key, price in odds.items():
        assert markets[key]["odds"] == price
        assert markets[key]["ev"] == pytest.approx(markets[key]["probability"] * price - 1)
    market = analysis["components"]["market"]
    assert market["handicap_pairs"] >= 10 and market["total_pairs"] >= 10
    assert market["total"] == pytest.approx(185.5, abs=1.5)
    assert market["margin"] == pytest.approx(6, abs=1.5)
    # Whole lines are reported conditional on no push: each pair still sums to 1.
    assert markets["ah_1_-6"]["probability"] + markets["ah_2_+6"]["probability"] == (
        pytest.approx(1)
    )
    assert markets["over_185"]["probability"] + markets["under_185"]["probability"] == (
        pytest.approx(1)
    )
    assert markets["odd"]["odds"] == 1.83 and markets["even"]["odds"] == 1.91


def test_no_data_and_no_odds_is_grade_d_with_a_default_level():
    analysis = validate_analysis(analyze_match(target(), []))
    assert analysis["grade"] == "D" and analysis["selection"] is None
    assert analysis["expected"]["total"] == pytest.approx(225, abs=6)  # NBA default level
    assert analysis["expected"]["margin"] > 0  # home court
    fiba = analyze_match(target(league="SPAIN: ACB"), [])
    assert fiba["expected"]["total"] == pytest.approx(bb.DEFAULT_TOTAL, abs=5)
    women = analyze_match(target(league="USA: WNBA"), [])
    assert women["expected"]["total"] < fiba["expected"]["total"]
    assert any("niciun rezultat" in note for note in analysis["insights"])


def test_strong_history_gives_a_confident_selection():
    analysis = validate_analysis(analyze_match(target(), league(), 0.6))
    assert analysis["grade"] in ("A", "B")
    markets = by_key(analysis)
    assert markets["1"]["probability"] > 0.75
    assert analysis["selection"] is not None and analysis["selection"]["selectable"]
    assert analysis["expected"]["home"] > analysis["expected"]["away"]
    ratings = analysis["components"]["model"]["ratings"]
    assert ratings["home"]["offence"] > ratings["away"]["offence"]
    assert ratings["home"]["pace"] == pytest.approx(
        ratings["home"]["offence"] + ratings["home"]["defence"]
    )
    keys = [m["key"] for m in main_markets(analysis)]
    assert keys[:2] == ["1", "2"] and keys[2].startswith("ah_") and keys[3].startswith("over_")


def test_ratings_recover_a_known_league():
    rows = league(days=400, seed=3)
    analysis = analyze_match(target(), rows)
    model = analysis["components"]["model"]
    assert model["base"]["home"] - model["base"]["away"] == pytest.approx(14, abs=3.5)
    assert model["total"] == pytest.approx(221, abs=6)
    assert 1.0 < model["home_factor"] < 1.06
    assert analysis["expected"]["margin_sd"] == pytest.approx(12, abs=2)
    assert analysis["expected"]["total_sd"] == pytest.approx(17, abs=3)


# ---------------------------------------------------------------- distribution invariants


@pytest.mark.parametrize("odds", [{}, {"1": 1.5, "2": 2.6, "over_214.5": 1.9, "under_214.5": 1.9}])
def test_probabilities_and_complements(odds):
    analysis = validate_analysis(analyze_match(target(odds=odds), league()))
    markets = by_key(analysis)
    for key, m in markets.items():
        assert 0 <= m["probability"] <= 1
        spec = parse(key)
        if spec and spec[0] == "total" and spec[1][0] == "over":
            assert m["probability"] + markets["under_" + spec[1][1]]["probability"] == (
                pytest.approx(1)
            )
        if spec and spec[0] == "handicap" and spec[1][0] == "1":
            other = handicap("2", -float(spec[1][1]))
            assert m["probability"] + markets[other]["probability"] == pytest.approx(1)
        if spec and spec[0] == "team_total" and spec[1][1] == "over":
            twin = f"{spec[1][0]}_under_{spec[1][2]}"
            assert m["probability"] + markets[twin]["probability"] == pytest.approx(1)
    assert markets["1"]["probability"] + markets["2"]["probability"] == pytest.approx(1)
    assert markets["odd"]["probability"] + markets["even"]["probability"] == pytest.approx(1)
    regulation = sum(markets[k]["probability"] for k in ("reg_1", "reg_X", "reg_2"))
    assert regulation == pytest.approx(1)
    assert sum(markets[k]["probability"] for k in ("ht_1", "ht_X", "ht_2")) == pytest.approx(1)
    # Overtime only adds wins: the winner beats the regulation-time win.
    assert markets["1"]["probability"] > markets["reg_1"]["probability"]
    assert 0.005 < markets["reg_X"]["probability"] < 0.1


def test_handicap_and_total_monotonicity():
    analysis = analyze_match(target(odds={"ah_1_-3": 1.9, "ah_2_+3": 1.9}), league())
    markets = analysis["markets"]
    home = sorted(
        (float(parse(m["key"])[1][1]), m["probability"])
        for m in markets
        if m["key"].startswith("ah_1_")
    )
    assert len(home) >= 7
    assert all(a[1] < b[1] for a, b in zip(home, home[1:], strict=False))
    overs = sorted(
        (float(m["key"][5:]), m["probability"]) for m in markets if m["key"].startswith("over_")
    )
    assert all(a[1] > b[1] for a, b in zip(overs, overs[1:], strict=False))
    home_team = sorted(
        (float(m["key"].rsplit("_", 1)[1]), m["probability"])
        for m in markets
        if m["key"].startswith("home_over_")
    )
    assert all(a[1] > b[1] for a, b in zip(home_team, home_team[1:], strict=False))


def test_symmetry_when_swapping_teams_on_a_neutral_court():
    rows = league(league_name="Olympic Games")
    first = analyze_match(target(league="Olympic Games"), rows)
    swapped = analyze_match(target(home="Beta", away="Alpha", league="Olympic Games"), rows)
    one, two = by_key(first), by_key(swapped)
    assert first["components"]["neutral"] is True
    assert one["1"]["probability"] == pytest.approx(two["2"]["probability"], abs=1e-9)
    assert first["expected"]["total"] == pytest.approx(swapped["expected"]["total"], abs=1e-9)
    assert first["expected"]["margin"] == pytest.approx(-swapped["expected"]["margin"], abs=1e-9)
    for key, market in one.items():
        spec = parse(key)
        if spec and spec[0] == "handicap":
            mirrored = handicap("2" if spec[1][0] == "1" else "1", float(spec[1][1]))
            if mirrored in two:
                assert market["probability"] == pytest.approx(
                    two[mirrored]["probability"], abs=1e-9
                )


def test_neutral_court_has_no_home_edge_without_data():
    neutral = analyze_match(target(league="ASIA: Asian Games Women - Play Offs"), [])
    assert neutral["components"]["neutral"] is True
    assert by_key(neutral)["1"]["probability"] == pytest.approx(0.5)
    qualifier = analyze_match(target(league="WORLD: World Cup - Qualification"), [])
    assert qualifier["components"]["neutral"] is False
    assert by_key(qualifier)["1"]["probability"] > 0.5


# ---------------------------------------------------------------- settlement consistency


def test_every_selectable_market_is_settleable_and_the_rest_are_not():
    analysis = analyze_match(target(odds={"1": 1.4, "2": 3.0}), league())
    for market in analysis["markets"]:
        assert market["selectable"] == is_settleable("basketball", market["key"]), market["key"]
    groups = {m["group"] for m in analysis["markets"] if not m["selectable"]}
    assert groups == {bb.REGULATION, bb.FIRST_HALF}


def test_margin_markets_match_settlement_on_the_final_margin_distribution():
    dist = bb.Distribution(4.3, 180.0, 12.0, 16.0, 40)
    final = {k: dist.margin_is(k) for k in range(-90, 91)}
    assert final[0] == pytest.approx(0) and sum(final.values()) == pytest.approx(1)
    markets = bb.build_markets(dist, {}, 92.0, 88.0)
    for market in markets:
        spec = parse(market["key"])
        if not market["selectable"] or spec[0] not in ("result", "handicap"):
            continue
        won = lost = 0.0
        for margin, p in final.items():
            outcome = settle("basketball", market["key"], 100 + margin, 100)
            won += p if outcome is True else 0.0
            lost += p if outcome is False else 0.0
        assert market["probability"] == pytest.approx(won / (won + lost), abs=1e-9), market["key"]


def test_total_markets_match_settlement_on_the_final_total_distribution():
    dist = bb.Distribution(-2.0, 171.3, 11.0, 15.0, 40)
    final = {s: dist.total_at_least(s) - dist.total_at_least(s + 1) for s in range(60, 320)}
    assert sum(final.values()) == pytest.approx(1, abs=1e-6)
    for line in (160.5, 171, 171.5, 185):
        win, push = dist.over(line)
        won = sum(p for s, p in final.items() if settle("basketball", over(line), s, 0))
        void = sum(p for s, p in final.items() if settle("basketball", over(line), s, 0) is None)
        assert win == pytest.approx(won, abs=1e-6) and push == pytest.approx(void, abs=1e-6)


def test_parity_follows_the_final_margin():
    dist = bb.Distribution(3.0, 170.0, 12.0, 16.0, 40)
    expected, odd = dist.summary()
    assert expected == pytest.approx(3.0, abs=0.3)
    assert odd == pytest.approx(sum(dist.margin_is(k) for k in range(-120, 121) if k % 2), abs=1e-9)
    assert 0.45 < odd < 0.55


def test_overtime_rows_are_fitted_as_tied_regulation():
    row = game("ot", 3, "A", "B", 110, 112, league="AUSTRALIA: NBL", finish_type="aet")
    home, away = bb.regulation_score(row)
    assert home == away == pytest.approx(222 * 40 / 45 / 2)
    nba = game("ot2", 3, "A", "B", 120, 118, finish_type="aet")
    assert bb.regulation_score(nba)[0] == pytest.approx(238 * 48 / 53 / 2)
    assert bb.regulation_score(game("r", 3, "A", "B", 99, 90)) == (99.0, 90.0)
    assert bb.game_minutes("USA: NBA") == bb.game_minutes("NBA G League") == 48
    assert bb.game_minutes("USA: WNBA") == bb.game_minutes("AUSTRALIA: NBL") == 40


# ---------------------------------------------------------------- market blend


def test_without_history_the_market_is_used_as_is():
    odds = {"1": 1.43, "2": 2.92, "over_185.5": 1.87, "under_185.5": 1.93}
    analysis = analyze_match(target(league="AUSTRALIA: NBL", odds=odds), [])
    markets = by_key(analysis)
    fair = (1 / 1.43) / (1 / 1.43 + 1 / 2.92)
    assert markets["1"]["probability"] == pytest.approx(fair, abs=1e-6)
    over_fair = (1 / 1.87) / (1 / 1.87 + 1 / 1.93)
    assert markets["over_185.5"]["probability"] == pytest.approx(over_fair, abs=1e-6)
    assert analysis["components"]["market_weight"] == 1.0


def test_market_blend_weight_limits():
    rows = league()
    model_only = analyze_match(target(), rows)
    odds = {"1": 2.6, "2": 1.55, "over_200.5": 1.9, "under_200.5": 1.9}
    blended = analyze_match(target(odds=odds), rows)
    market_only = analyze_match(target(odds=odds), [])
    weight = blended["components"]["market_weight"]
    assert weight == pytest.approx(bb.PARAMS.market_weight)
    model = blended["components"]["model"]
    market = blended["components"]["market"]
    regulation = blended["components"]["regulation"]
    assert regulation["margin"] == pytest.approx(
        (1 - weight) * model["margin"] + weight * market["margin"]
    )
    assert regulation["total"] == pytest.approx(
        (1 - weight) * model["total"] + weight * market["total"]
    )
    p = [by_key(a)["1"]["probability"] for a in (model_only, blended, market_only)]
    assert p[2] < p[1] < p[0]
    # The market dominates: the blend is much closer to it than to the model.
    assert abs(p[1] - p[2]) < abs(p[1] - p[0])
    full_market = analyze_match(target(odds=odds), rows, params=bb.with_params(market_weight=1))
    assert by_key(full_market)["1"]["probability"] == pytest.approx(p[2], abs=1e-6)


def test_thin_data_moves_the_weight_towards_the_market():
    rows = [game("a1", 150, "Alpha", "Gamma", 110, 100), game("b1", 150, "Gamma", "Beta", 105, 99)]
    odds = {"1": 1.8, "2": 2.0}
    thin = analyze_match(target(odds=odds), rows)
    assert 0.85 < thin["components"]["market_weight"] <= 1.0
    assert thin["grade"] == "D"


def test_implausible_books_are_ignored():
    odds = {"1": 1.2, "2": 1.2, "over_180.5": 3.0, "under_180.5": 3.0}
    analysis = analyze_match(target(odds=odds), [])
    market = analysis["components"]["market"]
    assert market["winner"] is None and market["margin"] is None and market["total"] is None


# ---------------------------------------------------------------- leakage and determinism


def test_results_at_or_after_the_cutoff_never_change_the_output():
    rows = league()
    cutoff = KICKOFF - timedelta(hours=3)
    late = []
    for i, (home, away) in enumerate([("Alpha", "Gamma"), ("Delta", "Beta"), ("Gamma", "Zeta")]):
        for minutes in (0, 30, 200):
            late.append(
                game(f"late{i}-{minutes}", cutoff + timedelta(minutes=minutes), home, away, 150, 60)
            )
    late.append(game("after", KICKOFF + timedelta(days=1), "Alpha", "Beta", 60, 150))
    odds = {"1": 1.6, "2": 2.4}
    clean = analyze_match(target(odds=odds), rows)
    leaked = analyze_match(target(odds=odds), rows + late)
    assert leaked == clean


def test_same_fixture_result_is_never_used():
    rows = league()
    fixture_ = target()
    own = game("t", 0.2, "Alpha", "Beta", 50, 140)
    assert analyze_match(fixture_, rows + [own]) == analyze_match(fixture_, rows)


def test_deterministic_and_index_equivalent():
    rows = league()
    first = analyze_match(target(), rows)
    assert analyze_match(target(), list(reversed(rows))) == first
    assert analyze_match(target(), HistoryIndex(rows)) == first
    json.dumps(first)  # JSON-ready (no NaN/inf/objects)


# ---------------------------------------------------------------- populations, form, rest


def test_women_youth_and_namesakes_never_mix():
    rows = league()
    base = analyze_match(target(home_id="alpha"), rows)
    noise = [game(f"w{i}", 5 + i, "Alpha", f"X{i}", 40, 120, league="USA: WNBA") for i in range(10)]
    noise += [
        game(f"y{i}", 5 + i, "Alpha", f"Y{i}", 40, 120, league="EUROPE: EuroLeague U18")
        for i in range(10)
    ]
    noise += [
        game(f"n{i}", 5 + i, "Alpha", f"Z{i}", 40, 120, league="USA: NBA", home_id="other")
        for i in range(10)
    ]
    mixed = analyze_match(target(home_id="alpha"), rows + noise)
    assert mixed["markets"] == base["markets"] and mixed["sample"] == base["sample"]
    women = analyze_match(target(league="USA: WNBA"), rows + noise)
    assert women["sample"]["home"] == 10 and women["components"]["population"]["women"]


def test_back_to_back_costs_the_tired_side():
    rows = league()
    yesterday = [game("b2b", KICKOFF - timedelta(hours=26), "Alpha", "Gamma", 110, 104)]
    rested = analyze_match(target(), rows)
    tired = analyze_match(target(), rows + yesterday)
    assert tired["form"]["home"]["back_to_back"] is True
    assert rested["form"]["home"]["back_to_back"] is False
    assert tired["components"]["model"]["adjustments"]["rest"] == pytest.approx(-1.5)
    assert any("back-to-back" in note for note in tired["insights"])
    away_tired = [game("b2b2", KICKOFF - timedelta(hours=20), "Delta", "Beta", 100, 104)]
    other = analyze_match(target(), rows + away_tired)
    assert other["components"]["model"]["adjustments"]["rest"] == pytest.approx(1.5)


def test_form_streaks_and_h2h_insights():
    rows = [game(f"s{i}", 2 + 2 * i, "Alpha", f"O{i}", 100, 90) for i in range(6)]
    rows += [game(f"h{i}", 30 + 10 * i, "Beta", "Alpha", 80, 95) for i in range(3)]
    analysis = validate_analysis(analyze_match(target(), rows))
    form = analysis["form"]["home"]
    assert form["streak"] == {"result": "W", "count": 9}
    assert form["margin_avg5"] == pytest.approx(10)
    assert analysis["h2h"]["played"] == 3 and analysis["h2h"]["home_wins"] == 3
    notes = " ".join(analysis["insights"])
    assert "serie de 9 victorii consecutive" in notes and "Meciuri directe: 3-0" in notes


def test_fit_points_shrinks_small_samples():
    rows = [("A", "B", 110.0, 90.0, 1.0, False)]
    fit = bb.fit_points(rows)
    assert 1.0 < fit.attack["A"] < 110 / 100 and fit.defence["B"] > 1.0
    assert bb.fit_points([]) is None
    heavy = bb.fit_points(rows * 40)
    assert heavy.attack["A"] > fit.attack["A"]


def test_complement_never_wins_the_closest_to_even_tie():
    for i in range(1, 2000):
        p = i / 2001 + 1e-7 * (i % 7)
        other = bb.complement(p)
        assert abs(other - 0.5) >= abs(p - 0.5)
        assert other == pytest.approx(1 - p, abs=1e-15)


@pytest.mark.parametrize("seed", range(6))
def test_main_markets_headline_is_stable(seed):
    analysis = analyze_match(target(), league(seed=seed))
    keys = [m["key"] for m in main_markets(analysis)]
    assert keys[2].startswith("ah_1_") and keys[3].startswith("over_")


def test_walk_forward_calibration_on_a_synthetic_league():
    """Blind walk-forward on a known league: close to the true-probability log loss."""
    import math
    from statistics import NormalDist

    rng = random.Random(5)
    teams = [f"T{i}" for i in range(12)]
    strength = {t: (rng.gauss(0, 3), rng.gauss(0, 3)) for t in teams}
    rows, truth = [], {}
    for day in range(240, 0, -1):
        rng.shuffle(teams)
        for i in range(0, 6, 2):
            h, a = teams[i], teams[i + 1]
            mean = 3 + strength[h][0] + strength[a][1] - strength[a][0] - strength[h][1]
            margin, total = mean + rng.gauss(0, 12), 170 + rng.gauss(0, 15)
            hs, as_ = round((total + margin) / 2), round((total - margin) / 2)
            hs += hs == as_
            match = game(f"c{day}-{i}", day, h, a, hs, as_, league="SPAIN: ACB")
            rows.append(match)
            truth[match.id] = 1 - NormalDist(mean, 12).cdf(0)
    index = HistoryIndex(rows)
    model = oracle = 0.0
    tested = [m for m in rows if m.kickoff >= KICKOFF - timedelta(days=30)]
    for match in tested:
        hidden = {"id": "x" + match.id, "status": "scheduled", "home_goals": None}
        blind = match.model_copy(update=hidden | {"away_goals": None})
        p = by_key(bb.analyze(blind, index))["1"]["probability"]
        won = match.home_goals > match.away_goals
        model -= math.log(p if won else 1 - p)
        oracle -= math.log(truth[match.id] if won else 1 - truth[match.id])
    assert len(tested) >= 80
    assert model / len(tested) < oracle / len(tested) + 0.03
    assert model / len(tested) < math.log(2)
