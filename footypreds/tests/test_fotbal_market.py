"""fotbalPrediction.market_model / market_eval: estimarea din cote, fără rețea și date reale."""

import json
from datetime import date

import pytest

from fotbalPrediction import market_eval, market_model
from fotbalPrediction import markets as mk
from fotbalPrediction.data import MatchRow
from fotbalPrediction.model import NO_BET, SELECT, demargin

ODDS = {"1": 1.8, "X": 3.6, "2": 4.6}


def test_probabilities_reproduce_the_market_and_stay_coherent():
    probs = market_model.goal_probabilities(ODDS)
    fair = demargin([ODDS["1"], ODDS["X"], ODDS["2"]])
    for key, target in zip(("1", "X", "2"), fair):
        assert probs[key] == pytest.approx(target, abs=1e-6)
    assert probs["1X"] == pytest.approx(probs["1"] + probs["X"], abs=1e-6)
    assert probs["over25"] + probs["under25"] == pytest.approx(1.0, abs=1e-6)
    assert probs["over05"] > probs["over15"] > probs["over25"] > probs["over35"]
    assert probs["home_over05"] > probs["away_over05"]  # the favourite scores more often
    assert all(mk.CATALOGUE[key].stat == "goals" for key in probs)


def test_swapping_the_sides_mirrors_every_market():
    probs = market_model.goal_probabilities(ODDS)
    mirrored = market_model.goal_probabilities({"1": ODDS["2"], "X": ODDS["X"], "2": ODDS["1"]})
    assert mirrored["2"] == pytest.approx(probs["1"], abs=1e-6)
    assert mirrored["away_over05"] == pytest.approx(probs["home_over05"], abs=1e-6)
    assert mirrored["over25"] == pytest.approx(probs["over25"], abs=1e-6)
    assert mirrored["X2"] == pytest.approx(probs["1X"], abs=1e-6)


def test_a_likelier_draw_means_fewer_goals():
    tight_odds = {"1": 2.9, "X": 2.7, "2": 2.9}
    open_odds = {"1": 2.45, "X": 4.2, "2": 2.45}
    tight = market_model.goal_probabilities(tight_odds)
    open_game = market_model.goal_probabilities(open_odds)
    assert tight["over25"] < open_game["over25"]
    assert tight["btts"] < open_game["btts"]
    # total_weight 0 ignores the draw and uses the base total for both
    flat = [
        market_model.goal_probabilities(odds, total_weight=0.0)["over25"]
        for odds in (tight_odds, open_odds)
    ]
    assert abs(flat[0] - flat[1]) < abs(tight["over25"] - open_game["over25"])


@pytest.mark.parametrize(
    "odds",
    [None, {}, {"1": 1.8, "X": 3.6}, {"1": 1.8, "X": 3.6, "2": 0.9}, {"1": 9, "X": 9, "2": 9}],
)
def test_no_estimate_without_a_valid_book(odds):
    assert market_model.goal_probabilities(odds) is None
    assert market_model.predict(odds) is None


def test_choose_takes_one_allowed_key_per_group_with_the_longest_odds():
    probs = {"1X": 0.86, "12": 0.82, "X2": 0.5, "over05": 0.95, "over15": 0.81, "under45": 0.9}
    allow = {"1X", "12", "over05", "over15"}
    assert market_model.choose(probs, 0.80, allow) == {"12", "over15"}
    assert market_model.choose(probs, 0.85, allow) == {"1X"}  # over05 is above the 0.93 cap
    assert market_model.choose(probs, 0.80, set()) == set()


def test_predict_marks_decisions_from_the_frozen_rule(tmp_path):
    rule = tmp_path / "rule.json"
    rule.write_text(json.dumps({"select": ["1X", "over15"], "select_high": ["1X"]}))
    favourite = {"1": 1.5, "X": 4.4, "2": 6.5}
    result = market_model.predict(favourite, rule)
    keys = {m["key"]: m for m in result["markets"]}
    assert 0.80 <= keys["1X"]["probability"] <= 0.93
    assert keys["1"]["odds"] == 1.5 and keys["1X"]["odds"] is None
    assert keys["1X"]["decision"] == SELECT and keys["1X"]["selectable"] is True
    expected = SELECT if keys["1X"]["probability"] >= 0.85 else NO_BET
    assert keys["1X"]["decision_high"] == expected
    assert keys["12"]["decision"] == NO_BET and keys["12"]["selectable"] is False
    empty = market_model.predict(ODDS, tmp_path / "missing.json")
    assert all(m["decision"] == NO_BET for m in empty["markets"])


def test_shipped_rule_only_lists_selectable_goal_keys():
    rule = market_model.load_rule()
    assert rule["select"] and set(rule["select_high"]) <= set(mk.KEYS)
    for key in rule["select"] + rule["select_high"]:
        assert mk.CATALOGUE[key].stat == "goals" and mk.CATALOGUE[key].selectable
    assert rule["derived_on"] == ["2223", "2324"]


def test_eval_helpers():
    book = {"1": 2.0, "X": 4.0, "2": 4.0}
    assert market_eval.real_price("1", book) == 2.0
    assert market_eval.real_price("1X", book) == pytest.approx(1 / (1 / 2.0 + 1 / 4.0))
    assert market_eval.real_price("over15", book) is None
    row = MatchRow("ROU", "2324", date(2024, 3, 1), "A", "B", 2, 1, odds={"avg_closing": book})
    assert market_eval.book_of(row, ("b365", "avg_closing")) == book
    assert market_eval.book_of(row, ("b365",)) is None
    stats = market_eval.band_stats([(row, {"1": 1.25, "X": 6.0, "2": 11.0})], 0.80)
    assert all(entry["n"] == 1 for entry in stats.values()) and "1X" in stats
    assert stats["1X"]["accuracy"] == 1.0
    season = {"2223": {"n": 25, "accuracy": 0.84}}
    good = {"n": 40, "accuracy": 0.85, "mean_p": 0.86, "seasons": season}
    assert market_eval.passes(good, 0.80)
    assert not market_eval.passes(good | {"n": 10}, 0.80)
    assert not market_eval.passes(good | {"mean_p": 0.9}, 0.80)
    weak_season = good | {"seasons": {"2223": {"n": 25, "accuracy": 0.7}}}
    assert not market_eval.passes(weak_season, 0.80)
