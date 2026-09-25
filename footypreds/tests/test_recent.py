"""The "recent" simulator dataset (last N days of the local store) and its background loader:
window, targets, blindness, request counting, idempotence, budget and the API contract."""

import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import sim_api
from footypreds import simulator as sim
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.domain import Match
from footypreds.evaluation import sim_datasets as sd
from footypreds.store import Store
from footypreds.tests.test_simulator import football_records
from footypreds.tests.test_simulator_datasets import tennis_matches

# football_records(): weekly rounds, the last one on 2025-05-03.
TODAY = date(2025, 5, 4)
NOW = datetime(2025, 5, 4, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(sd, "utcnow", lambda: NOW)
    monkeypatch.setattr(sd, "_RECENT_MEMO", {})
    monkeypatch.setattr(sd, "_MEMO", {})


def stored_football(records=None, unpriced=()):
    """football_records as FlashScore rows with list-by-date 1X2 prices (except `unpriced`)."""
    rows = []
    for record in records or football_records():
        match = Match.model_validate(record["match"])
        odds = {k: v for k, v in record["reference_odds"].items() if k in ("1", "X", "2")}
        rows.append(
            match.model_copy(
                update={"source": "flashscore", "odds": {} if match.id in unpriced else odds}
            )
        )
    return rows


def store_with(tmp_path, matches, name="recent.db"):
    store = Store(tmp_path / name)
    store.save_matches(matches)
    return store


def shifted_tennis():
    """The synthetic tennis season moved so that it ends yesterday (TODAY - 1)."""
    rows = tennis_matches(200)
    shift = TODAY - timedelta(days=1) - rows[-1].kickoff.date()
    flag = "https://flagcdn.com/w40/es.png"
    return [m.model_copy(update={"kickoff": m.kickoff + shift, "home_logo": flag}) for m in rows]


# --- dataset ---------------------------------------------------------------------------------


def test_recent_window_targets_and_history(tmp_path, monkeypatch):
    matches = stored_football()
    in_window = [m for m in matches if m.kickoff.date() >= TODAY - timedelta(days=30)]
    unpriced = in_window[0].id
    matches = stored_football(unpriced={unpriced})
    scheduled = Match(
        id="later",
        kickoff=NOW - timedelta(days=2),
        league="Test League",
        home="Alpha",
        away="Beta",
        odds={"1": 2.0, "X": 3.2, "2": 3.9},
        source="flashscore",
    )
    today_row = matches[-1].model_copy(update={"id": "today", "kickoff": NOW - timedelta(hours=2)})
    store = store_with(tmp_path, [*matches, scheduled, today_row])
    dataset = sd.load_recent(store, ["football"], TODAY, 30)
    assert dataset.window == (TODAY - timedelta(days=30), TODAY - timedelta(days=1))
    assert dataset.bounds() == dataset.window
    # All finished history before today is visible; nothing scheduled, nothing from today.
    assert len(dataset.matches) == 240
    assert {m.id for m in dataset.matches}.isdisjoint({"later", "today"})
    ids = {m.id for m in dataset.bettable}
    assert ids == {m.id for m in in_window} - {unpriced}
    assert all(dataset.period_of(m) == m.kickoff.strftime("%G-W%V") for m in dataset.bettable)
    assert dataset.period_of(dataset.matches[0]) == "2023-08"
    describe = dataset.describe()
    assert (
        describe["id"] == "recent" and describe["available"] and describe["sports"] == ["football"]
    )
    assert describe["start"] == (TODAY - timedelta(days=30)).isoformat()
    # The same cap as the recommendations: at most ANALYSIS_LIMIT fixtures per sport and day.
    monkeypatch.setattr(sd, "analysis_limit", lambda: 2)
    capped = sd.load_recent(store, ["football"], TODAY, 30)
    per_day = {}
    for match in capped.bettable:
        per_day[match.kickoff.date()] = per_day.get(match.kickoff.date(), 0) + 1
    assert per_day and max(per_day.values()) == 2


def test_recent_mixes_sports_in_independent_histories(tmp_path):
    store = store_with(tmp_path, [*stored_football(), *shifted_tennis()])
    dataset = sd.load_recent(store, ["tennis", "football"], TODAY, 20)
    assert dataset.sport == "multi" and dataset.sports == ["football", "tennis"]
    assert set(dataset.groups) == {"football", "tennis"}
    assert all(m.sport == "football" for m in dataset.groups["football"])
    assert all(m.sport == "tennis" for m in dataset.groups["tennis"])
    assert {m.sport for m in dataset.bettable} == {"football", "tennis"}
    only_tennis = sd.load_recent(store, ["tennis"], TODAY, 20)
    assert only_tennis.sport == "tennis" and only_tennis.label.startswith("Ultimele 20 zile")
    with pytest.raises(ValueError, match="cel puțin un sport"):
        sd.load_recent(store, [], TODAY, 20)
    with pytest.raises(FileNotFoundError):
        sd.load_recent(None, ["football"], TODAY, 20)


def test_recent_is_listed_with_the_other_datasets(tmp_path):
    items = {i["id"]: i for i in sd.availability(None, tmp_path)}
    assert "recent" in items and items["recent"]["available"] is False
    assert "Pregătește" in items["recent"]["hint"]
    store = store_with(tmp_path, stored_football())
    listed = {i["id"]: i for i in sd.availability(store, tmp_path)}["recent"]
    assert listed["available"] and listed["sports"] == ["football", "basketball", "tennis"]
    assert listed["end"] == (TODAY - timedelta(days=1)).isoformat()


def recent_ladder(store, days=60, **kw):
    dataset = sd.recent_dataset(store, ["football", "tennis"], TODAY, days)
    options = {"bankroll": 5, "strategy": "ladder", "target_odds": 2, "last_days": days}
    return sim.simulate(dataset, cache_dir=None, workers=1, **(options | kw))


def test_recent_ladder_logs_every_calendar_day(tmp_path):
    store = store_with(tmp_path, [*stored_football(), *shifted_tennis()])
    result = recent_ladder(store, days=60)
    assert result["dataset"]["id"] == "recent" and result["sports"] == ["football", "tennis"]
    assert [d["date"] for d in result["days"]] == [
        (TODAY - timedelta(days=n)).isoformat() for n in range(60, 0, -1)
    ]
    assert result["bets"] > 0
    legs = [leg for d in result["days"] if d["ticket"] for leg in d["ticket"]["legs"]]
    assert legs and all(leg["result"] in ("won", "lost", "void") for leg in legs)
    # Logos travel from the stored match to the leg as same-origin display URLs.
    tennis = [leg for leg in legs if leg["sport"] == "tennis"]
    assert tennis and all(
        leg["home_logo"] == "/api/img?u=https%3A%2F%2Fflagcdn.com%2Fw40%2Fes.png" for leg in tennis
    )
    assert all(leg["away_logo"] is None for leg in tennis)
    assert any("istoric de formă subțire" in w for w in result["warnings"])
    assert any("independente" in w for w in result["warnings"])


def test_recent_days_without_data_are_skipped_with_a_hint(tmp_path):
    rows = [m for m in stored_football() if m.kickoff.date() != date(2025, 4, 26)]
    store = store_with(tmp_path, rows)
    dataset = sd.recent_dataset(store, ["football"], TODAY, 10)
    result = sim.simulate(
        dataset,
        bankroll=5,
        strategy="ladder",
        target_odds=2,
        last_days=10,
        cache_dir=None,
        workers=1,
    )
    day = next(d for d in result["days"] if d["date"] == "2025-04-26")
    assert day["result"] == "skipped" and "baza locală" in day["reason"]
    assert result["ladder"]["days_without_ticket"] >= 1
    assert any("pregătește ultimele zile" in w for w in result["warnings"])


def poison(matches, cut):
    """Scores on or after `cut` swapped and shifted (the fixtures and prices stay the same)."""
    output = []
    for m in matches:
        if m.kickoff.date() >= cut:
            m = m.model_copy(update={"home_goals": m.away_goals, "away_goals": m.home_goals + 3})
        output.append(m)
    return output


def test_recent_simulation_is_blind(tmp_path):
    rows = [*stored_football(), *shifted_tennis()]
    clean = recent_ladder(store_with(tmp_path, rows, "clean.db"))
    ticket_days = [d["date"] for d in clean["days"] if d["ticket"]]
    assert len(ticket_days) >= 4
    cut = date.fromisoformat(ticket_days[len(ticket_days) // 2])
    dirty = recent_ladder(store_with(tmp_path, poison(rows, cut), "dirty.db"))
    before = {d["date"]: d for d in clean["days"]}
    after = {d["date"]: d for d in dirty["days"]}

    def picks(day):
        return [(x["match_id"], x["key"], x["odds"]) for x in day["ticket"]["legs"]]

    for key, day in before.items():
        if key < cut.isoformat():
            assert after[key] == day
    # The poisoned day's ticket was fixed before its (now different) results were revealed.
    assert picks(after[cut.isoformat()]) == picks(before[cut.isoformat()])


def test_recent_simulation_is_deterministic(tmp_path):
    store = store_with(tmp_path, [*stored_football(), *shifted_tennis()])
    first, second = recent_ladder(store, days=30), recent_ladder(store, days=30)
    for key in ("days", "ladder", "summary", "baseline"):
        assert first[key] == second[key]


# --- loader (MockTransport) ------------------------------------------------------------------

LOADER_NOW = datetime(2026, 9, 25, 20, tzinfo=timezone.utc)
SPORT_IDS = {"1": "football", "2": "tennis", "3": "basketball"}


class FakeLists:
    """matches/list-by-date for any day: two finished, priced games per sport; counts calls."""

    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, request):
        params = dict(request.url.params)
        assert request.url.path.endswith("matches/list-by-date"), request.url.path
        self.calls.append((params["date"], SPORT_IDS[params["sport_id"]]))
        if self.status != 200:
            return httpx.Response(self.status, json={})
        sport = SPORT_IDS[params["sport_id"]]
        day = date.fromisoformat(params["date"])
        kickoff = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=15)
        rows = []
        for n in range(2):
            scores = {"home": 2, "away": 1} if sport != "tennis" else {"home": 2, "away": 0}
            odds = {"1": 1.8, "X": 3.5, "2": 4.2} if sport == "football" else {"1": 1.6, "2": 2.3}
            rows.append(
                {
                    "match_id": f"{sport[:2]}-{day.isoformat()}-{n}",
                    "timestamp": (kickoff + timedelta(hours=n)).timestamp(),
                    "home_team": {"name": f"Home {n}", "team_id": f"h{n}"},
                    "away_team": {"name": f"Away {n}", "team_id": f"a{n}"},
                    "match_status": {"is_started": True, "is_finished": True},
                    "scores": scores,
                    "odds": odds,
                }
            )
        return httpx.Response(200, json=[{"name": "TEST: League", "matches": rows}])

    def count(self, sport=None):
        return sum(1 for _, s in self.calls if sport in (None, s))


@pytest.fixture
def loader_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "utcnow", lambda: LOADER_NOW)
    fake = FakeLists()
    app = create_app(
        Settings(api_key="k", database=tmp_path / "loader.db"), httpx.MockTransport(fake)
    )
    app.state.sim_benchmark_dir = tmp_path / "no-benchmark"
    app.state.sim_cache_dir = tmp_path / "cache"
    app.state.sim_workers = 1
    with TestClient(app) as client:
        yield client, fake
        loader = getattr(app.state, "recent_loader", None)
        if loader is not None:
            finish(client)


