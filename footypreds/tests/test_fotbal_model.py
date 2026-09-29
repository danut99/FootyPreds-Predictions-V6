"""fotbalPrediction: modelul de producție, regula de selecție, predictorul și numele FlashScore.

Totul rulează pe rânduri sintetice mici; nimic nu se descarcă și nu se citește din footypreds/data.
"""

import dataclasses
import math
import pickle
from datetime import date, timedelta

import pytest

from fotbalPrediction import benchmark as bench
from fotbalPrediction import data, names
from fotbalPrediction import markets as mk
from fotbalPrediction import model as fm
from fotbalPrediction.tune_rule import derive, passes

from .fotbal_helpers import synthetic_rows, tiny_predictor, write_rule


def test_power_demargin_removes_the_margin_and_keeps_the_favourite_longshot_shape():
    probs = fm.demargin([1.5, 4.2, 7.0])
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-12)
    proportional = fm.demargin([1.5, 4.2, 7.0], "proportional")
    # power trims the longshot more than proportional does
    assert probs[2] < proportional[2] and probs[0] > proportional[0]
    assert fm.demargin([1.5, None, 7.0]) is None
    assert fm.demargin([1.2, 1.3, 1.4]) is None  # implausible book (sum 2.3)
    fair = fm.fair_prices({"1": 2.0, "X": 3.4, "2": 3.8, "over25": 1.9, "under25": 1.95, "btts": 2})
    assert set(fair) == {"1", "X", "2", "over25", "under25"}
    assert math.isclose(sum(1 / fair[k] for k in ("1", "X", "2")), 1.0, abs_tol=1e-9)


def test_frozen_rule_file_is_derived_from_tune_seasons_only():
    rule = fm.load_rule()
    assert rule["derived_on"] == ["2223", "2324"]
    assert rule["select"] and set(rule["select_high"]) <= set(mk.CATALOGUE)
    for key in rule["select"] + rule["select_high"]:
        assert mk.CATALOGUE[key].stat in fm.GOAL_STATS and mk.CATALOGUE[key].selectable
    assert "cs_other" not in rule["select"]


def test_rule_criteria_reject_small_overconfident_or_unstable_keys():
    good = {
        "n": 200,
        "accuracy": 0.84,
        "mean_p": 0.85,
        "seasons": {"a": {"n": 100, "accuracy": 0.83}},
    }
    assert passes(good, 0.80)
    assert not passes({**good, "n": 10}, 0.80)
    assert not passes({**good, "mean_p": 0.90}, 0.80)
    unstable = {**good, "seasons": {"a": {"n": 60, "accuracy": 0.75}}}
    assert not passes(unstable, 0.80)
    assert not passes(good, 0.85)


def run(rows, seasons=("2223",), **params):
    return bench.run_benchmark(
        lambda: fm.FootballModel(**params),
        seasons,
        rows=rows,
        first_season="2122",
        leagues=("E0", "E1"),
        return_records=True,
    )


def test_model_covers_every_stat_and_selects_one_key_per_group(tmp_path):
    rule = write_rule(tmp_path / "rule.json")
    result = run(synthetic_rows(), rule_path=rule)
    records = result["records"]
    keys = {records.keys[i] for i in set(records.key)}
    for stat in ("goals", "ht_goals", "corners", "cards", "bookings", "sot"):
        assert any(mk.CATALOGUE[k].stat == stat for k in keys), stat
    picked: dict[tuple[int, str], int] = {}
    for i in range(len(records.p)):
        key = records.keys[records.key[i]]
        market = mk.CATALOGUE[key]
        if records.sel[i] and market.stat in fm.GOAL_STATS:
            assert key in {"1X", "X2", "over15", "under35", "home_over05"}
            assert 0.80 - 1e-9 <= records.p[i] <= fm.MAX_P
            slot = (records.match[i], market.group)
            picked[slot] = picked.get(slot, 0) + 1
        if records.sel_high[i] and market.stat in fm.GOAL_STATS:
            assert key == "1X" and records.p[i] >= 0.85 - 1e-9
    assert picked and max(picked.values()) == 1


def test_model_never_uses_same_day_or_later_results(tmp_path):
    rule = write_rule(tmp_path / "rule.json")
    rows = synthetic_rows()
    cut = date(2022, 10, 1)
    changed = [
        dataclasses.replace(
            r,
            home_goals=9,
            away_goals=0,
            ht_home_goals=5,
            home_corners=20,
            home_yellow=8,
            home_sot=25,
        )
        if r.date >= cut
        else r
        for r in rows
    ]
    first = run(rows, rule_path=rule)["records"]
    second = run(changed, rule_path=rule)["records"]
    early = [i for i, m in enumerate(first.match) if date.fromisoformat(first.matches[m][2]) <= cut]
    assert early
    for i in early:
        assert first.p[i] == second.p[i] and first.sel[i] == second.sel[i]
    later = [i for i, m in enumerate(first.match) if date.fromisoformat(first.matches[m][2]) > cut]
    assert any(first.p[i] != second.p[i] for i in later)


