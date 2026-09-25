"""Tennis walk-forward evaluation: blindness, metrics, tuning and the locked test."""

import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from footypreds.domain import Match
from footypreds.evaluation import tennis_eval as ev
from footypreds.sports import analyze_match
from footypreds.sports import tennis as t

START = datetime(2022, 1, 3, 12, tzinfo=timezone.utc)


def synthetic(days=500, players=24, seed=7):
    """Players with fixed true strength; fair prices with a 6% margin; iid sets."""
    rng = random.Random(seed)
    strength = {f"P{i} X.": rng.gauss(0, 0.8) for i in range(players)}
    names = list(strength)
    matches = []
    for day in range(days):
        kickoff = START + timedelta(days=day)
        rng.shuffle(names)
        for n in range(0, 8, 2):
            home, away = names[n], names[n + 1]
            p = 1 / (1 + math.exp(-(strength[home] - strength[away])))
            q = t.set_probability(p, 3)
            h = a = 0
            games = []
            while h < 2 and a < 2:
                if rng.random() < q:
                    h += 1
                    games.append(f"6-{rng.randint(0, 4)}")
                else:
                    a += 1
                    games.append(f"{rng.randint(0, 4)}-6")
            source = f"tennis-data.co.uk;tour={'atp' if n < 4 else 'wta'};best_of=3"
            matches.append(
                Match(
                    id=f"syn-{day}-{n}",
                    kickoff=kickoff,
                    league="ATP - SINGLES: Synthetic (Nowhere), hard",
                    home=home,
                    away=away,
                    status="finished",
                    home_goals=h,
                    away_goals=a,
                    odds={"1": 1 / (p * 1.06), "2": 1 / ((1 - p) * 1.06)},
                    source=source + ";games=" + " ".join(games),
                    sport="tennis",
                )
            )
    return matches


@pytest.fixture(scope="module")
def data():
    return synthetic()


@pytest.fixture(autouse=True)
def fast_games(monkeypatch):
    # The point-level games model is slow; sample it sparsely in tests.
    monkeypatch.setattr(ev, "GAMES_STEP", 25)
    monkeypatch.setattr(ev, "TUNE_GAMES_STEP", 50)
    monkeypatch.setattr(ev, "SERVE_MEN", [0.60, 0.64])
    monkeypatch.setattr(ev, "SERVE_WOMEN", [0.56])


def years_of(matches):
    return sorted({m.kickoff.year for m in matches})


def test_predictions_are_blind_to_the_day_being_predicted(data):
    year = years_of(data)[-1]
    base = {r["id"]: r for r in ev.walk_forward(data, t.PARAMS, [year])}
    target_day = next(r["date"] for r in base.values())
    flipped = [
        m.model_copy(update={"home_goals": m.away_goals, "away_goals": m.home_goals})
        if m.kickoff.date().isoformat() == target_day
        else m
        for m in data
    ]
    after = {r["id"]: r for r in ev.walk_forward(flipped, t.PARAMS, [year])}
    same_day = [i for i, r in base.items() if r["date"] == target_day]
    assert same_day
    for match_id in same_day:
        assert after[match_id]["elo"] == base[match_id]["elo"]
        assert after[match_id]["home_won"] != base[match_id]["home_won"]
    later = [i for i, r in base.items() if r["date"] > target_day]
    assert any(after[i]["elo"] != base[i]["elo"] for i in later)