def finish(client):
    for _ in range(500):
        state = client.get("/api/simulate/recent/status").json()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("loader still running")


def test_loader_fetches_one_list_per_day_and_sport_once(loader_env):
    client, fake = loader_env
    body = {"days": 3, "sports": ["football", "tennis"], "warmup_days": 0}
    started = client.post("/api/simulate/recent/prepare", json=body)
    assert started.status_code == 202, started.text
    assert started.json()["total"] == 6
    state = finish(client)
    assert state["status"] == "done" and state["done"] == 6 and state["requests"] == 6
    assert fake.count() == 6 and fake.count("football") == 3
    assert state["days_loaded"] == 6 and state["days_total"] == 6
    assert state["loaded_matches"] == 12 and state["matches"] == 12
    store = client.app.state.store
    # Days at least two days old are synced (shared with the history sync); yesterday is not.
    assert store.synced_days("tennis") == {"2026-09-22", "2026-09-23"}
    assert store.synced_days("football") == {"2026-09-22", "2026-09-23"}
    # A second prepare only re-checks yesterday, through the provider cache: no new request.
    again = client.post("/api/simulate/recent/prepare", json=body).json()
    assert again["total"] == 2
    state = finish(client)
    assert state["status"] == "done" and state["requests"] == 0 and fake.count() == 6


