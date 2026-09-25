"""Regression tests for bugs found in the code audit (one test per fixed defect)."""

import asyncio
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds.api import LEDGER_THRESHOLD, PAST_DAY_TTL, AnalysisCache, HistorySync, create_app
from footypreds.config import PACKAGE, Settings
from footypreds.domain import Match
from footypreds.engine import analyze, with_params
from footypreds.engine import markets as mk
from footypreds.engine.analyzer import H2H_DISPLAY_DAYS, form_factors, market_probabilities
from footypreds.engine.history import is_youth
from footypreds.engine.ratings import Ratings
from footypreds.provider import normalize_matches, parse_standings
from footypreds.store import Store
from footypreds.tests.helpers import KICKOFF, fixture, result, strong_history

NOW = datetime.now(timezone.utc)


def euro_history(kickoff, final_days=800):
    """England and Spain: recent form in the last 2 years, the Euro final long before."""
    rows = [
        Match(
            id="euro-final",
            kickoff=kickoff - timedelta(days=final_days),
            league="EUROPE: Euro",
            home="Spain",
            away="England",
            status="finished",
            home_goals=2,
            away_goals=1,
        ),
        # Older than the 10-year display window: never shown.
        Match(
            id="ancient",
            kickoff=kickoff - timedelta(days=H2H_DISPLAY_DAYS + 30),
            league="World: Friendly International",
            home="England",
            away="Spain",
            status="finished",
            home_goals=0,
            away_goals=1,
        ),
    ]
    for i in range(12):
        rows.append(
            Match(
                id=f"eng-{i}",
                kickoff=kickoff - timedelta(days=20 * (i + 1)),
                league="EUROPE: World Cup Qualification",
                home="England",
                away=f"Opp {i}",
                status="finished",
                home_goals=2,
                away_goals=0,
            )
        )
        rows.append(
            Match(
                id=f"esp-{i}",
                kickoff=kickoff - timedelta(days=20 * (i + 1) + 3),
                league="EUROPE: Nations League",
                home=f"Rival {i}",
                away="Spain",
                status="finished",
                home_goals=1,
                away_goals=2,
            )
        )
    return rows


# --- engine -------------------------------------------------------------------------------


def test_h2h_display_uses_ten_years_but_model_h2h_keeps_its_window():
    game = fixture(home="England", away="Spain", league="World: Friendly International")
    analysis = analyze(game, euro_history(game.kickoff))
    h2h = analysis["h2h"]
    assert h2h["played"] == 1
    assert h2h["matches"][0]["id"] == "euro-final"
    # From England's (current home) perspective the final was a defeat.
    assert (h2h["home_wins"], h2h["draws"], h2h["away_wins"]) == (0, 0, 1)
    assert h2h["window_days"] == H2H_DISPLAY_DAYS
    assert analysis["sample"]["h2h"] == 1
    assert any("Meciuri directe" in note for note in analysis["insights"])
    # The tuned model input still only sees mutual games within max_days (730).
    assert analysis["components"]["h2h"]["matches"] == 0
    # Form stays limited to the 2-year window.
    assert all(g["id"] != "euro-final" for g in analysis["form"]["home"]["last"])


def test_h2h_display_never_leaks_results_inside_the_cutoff():
    game = fixture(home="England", away="Spain")
    late = Match(
        id="late",
        kickoff=game.kickoff - timedelta(hours=2),
        league="Test",
        home="England",
        away="Spain",
        status="finished",
        home_goals=5,
        away_goals=0,
    )
    analysis = analyze(game, [*euro_history(game.kickoff), late])
    assert [m["id"] for m in analysis["h2h"]["matches"]] == ["euro-final"]


def test_h2h_ignores_a_namesake_opponent_with_another_team_id():
    game = fixture(home_id="h1", away_id="w1")
    rows = [
        result("real", 30, "Strong", "Weak", 1, 1, home_id="h1", away_id="w1"),
        # Same name, different known ID: a namesake from another country.
        result("namesake", 40, "Strong", "Weak", 7, 0, home_id="h1", away_id="w-other"),
        # H2H payload rows carry no IDs and are matched by name.
        result("no-ids", 50, "Weak", "Strong", 0, 2),
    ]
    analysis = analyze(game, rows)
    assert [m["id"] for m in analysis["h2h"]["matches"]] == ["real", "no-ids"]
    assert analysis["components"]["h2h"]["matches"] == 2


def test_form_window_zero_means_no_form_adjustment():
    rows = [(m, "home") for m in strong_history()]
    attack, defence = form_factors(rows, "strong", Ratings(), with_params(form_window=0))
    assert (attack, defence) == (1.0, 1.0)