def test_odds_are_demargined_and_pooled_with_weight_point_nine(tmp_path):
    rule = write_rule(tmp_path / "rule.json")
    rows = synthetic_rows()
    model = fm.FootballModel(rule_path=rule)
    for row in rows:
        model.update(row)
    last = rows[-1]
    ctx = bench.make_context(dataclasses.replace(last, date=last.date + timedelta(days=7)), None)
    plain = model.predict(ctx)
    prices = {"1": 1.25, "X": 6.0, "2": 12.0}
    priced = model.predict(dataclasses.replace(ctx, odds=prices, odds_source="avg"))
    market = fm.demargin([prices[k] for k in ("1", "X", "2")])
    assert abs(priced["1"] - market[0]) < abs(plain["1"] - market[0])
    assert abs(priced["1"] - market[0]) < 0.05
    # a book with an implausible margin is ignored
    junk = model.predict(dataclasses.replace(ctx, odds={"1": 1.1, "X": 1.2, "2": 1.3}))
    assert junk["1"] == pytest.approx(plain["1"])


def test_derive_rule_from_records_keeps_only_keys_that_pass_in_every_variant():
    rows = synthetic_rows(weeks=90)
    runs = {}
    for odds in (None, "avg"):
        runs[odds or "none"] = bench.run_benchmark(
            lambda: fm.FootballModel(rule="derive", with_counts=False, min_team_matches=3),
            ("2223",),
            rows=rows,
            first_season="2122",
            leagues=("E0", "E1"),
            markets="goals,ht_goals",
            odds=odds,
            return_records=True,
        )["records"]
    rule = derive(runs)
    assert set(rule["select_high"]) <= set(mk.CATALOGUE)
    for key in rule["select"]:
        for stats in rule["stats"]["select"].values():
            assert passes(stats[key], 0.80)


def test_settle_uses_the_score_for_goals_and_the_football_data_row_for_stats():
    row = synthetic_rows(weeks=1)[0]
    score = (row.home_goals, row.away_goals)
    assert fm.settle("1X", score) is (score[0] >= score[1])
    assert fm.settle("corners_over_7.5", score) is None
    total = row.home_corners + row.away_corners
    assert fm.settle("corners_over_7.5", score, row) is (total > 7.5)
    assert fm.settle("ht_over05", score, row) is (row.ht_home_goals + row.ht_away_goals > 0)
    assert fm.settle("ah_1_0", (1, 1)) is None  # push
    assert fm.settle("ah_1_-0.25", (2, 1)) is True
    assert fm.settle("ah_1_-0.25", (1, 1)) is False  # half lost


def test_predictor_resolves_flashscore_names_and_predicts_every_market(tmp_path):
    rule = write_rule(tmp_path / "rule.json")
    predictor = tiny_predictor(rule_path=rule)
    resolved = predictor.resolve("ENGLAND: Premier League", "England", "Manchester Utd", "Arsenal")
    assert resolved == {"code": "E0", "home": "Man United", "away": "Arsenal", "reason": None}
    assert (
        predictor.resolve("ENGLAND: Premier League 2", "", "Arsenal U21", "Chelsea U21")["reason"]
        == "ligă fără model"
    )
    missing = predictor.resolve("ENGLAND: Premier League", "", "Nowhere Rovers", "Arsenal")
    assert missing["home"] is None and "Nowhere Rovers" in missing["reason"]
    day = predictor.trained_through + timedelta(days=5)
    full = predictor.predict(
        "E0", "Man United", "Arsenal", day, {"1": 2.1, "X": 3.4, "2": 3.5}, full=True
    )
    keys = {m["key"] for m in full["markets"]}
    assert {"1", "X", "2", "1X", "over25", "ht_1", "corners_over_9.5", "cards_over_3.5"} <= keys
    assert full["odds_blend"] is True and full["retro"] is False
    for market in full["markets"]:
        assert market["decision"] in (fm.SELECT, fm.NO_BET)
        assert market["fair_odds"] == pytest.approx(1 / market["probability"], rel=1e-3)
        if market["decision"] == fm.SELECT:
            assert market["selectable"] and 0.80 - 1e-9 <= market["probability"] <= fm.MAX_P
    one_x_two = [m["probability"] for m in full["markets"] if m["group"] == "1x2"]
    assert sum(one_x_two) == pytest.approx(1.0, abs=1e-6)
    shown = predictor.predict("E0", "Man United", "Arsenal", day)["markets"]
    assert len(shown) < len(full["markets"]) and {"1", "X", "2"} <= {m["key"] for m in shown}
    retro = predictor.predict("E0", "Man United", "Arsenal", predictor.trained_through)
    assert retro["retro"] is True
    first = predictor.model.goals._sample
    assert not any(k.startswith("E0-") for k in first)  # production state is forgotten