def test_loader_warmup_days_and_status_coverage(loader_env):
    client, fake = loader_env
    status = client.get("/api/simulate/recent/status?days=5&sports=basketball").json()
    assert status["status"] == "idle" and status["days_loaded"] == 0 and status["days_total"] == 5
    client.post(
        "/api/simulate/recent/prepare",
        json={"days": 2, "sports": ["basketball"], "warmup_days": 3},
    )
    state = finish(client)
    assert state["status"] == "done" and fake.count("basketball") == 5
    assert (state["days_loaded"], state["days_total"], state["warmup_days"]) == (2, 2, 3)
    coverage = client.get("/api/simulate/recent/status?days=5&sports=basketball").json()
    assert coverage["days_loaded"] == 5
    assert client.get("/api/simulate/recent/status?sports=golf").status_code == 422
    assert client.get("/api/simulate/recent/status?days=61").status_code == 422


def test_loader_respects_the_request_budget(loader_env):
    client, fake = loader_env
    app = client.app
    app.state.recent_loader = sim_api.RecentLoader(app.state.store, app.state.provider, budget=2)
    body = {"days": 5, "sports": ["football"], "warmup_days": 0}
    client.post("/api/simulate/recent/prepare", json=body)
    state = finish(client)
    assert state["status"] == "partial" and fake.count() == 2 and state["done"] == 2
    assert "limita de 2 cereri" in state["message"]
    # Newest days first; the next call continues with the days still missing.
    assert [d for d, _ in fake.calls] == ["2026-09-24", "2026-09-23"]
    client.post("/api/simulate/recent/prepare", json=body)
    finish(client)
    assert fake.count() == 4 and [d for d, _ in fake.calls[2:]] == ["2026-09-22", "2026-09-21"]