@pytest.mark.parametrize(
    "odds",
    [
        {"1": 1.0, "X": 3.0, "2": 4.0},
        {"1": 0.5, "X": 3.0, "2": 4.0},
        {"1": float("nan"), "X": 3.0, "2": 4.0},
        {"1": float("inf"), "X": 3.0, "2": 4.0},
        {"1": "2.1", "X": 3.0, "2": 4.0},
        {"1": 2.0, "X": 3.0},
    ],
)
def test_market_probabilities_rejects_non_prices(odds):
    assert market_probabilities(odds) is None


def test_analysis_with_unvalidated_odds_falls_back_to_model():
    # model_copy() skips the Match validator, so analyze() must defend itself.
    game = fixture().model_copy(update={"odds": {"1": 0.9, "X": 0.9, "2": 0.9}})
    analysis = analyze(game, strong_history())
    assert analysis["components"]["market_1x2"] is None
    assert sum(m["probability"] for m in analysis["markets"] if m["key"] in ("1", "X", "2")) == (
        pytest.approx(1)
    )


def test_reweight_stays_a_distribution_when_a_region_is_empty():
    matrix = [[0.0, 0.5], [0.5, 0.0]]  # no draws at all
    output = mk.reweight(matrix, {"1": 0.4, "X": 0.2, "2": 0.4})
    assert sum(map(sum, output)) == pytest.approx(1)
    assert mk.one_x_two(output)["1"] == pytest.approx(mk.one_x_two(output)["2"])


def test_u16_competitions_are_youth_like_on_the_board():
    assert is_youth("GERMANY: U16 Bundesliga")
    assert is_youth("ENGLAND: Premier League U15")
    assert not is_youth("ENGLAND: Premier League")


# --- store ----------------------------------------------------------------------------------


