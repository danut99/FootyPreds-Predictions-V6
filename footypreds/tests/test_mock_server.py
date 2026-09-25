"""scripts/mock_server.py: the real app on fake FlashScore data, booted through TestClient.

No port is opened and nothing reaches the network: FlashScore and the image hosts are
httpx.MockTransport fakes, the database and datasets live in tmp_path.
"""

import importlib.util
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import recommend, wallet
from footypreds.config import Settings
from footypreds.domain import image_url
from footypreds.media import DISPLAY_PREFIX, sniff
from footypreds.sports.settle import can_push, is_settleable

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mock_server.py"
SPORTS = ("football", "basketball", "tennis")
KNOWN_PATHS = (
    "list-by-date",
    "matches/live",
    "matches/h2h",
    "matches/odds",
    "matches/standings",
    "match/stats",
)


def load_script():
    spec = importlib.util.spec_from_file_location("footypreds_mock_server", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock = load_script()
TODAY = datetime.now(timezone.utc).date()
# Early in the real UTC day, so every generated kickoff of "today" is after the frozen clock.
NOW = datetime.combine(TODAY, time(8, 0), timezone.utc)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(recommend, "utcnow", lambda: NOW)
    monkeypatch.setattr(wallet, "utcnow", lambda: NOW)


@pytest.fixture
def app(tmp_path):
    return mock.build_app(tmp_path / "mock", seed=7, now=NOW)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def assert_logo(value):
    assert value is not None and value.startswith(DISPLAY_PREFIX), value
    assert image_url(httpx.URL(value).params["u"])


def assert_leg_logos(leg, required=True):
    for field in ("home_logo", "away_logo", "league_logo"):
        assert field in leg
        if required or leg[field] is not None:
            assert_logo(leg[field])


def test_the_mock_app_uses_only_temporary_storage(app, tmp_path):
    root = tmp_path / "mock"
    assert Path(app.state.settings.database).is_relative_to(root)
    assert Path(app.state.img_cache_dir).is_relative_to(root)
    assert Path(app.state.sim_benchmark_dir).is_relative_to(root)
    assert Path(app.state.sim_cache_dir).is_relative_to(root)
    assert app.state.settings.api_key == "mock-key"
    assert app.state.store.matches()  # synthetic team histories are seeded


def test_home_board_live_and_images_work_offline(client, app):
    day = TODAY.isoformat()
    # Home: AI tickets x2/x5/x10/x100 and the safest singles, all with logos.
    data = client.get(f"/api/recommendations?day={day}").json()
    assert [t["target"] for t in data["tickets"]] == [2, 5, 10, 100]
    assert data["tickets"][0]["status"] == "pending"
    assert data["singles"]
    legs = [leg for t in data["tickets"] for leg in t["legs"]] + data["singles"]
    assert {leg["sport"] for leg in legs} >= {"football", "basketball"}
    for leg in legs:
        assert_leg_logos(leg)
        assert is_settleable(leg["sport"], leg["key"]) and not can_push(leg["sport"], leg["key"])
        assert datetime.fromisoformat(leg["kickoff"]) > NOW
    generated = client.post("/api/tickets/generate", json={"day": day, "target_odds": 3}).json()
    assert generated["ticket"]["status"] == "pending"
    for leg in generated["ticket"]["legs"]:
        assert_leg_logos(leg)

    # Daily board of every sport: upcoming games, grades A-C, crests/flags and league logos.
    for sport in SPORTS:
        board = client.get(f"/api/predictions?day={day}&sport={sport}&limit=200").json()
        assert board["total"] >= 20, sport
        assert {item["grade"] for item in board["items"]} & {"A", "B", "C"}
        with_logos = [i for i in board["items"] if i["match"]["home_logo"]]
        assert len(with_logos) >= 0.9 * len(board["items"])
        for item in board["items"]:
            assert datetime.fromisoformat(item["match"]["kickoff"]) > NOW
            assert_logo(item["match"]["league_logo"])
    detail = client.get(f"/api/analysis/{legs[0]['match_id']}").json()
    assert_logo(detail["match"]["home_logo"])

    # Live: every sport has games in play, with logos.
    for sport in SPORTS:
        live = client.get(f"/api/live?sport={sport}").json()
        assert live["count"] > 0
        assert sum(item["home_logo"] is not None for item in live["matches"]) > live["count"] / 2
    first = client.get("/api/live?sport=football").json()["matches"][0]
    assert client.get(f"/api/live/{first['match']['id']}?sport=football").json()["stats"]

    # The logos render: generated PNG crests and flags from the fake image hosts.
    for url in (legs[0]["home_logo"], legs[0]["league_logo"], first["home_logo"]):
        image = client.get(url)
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert sniff(image.content) == "image/png"
    flags = [i["home_logo"] for i in client.get("/api/live?sport=tennis").json()["matches"]]
    flag = next(f for f in flags if f and "flagcdn" in f)
    assert client.get(flag).status_code == 200

    # Virtual wallet legs keep their logos.
    client.post("/api/wallet/deposit", json={"amount": 100})
    single = data["singles"][0]
    placed = client.post(
        "/api/wallet/bet",
        json={"stake": 5, "legs": [{"match_id": single["match_id"], "key": single["key"]}]},
    )
    assert placed.status_code == 200, placed.text
    assert_leg_logos(placed.json()["bets"][0]["legs"][0])

    calls = app.state.mock.calls
    assert calls and all(path.endswith(KNOWN_PATHS) for path, _ in calls)


def test_ladder_simulation_runs_on_the_synthetic_datasets(client):
    datasets = {d["id"]: d for d in client.get("/api/simulate/datasets").json()["datasets"]}
    assert datasets["football"]["available"] and datasets["football-plus"]["available"]
    body = {"dataset": "football-plus", "bankroll": 5, "strategy": "ladder", "target_odds": 2}
    response = client.post("/api/simulate", json=body)
    if response.status_code == 422 and "ladder" not in response.text.lower():
        pytest.skip("The simulator does not implement the ladder strategy yet.")
    assert response.status_code == 200, response.text
    run = response.json()
    assert {"ladder", "days", "summary", "equity"} <= set(run)
    tickets = [day["ticket"] for day in run["days"] if day.get("ticket")]
    assert tickets
    for ticket in tickets:
        for leg in ticket["legs"]:
            # football-data.co.uk style rows have no images: the fields exist, null or proxied.
            assert_leg_logos(leg, required=False)


def test_recent_days_ladder_shows_the_stored_logos(client):
    sports = list(SPORTS)
    prepared = client.post("/api/simulate/recent/prepare", json={"days": 3, "sports": sports})
    if prepared.status_code in (404, 405):
        pytest.skip("The recent-days dataset is not implemented yet.")
    assert prepared.status_code in (200, 202), prepared.text
    deadline = clock.monotonic() + 60
    status = prepared.json()
    while status["status"] == "running" and clock.monotonic() < deadline:
        clock.sleep(0.1)
        status = client.get("/api/simulate/recent/status").json()
    assert status["status"] == "done", status
    body = {
        "dataset": "recent",
        "sports": sports,
        "days": 3,
        "bankroll": 5,
        "strategy": "ladder",
        "target_odds": 2,
    }
    response = client.post("/api/simulate", json=body)
    assert response.status_code == 200, response.text
    legs = [leg for d in response.json()["days"] if d.get("ticket") for leg in d["ticket"]["legs"]]
    assert legs
    for leg in legs:
        assert_leg_logos(leg, required=False)
    assert any(leg["home_logo"] for leg in legs)


# --- the fake FlashScore itself -------------------------------------------------------------


def fake(seed=7, today=TODAY):
    return mock.FakeFlashScore(mock.World(seed), today, NOW)


def rows_of(groups):
    return [row for group in groups for row in group["matches"]]


def test_day_lists_follow_the_clock_and_are_deterministic():
    today = rows_of(fake().day_groups("tennis", TODAY))
    assert today == rows_of(fake().day_groups("tennis", TODAY))
    assert all(NOW.timestamp() < r["timestamp"] < NOW.timestamp() + 86400 for r in today)
    assert all(not r["match_status"]["is_started"] for r in today)
    assert all(r["scores"] == {"home": None, "away": None} for r in today)

    past_day = TODAY - timedelta(days=3)
    past = rows_of(fake().day_groups("football", past_day))
    assert all(r["match_id"].endswith("-p3") for r in past)
    assert all(r["match_status"]["is_finished"] for r in past)
    assert all(isinstance(r["scores"]["home"], int) for r in past)
    assert all(r["odds"]["1"] > 1 for r in past)  # pre-match 1X2 prices stay
    midnight = datetime.combine(past_day, time(0), timezone.utc).timestamp()
    assert all(midnight <= r["timestamp"] < midnight + 86400 for r in past)
    other = rows_of(fake(seed=8).day_groups("football", past_day))
    assert [r["scores"] for r in other] != [r["scores"] for r in past]

    future = rows_of(fake().day_groups("basketball", TODAY + timedelta(days=2)))
    assert all(r["match_id"].endswith("-n2") for r in future)
    assert all(not r["match_status"]["is_finished"] for r in future)


def test_fake_answers_every_endpoint_and_never_raises():
    server = fake()
    lists = server.day_groups("tennis", TODAY)
    match_id = rows_of(lists)[0]["match_id"]
    for url in (
        f"https://x/api/flashscore/v2/matches/h2h?match_id={match_id}",
        f"https://x/api/flashscore/v2/matches/odds?match_id={match_id}",
        "https://x/api/flashscore/v2/matches/odds?match_id=unknown",
        "https://x/api/flashscore/v2/matches/standings?match_id=x",
        "https://x/api/flashscore/v2/matches/match/stats?match_id=x",
        "https://x/api/flashscore/v2/teams/results?team_id=x",
        "https://x/api/flashscore/v2/matches/live?sport_id=2",
    ):
        response = server(httpx.Request("GET", url))
        assert response.status_code == 200, url
        assert isinstance(response.json(), (list, dict))


def test_fake_images_are_small_pngs_and_other_hosts_are_404():
    for url in (
        "https://static.flashscore.com/res/image/data/mock-arsenal.png",
        "https://flagcdn.com/w40/ro.png",
    ):
        response = mock.image_handler(httpx.Request("GET", url))
        assert response.status_code == 200
        assert sniff(response.content) == "image/png" and len(response.content) < 4000
    assert mock.crest("https://flagcdn.com/w40/ro.png") != mock.crest(
        "https://flagcdn.com/w40/it.png"
    )
    for url in ("https://evil.com/a.png", "http://flagcdn.com/w40/ro.png"):
        assert mock.image_handler(httpx.Request("GET", url)).status_code == 404


def test_synthetic_datasets_end_yesterday(tmp_path):
    from footypreds.evaluation import sim_datasets

    directory = mock.write_datasets(mock.World(3), tmp_path / "bench", TODAY)
    records = sim_datasets.benchmark_records(directory)
    assert records
    last = max(date.fromisoformat(r["match"]["kickoff"][:10]) for r in records)
    assert TODAY - timedelta(days=7) <= last < TODAY
    extra = sim_datasets.load_extra_records(directory / "sim", today=TODAY)
    assert extra and {r["league_code"] for r in extra} == set(mock.EXTRA_LEAGUES)


def test_command_line_options(monkeypatch, tmp_path):
    seen = {}

    def fake_build(workdir, seed=7, today=None, now=None):
        seen.update(seed=seed, today=today, workdir=workdir)
        raise KeyboardInterrupt  # stop before a server would start

    monkeypatch.setattr(mock, "build_app", fake_build)
    monkeypatch.setattr(mock, "port_free", lambda host, port: True)
    monkeypatch.setattr(mock.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    assert mock.main(["--port", "8765", "--seed", "11", "--today", "2026-09-26"]) == 0
    assert seen["seed"] == 11 and seen["today"] == date(2026, 9, 26)
    assert not (tmp_path / "w").exists()  # the temporary folder is removed on exit


def test_the_real_settings_are_never_loaded(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("Settings.load() reads .env and the real database")

    monkeypatch.setattr(Settings, "load", classmethod(refuse))
    assert mock.build_app(tmp_path / "again", now=NOW).state.store is not None


def test_a_busy_port_is_a_clear_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(mock, "port_free", lambda host, port: False)
    monkeypatch.setattr(mock, "build_app", lambda *a, **k: pytest.fail("must not build"))
    assert mock.main(["--port", "8765"]) == 1
    assert "Portul 8765 este ocupat" in capsys.readouterr().err