def test_loader_reports_provider_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "utcnow", lambda: LOADER_NOW)
    fake = FakeLists(status=429)
    app = create_app(Settings(api_key="k", database=tmp_path / "f.db"), httpx.MockTransport(fake))
    with TestClient(app) as client:
        client.post("/api/simulate/recent/prepare", json={"days": 3, "warmup_days": 0})
        state = finish(client)
        assert state["status"] == "failed" and "Limita RapidAPI" in state["message"]
        assert fake.count() == 1
        assert client.app.state.store.synced_days() == set()
    missing_key = create_app(Settings(api_key="", database=tmp_path / "k.db"))
    with TestClient(missing_key) as client:
        client.post("/api/simulate/recent/prepare", json={"days": 1, "warmup_days": 0})
        state = finish(client)
        assert state["status"] == "failed" and "RAPIDAPI_KEY" in state["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"days": 0},
        {"days": 61},
        {"days": 5, "sports": ["golf"]},
        {"days": 5, "sports": []},
        {"days": 5, "warmup_days": 31},
    ],
)
def test_prepare_validation(loader_env, body):
    client, fake = loader_env
    assert client.post("/api/simulate/recent/prepare", json=body).status_code == 422
    assert fake.calls == []


# --- simulate API on "recent" ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(api_key="", database=tmp_path / "api.db"))
    app.state.sim_benchmark_dir = tmp_path / "no-benchmark"
    app.state.sim_cache_dir = tmp_path / "cache"
    app.state.sim_workers = 1
    with TestClient(app) as test_client:
        yield test_client


def test_simulate_recent_via_api(client):
    client.app.state.store.save_matches([*stored_football(), *shifted_tennis()])
    body = {
        "dataset": "recent",
        "sports": ["football", "tennis"],
        "days": 10,
        "strategy": "ladder",
        "bankroll": 5,
        "target_odds": 2,
    }
    response = client.post("/api/simulate", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dataset"]["id"] == "recent" and data["sports"] == ["football", "tennis"]
    assert len(data["days"]) == 10 and data["end"] == "2025-05-03"
    assert data["start"] == "2025-04-24"
    for key in ("ladder", "summary", "equity", "warnings", "baseline", "disclaimer"):
        assert key in data
    flat = client.post(
        "/api/simulate",
        json={"dataset": "recent", "sports": ["tennis"], "days": 10, "strategy": "singles"},
    )
    assert flat.status_code == 200 and flat.json()["mode"] == "singles"
    listed = {d["id"]: d for d in client.get("/api/simulate/datasets").json()["datasets"]}
    assert listed["recent"]["available"]


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"days": 61}, 422, "între 1 și 60"),
        ({"days": 0}, 422, "între 1 și 60"),
        ({"sports": ["golf"]}, 422, "Parametri invalizi"),
        ({"sports": ["tennis"], "sport": "football"}, 422, "nu este printre"),
        ({"sports": ["basketball"]}, 404, "Pregătește"),
    ],
)
def test_simulate_recent_errors(client, body, status, text):
    client.app.state.store.save_matches(stored_football())
    base = {"dataset": "recent", "strategy": "ladder", "bankroll": 5, "target_odds": 2}
    response = client.post("/api/simulate", json=base | body)
    assert response.status_code == status, response.text
    assert text in response.json()["detail"]


def test_loader_never_refetches_days_the_history_sync_completed(loader_env):
    client, fake = loader_env
    store = client.app.state.store
    store.mark_synced(date(2026, 9, 23), 10)
    store.mark_synced(date(2026, 9, 22), 10, "tennis")
    body = {"days": 3, "sports": ["football", "tennis"], "warmup_days": 0}
    assert client.post("/api/simulate/recent/prepare", json=body).json()["total"] == 4
    finish(client)
    assert ("2026-09-23", "football") not in fake.calls
    assert ("2026-09-22", "tennis") not in fake.calls and fake.count() == 4
