"""Extended football markets: DNB, Asian handicaps, other totals, team totals, parity, correct
score and HT/FT, all from the same score matrix and settled like sports.settle."""

import json
import math
from pathlib import Path

import pytest

from footypreds.engine import analyze
from footypreds.engine import markets as mk
from footypreds.sports import validate_analysis
from footypreds.sports.keys import parse
from footypreds.sports.odds import best_prices, parse_odds
from footypreds.sports.settle import is_settleable, settle
from footypreds.tests.helpers import fixture, league_history

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
GENERIC = [
    "dnb_1",
    "dnb_2",
    "ah_1_-1.5",
    "ah_1_-1",
    "ah_1_0",
    "ah_1_+0.5",
    "ah_1_+2",
    "ah_2_-2.5",
    "ah_2_+1",
    "ah_2_0",
    "over_3",
    "under_3",
    "over_5.5",
    "under_0.5",
    "home_over_2.5",
    "home_under_1",
    "away_over_0.5",
    "away_under_3.5",
    "odd",
    "even",
    "cs_0-0",
    "cs_2-1",
    "cs_4-4",
]


def real_prices():
    payload = json.loads((FIXTURES / "odds_football.json").read_text(encoding="utf-8"))
    return best_prices(parse_odds(payload, "football", "QsL3TXzh", "CbJBRB54"))


@pytest.mark.parametrize("key", GENERIC)
def test_predicate_settles_every_score_exactly_like_sports_settle(key):
    decide = mk.predicate(key)
    assert decide is not None
    for h in range(9):
        for a in range(9):
            assert decide(h, a) == settle("football", key, h, a), (key, h, a)


@pytest.mark.parametrize("key", ["ah_1_-0.25", "over_2.75", "sets_2-0", "games_over_20.5", "x"])
def test_predicate_refuses_quarter_lines_and_foreign_keys(key):
    assert mk.predicate(key) is None and mk.win_push(mk.score_matrix(1.4, 1.1), key) is None


def test_extended_probabilities_agree_with_the_legacy_markets():
    matrix = mk.score_matrix(1.6, 1.1, -0.12)
    ft = mk.full_time(matrix)
    one, draw, two = ft["1"], ft["X"], ft["2"]

    def win(key):
        return mk.win_push(matrix, key)[0]

    assert win("ah_1_-0.5") == pytest.approx(one)
    assert win("ah_1_+0.5") == pytest.approx(ft["1X"])
    assert win("ah_2_+0.5") == pytest.approx(ft["X2"])
    assert win("over_2.5") == pytest.approx(ft["over25"])
    assert win("home_over_0.5") == pytest.approx(ft["home_over05"])
    assert win("odd") + win("even") == pytest.approx(1)
    assert win("cs_1-0") == pytest.approx(matrix[1][0])
    won, push = mk.win_push(matrix, "dnb_1")
    assert (won, push) == (pytest.approx(one), pytest.approx(draw))
    assert mk.win_push(matrix, "ah_1_0") == pytest.approx((one, draw))
    over3, push3 = mk.win_push(matrix, "over_3")
    assert push3 == pytest.approx(sum(p for h, a, p in mk.cells(matrix) if h + a == 3))
    under3, _ = mk.win_push(matrix, "under_3")
    assert over3 + under3 + push3 == pytest.approx(1)
    rows = {key: p for key, _, _, p, _ in mk.extended(matrix, {})}
    # Probabilities of refundable bets are conditional on the bet being decided.
    assert rows["dnb_1"] == pytest.approx(one / (one + two))
    assert rows["dnb_1"] + rows["dnb_2"] == pytest.approx(1)


def test_extended_keys_are_the_defaults_plus_every_quoted_generic_key():
    keys = mk.extended_keys({"over_3": 2.0, "over25": 1.9, "ht_1": 3.0, "1/1": 4.0})
    assert set(mk.EXTENDED_DEFAULT) <= set(keys) and "over_3" in keys
    assert not {"over25", "ht_1", "1/1"} & set(keys)
    # Legacy spelling wins: a generic alias never duplicates a legacy market.
    assert "over_2.5" not in mk.extended_keys({"over_2.5": 1.9})
    assert "ah_1_-0.25" not in mk.extended_keys({"ah_1_-0.25": 1.9})
    # Grouped by family (DNB, handicap, totals, team totals, parity, correct score).
    ordered = mk.extended_keys(real_prices())
    families = [mk.EXTENDED_ORDER.index(parse(k)[0]) for k in ordered]
    assert families == sorted(families) and ordered.index("ah_1_-1.5") < ordered.index("ah_1_+1")