def test_saving_unchanged_matches_keeps_version_and_analysis_cache(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = strong_history()
    assert store.save_matches(rows) == len(rows)
    version = store.version
    cache = AnalysisCache(store)
    first = cache.get(fixture())
    # Re-loading the same day must not throw away every cached analysis.
    assert store.save_matches(rows) == 0
    assert store.version == version
    assert cache.get(fixture()) is first
    changed = rows[0].model_copy(update={"home_goals": 0})
    assert store.save_matches([changed]) == 1
    assert store.version == version + 1


def test_finished_match_is_not_downgraded_and_keeps_ids_and_odds(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    finished = result("m1", 1, "Strong", "Weak", 2, 1, home_id="h", away_id="a")
    finished = finished.model_copy(update={"odds": {"1": 1.5}, "country": "England"})
    store.save_matches([finished])
    version = store.version
    scheduled = fixture(id="m1", kickoff=finished.kickoff)
    assert store.save_matches([scheduled]) == 0 and store.version == version
    bare = finished.model_copy(update={"home_id": "", "away_id": "", "odds": {}, "country": ""})
    assert store.save_matches([bare]) == 0
    saved = store.match("m1")
    assert (saved.home_id, saved.away_id, saved.odds, saved.country) == (
        "h",
        "a",
        {"1": 1.5},
        "England",
    )


class RacyStore(Store):
    """Runs `hook` inside the write transaction, i.e. before the commit."""

    hook = None

    @contextmanager
    def connect(self):
        with super().connect() as db:
            yield db
            hook, self.hook = self.hook, None
            if hook:
                hook()


def test_reader_during_a_write_never_caches_stale_rows(tmp_path):
    store = RacyStore(tmp_path / "db.sqlite3")
    store.save_matches(strong_history()[:1])
    seen = []
    store.hook = lambda: seen.append(len(store.matches()))  # concurrent reader, pre-commit
    store.save_matches(strong_history()[1:3])
    assert seen == [1]
    assert len(store.matches()) == 3


# --- analysis cache -------------------------------------------------------------------------


def test_analysis_cache_key_includes_kickoff_and_team_ids(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_matches(strong_history())
    cache = AnalysisCache(store)
    first = cache.get(fixture())
    moved = cache.get(fixture(kickoff=KICKOFF + timedelta(days=200)))
    assert moved is not first
    assert moved["form"]["home"]["days_since_last"] != first["form"]["home"]["days_since_last"]
    assert cache.get(fixture(home_id="other")) is not first


def test_analysis_cache_is_thread_safe_under_eviction(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_matches(strong_history())
    cache = AnalysisCache(store, size=2)
    games = [fixture(id=f"f{i}", kickoff=KICKOFF + timedelta(hours=i)) for i in range(6)]
    errors = []

    def work():
        try:
            for _ in range(15):
                for game in games:
                    cache.get(game)
        except Exception as exc:  # pragma: no cover - the regression
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(cache.items) <= 2
    index, version = cache.snapshot()
    assert version == store.version and index is cache.history()


# --- history sync ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def fixtures(self, day, refresh=False, ttl=None):
        self.calls.append((day, ttl))
        if self.error:
            raise self.error
        return [], False, 0


def test_history_sync_unexpected_error_is_reported_not_stuck_running(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    sync = HistorySync(store, FakeProvider(RuntimeError("database is locked")))
    today = NOW.date()
    sync.state["status"] = "running"
    asyncio.run(sync.run([today - timedelta(days=3)], today))
    assert sync.state["status"] == "failed"
    assert "database" not in sync.state["message"]
    assert store.synced_days() == set()


def test_history_sync_does_not_cache_yesterday_for_a_month(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    provider = FakeProvider()
    sync = HistorySync(store, provider)
    today = NOW.date()
    days = [today - timedelta(days=1), today - timedelta(days=2)]
    asyncio.run(sync.run(days, today))
    assert dict(provider.calls) == {days[0]: None, days[1]: PAST_DAY_TTL}
    # Yesterday is re-checked next time; older days are done.
    assert store.synced_days() == {days[1].isoformat()}


# --- API ------------------------------------------------------------------------------------


def no_network():
    def handle(request):
        raise AssertionError(f"unexpected FlashScore call: {request.url.path}")

    return httpx.MockTransport(handle)


def upcoming_game(**extra):
    kickoff = (NOW + timedelta(days=1)).replace(microsecond=0)
    return fixture(kickoff=kickoff, **extra)


def recent_strong_history(kickoff):
    return [
        Match(
            id=f"recent-{i}",
            kickoff=kickoff - timedelta(days=7 * (i + 1)),
            league="Test",
            home="Strong",
            away="Weak",
            status="finished",
            home_goals=4,
            away_goals=0,
        )
        for i in range(25)
    ]


@pytest.fixture
def client(tmp_path):
    settings = Settings(api_key="k", database=tmp_path / "api.db")
    with TestClient(create_app(settings, no_network())) as test_client:
        yield test_client


def test_ledger_snapshot_always_uses_the_ledger_threshold(client):
    store = client.app.state.store
    game = upcoming_game()
    store.save_matches([game, *recent_strong_history(game.kickoff)])
    body = client.post("/api/analyze/fixture", json={"threshold": 0.5, "enrich": False}).json()
    assert body["prediction"]["threshold"] == 0.5
    assert body["saved"]
    ledger = store.predictions()
    assert len(ledger) == 1
    assert ledger[0]["prediction"]["threshold"] == LEDGER_THRESHOLD


def test_local_analysis_of_a_started_scheduled_match_is_retrospective(client):
    store = client.app.state.store
    store.save_matches([fixture(kickoff=NOW - timedelta(hours=1))])
    assert client.get("/api/analysis/fixture").json()["retrospective"] is True
    store.save_matches([upcoming_game(id="later")])
    assert client.get("/api/analysis/later").json()["retrospective"] is False


def test_unexpected_server_error_is_json_for_the_spa(tmp_path, monkeypatch):
    settings = Settings(api_key="k", database=tmp_path / "api.db")
    app = create_app(settings, no_network())

    def boom(_match_id):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(app.state.store, "match", boom)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/analysis/x")
    assert response.status_code == 500
    assert "jurnalul serverului" in response.json()["detail"]
    assert "secret" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]


def test_cross_site_browser_gets_cannot_spend_the_api_quota(client):
    blocked = client.get("/api/health", headers={"sec-fetch-site": "cross-site"})
    assert blocked.status_code == 403
    assert blocked.headers["Referrer-Policy"] == "no-referrer"
    assert client.get("/api/health", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    # Non-browser clients (Excel, scripts) send no fetch metadata.
    assert client.get("/api/health").status_code == 200
    dev = {"sec-fetch-site": "cross-site", "origin": "http://127.0.0.1:5500"}
    assert client.get("/api/health", headers=dev).status_code == 200
    # Static files are not API calls.
    assert client.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 200


def test_england_spain_full_analysis_shows_the_euro_final(tmp_path):
    """The reported case: a real-schema H2H payload, pre-1970 rows, final > 730 days ago."""
    kickoff = (NOW + timedelta(days=1)).replace(microsecond=0)
    final = kickoff - timedelta(days=800)
    rows = [
        {
            "match_id": "final",
            "timestamp": final.timestamp(),
            "status": "FINISHED",
            "winner": "home",
            "tournament_name": "Euro",
            "home_team": {"name": "Spain", "image_path": "x.png"},
            "away_team": {"name": "England", "image_path": "y.png"},
            "scores": {"home": "2", "away": "1"},
        },
        {
            "match_id": "1950",
            "timestamp": -615_000_000,
            "status": "FINISHED",
            "tournament_name": "World Cup",
            "home_team": {"name": "England"},
            "away_team": {"name": "Spain"},
            "scores": {"home": "0", "away": "1"},
        },
        "not a row",
    ]
    for i in range(10):
        when = (kickoff - timedelta(days=15 * (i + 1))).timestamp()
        rows.append(
            {
                "match_id": f"e{i}",
                "timestamp": when,
                "status": "FINISHED",
                "tournament_name": "World Cup - Qualification",
                "home_team": {"name": "England"},
                "away_team": {"name": f"Opp {i}"},
                "scores": {"home": "3", "away": "0"},
            }
        )
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("matches/h2h"):
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("matches/standings"):
            return httpx.Response(200, json=[{"group": "A"}, "junk", 3])
        if request.url.path.endswith("matches/odds"):
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    settings = Settings(api_key="k", database=tmp_path / "api.db")
    with TestClient(create_app(settings, httpx.MockTransport(handle))) as test_client:
        store = test_client.app.state.store
        store.save_matches(
            [
                Match(
                    id="eng-esp",
                    kickoff=kickoff,
                    league="World: Friendly International",
                    home="England",
                    away="Spain",
                    home_id="eng",
                    away_id="esp",
                )
            ]
        )
        response = test_client.post("/api/analyze/eng-esp", json={})
    assert response.status_code == 200
    body = response.json()
    h2h = body["prediction"]["h2h"]
    assert h2h["played"] == 1 and h2h["matches"][0]["id"] == "final"
    assert body["standings"] == []
    # Enrichment = H2H + standings + every quoted market price (docs/CONTRACTS.md).
    assert sorted(calls) == [
        "/api/flashscore/v2/matches/h2h",
        "/api/flashscore/v2/matches/odds",
        "/api/flashscore/v2/matches/standings",
    ]


def provider_row(**changes):
    row = {
        "match_id": "m1",
        "timestamp": NOW.timestamp(),
        "home_team": {"name": "Home"},
        "away_team": {"name": "Away"},
    }
    return row | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"scores": "1-0"},
        {"match_status": "FINISHED"},
        {"home_team": "Home"},
        {"away_team": None},
    ],
)
def test_one_malformed_provider_row_does_not_abort_the_day(changes):
    rows, rejected = normalize_matches([provider_row(**changes), provider_row(match_id="ok")])
    assert [m.id for m in rows] == ["ok"] and rejected == 1


def test_malformed_odds_keep_the_fixture_without_prices():
    (row,), rejected = normalize_matches([provider_row(odds=[1.5, 3.2, 4.0])])
    assert rejected == 0 and row.odds == {}


def test_dash_placeholder_keeps_a_scheduled_fixture_but_never_a_result():
    (row,), _ = normalize_matches([provider_row(scores={"home": "-", "away": "-"})])
    assert row.status == "scheduled" and row.home_goals is None
    rows, rejected = normalize_matches(
        [provider_row(scores={"home": "-", "away": "-"})], results=True
    )
    assert rows == [] and rejected == 1


def test_group_with_null_country_or_name_keeps_its_matches():
    payload = [{"name": None, "country_name": None, "matches": [provider_row()]}]
    (row,), rejected = normalize_matches(payload)
    assert rejected == 0 and row.country == "" and row.league == "Unknown"


def test_standings_parser_skips_non_object_rows():
    rows = parse_standings(["junk", 3, None, {"name": "Team", "goals": "3:1", "points": 3}])
    assert [(r["position"], r["name"], r["scored"]) for r in rows] == [(4, "Team", 3)]


# --- frontend (static checks: no browser in the test environment) ---------------------------

WEB = PACKAGE / "web"


def test_frontend_has_no_inline_styles_blocked_by_csp():
    for name in ("app.js", "index.html"):
        assert 'style="' not in (WEB / name).read_text(encoding="utf-8"), name


def test_frontend_polling_and_download_do_not_leak_or_navigate_away():
    source = (WEB / "app.js").read_text(encoding="utf-8")
    # setInterval piled up overlapping plan polls and outlived the tickets page.
    assert "setInterval" not in source
    # Navigating to the export URL replaced the SPA with raw JSON on errors.
    assert "location.href" not in source
    assert "state.boardRequest" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_script_parses():
    check = subprocess.run(
        ["node", "--check", str(WEB / "app.js")], capture_output=True, text=True, check=False
    )
    assert check.returncode == 0, check.stderr