def test_predictor_pickles_and_finds_rows_within_one_day(tmp_path):
    rule = write_rule(tmp_path / "rule.json")
    rows = synthetic_rows(weeks=30)
    predictor = tiny_predictor(rows, rule_path=rule)
    clone = pickle.loads(pickle.dumps(predictor))
    last = rows[-1]
    assert clone.find_row(last.league, last.date, last.home, last.away) == last
    assert clone.find_row(last.league, last.date + timedelta(days=1), last.home, last.away) == last
    assert clone.find_row(last.league, last.date + timedelta(days=2), last.home, last.away) is None
    assert clone.find_row(last.league, last.date, last.away, last.home) is None
    day = clone.trained_through
    assert clone.predict("E0", "Arsenal", "Chelsea", day)["markets"]


def test_load_or_train_caches_by_version_params_and_data_files(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    (raw / "main" / "2526").mkdir(parents=True)
    csv = raw / "main" / "2526" / "E0.csv"
    csv.write_text("Div,Date\n", encoding="utf-8")
    calls = []

    def fake_rows(leagues, first, last, raw_dir, cache_dir):
        calls.append((first, last))
        return synthetic_rows(weeks=25)

    monkeypatch.setattr(fm.data, "load_rows", fake_rows)
    store = tmp_path / "store"
    first = fm.FootballPredictor.load_or_train(raw_dir=raw, cache_dir=None, store_dir=store)
    again = fm.FootballPredictor.load_or_train(raw_dir=raw, cache_dir=None, store_dir=store)
    assert len(calls) == 1 and again.key == first.key
    assert calls[0] == (data.FIRST_SEASON, data.RUNNING_SEASON)
    csv.write_text("Div,Date,HomeTeam\n", encoding="utf-8")
    third = fm.FootballPredictor.load_or_train(raw_dir=raw, cache_dir=None, store_dir=store)
    assert len(calls) == 2 and third.key != first.key
    assert [p.name for p in store.glob("model-*.pkl")] == [f"model-{third.key}.pkl"]


def test_load_or_train_without_data_explains_how_to_download(tmp_path, monkeypatch):
    monkeypatch.setattr(fm.data, "load_rows", lambda *a, **k: [])
    with pytest.raises(FileNotFoundError, match="--download"):
        fm.FootballPredictor.load_or_train(raw_dir=tmp_path, cache_dir=None, store_dir=tmp_path)


def test_league_codes_follow_flashscore_names_and_reject_other_competitions():
    assert names.league_code("ENGLAND: Premier League") == "E0"
    assert names.league_code("SCOTLAND: Premiership - Relegation Group") == "SC0"
    assert names.league_code("SPAIN: LaLiga2") == "SP2"
    assert names.league_code("GERMANY: 2. Bundesliga") == "D2"
    assert names.league_code("PORTUGAL: Liga Portugal") == "P1"
    assert names.league_code("Premier League", "England") == "E0"
    assert names.league_code("ENGLAND: Premier League 2") is None
    assert names.league_code("ENGLAND: FA Cup") is None
    assert names.league_code("WORLD: Club Friendly") is None


def test_team_resolver_uses_overrides_then_exact_then_fuzzy_and_never_guesses():
    teams = {"E0": ["Man United", "Man City", "Nott'm Forest", "Arsenal"], "E1": ["Leeds United"]}
    resolver = names.TeamResolver(
        teams,
        {"E0": "Anglia", "E1": "Anglia"},
        {"E0": {names.normalize_team("Nottingham"): "Nott'm Forest"}},
    )
    assert resolver.resolve("E0", "Nottingham") == "Nott'm Forest"
    assert resolver.resolve("E0", "Manchester United") == "Man United"
    assert resolver.resolve("E0", "Arsenal FC") == "Arsenal"
    assert resolver.resolve("E0", "Leeds") == "Leeds United"  # promoted: same country
    assert resolver.resolve("E0", "Manchester") is None  # ambiguous: City or United
    assert resolver.resolve("E0", "Arsenal U21") is None
    assert resolver.resolve("E0", "Arsenal W") is None
    assert resolver.resolve(None, "Arsenal") is None
    assert resolver.search("man")[0] == {"team": "Man City", "league": "E0"}


def test_overrides_file_is_valid_and_points_to_known_leagues():
    overrides = names.load_overrides()
    assert set(overrides) <= set(data.MAIN_LEAGUES)
    assert overrides["E0"][names.normalize_team("Manchester Utd")] == "Man United"
    assert overrides["N1"][names.normalize_team("Fortuna Sittard")] == "For Sittard"
    assert overrides["G1"][names.normalize_team("AEL Larissa")] == "Larisa"


def test_cache_key_covers_the_model_source_code(tmp_path, monkeypatch):
    rule = write_rule(tmp_path / "rule.json")
    assert set(fm.MODEL_SOURCES) >= {"model.py", "candidates/goals_model.py"}
    before = fm.cache_key({}, tmp_path, rule)
    assert fm.predictor_key(tmp_path) == fm.cache_key({"first_season": data.FIRST_SEASON}, tmp_path)
    monkeypatch.setattr(fm, "code_signature", lambda: "edited")
    assert fm.cache_key({}, tmp_path, rule) != before
