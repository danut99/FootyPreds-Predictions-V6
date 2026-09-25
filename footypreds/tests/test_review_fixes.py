"""Regression tests for the final review round: one leg rule for the app and the simulator,
pre-match prices only (no in-play price reaches a prediction or a "recent" simulation), void
candidates in "recent", and Romanian ladder wording."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from footypreds import recommend
from footypreds import simulator as sim
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.engine import HistoryIndex
from footypreds.evaluation import sim_datasets as sd
from footypreds.sports import analyze_match
from footypreds.store import Store
from footypreds.tests.helpers import KICKOFF, fixture, league_history

NOW = KICKOFF - timedelta(days=1)

# --- one leg rule ----------------------------------------------------------------------------


def priced_analysis(odds):
    match = fixture(home="Strong", away="Weak", odds=odds)
    index = HistoryIndex()
    index.extend(league_history())
    return match, analyze_match(match, index, 0.85)


def test_simulator_legs_equal_recommendation_legs_including_the_value_cap():
    # Prices chosen so that some legs exceed MAX_VALUE (the model far above the price), some
    # fall below MIN_VALUE, some are outside the odds band and some are whole lines (push).
    odds = {
        "1": 1.30,
        "X": 5.5,
        "2": 9.0,
        "over25": 2.6,
        "under25": 1.55,
        "over15": 1.2,
        "under15": 4.5,
        "btts": 2.1,
        "no_btts": 1.7,
        "over_3": 2.0,
        "under_3": 1.8,
        "ah_1_-1": 1.9,
        "ah_2_+1": 1.9,
        "1X": 1.05,
        "12": 1.12,
        "X2": 3.5,
    }
    match, analysis = priced_analysis(odds)
    app_legs = {(x["match_id"], x["key"]) for x in recommend.eligible_legs(match, analysis, NOW)}
    row = sim.prediction_row(match, analysis)
    sim_legs = {(x["match_id"], x["key"]) for x in sim.model_legs(row, sim.product_rules())}
    assert app_legs == sim_legs
    values = {m["key"]: m["probability"] * m["odds"] for m in row["markets"]}
    # The fixture really exercises the cap: a priced leg above MAX_VALUE exists and is dropped.
    capped = {k for k, v in values.items() if v > recommend.MAX_VALUE}
    assert capped and capped.isdisjoint({key for _, key in sim_legs})
    uncapped = sim.model_legs(row, replace(sim.product_rules(), max_value=None))
    assert capped & {x["key"] for x in uncapped}


def test_leg_allowed_bounds_and_refunds():
    assert recommend.leg_allowed("football", "1", 0.5, 2.0)
    assert not recommend.leg_allowed("football", "1", 0.6, 2.0)  # 1.2 > MAX_VALUE
    assert not recommend.leg_allowed("football", "1", 0.4, 2.0)  # 0.8 < MIN_VALUE
    assert recommend.leg_allowed("football", "1", 0.6, 2.0, max_value=None)
    assert not recommend.leg_allowed("football", "1", 0.95, 1.05)  # below the odds band
    assert not recommend.leg_allowed("football", "over25", 0.5, 2.0, push=True)
    assert not recommend.leg_allowed("basketball", "over_180", 0.5, 2.0)  # whole line
    assert not recommend.leg_allowed("football", "dnb_1", 0.5, 2.0)


def test_simulation_rules_expose_the_value_window():
    rules = sim.product_rules()
    assert (rules.min_value, rules.max_value) == (recommend.MIN_VALUE, recommend.MAX_VALUE)
    text = sim.method_text("ticket", "flat", rules)
    assert "între 0.95 și 1.05" in text
    assert "∞" in sim.method_text("value", "flat", rules)


def test_value_strategy_stays_uncapped_and_ticket_strategies_are_capped():
    from footypreds.tests.test_simulator import row

    rows = [row("a", [("1", 0.60, 1.9)]), row("b", [("1", 0.52, 1.95)])]
    rules = sim.product_rules()
    singles = sim.choose_singles(rows, 5, rules)
    assert [b["legs"][0]["match_id"] for b in singles] == ["b"]
    value = sim.choose_value(rows, 5, rules)
    assert [b["legs"][0]["match_id"] for b in value] == ["a"]


# --- pre-match prices only ---------------------------------------------------------------------


def odds_answer():
    return [
        {
            "name": "bet365",
            "odds": [
                {
                    "bettingType": "OVER_UNDER",
                    "bettingScope": "FULL_TIME",
                    "odds": [
                        {
                            "handicap": {"value": "2.5"},
                            "selection": "OVER",
                            "value": "1.20",
                            "active": True,
                        },
                        {
                            "handicap": {"value": "2.5"},
                            "selection": "UNDER",
                            "value": "4.50",
                            "active": True,
                        },
                    ],
                }
            ],
        }
    ]


def run_analyze(tmp_path, status, kickoff):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("matches/h2h"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("matches/standings"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("matches/odds"):
            return httpx.Response(200, json=odds_answer())
        raise AssertionError(request.url.path)

    settings = Settings(api_key="k", database=tmp_path / f"{status}.db")
    goals = {"home_goals": 2, "away_goals": 1} if status != "scheduled" else {}
    match = Match(
        id="m1",
        kickoff=kickoff,
        league="ENGLAND: Test",
        home="Strong",
        away="Weak",
        status=status,
        odds={"1": 1.8, "X": 3.6, "2": 4.2},
        **goals,
    )
    with TestClient(create_app(settings, httpx.MockTransport(handle))) as client:
        client.app.state.store.save_matches([match])
        response = client.post("/api/analyze/m1", json={"enrich": True})
        stored = client.app.state.store.match("m1")
    assert response.status_code == 200
    return calls, stored


def test_enrich_never_merges_prices_after_kickoff(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(hours=5)
    calls, stored = run_analyze(tmp_path, "finished", past)
    assert not any(path.endswith("matches/odds") for path in calls)
    assert stored.odds == {"1": 1.8, "X": 3.6, "2": 4.2}
    # A scheduled match past its kickoff is not pre-match either.
    calls, stored = run_analyze(tmp_path, "scheduled", past)
    assert not any(path.endswith("matches/odds") for path in calls)
    assert "over25" not in stored.odds


def test_enrich_still_merges_prices_before_kickoff(tmp_path):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    calls, stored = run_analyze(tmp_path, "scheduled", future)
    assert any(path.endswith("matches/odds") for path in calls)
    assert stored.odds.get("over25") and stored.odds["1"] == 1.8


def test_finished_rows_keep_the_stored_pre_match_prices(tmp_path):
    store = Store(tmp_path / "prices.db")
    kickoff = datetime(2026, 5, 1, 18, tzinfo=timezone.utc)
    base = {"id": "m", "kickoff": kickoff, "league": "L", "home": "A", "away": "B"}
    store.save_matches([Match(**base, odds={"1": 2.0, "X": 3.3, "2": 3.8})])
    # A later scheduled list still updates the price (moves before kickoff are pre-match).
    store.save_matches([Match(**base, odds={"1": 2.1, "X": 3.3, "2": 3.6})])
    assert store.match("m").odds["1"] == 2.1
    finished = Match(
        **base,
        status="finished",
        home_goals=1,
        away_goals=0,
        odds={"1": 1.01, "X": 25.0, "2": 50.0, "over25": 3.0},
    )
    store.save_matches([finished])
    stored = store.match("m")
    assert stored.status == "finished"
    assert stored.odds == {"1": 2.1, "X": 3.3, "2": 3.6, "over25": 3.0}
    # Re-fetching the finished day again changes nothing either.
    store.save_matches([finished.model_copy(update={"odds": {"1": 1.02}})])
    assert store.match("m").odds["1"] == 2.1


# --- the "recent" dataset --------------------------------------------------------------------

TODAY = date(2026, 5, 10)


def recent_store(tmp_path):
    rows = []
    start = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
    teams = ["A", "B", "C", "D"]
    n = 0
    for week in range(18):
        for i in range(0, 4, 2):
            n += 1
            home, away = teams[(i + week) % 4], teams[(i + week + 1) % 4]
            rows.append(
                Match(
                    id=f"r{n}",
                    kickoff=start + timedelta(days=7 * week, hours=i),
                    league="ENGLAND: Test",
                    home=home,
                    away=away,
                    status="finished",
                    home_goals=(n % 3),
                    away_goals=(n % 2),
                    odds={"1": 2.2, "X": 3.3, "2": 3.2, "over25": 1.01, "ah_1_-1.5": 9.0},
                )
            )
    called_off = Match(
        id="postponed",
        kickoff=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        league="ENGLAND: Test",
        home="A",
        away="C",
        status="unavailable",
        odds={"1": 2.0, "X": 3.4, "2": 3.6},
    )
    old_called_off = called_off.model_copy(
        update={"id": "old-postponed", "kickoff": datetime(2026, 1, 20, tzinfo=timezone.utc)}
    )
    store = Store(tmp_path / "recent.db")
    store.save_matches([*rows, called_off, old_called_off])
    return store


def test_recent_keeps_only_list_result_prices(tmp_path):
    dataset = sd.load_recent(recent_store(tmp_path), ["football"], TODAY, 30)
    assert dataset.bettable
    for match in dataset.matches:
        assert set(match.odds) <= {"1", "X", "2"}
    rows, _ = sim.predictions(dataset, *dataset.window, cache_dir=None, workers=1)
    keys = {m["key"] for day in rows.values() for r in day for m in r["markets"]}
    assert keys <= {"1", "X", "2"}


def test_recent_keeps_called_off_matches_as_void_candidates(tmp_path):
    dataset = sd.load_recent(recent_store(tmp_path), ["football"], TODAY, 30)
    ids = {m.id for m in dataset.bettable}
    assert "postponed" in ids
    # Outside the window a called-off match is neither history nor a candidate.
    assert "old-postponed" not in {m.id for m in dataset.matches}
    results = {m.id: m for m in dataset.matches}
    bet = {"legs": [{"match_id": "postponed", "key": "1", "odds": 2.0, "probability": 0.5}]}
    legs, status, multiplier = sim.settle_bet(bet, results)
    assert status == "void" and multiplier == 1.0 and legs[0]["status"] == "void"


# --- Romanian wording ------------------------------------------------------------------------


def test_romanian_counts_dates_and_money():
    assert sim.ro_times(1) == "o dată"
    assert sim.ro_times(3) == "de 3 ori"
    assert sim.ro_times(19) == "de 19 ori"
    assert sim.ro_times(20) == "de 20 de ori"
    assert sim.ro_times(50) == "de 50 de ori"
    assert sim.ro_times(101) == "de 101 ori"
    assert sim.ro_times(120) == "de 120 de ori"
    assert sim.ro_date("2026-04-20") == "20 apr. 2026"
    assert sim.ro_date("2026-09-01") == "1 sept. 2026"
    assert sim.ro_money(255) == "255,00 RON"
    assert sim.ro_money(-1234.5) == "-1.234,50 RON"


def ladder_run(outcomes, restart=True, reinvest=1.0):
    """A ladder over synthetic days whose single ticket wins (True) or loses (False)."""
    from footypreds.tests.test_simulator import row

    day_keys, by_day, results = [], {}, {}
    for i, won in enumerate(outcomes):
        day = (date(2026, 4, 1) + timedelta(days=i)).isoformat()
        day_keys.append(day)
        item = row(f"m{i}", [("1", 0.5, 2.0)]) | {"day": day}
        by_day[day] = [item]
        results[f"m{i}"] = Match(
            id=f"m{i}",
            kickoff=datetime.fromisoformat(f"{day}T12:00:00+00:00"),
            league="L",
            home="H",
            away="A",
            status="finished",
            home_goals=1 if won else 0,
            away_goals=0 if won else 1,
        )

    def choose(rows):
        return [{"legs": [sim.leg_of(rows[0], rows[0]["markets"][0])]}]

    return sim.run_ladder(
        day_keys,
        by_day,
        results,
        choose,
        bankroll=5,
        reinvest=reinvest,
        restart_on_loss=restart,
    )


def test_ladder_warnings_use_romanian_agreement_money_and_dates():
    from footypreds.evaluation.sim_datasets import Dataset

    run = ladder_run([False] * 21)
    dataset = Dataset(id="local-football", sport="football", label="x", source="x")
    notes = sim.ladder_warnings(dataset, run, {}, [], date(2026, 4, 1), date(2026, 4, 21))
    text = " ".join(notes)
    assert "repornită de 20 de ori" in text and "(105,00 RON)" in text
    stopped = ladder_run([True, False, True], restart=False, reinvest=0.5)
    assert stopped["ladder"]["stopped"] == "2026-04-02"
    notes = sim.ladder_warnings(dataset, stopped, {}, [], date(2026, 4, 1), date(2026, 4, 3))
    assert any("2 apr. 2026" in n for n in notes)


def test_in_sample_season_is_flagged_for_football_datasets():
    from footypreds.evaluation.sim_datasets import Dataset

    dataset = Dataset(id="football-plus", sport="football", label="x", source="x")
    inside = sim.source_notes(dataset, date(2024, 8, 1), date(2025, 6, 30))
    outside = sim.source_notes(dataset, date(2025, 8, 1), date(2026, 5, 24))
    assert any("în eșantion" in n for n in inside)
    assert not any("în eșantion" in n for n in outside)
    assert any("Avg" in n for n in outside)


def test_recent_hint_names_the_real_buttons():
    from footypreds.config import PACKAGE

    assert "„Pregătește datele”" in sd.RECENT_HINT
    assert "Pregătește datele" in (PACKAGE / "web" / "simulator.js").read_text(encoding="utf-8")
    assert "Rulează simularea" in sd.RECENT_HINT