def test_walk_forward_agrees_with_the_analyzer(data):
    year = years_of(data)[-1]
    records = ev.walk_forward(data, t.PARAMS, [year])
    record = records[len(records) // 2]
    match = next(m for m in data if m.id == record["id"])
    analysis = analyze_match(ev.blind(match), data)
    assert analysis["components"]["model_home_win"] == pytest.approx(record["elo"], abs=1e-12)
    final = ev.final_probability(record, t.PARAMS)
    assert analysis["expected"]["home_win"] == pytest.approx(final, abs=1e-12)


def test_only_completed_games_of_the_requested_years_are_scored(data):
    retired = data[-1].model_copy(update={"finish_type": "retired", "id": "ret"})
    walkover = data[-2].model_copy(
        update={
            "status": "unavailable",
            "home_goals": None,
            "away_goals": None,
            "finish_type": "walkover",
            "id": "wo",
        }
    )
    year = years_of(data)[-1]
    records = ev.walk_forward(data + [retired, walkover], t.PARAMS, [year])
    ids = {r["id"] for r in records}
    assert "ret" not in ids and "wo" not in ids
    assert all(r["year"] == year for r in records)
    assert ev.blind(data[0]).home_goals is None and ev.blind(data[0]).status == "scheduled"
    assert ev.games_total(data[0]) == sum(
        int(x) for s in t.source_hint(data[0], "games").split() for x in s.split("-")
    )


def test_evaluate_reports_every_metric(data):
    year = years_of(data)[-1]
    records = ev.walk_forward(data, t.PARAMS, [year])
    report = ev.evaluate(records, t.PARAMS, games_step=20)
    assert report["matches"] == len(records)
    for name in ("final", "elo_only", "market_only"):
        m = report[name]
        assert m["n"] == len(records) and 0 < m["log_loss"] < 1 and 0 <= m["accuracy"] <= 1
    # Fair synthetic prices are the truth: the market beats a 50% guess.
    assert report["market_only"]["log_loss"] < math.log(2)
    assert sum(row["n"] for row in report["calibration"]) == len(records)
    assert report["sets"]["bo3"]["n"] == len(records)
    assert report["games"]["n"] > 0 and report["games"]["mae"] >= 0
    assert set(report["by_tour"]) == {"atp", "wta"}
    assert report["value_bets"]["edge"] == 0.03


def test_metric_helpers():
    assert ev.log_loss(1.0, True) == pytest.approx(0, abs=1e-9)
    assert ev.log_loss(0.5, False) == pytest.approx(math.log(2))
    metrics = ev.winner_metrics([(0.8, True), (0.3, False), (0.6, False)])
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert ev.winner_metrics([]) == {"n": 0}
    table = ev.calibration([(0.9, True), (0.1, False), (0.52, False)])
    assert [row["n"] for row in table] == [1, 2]
    bets = ev.value_bets(
        [{"odds": (2.5, 1.6), "home_won": True}, {"odds": (1.2, 5.0), "home_won": True}],
        [0.5, 0.5],
    )
    assert bets["bets"] == 2 and bets["won"] == 1 and bets["roi"] == pytest.approx(0.25)


def test_tuning_uses_only_the_validation_year_and_returns_a_trace(data):
    years = years_of(data)
    grid = {"k_base": [150.0, 250.0], "surface_weight": [0.0, 0.5]}
    seen = []
    real = ev.walk_forward

    def spy(matches, params, wanted):
        seen.append(tuple(wanted))
        return real(matches, params, wanted)

    ev_walk, ev.walk_forward = ev.walk_forward, spy
    try:
        params, trace = ev.tune(data, years[1], passes=1, grid=grid, log=lambda *_: None)
    finally:
        ev.walk_forward = ev_walk
    assert set(seen) == {(years[1],)}
    assert isinstance(params, t.TennisParams)
    assert params.market_weight in ev.MARKET_WEIGHTS and params.set_spread in ev.SPREADS
    assert {row["stage"] for row in trace} == {"elo", "blend", "sets", "games"}


def test_locked_test_runs_once_per_version(tmp_path, monkeypatch, data):
    monkeypatch.setitem(ev.REPORTS, "test", tmp_path / "report-test.json")
    protocol = {**ev.PROTOCOL, "history_years": [2022], "validation_year": 2022}
    monkeypatch.setattr(ev, "PROTOCOL", {**protocol, "test_year": 2023})
    loads = []
    monkeypatch.setattr(ev, "load", lambda years: loads.append(years) or data)
    first = ev.run_test(log=lambda *_: None)
    assert first["version"] == t.VERSION and first["test"]["matches"] > 0
    assert json.loads((tmp_path / "report-test.json").read_text(encoding="utf-8"))["version"]
    again = ev.run_test(log=lambda *_: None)
    assert again == first and len(loads) == 1
    ev.run_test(force=True, log=lambda *_: None)
    assert len(loads) == 2


def test_params_json_is_plain_json():
    payload = ev.params_json(t.TennisParams(idle_half_life=float("inf")))
    assert payload["idle_half_life"] is None
    json.dumps(payload, allow_nan=False)
