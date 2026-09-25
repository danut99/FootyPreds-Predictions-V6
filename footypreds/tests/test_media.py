"""Logos and flags: URL allowlist, parsing, storage merge and display URLs in every response."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import api, recommend, wallet
from footypreds.api import SECURITY_HEADERS, create_app
from footypreds.config import Settings
from footypreds.domain import Match, image_url
from footypreds.media import (
    DISPLAY_PREFIX,
    display_url,
    match_media,
    public_match,
    with_leg_media,
)
from footypreds.provider import normalize_matches
from footypreds.sports.legs import leg
from footypreds.store import Store
from footypreds.tests.helpers import KICKOFF, fixture
from footypreds.tests.test_e2e_multisport import DAY, NOW, Fake, football_history

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"
CREST = "https://static.flashscore.com/res/image/data/Q1Fx8AcM-bqw7ByeG.png"
FLAG = "https://flagcdn.com/w40/ar.png"
LEAGUE = "https://static.flashscore.com/res/image/data/8b2S0lfU-Me7UOoo6.png"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def upstream(display):
    """The upstream URL inside a display URL (asserting its shape)."""
    assert display.startswith(DISPLAY_PREFIX)
    query = parse_qs(urlsplit(display).query)
    assert list(query) == ["u"]
    return query["u"][0]


# --- allowlist ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        CREST,
        FLAG,
        "https://flagcdn.com/w40/gb-eng.png",
        "https://static.flashscore.com/res/image/data/bookmakers/80-417.png",
    ],
)
def test_allowed_image_urls_are_kept_as_is(url):
    assert image_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        42,
        ["https://flagcdn.com/w40/ar.png"],
        "x.png",
        "/res/image/data/a.png",
        "http://static.flashscore.com/res/image/data/a.png",  # not https
        "ftp://static.flashscore.com/a.png",
        "https://evil.com/a.png",
        "https://static.flashscore.com.evil.com/a.png",
        "https://evilstatic.flashscore.com/a.png",
        "https://static.flashscore.com@evil.com/a.png",  # userinfo trick: host is evil.com
        "https://user:pass@static.flashscore.com/a.png",
        "https://evil.com/static.flashscore.com/a.png",
        "https://evil.com?u=https://static.flashscore.com/a.png",
        "https://static.flashscore.com:8443/a.png",
        "https://static.flashscore.com:443/a.png",
        "https://STATIC.FLASHSCORE.COM/a.png",
        "https://static.flashscore.com/res/../../etc/passwd",
        "https://static.flashscore.com/res/./a.png",
        "https://static.flashscore.com/res/%2e%2e/a.png",
        "https://static.flashscore.com/res\\..\\a.png",
        "https://static.flashscore.com//evil.com/a.png",
        "https://static.flashscore.com/a.png?x=1",
        "https://static.flashscore.com/a.png#frag",
        "https://static.flashscore.com/a b.png",
        "https://static.flashscore.com/a\n.png",
        "https://static.flashscore.com/a\t.png",
        " https://static.flashscore.com/a.png",
        "https://static.flashscore.com/ă.png",
        "https:///static.flashscore.com/a.png",
        "https://static.flashscore.com",
        "https://static.flashscore.com/",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "https://static.flashscore.com/" + "a" * 300 + ".png",
    ],
)
def test_other_image_urls_are_refused(url):
    assert image_url(url) is None
    assert display_url(url) is None


def test_match_keeps_allowed_logos_and_drops_the_rest():
    match = fixture(home_logo=CREST, away_logo="https://evil.com/x.png", league_logo=7)
    assert match.home_logo == CREST
    assert match.away_logo is None and match.league_logo is None
    # Old stored rows (no logo fields) stay valid.
    old = json.loads(fixture().model_dump_json())
    for field in ("home_logo", "away_logo", "league_logo"):
        old.pop(field)
    assert Match.model_validate(old).home_logo is None


# --- parsing ----------------------------------------------------------------------------------


def team(name, **images):
    return {"name": name, "team_id": f"t-{name}", **images}


def day_payload(home, away, group_image=None):
    group = {"name": "ENGLAND: Premier League", "country_name": "England", "matches": []}
    if group_image is not None:
        group["image_path"] = group_image
    group["matches"].append(
        {
            "match_id": "m1",
            "timestamp": KICKOFF.timestamp(),
            "home_team": home,
            "away_team": away,
            "match_status": {"is_started": False, "is_finished": False},
            "scores": {"home": None, "away": None},
        }
    )
    return [group]


def test_team_images_prefer_small_image_path_then_the_live_typo_then_image_path():
    other = "https://static.flashscore.com/res/image/data/other.png"
    payload = day_payload(
        team("A", small_image_path=CREST, smaill_image_path=other, image_path=other),
        team("B", smaill_image_path=other, image_path=CREST),
        group_image=LEAGUE,
    )
    (match,), rejected = normalize_matches(payload)
    assert rejected == 0
    assert (match.home_logo, match.away_logo, match.league_logo) == (CREST, other, LEAGUE)
    payload = day_payload(team("A", image_path=CREST), team("B"), group_image=None)
    (match,), _ = normalize_matches(payload)
    assert (match.home_logo, match.away_logo, match.league_logo) == (CREST, None, None)


def test_disallowed_images_are_dropped_but_the_fixture_is_kept():
    payload = day_payload(
        team("A", small_image_path="https://evil.com/a.png", image_path=CREST),
        team("B", small_image_path="http://static.flashscore.com/b.png", image_path=12),
        group_image="https://static.flashscore.com@evil.com/l.png",
    )
    (match,), rejected = normalize_matches(payload)
    assert rejected == 0
    # A refused field falls back to the next allowed one.
    assert (match.home_logo, match.away_logo, match.league_logo) == (CREST, None, None)


def test_nested_groups_inherit_the_league_logo():
    inner = day_payload(team("A"), team("B"))[0]
    payload = [{"name": "ENGLAND", "image_path": LEAGUE, "matches": [inner]}]
    (match,), _ = normalize_matches(payload)
    assert match.league_logo == LEAGUE


def test_captured_lists_carry_crests_flags_and_league_logos():
    tennis, _ = normalize_matches(load("list_tennis.json"), sport="tennis")
    captured = next(m for m in tennis if m.id == "KnR6QDo1")
    assert captured.home_logo == FLAG
    assert captured.away_logo == "https://flagcdn.com/w40/es.png"
    assert captured.league_logo == LEAGUE
    doubles = next(m for m in tennis if m.home == "Peers J./Venus M.")
    assert doubles.home_logo == "https://flagcdn.com/w40/au.png"  # first player's flag
    for name, sport in (("list_basketball", "basketball"), ("live_football", "football")):
        matches, _ = normalize_matches(load(f"{name}.json"), sport=sport)
        assert matches and all(m.home_logo and m.away_logo and m.league_logo for m in matches)


def test_h2h_rows_keep_their_image_path():
    rows, _ = normalize_matches(load("h2h_basketball.json"), results=True, sport="basketball")
    first = next(r for r in rows if r.id == "8xXArSOO")
    assert first.home_logo == CREST
    assert first.league_logo is None


# --- storage --------------------------------------------------------------------------------


def test_store_keeps_logos_when_a_newer_row_lacks_them(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_matches([fixture(home_logo=CREST, away_logo=FLAG, league_logo=LEAGUE)])
    store.save_matches([fixture(odds={"1": 2.0})])  # e.g. an H2H/results row without images
    kept = store.match("fixture")
    assert (kept.home_logo, kept.away_logo, kept.league_logo) == (CREST, FLAG, LEAGUE)
    assert kept.odds == {"1": 2.0}
    newer = "https://static.flashscore.com/res/image/data/new.png"
    store.save_matches([fixture(home_logo=newer)])
    assert store.match("fixture").home_logo == newer
    assert store.match("fixture").away_logo == FLAG


def test_saving_the_same_row_again_changes_nothing(tmp_path):
    store = Store(tmp_path / "s.db")
    row = fixture(home_logo=CREST)
    assert store.save_matches([row]) == 1
    version = store.version
    assert store.save_matches([fixture()]) == 0 and store.version == version


# --- display URLs ---------------------------------------------------------------------------


def test_display_url_is_same_origin_and_round_trips():
    shown = display_url(CREST)
    encoded = "https%3A%2F%2Fstatic.flashscore.com%2Fres%2Fimage%2Fdata%2FQ1Fx8AcM-bqw7ByeG.png"
    assert shown == "/api/img?u=" + encoded
    assert upstream(shown) == CREST


def test_public_match_exposes_only_display_urls():
    match = fixture(home_logo=CREST, away_logo=FLAG)
    shown = public_match(match)
    assert upstream(shown["home_logo"]) == CREST
    assert upstream(shown["away_logo"]) == FLAG
    assert shown["league_logo"] is None
    assert CREST not in json.dumps({k: v for k, v in shown.items() if k != "home_logo"})
    assert match_media(fixture()) == dict.fromkeys(("home_logo", "away_logo", "league_logo"))


def test_leg_carries_display_logos():
    match = fixture(home_logo=CREST, league_logo=LEAGUE)
    market = {
        "key": "1",
        "label": "Victorie gazde",
        "group": "Rezultat final",
        "probability": 0.6,
        "odds": 1.8,
        "fair_odds": 1.67,
        "ev": 0.08,
    }
    item = leg(match, {"grade": "A", "confidence": 80}, market)
    assert upstream(item["home_logo"]) == CREST
    assert item["away_logo"] is None
    assert upstream(item["league_logo"]) == LEAGUE


def test_legs_stored_before_logos_get_them_from_the_stored_match(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_matches([fixture(home_logo=CREST, away_logo=FLAG)])
    old_leg = {"match_id": "fixture", "home": "Strong", "away": "Weak", "key": "1"}
    unknown = {"match_id": "gone", "home": "A", "away": "B", "key": "2"}
    current = old_leg | {"home_logo": "/api/img?u=kept", "away_logo": None, "league_logo": None}
    payload = {
        "tickets": [{"legs": [old_leg, unknown]}],
        "singles": [current],
        "ticket": {"legs": []},
        "note": "text",
    }
    shown = with_leg_media(payload, store)
    first, second = shown["tickets"][0]["legs"]
    assert upstream(first["home_logo"]) == CREST and upstream(first["away_logo"]) == FLAG
    assert second["home_logo"] is None and second["league_logo"] is None
    assert shown["singles"][0] == current  # already decorated: unchanged
    assert shown["note"] == "text" and payload["tickets"][0]["legs"][0] == old_leg


# --- every response -------------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(recommend, "utcnow", lambda: NOW)
    monkeypatch.setattr(wallet, "utcnow", lambda: NOW)


@pytest.fixture
def client(tmp_path, frozen):
    settings = Settings(api_key="k", database=tmp_path / "media.db")
    app = create_app(settings, httpx.MockTransport(Fake()))
    app.state.store.save_matches(football_history())
    with TestClient(app) as test_client:
        yield test_client


def assert_logos(item, required=True):
    for field in ("home_logo", "away_logo", "league_logo"):
        assert field in item, field
        value = item[field]
        if value is None:
            assert not required, field
        else:
            assert image_url(upstream(value)), value


def test_csp_images_stay_same_origin(client):
    policy = client.get("/api/sports").headers["content-security-policy"]
    assert "img-src 'self' data:;" in policy
    assert "flashscore" not in policy and "flagcdn" not in policy
    assert SECURITY_HEADERS["Content-Security-Policy"] == policy


def test_board_analysis_and_match_list_show_display_logos(client):
    board = client.get(f"/api/predictions?day={DAY}&sport=tennis&limit=100").json()
    item = next(i for i in board["items"] if i["match"]["id"] == "KnR6QDo1")
    assert_logos(item["match"])
    assert upstream(item["match"]["home_logo"]) == FLAG
    assert item["home_logo"] == item["match"]["home_logo"]
    raw = json.dumps(board)
    assert '"https://flagcdn.com' not in raw and '"https://static.flashscore.com' not in raw
    assert_logos(client.get("/api/analysis/KnR6QDo1?sport=tennis").json()["match"])
    analyzed = client.post("/api/analyze/KnR6QDo1?sport=tennis", json={"enrich": False}).json()
    assert_logos(analyzed["match"])
    listed = client.get(f"/api/matches?day={DAY}&sport=basketball").json()["matches"]
    assert listed and all(m["home_logo"].startswith(DISPLAY_PREFIX) for m in listed)
    football = client.get(f"/api/predictions?day={DAY}").json()["items"][0]
    assert_logos(football["match"], required=False)  # the synthetic football day has no images


def test_recommendations_tickets_and_wallet_legs_show_display_logos(client):
    data = client.get(f"/api/recommendations?day={DAY}").json()
    legs = [leg for t in data["tickets"] for leg in t["legs"]] + data["singles"]
    assert {leg["sport"] for leg in legs} >= {"football", "tennis"}
    for item in legs:
        # The synthetic football day of the shared Fake has no images; the captured lists do.
        assert_logos(item, required=item["sport"] != "football")
    generated = client.post("/api/tickets/generate", json={"day": DAY, "target_odds": 3}).json()
    assert generated["ticket"]["legs"]
    for item in generated["ticket"]["legs"] + [
        item for alternative in generated["alternatives"] for item in alternative["legs"]
    ]:
        assert_logos(item, required=item["sport"] != "football")
    client.post("/api/wallet/deposit", json={"amount": 100})
    single = next(s for s in data["singles"] if s["sport"] == "tennis")
    body = {"stake": 5, "legs": [{"match_id": single["match_id"], "key": single["key"]}]}
    placed = client.post("/api/wallet/bet", json=body)
    assert placed.status_code == 200, placed.text
    assert_logos(placed.json()["bets"][0]["legs"][0])


def test_live_items_show_display_logos(client):
    for sport in ("football", "basketball", "tennis"):
        items = client.get(f"/api/live?sport={sport}").json()["matches"]
        assert items
        for item in items:
            # A few captured players have no flag (small_image_path null): None is right.
            assert_logos(item["match"], required=False)
            assert item["home_logo"] == item["match"]["home_logo"]
        assert sum(item["home_logo"] is not None for item in items) > len(items) / 2
    first = client.get("/api/live?sport=tennis").json()["matches"][0]["match"]["id"]
    assert_logos(client.get(f"/api/live/{first}?sport=tennis").json()["match"])


def test_importing_the_api_module_does_not_build_the_app(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(api, "create_app", lambda: sentinel)
    had = vars(api).pop("app", None)
    try:
        assert api.app is sentinel
        with pytest.raises(AttributeError):
            api.no_such_name  # noqa: B018
    finally:
        vars(api).pop("app", None)
        if had is not None:
            vars(api)["app"] = had