def test_analysis_with_real_prices_offers_priced_extended_markets():
    prices = real_prices()
    history = league_history()
    # 1X2 (blend) and over/under 2.5 (goals pool) are the only prices the model reads.
    model_keys = ("1", "X", "2", "over25", "under25")
    assert all(key in prices for key in model_keys)
    plain = analyze(fixture(odds={k: prices[k] for k in model_keys}), history)
    rich = validate_analysis(analyze(fixture(odds=prices), history))
    by_key = {m["key"]: m for m in rich["markets"]}
    # Extra prices never move the football model: every shared market is identical.
    for market in plain["markets"]:
        assert by_key[market["key"]]["probability"] == market["probability"]
    assert rich["selection"] == plain["selection"] or rich["selection"]["key"] in mk.SELECTABLE
    for key in ("dnb_1", "ah_1_-1.5", "ah_2_+1", "over_3", "over_5.5", "odd", "cs_2-1"):
        market = by_key[key]
        assert market["odds"] == prices[key] and market["selectable"], key
        assert market["ev"] == pytest.approx(market["probability"] * prices[key] - 1)
        assert market["fair_odds"] == pytest.approx(1 / market["probability"])
    assert by_key["over_3"]["push"] > 0 and "push" not in by_key["over_5.5"]
    assert by_key["dnb_1"]["group"] == "Egal = pariu anulat"
    assert by_key["ah_1_-1.5"]["label"] == "Handicap gazde -1.5"
    # Half-time and HT/FT prices are shown, but a final score cannot settle them.
    for key in ("ht_1", "ht_over05", "1/1", "X/2"):
        assert by_key[key]["odds"] == prices[key] and not by_key[key]["selectable"]
    # Legacy extras become bettable only with a price; the ledger set is always selectable.
    assert by_key["over05"]["selectable"] and not by_key["home_win_nil"]["selectable"]
    for market in rich["markets"]:
        if market["selectable"]:
            assert is_settleable("football", market["key"]), market["key"]
        if market["key"] in mk.SELECTABLE:
            assert market["selectable"]


def test_unpriced_extended_markets_are_shown_but_not_selectable():
    analysis = validate_analysis(analyze(fixture(odds={"1": 1.5, "X": 4.0, "2": 6.5}), []))
    extended = [m for m in analysis["markets"] if m["key"] in mk.EXTENDED_DEFAULT]
    assert {m["key"] for m in extended} == set(mk.EXTENDED_DEFAULT)
    assert not any(m["selectable"] for m in extended)
    assert {m["key"] for m in analysis["markets"] if m["selectable"]} == set(mk.SELECTABLE)
    htft = [m for m in analysis["markets"] if m["group"] == mk.HTFT_GROUP]
    assert len(htft) == 9 and sum(m["probability"] for m in htft) == pytest.approx(1)


def test_ledger_selection_ignores_extended_markets():
    # A near-certain priced handicap must not become the ledger pick.
    prices = {"1": 1.25, "X": 6.0, "2": 12.0, "ah_1_+2.5": 1.01, "over05": 1.03}
    analysis = analyze(fixture(odds=prices), league_history(), 0.5)
    by_key = {m["key"]: m for m in analysis["markets"]}
    assert by_key["ah_1_+2.5"]["selectable"] and by_key["ah_1_+2.5"]["probability"] > 0.95
    assert analysis["selection"] is None or analysis["selection"]["key"] in mk.SELECTABLE
    best = max(
        (m for m in analysis["markets"] if m["key"] in mk.SELECTABLE),
        key=lambda m: m["probability"],
    )
    if analysis["selection"] is not None:
        assert analysis["selection"]["key"] == best["key"]


def test_every_market_probability_is_a_probability():
    analysis = analyze(fixture(odds=real_prices()), league_history())
    keys = [m["key"] for m in analysis["markets"]]
    assert len(keys) == len(set(keys))
    for market in analysis["markets"]:
        assert 0 <= market["probability"] <= 1 and math.isfinite(market["probability"])
