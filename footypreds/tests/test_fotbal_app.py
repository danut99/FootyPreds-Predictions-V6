"""fotbalPrediction.app: rute, jurnal, decontare și rezervă; fără rețea și fără date reale."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fotbalPrediction import app as app_module
from fotbalPrediction.model import NO_BET, SELECT

from .fotbal_helpers import synthetic_rows, tiny_predictor, write_rule


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    rule = write_rule(tmp_path_factory.mktemp("rule") / "rule.json")
    rows = synthetic_rows()
    return tiny_predictor(rows, rule_path=rule), rows


@pytest.fixture
def client(trained, tmp_path):
    predictor, rows = trained
    now = datetime.combine(predictor.trained_through, datetime.min.time(), UTC) + timedelta(days=3)
    app = app_module.create_app(
        include_core=False,
        predictor=predictor,
        warm=False,
        journal_path=tmp_path / "journal.sqlite3",
        clock=lambda: now,
    )
    with TestClient(app) as test_client:
        yield test_client, predictor, rows, now


def board_match(**kw):
    match = {
        "id": "fs-1",
        "home": "Manchester Utd",
        "away": "Arsenal",
        "league": "ENGLAND: Premier League",
        "country": "England",
        "status": "scheduled",
        "odds": {"1": 2.1, "X": 3.4, "2": 3.6, "junk": 4.0},
    }
    match.update(kw)
    return match


def test_index_assets_and_security_headers(client):
    test_client, *_ = client
    page = test_client.get("/")
    assert page.status_code == 200 and "FotbalPrediction" in page.text
    assert "Content-Security-Policy" in page.headers and "no-store" in page.headers["cache-control"]
    assert "style=" not in page.text and "<script>" not in page.text
    script = test_client.get("/assets/app.js")
    assert script.status_code == 200 and "'use strict'" in script.text
    assert script.headers["x-content-type-options"] == "nosniff"
    assert test_client.get("/favicon.ico").status_code == 204


def test_health_teams_and_predict(client):
    test_client, predictor, *_ = client
    health = test_client.get("/api/health").json()
    assert health["status"] == "ok" and health["teams"] == predictor.team_count
    assert health["trained_through"] == predictor.trained_through.isoformat()
    assert health["market_weights"] == [0.9, 0.9]
    teams = test_client.get("/api/teams", params={"q": "man"}).json()["teams"]
    assert {"team": "Man United", "league": "E0"} in teams
    day = (predictor.trained_through + timedelta(days=4)).isoformat()
    result = test_client.get(
        "/api/predict",
        params={"home": "Manchester Utd", "away": "Arsenal", "league": "E0", "day": day},
    ).json()
    assert result["home"] == "Man United" and result["odds_blend"] is False
    assert {"1", "X", "2"} <= {m["key"] for m in result["markets"]}
    missing = test_client.get(
        "/api/predict", params={"home": "Nowhere", "away": "Arsenal", "league": "E0"}
    )
    assert missing.status_code == 404
    assert (
        test_client.get(
            "/api/predict", params={"home": "Ab", "away": "Cd", "league": "RO1"}
        ).status_code
        == 404
    )


def test_loading_state_answers_503_without_blocking(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_trained", None)
    monkeypatch.setattr(app_module, "ensure_warming", lambda: None)
    app = app_module.create_app(include_core=False, warm=False, journal_path=tmp_path / "j.db")
    with TestClient(app) as test_client:
        assert test_client.get("/api/health").json()["status"] == "loading"
        response = test_client.post("/api/fotbal-probabilities", json=[board_match()])
        assert response.status_code == 503 and "antrenează" in response.json()["detail"]


def test_upcoming_match_is_predicted_blended_and_journaled(client):
    test_client, predictor, _, now = client
    kickoff = (now + timedelta(hours=5)).isoformat()
    first = test_client.post(
        "/api/fotbal-probabilities", json=[board_match(kickoff=kickoff)]
    ).json()
    entry = first["matches"][0]
    assert entry["known"] and entry["source"] == "model" and entry["league_code"] == "E0"
    assert entry["home_model"] == "Man United" and entry["odds_blend"] is True
    assert entry["journal"] is False and entry["retro"] is False
    for market in entry["markets"]:
        assert market["won"] is None
        assert market["decision"] in (SELECT, NO_BET)
    one = next(m for m in entry["markets"] if m["key"] == "1")
    assert one["odds"] == 2.1
    # after kick-off the stored pre-match prediction is used, even with other odds
    finished = board_match(
        kickoff=kickoff,
        status="finished",
        home_goals=2,
        away_goals=0,
        odds={"1": 1.2, "X": 7, "2": 15},
    )
    again = test_client.post("/api/fotbal-probabilities", json=[finished]).json()["matches"][0]
    assert again["journal"] is True
    assert [m["probability"] for m in again["markets"]] == [
        m["probability"] for m in entry["markets"]
    ]
    won = {m["key"]: m["won"] for m in again["markets"]}
    assert won["1"] is True and won["X"] is False and won["2"] is False
    assert all(won[m["key"]] is None for m in again["markets"] if m["stat"] != "goals")
    assert again["settled_stats"] is False


def test_journal_keeps_the_last_prediction_before_kickoff(client):
    test_client, _, _, now = client
    kickoff = (now + timedelta(hours=5)).isoformat()
    early = board_match(kickoff=kickoff, odds={"1": 2.1, "X": 3.4, "2": 3.6})
    late = board_match(kickoff=kickoff, odds={"1": 1.5, "X": 4.2, "2": 6.5})
    first = test_client.post("/api/fotbal-probabilities", json=[early]).json()["matches"][0]
    second = test_client.post("/api/fotbal-probabilities", json=[late]).json()["matches"][0]
    home = {m["key"]: m["probability"] for m in first["markets"]}["1"]
    updated = {m["key"]: m["probability"] for m in second["markets"]}["1"]
    assert second["journal"] is False and updated > home  # new prices, new pre-match prediction
    finished = board_match(
        kickoff=kickoff, status="finished", home_goals=1, away_goals=1, odds={"1": 9, "2": 1.1}
    )
    after = test_client.post("/api/fotbal-probabilities", json=[finished]).json()["matches"][0]
    assert after["journal"] is True
    assert {m["key"]: m["probability"] for m in after["markets"]}["1"] == updated


def test_finished_match_settles_stats_from_the_football_data_row(client):
    test_client, predictor, rows, _ = client
    row = rows[-1]
    home = "Manchester Utd" if row.home == "Man United" else row.home
    match = board_match(
        id="fs-2",
        home=home,
        away=row.away,
        league="ENGLAND: Premier League" if row.league == "E0" else "ENGLAND: Championship",
        status="finished",
        kickoff=datetime.combine(row.date, datetime.min.time(), UTC).isoformat(),
        home_goals=row.home_goals,
        away_goals=row.away_goals,
        odds=None,
    )
    entry = test_client.post("/api/fotbal-probabilities", json=[match]).json()["matches"][0]
    assert entry["known"] and entry["settled_stats"] is True
    assert entry["retro"] is True  # the model was trained on that day
    stats = [m for m in entry["markets"] if m["stat"] in ("corners", "cards", "ht_goals")]
    assert stats and all(isinstance(m["won"], bool) for m in stats)
    # a different final score means another match: statistics are not settled
    wrong = dict(match, id="fs-3", home_goals=row.home_goals + 3)
    other = test_client.post("/api/fotbal-probabilities", json=[wrong]).json()["matches"][0]
    assert other["settled_stats"] is False
    assert all(m["won"] is None for m in other["markets"] if m["stat"] != "goals")


def test_extra_time_is_never_settled(client):
    test_client, *_ = client
    match = board_match(id="fs-4", status="finished", home_goals=3, away_goals=2, finish_type="aet")
    entry = test_client.post("/api/fotbal-probabilities", json=[match]).json()["matches"][0]
    assert entry["extra_time"] is True and all(m["won"] is None for m in entry["markets"])


def test_unknown_league_falls_back_to_core_and_unknown_team_is_listed(client):
    test_client, *_ = client
    core = board_match(
        id="fs-5",
        league="ROMANIA: Superliga",
        home="FCSB",
        away="CFR Cluj",
        status="finished",
        home_goals=1,
        away_goals=1,
        probabilities={"1": 0.5, "X": 0.3, "2": 0.2, "over25": 0.45, "ht_1": 0.3, "bogus": 0.4},
    )
    unknown = board_match(id="fs-6", home="Nowhere Rovers")
    response = test_client.post("/api/fotbal-probabilities", json=[core, unknown]).json()
    fallback, missing = response["matches"]
    assert fallback["source"] == "core" and fallback["known"] is False
    assert fallback["reason"] == "ligă fără model"
    keys = {m["key"]: m for m in fallback["markets"]}
    assert set(keys) == {"1", "X", "2", "over25"}
    assert all(m["decision"] == NO_BET and m["selectable"] is False for m in keys.values())
    assert keys["X"]["won"] is True and keys["over25"]["won"] is False
    assert missing["source"] == "none" and "Nowhere Rovers" in missing["reason"]
    listed = test_client.get("/api/unresolved").json()["unresolved"]
    assert {
        "league": "E0",
        "name": "Nowhere Rovers",
        "flashscore_league": "ENGLAND: Premier League",
    } in listed


def test_payload_is_validated_and_capped(client):
    test_client, *_ = client
    bad = test_client.post("/api/fotbal-probabilities", json=[board_match(home="x" * 101)])
    assert bad.status_code == 422
    many = [board_match(id=f"fs-{i}", league="WORLD: Club Friendly") for i in range(401)]
    response = test_client.post("/api/fotbal-probabilities", json=many).json()
    assert len(response["matches"]) == 400


def test_match_after_midnight_already_ingested_is_retro(client):
    # Meci jucat seara (data football-data D) care la București cade în ziua D+1.
    test_client, predictor, rows, _ = client
    row = rows[-1]
    home = "Manchester Utd" if row.home == "Man United" else row.home
    match = board_match(
        id="fs-7",
        home=home,
        away=row.away,
        league="ENGLAND: Premier League" if row.league == "E0" else "ENGLAND: Championship",
        status="finished",
        day=(row.date + timedelta(days=1)).isoformat(),
        home_goals=row.home_goals,
        away_goals=row.away_goals,
        odds=None,
    )
    assert row.date + timedelta(days=1) > predictor.trained_through
    entry = test_client.post("/api/fotbal-probabilities", json=[match]).json()["matches"][0]
    assert entry["known"] and entry["retro"] is True and entry["settled_stats"] is True


def test_changed_data_reloads_the_model_in_the_background(monkeypatch):
    class Stub:
        def __init__(self, key):
            self.key = key

    monkeypatch.setattr(app_module, "_trained", Stub("old"))
    monkeypatch.setattr(app_module, "_warm_thread", None)
    monkeypatch.setattr(app_module, "_checked_at", None)
    keys = iter(["new", "new"])
    monkeypatch.setattr(app_module, "predictor_key", lambda: next(keys))
    monkeypatch.setattr(
        app_module.FootballPredictor, "load_or_train", classmethod(lambda cls: Stub("new"))
    )
    assert app_module.ensure_fresh(clock=lambda: 1000.0) is True
    app_module._warm_thread.join(timeout=5)
    assert app_module._trained.key == "new"
    # at most one check per interval, and no reload when the key is unchanged
    assert app_module.ensure_fresh(clock=lambda: 1010.0) is False
    later = 1000.0 + app_module.RELOAD_CHECK_SECONDS + 1
    assert app_module.ensure_fresh(clock=lambda: later) is False
    assert app_module._trained.key == "new"


def test_unchanged_model_is_not_reloaded(monkeypatch):
    class Stub:
        key = "same"

    monkeypatch.setattr(app_module, "_trained", Stub())
    monkeypatch.setattr(app_module, "_warm_thread", None)
    monkeypatch.setattr(app_module, "_checked_at", None)
    monkeypatch.setattr(app_module, "predictor_key", lambda: "same")
    assert app_module.ensure_fresh(clock=lambda: 5.0) is False
    assert app_module._warm_thread is None
