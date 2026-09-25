"""Bankroll simulator: blind walk-forward, anti-leakage, staking arithmetic, cache, determinism."""

import itertools
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from footypreds import simulator as sim
from footypreds.domain import Match
from footypreds.evaluation.sim_datasets import Dataset, _football_dataset

TEAMS = {"Alpha": 2.1, "Beta": 1.7, "Gamma": 1.35, "Delta": 1.1, "Epsilon": 0.85, "Zeta": 0.7}


def poisson(rng, lam):
    """Deterministic Poisson draw (Knuth) from a seeded generator."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def pmf(lam, k):
    return math.exp(-lam) * lam**k / math.factorial(k)


def prices(lam_home, lam_away, margin=1.05):
    grid = [(h, a, pmf(lam_home, h) * pmf(lam_away, a)) for h in range(11) for a in range(11)]
    home = sum(p for h, a, p in grid if h > a)
    draw = sum(p for h, a, p in grid if h == a)
    away = sum(p for h, a, p in grid if h < a)
    over = sum(p for h, a, p in grid if h + a > 2)
    probabilities = {"1": home, "X": draw, "2": away, "over25": over, "under25": 1 - over}
    return {k: round(1 / (p * margin), 2) for k, p in probabilities.items()}


def season_records(start, weeks, season, seed, code="T1", league="Test League"):
    """Six-team double round robin, one round per week, deterministic scores and odds."""
    rng = random.Random(seed)
    names = list(TEAMS)
    records = []
    for week in range(weeks):
        day = datetime.combine(start + timedelta(days=7 * week), datetime.min.time(), timezone.utc)
        rest = names[1:]
        shift = week % len(rest)
        order = [names[0]] + rest[shift:] + rest[:shift]
        for i in range(3):
            home, away = order[i], order[5 - i]
            if week % 2:
                home, away = away, home
            lam_home = 1.45 * TEAMS[home] / TEAMS[away] ** 0.6
            lam_away = 1.1 * TEAMS[away] / TEAMS[home] ** 0.6
            match = Match(
                id=f"fd-{code}-{day.date()}-{home}-{away}",
                kickoff=day,
                league=league,
                home=home,
                away=away,
                status="finished",
                home_goals=min(9, poisson(rng, lam_home)),
                away_goals=min(9, poisson(rng, lam_away)),
                source="football-data.co.uk",
            )
            records.append(
                {
                    "match": match.model_dump(mode="json"),
                    "season": season,
                    "league_code": code,
                    "reference_odds": prices(lam_home, lam_away),
                }
            )
    return records


def football_records():
    return season_records(date(2023, 8, 5), 40, "2324", 1) + season_records(
        date(2024, 8, 3), 40, "2425", 2
    )


def football_dataset(records=None):
    return _football_dataset("football", "Test", records or football_records(), "")


def poisoned(records, after, swap=True):
    """Copy of `records` whose results on or after `after` are changed (scores swapped/+3)."""
    output = []
    for record in records:
        match = dict(record["match"])
        if match["kickoff"][:10] >= after.isoformat():
            h, a = match["home_goals"], match["away_goals"]
            match["home_goals"], match["away_goals"] = (a, h + 3) if swap else (h + 3, a)
        output.append(record | {"match": match})
    return output


SEASON = (date(2024, 8, 3), date(2025, 5, 3))


@pytest.fixture
def dataset():
    return football_dataset()


def run_predictions(data, start=SEASON[0], end=SEASON[1], cache_dir=None):
    return sim.predictions(data, start, end, cache_dir, workers=1)[0]


# --- blind prediction phase ---------------------------------------------------------------


def test_prediction_input_never_contains_the_target_score_or_same_day_results(dataset, monkeypatch):
    seen = []
    real = sim.analyze

    def spy(fixture, index, threshold, **kw):
        assert fixture.home_goals is None and fixture.away_goals is None
        assert fixture.status == "scheduled" and fixture.finish_type == "" and not fixture.live
        assert fixture.id not in index.ids
        assert all(m.kickoff.date() < fixture.kickoff.date() for m in index.rows)
        seen.append(fixture.id)
        return real(fixture, index, threshold, **kw)

    monkeypatch.setattr(sim, "analyze", spy)
    days = run_predictions(dataset)
    assert len(seen) == sum(len(rows) for rows in days.values()) > 50


def test_prediction_rows_carry_no_result_fields(dataset):
    days = run_predictions(dataset, SEASON[0], SEASON[0] + timedelta(days=30))
    rows = [row for day in days.values() for row in day]
    assert rows
    for row in rows:
        text = repr(row)
        assert "home_goals" not in text and "away_goals" not in text and "score" not in row
        assert set(row["odds"]) == {"1", "X", "2"}
        assert all(m["odds"] > 1 for m in row["markets"])


def test_future_results_never_change_earlier_predictions():
    clean = football_dataset()
    cut = date(2025, 1, 4)
    dirty = football_dataset(poisoned(football_records(), cut))
    before = {d: r for d, r in run_predictions(clean).items() if d < cut.isoformat()}
    after = {d: r for d, r in run_predictions(dirty).items() if d < cut.isoformat()}
    assert before and before == after


def test_same_day_results_including_the_target_never_change_that_days_predictions():
    clean = football_dataset()
    day = date(2024, 11, 2)
    only_day = [
        r
        | {
            "match": r["match"]
            | {"home_goals": r["match"]["away_goals"] + 4, "away_goals": r["match"]["home_goals"]}
        }
        if r["match"]["kickoff"][:10] == day.isoformat()
        else r
        for r in football_records()
    ]
    dirty = football_dataset(only_day)
    assert run_predictions(clean, day, day) == run_predictions(dirty, day, day)


def test_poisoned_future_never_changes_earlier_bets_or_bankroll():
    cut = date(2025, 1, 4)
    clean = sim.simulate(
        football_dataset(),
        bankroll=500,
        strategy="singles",
        stake=10,
        workers=1,
        cache_dir=None,
        start=SEASON[0],
        end=SEASON[1],
    )
    dirty = sim.simulate(
        football_dataset(poisoned(football_records(), cut)),
        bankroll=500,
        strategy="singles",
        stake=10,
        workers=1,
        cache_dir=None,
        start=SEASON[0],
        end=SEASON[1],
    )

    def early(result):
        return [r for r in result["rows"] if r["date"] < cut.isoformat()]

    assert early(clean) and early(clean) == early(dirty)
    early_history = [h for h in clean["history"] if h["date"] < cut.isoformat()]
    assert early_history == [h for h in dirty["history"] if h["date"] < cut.isoformat()]
    assert clean["rows"] != dirty["rows"]  # the poisoned results do matter afterwards


def test_choosers_never_receive_results(dataset, monkeypatch):
    """The strategy sees prediction rows only; results are looked up after the stakes."""
    calls = []
    real = sim.choose_singles

    def spy(rows, count, rules):
        for row in rows:
            assert not any(k in row for k in ("home_goals", "away_goals", "status", "score"))
        calls.append(len(rows))
        return real(rows, count, rules)

    monkeypatch.setattr(sim, "choose_singles", spy)
    sim.simulate(
        dataset,
        strategy="singles",
        stake=5,
        workers=1,
        cache_dir=None,
        start=SEASON[0],
        end=SEASON[0] + timedelta(days=60),
    )
    assert calls


def test_units_restart_ratings_per_period_like_the_benchmark(dataset):
    units = sim.units(dataset, *SEASON)
    assert [(u["group"], u["period"]) for u in units] == [("T1", "2425")]
    unit = units[0]
    assert unit["population"][-1].kickoff.date() <= SEASON[1]
    assert unit["start"] == SEASON[0]
    assert len(unit["targets"]) == 120


# --- cache and determinism ----------------------------------------------------------------


def test_disk_cache_is_reused_and_invalidated_by_data_and_code(dataset, tmp_path, monkeypatch):
    first = sim.predictions(dataset, *SEASON, tmp_path, workers=1)
    assert first[1]["computed"] == 1 and list((tmp_path / "football").glob("*.json"))
    again = sim.predictions(dataset, *SEASON, tmp_path, workers=1)
    assert again[1]["computed"] == 0 and again[0] == first[0]
    changed = football_dataset(poisoned(football_records(), date(2025, 4, 1)))
    assert sim.predictions(changed, *SEASON, tmp_path, workers=1)[1]["computed"] == 1
    # An earlier season is untouched by a change in a later one.
    early = (date(2023, 8, 5), date(2024, 5, 1))
    sim.predictions(dataset, *early, tmp_path, workers=1)
    assert sim.predictions(changed, *early, tmp_path, workers=1)[1]["computed"] == 0
    monkeypatch.setattr(sim, "code_hash", lambda sport="football": "other-code")
    assert sim.predictions(dataset, *SEASON, tmp_path, workers=1)[1]["computed"] == 1
    assert len(list((tmp_path / "football").glob("T1-2425-*.json"))) == 1


def test_corrupt_cache_file_is_recomputed(dataset, tmp_path):
    rows, _ = sim.predictions(dataset, *SEASON, tmp_path, workers=1)
    for path in (tmp_path / "football").glob("*.json"):
        path.write_text("{broken", encoding="utf-8")
    again, stats = sim.predictions(dataset, *SEASON, tmp_path, workers=1)
    assert stats["computed"] == 1 and again == rows


def test_simulation_is_deterministic(dataset, tmp_path):
    kwargs = dict(
        bankroll=1000,
        strategy="ticket",
        target_odds=2,
        stake=10,
        workers=1,
        start=SEASON[0],
        end=SEASON[1],
    )
    one = sim.simulate(dataset, cache_dir=None, **kwargs)
    two = sim.simulate(football_dataset(), cache_dir=tmp_path, **kwargs)
    three = sim.simulate(football_dataset(), cache_dir=tmp_path, **kwargs)
    for key in ("final", "rows", "history", "baseline", "summary"):
        assert one[key] == two[key] == three[key]


# --- strategies ---------------------------------------------------------------------------


def row(match_id, markets, grade="B", odds=None, sport="football"):
    return {
        "id": match_id,
        "day": "2025-01-01",
        "kickoff": "2025-01-01T00:00:00+00:00",
        "sport": sport,
        "league": "L",
        "home": f"H{match_id}",
        "away": f"A{match_id}",
        "grade": grade,
        "confidence": 60,
        "odds": odds or {"1": 1.5, "X": 4.0, "2": 6.0},
        "markets": [
            {"key": k, "label": k, "group": "g", "probability": p, "fair_odds": 1 / p, "odds": o}
            for k, p, o in markets
        ],
    }


RULES = sim.local_rules()


def test_singles_pick_the_safest_leg_per_match_and_skip_grade_d():
    rows = [
        row("a", [("1", 0.70, 1.5), ("over25", 0.72, 1.4)]),
        row("b", [("1", 0.80, 1.3)]),
        row("c", [("1", 0.95, 1.25)], grade="D"),
        row("d", [("2", 0.90, 1.05)]),  # below the odds band
        row("e", [("X", 0.50, 1.5)]),  # clearly negative value: 0.75 < 0.95
    ]
    bets = sim.choose_singles(rows, 5, RULES)
    assert [(b["legs"][0]["match_id"], b["legs"][0]["key"]) for b in bets] == [
        ("b", "1"),
        ("a", "over25"),
    ]
    assert sim.choose_singles(rows, 1, RULES)[0]["legs"][0]["match_id"] == "b"


def test_value_strategy_keeps_positive_expected_value_only():
    rows = [
        row("a", [("1", 0.60, 1.9), ("2", 0.30, 3.0)]),
        row("b", [("1", 0.55, 1.7)]),
        row("c", [("2", 0.20, 6.0)]),  # EV positive but too unlikely
    ]
    bets = sim.choose_value(rows, 3, RULES)
    assert [(b["legs"][0]["match_id"], b["legs"][0]["key"]) for b in bets] == [("a", "1")]


def brute_force(legs, target, window, max_legs):
    best = None
    by_match = {}
    for item in legs:
        by_match.setdefault(item["match_id"], []).append(item)
    groups = list(by_match.values())
    for size in range(1, max_legs + 1):
        for chosen_groups in itertools.combinations(groups, size):
            for combo in itertools.product(*chosen_groups):
                total = math.prod(x["odds"] for x in combo)
                if window[0] * target <= total <= window[1] * target:
                    p = math.prod(x["probability"] for x in combo)
                    if best is None or p > best + 1e-12:
                        best = p
    return best


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("target", [2.0, 5.0])
def test_local_optimizer_matches_brute_force(seed, target):
    rng = random.Random(seed)
    legs = []
    for m in range(6):
        for k in range(2):
            odds = round(rng.uniform(1.15, 3.2), 2)
            legs.append(
                {
                    "match_id": f"m{m}",
                    "key": f"k{k}",
                    "odds": odds,
                    "probability": min(0.97, 1 / odds * rng.uniform(0.9, 1.1)),
                    "kickoff": "2025-01-01T00:00:00+00:00",
                    "home": f"h{m}",
                    "away": f"a{m}",
                }
            )
    chosen = sim.best_combination(legs, target, max_legs=4)
    expected = brute_force(legs, target, sim.TICKET_RANGE, 4)
    if expected is None:
        assert chosen == []
        return
    assert len({x["match_id"] for x in chosen}) == len(chosen)
    total = math.prod(x["odds"] for x in chosen)
    assert sim.TICKET_RANGE[0] * target - 1e-9 <= total <= sim.TICKET_RANGE[1] * target + 1e-9
    assert math.prod(x["probability"] for x in chosen) == pytest.approx(expected, rel=0.02)


def test_ticket_uses_the_recommendation_rules_when_available():
    rules = sim.product_rules()
    pytest.importorskip("footypreds.recommend")
    from footypreds import recommend

    assert rules.source == "recommend"
    assert rules.leg_odds == tuple(recommend.LEG_ODDS)
    assert rules.window == tuple(recommend.ODDS_WINDOW)
    rows = [row(str(i), [("1", 0.72, 1.42), ("over25", 0.6, 1.62)]) for i in range(4)]
    bets = sim.choose_ticket(rows, 2.0, rules)
    assert len(bets) == 1
    total = math.prod(x["odds"] for x in bets[0]["legs"])
    assert rules.window[0] * 2 <= total <= rules.window[1] * 2
    assert len({x["match_id"] for x in bets[0]["legs"]}) == len(bets[0]["legs"])


def test_baseline_bets_the_bookmaker_favourite_with_margin_free_probability():
    rows = [
        row("a", [], odds={"1": 1.6, "X": 4.0, "2": 5.5}),
        row("b", [], odds={"1": 3.2, "X": 3.4, "2": 2.2}),
    ]
    bets = sim.choose_baseline(rows, "singles", 5, None, RULES)
    legs = [b["legs"][0] for b in bets]
    assert [(x["match_id"], x["key"]) for x in legs] == [("a", "1"), ("b", "2")]
    total = 1 / 1.6 + 1 / 4.0 + 1 / 5.5
    assert legs[0]["probability"] == pytest.approx((1 / 1.6) / total)
    tennis = row("t", [], odds={"1": 1.3, "2": 3.6}, sport="tennis")
    assert sim.favourite_leg(tennis, RULES)["label"] == "Victorie jucătorul 1"


# --- staking and settlement ---------------------------------------------------------------


def bet(*legs):
    return {
        "legs": [
            {
                "match_id": m,
                "key": k,
                "odds": o,
                "probability": p,
                "home": m,
                "away": "x",
                "competition": "c",
                "label": k,
            }
            for m, k, o, p in legs
        ]
    }


def test_flat_percent_and_kelly_stakes():
    bets = [bet(("a", "1", 2.0, 0.6)), bet(("b", "1", 2.0, 0.6))]
    assert sim.stakes_for(bets, 1000, "flat", 10) == [10, 10]
    assert sim.stakes_for(bets, 15, "flat", 10) == [7.5, 7.5]  # never above the bankroll
    assert sim.stakes_for(bets, 500, "percent", 0.02) == [10, 10]
    # Full Kelly f* = (0.6 * 2 - 1) / (2 - 1) = 0.2; half Kelly = 0.1 = the default cap.
    assert sim.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    assert sim.stakes_for(bets[:1], 1000, "kelly", 0.5) == [100]
    assert sim.stakes_for(bets[:1], 1000, "kelly", 0.5, kelly_cap=0.05) == [50]
    assert sim.stakes_for([bet(("a", "1", 1.5, 0.6))], 1000, "kelly", 1) == [0.0]  # no edge
    assert sim.stakes_for(bets, 0.01, "flat", 10) == [0.0, 0.0]


class Result:
    def __init__(self, h, a, status="finished", finish_type="", sport="football"):
        self.home_goals, self.away_goals = h, a
        self.status, self.finish_type, self.sport = status, finish_type, sport


def test_bankroll_arithmetic_wins_losses_and_voids():
    days = {"2025-01-01": ["d1"], "2025-01-02": ["d2"], "2025-01-03": ["d3"]}
    plan = {
        "d1": [bet(("a", "1", 2.0, 0.6)), bet(("b", "1", 3.0, 0.4))],  # +10, -10
        "d2": [bet(("c", "over_3", 1.9, 0.6))],  # push on a whole line -> void, refunded
        "d3": [bet(("d", "1", 2.0, 0.6), ("e", "1", 1.5, 0.7))],  # ticket with a void leg
    }
    results = {
        "a": Result(2, 0),
        "b": Result(0, 1),
        "c": Result(2, 1),
        "d": Result(1, 0),
        "e": Result(1, 1, "unavailable"),
    }
    run = sim.run_bankroll(
        days, results, lambda rows: plan[rows[0]], bankroll=100, staking="flat", stake=10
    )
    assert [r["result"] for r in run["rows"]] == ["won", "lost", "void", "won"]
    assert [r["payout"] for r in run["rows"]] == [20, 0, 10, 20]
    assert [h["bankroll"] for h in run["history"]] == [100, 100, 110]
    assert (run["won"], run["lost"], run["void"], run["bets"]) == (2, 1, 1, 4)
    assert run["final"] == 110 and run["profit"] == 10 and run["staked"] == 40
    assert run["roi"] == pytest.approx(0.25) and run["hit_rate"] == pytest.approx(2 / 3)
    assert run["rows"][3]["legs"][1]["status"] == "void"
    assert run["rows"][0]["bankroll_before"] == 100


def test_tennis_retirement_voids_every_leg():
    legs, status, multiplier = sim.settle_bet(
        {"legs": [{"match_id": "t", "key": "1", "odds": 1.4}]},
        {"t": Result(0, 1, "finished", "retired", "tennis")},
    )
    assert status == "void" and multiplier == 1.0 and legs[0]["status"] == "void"


def test_bankroll_never_goes_negative_and_stops_when_broke():
    days = {f"2025-01-0{i}": [i] for i in range(1, 6)}
    results = {str(i): Result(0, 1) for i in range(1, 6)}
    run = sim.run_bankroll(
        days,
        results,
        lambda rows: [bet((str(rows[0]), "1", 2.0, 0.6))],
        bankroll=25,
        staking="flat",
        stake=10,
    )
    assert [r["stake"] for r in run["rows"]] == [10, 10, 5]
    assert run["final"] == 0 and run["stopped"] == "2025-01-04"
    assert all(h["bankroll"] >= 0 for h in run["history"])
    assert run["max_drawdown"] == pytest.approx(1.0)
    assert run["longest_losing_streak"] == 3


def test_max_drawdown_and_losing_streak():
    outcomes = {"1": Result(1, 0), "2": Result(0, 1), "3": Result(0, 1), "4": Result(1, 0)}
    days = {f"2025-02-0{i}": [str(i)] for i in range(1, 5)}
    run = sim.run_bankroll(
        days,
        outcomes,
        lambda rows: [bet((rows[0], "1", 2.0, 0.6))],
        bankroll=100,
        staking="flat",
        stake=50,
    )
    assert [h["bankroll"] for h in run["history"]] == [150, 100, 50, 100]
    assert run["peak"] == 150 and run["max_drawdown"] == pytest.approx(100 / 150)
    assert run["longest_losing_streak"] == 2


# --- request validation and output --------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bankroll": 0}, "Suma inițială"),
        ({"bankroll": -5}, "Suma inițială"),
        ({"strategy": "martingale"}, "Strategie necunoscută"),
        ({"strategy": "ticket"}, "cota țintă"),
        ({"strategy": "ticket", "target_odds": 500}, "Cota țintă"),
        ({"strategy": "percent", "stake": 0.5}, "Procentul"),
        ({"strategy": "kelly", "stake": 3}, "Fracția Kelly"),
        ({"strategy": "flat", "stake": 5000}, "Miza fixă"),
        ({"start": date(2020, 1, 1)}, "Intervalul trebuie"),
        ({"start": date(2025, 3, 1), "end": date(2025, 2, 1)}, "Data de început"),
        ({"max_bets_per_day": 50}, "pariuri pe zi"),
        ({"staking": "double"}, "Miza trebuie"),
    ],
)
def test_invalid_requests_raise_romanian_errors(dataset, kwargs, message):
    with pytest.raises(sim.SimulationError, match=message):
        sim.simulate(dataset, cache_dir=None, workers=1, **kwargs)


def test_contract_and_long_strategy_forms():
    assert sim.resolve_strategy("flat") == ("singles", "flat")
    assert sim.resolve_strategy("kelly", target_odds=5) == ("ticket", "kelly")
    assert sim.resolve_strategy("value", staking="percent") == ("value", "percent")
    assert sim.resolve_strategy("ticket") == ("ticket", "flat")


def test_full_simulation_output_shape(dataset):
    result = sim.simulate(
        dataset,
        bankroll=1000,
        strategy="flat",
        stake=20,
        target_odds=2,
        cache_dir=None,
        workers=1,
        start=SEASON[0],
        end=SEASON[1],
    )
    for key in (
        "dataset",
        "sport",
        "start",
        "end",
        "initial",
        "final",
        "profit",
        "staked",
        "roi",
        "bets",
        "won",
        "lost",
        "void",
        "hit_rate",
        "max_drawdown",
        "peak",
        "history",
        "rows",
        "method",
        "warning",
        "summary",
        "equity",
        "baseline",
        "warnings",
        "rules",
    ):
        assert key in result, key
    assert result["mode"] == "ticket" and result["staking"] == "flat"
    assert result["bets"] == result["won"] + result["lost"] + result["void"] > 0
    assert result["final"] == pytest.approx(result["initial"] + result["profit"])
    summary = result["summary"]
    assert summary["start"] == 1000 and summary["final"] == result["final"]
    assert result["history"] == result["equity"]
    for bet_row in result["rows"]:
        assert set(bet_row) >= {
            "date",
            "match_id",
            "home",
            "away",
            "competition",
            "key",
            "label",
            "probability",
            "odds",
            "stake",
            "result",
            "payout",
            "bankroll",
            "legs",
            "return",
            "bankroll_after",
        }
        assert bet_row["result"] in ("won", "lost", "void")
        assert all(leg["status"] in ("won", "lost", "void") for leg in bet_row["legs"])
        assert sim.TICKET_RANGE[0] * 2 - 0.2 <= bet_row["odds"] <= 2.5
    assert "Walk-forward orb" in result["method"]
    assert any("independente" in w for w in result["warnings"])
    assert result["baseline"]["label"].startswith("Favoritul")
    assert result["dataset"]["id"] == "football"


def test_default_window_is_the_last_year_of_the_dataset(dataset):
    result = sim.simulate(dataset, strategy="singles", cache_dir=None, workers=1)
    assert result["end"] == "2025-05-03" and result["start"] == "2024-06-30"
    # The first season only warms the ratings up.
    start, _ = dataset.bounds()
    assert start == date(2023, 8, 5) + timedelta(days=330)


def test_empty_dataset_is_rejected():
    empty = Dataset(id="local-basketball", sport="basketball", label="x", source="x")
    with pytest.raises(sim.SimulationError, match="nu are meciuri"):
        sim.simulate(empty, cache_dir=None, workers=1)


def test_blind_fixture_keeps_pre_match_fields_only():
    match = Match(
        id="m",
        kickoff=datetime(2025, 1, 1, tzinfo=timezone.utc),
        league="L",
        home="A",
        away="B",
        status="finished",
        home_goals=3,
        away_goals=1,
        odds={"1": 1.5},
        finish_type="aet",
        live={"minute": 90},
        sport="football",
    )
    fixture = sim.blind_fixture(match)
    assert fixture.home_goals is None and fixture.away_goals is None
    assert fixture.status == "scheduled" and fixture.finish_type == "" and fixture.live == {}
    assert fixture.odds == {"1": 1.5} and fixture.home == "A"
