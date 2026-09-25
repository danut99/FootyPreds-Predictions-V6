"""Excel client: /api/excel contract, VBA <-> API cross-contract, VBA static checks, builder.

VBA cannot run on the CI machine (no Excel), so FootyPreds.bas is verified statically:
it must compile-in-principle (declarations, balanced blocks, known procedures) and every
URL, query parameter, section and column it uses must be served by footypreds/excel_api.py.

Two apps are used: a small football day (``client``) for the historical tables, and the
multi-sport app of tests/test_e2e_multisport.py (captured FlashScore payloads, frozen clocks,
a tmp simulator benchmark) for basketball/tennis and the product tables (``multi``).
"""

import csv
import importlib.util
import io
import itertools
import math
import re
import subprocess
import sys
import types
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import footypreds.provider as provider_module
from footypreds import excel_api as xa
from footypreds import recommend, wallet
from footypreds.api import create_app
from footypreds.config import PACKAGE, Settings
from footypreds.evaluation import sim_datasets
from footypreds.tests import test_e2e_multisport as e2e
from footypreds.tests.helpers import h2h_payload
from footypreds.tests.test_simulator import football_records
from footypreds.tests.test_simulator_datasets import write_benchmark

CLIENT_DIR = PACKAGE / "excel_client"
BAS_PATH = CLIENT_DIR / "FootyPreds.bas"
TSV_TYPE = "text/tab-separated-values; charset=utf-8"
CSV_TYPE = "text/csv; charset=utf-8"
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")

# Two days ahead: every fixture is in the future whatever the local time zone.
DAY = (datetime.now(timezone.utc) + timedelta(days=2)).date()
KICKOFF = datetime.combine(DAY, time(15), timezone.utc)
# 1/2.2 + 1/3.6 + 1/4 = 0.982 < 1, so at least one 1X2 market has a positive EV.
VALUE_ODDS = {"1": 2.2, "X": 3.6, "2": 4.0}
# A clear favourite: 1X is above the 0.85 ledger threshold, so the ledger gets a pick.
FAVOURITE_ODDS = {"1": 1.15, "X": 8.5, "2": 21.0}
NASTY_HOME = 'Oțelul\tGalați "FC"'
NASTY_AWAY = "=Steaua\nBucurești\r"
STANDINGS = [
    {"name": "Strong", "team_id": "h", "matches_played": 3, "wins": 3, "goals": "7:2", "points": 9},
    {"name": "Mid", "team_id": "m", "matches_played": 3, "wins": 1, "goals": "3:3", "points": 4},
    {"name": "Weak", "team_id": "a", "matches_played": 3, "goals": "1:9", "points": 0},
]


def fixture_row(match_id, kickoff, home, away, odds=None, score=None, ids=("", "")):
    return {
        "match_id": match_id,
        "timestamp": kickoff.timestamp(),
        "home_team": {"name": home, "team_id": ids[0]},
        "away_team": {"name": away, "team_id": ids[1]},
        "match_status": {"is_started": score is not None, "is_finished": score is not None},
        "scores": {"home": score[0], "away": score[1]} if score else {"home": None, "away": None},
        "odds": odds or {},
    }


def day_payload(day):
    if day != DAY:
        return []
    return [
        {
            "name": "ENGLAND: Premier League",
            "country_name": "England",
            "matches": [
                fixture_row("m1", KICKOFF, "Strong", "Weak", FAVOURITE_ODDS, ids=("h", "a")),
                fixture_row("m0", KICKOFF - timedelta(hours=6), "Mid", "Other", score=(2, 1)),
            ],
        },
        {
            "name": "ROMANIA: Superliga",
            "country_name": "Romania",
            "matches": [
                fixture_row("m2", KICKOFF + timedelta(hours=2), NASTY_HOME, NASTY_AWAY, VALUE_ODDS),
            ],
        },
    ]


def flashscore(calls=None, status=200, standings=None):
    """Mock RapidAPI FlashScore: fixtures by date, h2h and standings. Never the network."""

    def handle(request):
        path = request.url.path
        if calls is not None:
            calls.append(path)
        if status != 200:
            return httpx.Response(status, json={"message": "secret upstream detail"})
        if path.endswith("matches/h2h"):
            return httpx.Response(200, json=h2h_payload(KICKOFF))
        if path.endswith("matches/standings"):
            return httpx.Response(200, json=STANDINGS if standings is None else standings)
        if path.endswith("matches/odds"):
            return httpx.Response(200, json=[])
        day = datetime.fromisoformat(request.url.params["date"]).date()
        return httpx.Response(200, json=day_payload(day))

    return httpx.MockTransport(handle)


def make_client(tmp_path, calls=None, status=200, api_key="test-key", **kwargs):
    settings = Settings(api_key=api_key, database=tmp_path / "excel.db")
    return TestClient(create_app(settings, flashscore(calls, status, **kwargs)))


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as test_client:
        yield test_client


def parse_tsv(response, status=200):
    """Strict reader: the exact rules FootyPreds.bas relies on."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == TSV_TYPE
    assert response.headers["cache-control"] == "no-store"
    text = response.content.decode("utf-8")
    assert not text.startswith("﻿")
    assert text.endswith("\r\n")
    lines = text.split("\r\n")[:-1]
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        assert "\n" not in line and "\r" not in line
        values = line.split("\t")
        assert len(values) == len(header), line
        rows.append(dict(zip(header, values)))
    return header, rows


def parse_csv(response, status=200):
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == CSV_TYPE
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.content.count(b"\xef\xbb\xbf") == 1
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    header = rows[0]
    assert all(len(r) == len(header) for r in rows[1:])
    return header, [dict(zip(header, r)) for r in rows[1:]]


def vba_parse(text):
    """Python twin of ParseTsv in FootyPreds.bas (vbCrLf/vbCr -> vbLf, Split, trim end)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    n = len(lines) - 1
    while n > 0 and not lines[n]:
        n -= 1
    header = lines[0].split("\t")
    return header, [dict(zip(header, line.split("\t"))) for line in lines[1 : n + 1]]


def get(client, path, fmt="tsv", **params):
    return client.get(path, params={"format": fmt, **params})


def load_day(client, **params):
    return parse_tsv(get(client, "/api/excel/predictions", day=DAY.isoformat(), **params))


# ------------------------------------------------------------------ rendering unit tests


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "1"),
        (False, "0"),
        (0, "0"),
        (42, "42"),
        (0.5, "0.5"),
        (1 / 3, "0.333333"),
        (2.0, "2"),
        (1.5e-5, "0.000015"),
        (1e-9, "0"),
        (-1e-9, "0"),
        (-0.25, "-0.25"),
        (12345678.9, "12345678.9"),
        (float("nan"), ""),
        (float("inf"), ""),
        ("Oțelul", "Oțelul"),
    ],
)
def test_cell_is_locale_free_and_never_scientific(value, expected):
    assert xa.cell(value) == expected


def test_render_sanitises_tsv_and_quotes_csv():
    rows = [{"a": "x\ty\nz\r w", "b": 'say "hi", ok', "c": "=SUM(A1)"}]
    tsv = xa.render(("a", "b", "c", "missing"), rows, "tsv")
    assert tsv == 'a\tb\tc\tmissing\r\nx y z  w\tsay "hi", ok\t=SUM(A1)\t\r\n'
    text = xa.render(("a", "b", "c"), rows, "csv")
    assert text.startswith("﻿a,b,c\r\n")
    assert '"say ""hi"", ok"' in text
    parsed = list(csv.reader(io.StringIO(text[1:], newline="")))
    assert parsed[1] == ["x y z  w", 'say "hi", ok', "'=SUM(A1)"]


def test_csv_formula_guard_never_touches_numbers():
    text = xa.render(("n", "s"), [{"n": -0.5, "s": "-bad"}], "csv")
    assert text.splitlines()[1] == "-0.5,'-bad"


# ------------------------------------------------------------------ endpoint contract


def test_every_excel_route_is_documented_and_uses_error_tables():
    routes = [r for r in xa.router.routes]
    assert {r.path for r in routes} == set(xa.TABLES)
    assert all(isinstance(r, xa.ExcelRoute) for r in routes)


def test_health_in_tsv_and_csv(client):
    header, rows = parse_tsv(get(client, "/api/excel/health"))
    assert header == list(xa.HEALTH_COLUMNS)
    assert rows[0]["status"] == "ok" and rows[0]["api_configured"] == "1"
    assert rows[0]["excel_api"] == xa.EXCEL_API_VERSION
    assert "test-key" not in client.get("/api/excel/health").text
    header, rows = parse_csv(client.get("/api/excel/health"))
    assert header == list(xa.HEALTH_COLUMNS) and len(rows) == 1


def test_predictions_board_columns_numbers_and_order(client):
    header, rows = load_day(client)
    assert header == list(xa.PREDICTION_COLUMNS)
    # Popular competitions first, then fixtures with odds, then kickoff.
    assert [r["match_id"] for r in rows] == ["m1", "m2", "m0"]
    numeric = [
        c
        for c in header
        if c.startswith(("p_", "xg_", "odds_", "fair_", "ppg_", "win_rate_", "sample_"))
        or c in ("confidence", "value_odds", "value_ev")
    ]
    for row in rows:
        for column in numeric:
            assert row[column] == "" or NUMBER.match(row[column]), (column, row[column])
        assert row["grade"] in "ABCD" and row["upcoming"] in ("0", "1")
        total = sum(float(row[k]) for k in ("p_1", "p_x", "p_2"))
        assert total == pytest.approx(1, abs=1e-5)
        for column in header:
            if column.startswith("p_"):
                assert 0 <= float(row[column]) <= 1
    strong = rows[0]
    assert strong["competition"] == "Premier League"
    assert strong["competition_id"] == "england|premier league"
    assert (strong["odds_1"], strong["odds_x"], strong["odds_2"]) == ("1.15", "8.5", "21")
    assert strong["date_utc"] == DAY.isoformat() and strong["time_utc"] == "15:00"
    assert strong["kickoff_utc"] == f"{DAY.isoformat()}T15:00:00Z"
    assert strong["upcoming"] == "1" and strong["status"] == "scheduled"
    assert strong["result"] == "" and strong["tip_won"] == ""
    finished = rows[2]
    assert finished["status"] == "finished" and finished["upcoming"] == "0"
    assert finished["result"] == "2-1" and finished["tip_won"] in ("0", "1")


def test_hostile_team_names_are_sanitised_in_both_formats(client):
    _, rows = load_day(client)
    nasty = next(r for r in rows if r["match_id"] == "m2")
    assert nasty["home"] == 'Oțelul Galați "FC"'
    assert nasty["away"] == "=Steaua București "
    assert nasty["competition"] == "Superliga"
    _, rows = parse_csv(get(client, "/api/excel/predictions", "csv", day=DAY.isoformat()))
    nasty = next(r for r in rows if r["match_id"] == "m2")
    assert nasty["home"] == 'Oțelul Galați "FC"'
    # Opened by double-click, a CSV cell starting with "=" would run as a formula.
    assert nasty["away"] == "'=Steaua București "
    raw = get(client, "/api/excel/predictions", "csv", day=DAY.isoformat()).content
    assert '"Oțelul Galați ""FC"""'.encode() in raw


def test_predictions_filters(client):
    _, rows = load_day(client, competition="romania|superliga")
    assert [r["match_id"] for r in rows] == ["m2"]
    _, rows = load_day(client, upcoming_only="1")
    assert {r["match_id"] for r in rows} == {"m1", "m2"}
    _, rows = load_day(client, limit="1", offset="1")
    assert [r["match_id"] for r in rows] == ["m2"]
    _, everything = load_day(client)
    for grade in "ABCD":
        _, rows = load_day(client, min_grade=grade.lower())
        allowed = "ABCD"[: "ABCD".index(grade) + 1]
        assert [r["match_id"] for r in rows] == [
            r["match_id"] for r in everything if r["grade"] in allowed
        ]


def test_demo_board_needs_no_provider_and_persists_nothing(tmp_path):
    calls = []
    with make_client(tmp_path, calls, api_key="") as client:
        header, rows = load_day(client, demo="1")
        assert len(rows) == 6 and {r["source"] for r in rows} == {"synthetic"}
        _, competitions = parse_tsv(
            get(client, "/api/excel/competitions", day=DAY.isoformat(), demo="1")
        )
        assert competitions[0]["matches"] == "6"
        assert calls == []
        assert client.app.state.store.matches() == []


def test_competitions_match_board_ids(client):
    header, rows = parse_tsv(get(client, "/api/excel/competitions", day=DAY.isoformat()))
    assert header == list(xa.COMPETITION_COLUMNS)
    _, board = load_day(client)
    assert {r["competition_id"] for r in rows} == {r["competition_id"] for r in board}
    england = next(r for r in rows if r["competition"] == "Premier League")
    assert england["matches"] == "2" and england["country"] == "England"
    assert england["popular"] == "1"


def test_every_match_section_before_full_analysis(client):
    load_day(client)
    for section, columns in xa.SECTIONS.items():
        header, rows = parse_tsv(get(client, "/api/excel/match/m1", section=section))
        assert header == list(columns), section
    _, rows = parse_tsv(get(client, "/api/excel/match/m1", section="summary"))
    assert rows[0]["retrospective"] == "0" and rows[0]["h2h_played"] == "0"
    _, markets = parse_tsv(get(client, "/api/excel/match/m1", section="markets"))
    assert sum(r["is_tip"] == "1" for r in markets) == 1
    assert {"1", "X", "2", "btts", "ht_1"} <= {r["key"] for r in markets}
    _, scores = parse_tsv(get(client, "/api/excel/match/m1", section="scores"))
    assert len(scores) == 10 and scores[0]["rank"] == "1"
    assert float(scores[0]["probability"]) >= float(scores[-1]["probability"])
    _, grid = parse_tsv(get(client, "/api/excel/match/m1", section="grid"))
    assert [r["home_goals"] for r in grid] == ["0", "1", "2", "3", "4", "5"]
    _, htft = parse_tsv(get(client, "/api/excel/match/m1", section="htft"))
    assert len(htft) == 9
    assert sum(float(r["probability"]) for r in htft) == pytest.approx(1, abs=1e-4)


def test_full_analysis_enriches_saves_and_fills_every_section(tmp_path):
    calls = []
    with make_client(tmp_path, calls) as client:
        load_day(client)
        response = client.post(
            "/api/excel/analyze/m1", params={"format": "tsv", "threshold": "0.5"}
        )
        header, rows = parse_tsv(response)
        assert header == list(xa.ANALYZE_COLUMNS)
        row = rows[0]
        assert row["saved"] == "1" and row["retrospective"] == "0"
        assert row["selection_key"] and float(row["selection_p"]) >= 0.5
        assert int(row["sample_home"]) >= 6 and row["h2h_played"] == "1"
        assert calls.count("/api/flashscore/v2/matches/h2h") == 1
        _, form = parse_tsv(get(client, "/api/excel/match/m1", section="form"))
        assert {"Cup", "Test"} <= {r["competition"] for r in form}
        assert {r["side"] for r in form} == {"Gazde", "Oaspeți"}
        assert all(r["result"] in "WDL" and r["venue"] in "AD" for r in form)
        _, stats = parse_tsv(get(client, "/api/excel/match/m1", section="formstats"))
        assert {r["window"] for r in stats} >= {"Ultimele 5", "Ultimele 10"}
        _, h2h = parse_tsv(get(client, "/api/excel/match/m1", section="h2h"))
        assert h2h == [
            {
                "date": h2h[0]["date"],
                "competition": "Test",
                "home": "Strong",
                "away": "Weak",
                "score": "3-1",
                "home_goals": "3",
                "away_goals": "1",
                "result": "W",
            }
        ]
        _, insights = parse_tsv(get(client, "/api/excel/match/m1", section="insights"))
        assert insights and insights[0]["n"] == "1"
        _, table = parse_tsv(get(client, "/api/excel/match/m1", section="standings"))
        roles = {r["team"]: r["role"] for r in table}
        assert roles == {"Strong": "Gazde", "Mid": "", "Weak": "Oaspeți"}
        assert table[0]["goal_diff"] == "5"
        # A second analysis reuses the cached h2h response.
        client.post("/api/excel/analyze/m1", params={"format": "tsv"})
        assert calls.count("/api/flashscore/v2/matches/h2h") == 1
        _, ledger = parse_tsv(get(client, "/api/excel/record", section="rows"))
        assert [r["match_id"] for r in ledger] == ["m1"]
        assert ledger[0]["status"] == "în așteptare" and ledger[0]["won"] == ""
        # The view used 0.5, but the ledger snapshot is always taken at 0.85.
        assert float(ledger[0]["probability"]) >= 0.85


def test_low_threshold_view_never_freezes_a_weak_pick_into_the_ledger(client):
    load_day(client)
    store = client.app.state.store
    store.save_matches([store.match("m1").model_copy(update={"odds": VALUE_ODDS})])
    _, rows = parse_tsv(client.post("/api/excel/analyze/m1?format=tsv&threshold=0.5"))
    row = rows[0]
    # Excel shows the 0.5 selection (below 0.85) but nothing enters the prospective ledger.
    assert row["threshold"] == "0.5" and row["selection_key"]
    assert 0.5 <= float(row["selection_p"]) < 0.85
    assert row["saved"] == "0"
    assert store.predictions() == [] and store.assessment_count() == 1
    _, strict = parse_tsv(get(client, "/api/excel/match/m1", section="summary"))
    assert strict[0]["selection_key"] == "" and strict[0]["threshold"] == "0.85"


def test_started_match_analysis_is_retrospective_and_not_saved(client):
    load_day(client)
    header, rows = parse_tsv(client.post("/api/excel/analyze/m0?format=tsv&enrich=0"))
    assert rows[0]["retrospective"] == "1" and rows[0]["saved"] == "0"
    assert client.app.state.store.predictions() == []


def test_record_metrics_and_calibration_after_settlement(client):
    load_day(client)
    parse_tsv(client.post("/api/excel/analyze/m1?format=tsv&threshold=0.5"))
    store = client.app.state.store
    final = store.match("m1").model_copy(
        update={"status": "finished", "home_goals": 3, "away_goals": 0}
    )
    store.save_matches([final])
    assert store.settle([final]) == 1
    header, metrics = parse_tsv(get(client, "/api/excel/record", section="metrics"))
    assert header == list(xa.METRIC_COLUMNS)
    assert metrics[0]["selected"] == "1" and metrics[0]["settled"] == "1"
    assert NUMBER.match(metrics[0]["ci_low"]) and NUMBER.match(metrics[0]["ci_high"])
    header, bands = parse_tsv(get(client, "/api/excel/record", section="calibration"))
    assert header == list(xa.CALIBRATION_COLUMNS) and len(bands) == 1
    _, ledger = parse_tsv(get(client, "/api/excel/record"))
    assert ledger[0]["status"] in ("câștigat", "pierdut") and ledger[0]["score"] == "3-0"
    assert ledger[0]["won"] in ("0", "1")


def test_empty_record_is_a_header_only_table(client):
    header, rows = parse_tsv(get(client, "/api/excel/record"))
    assert header == list(xa.RECORD_COLUMNS) and rows == []
    _, metrics = parse_tsv(get(client, "/api/excel/record", section="metrics"))
    assert metrics[0]["accuracy"] == "" and metrics[0]["selected"] == "0"


def test_value_lists_only_positive_ev_sorted(client):
    header, rows = parse_tsv(get(client, "/api/excel/value", day=DAY.isoformat()))
    assert header == list(xa.VALUE_COLUMNS)
    # m2 odds sum to < 100%, so at least one of its 1X2 markets must carry a positive EV.
    assert "m2" in {r["match_id"] for r in rows}
    assert {r["market_key"] for r in rows} <= {"1", "X", "2"}
    evs = [float(r["ev"]) for r in rows]
    assert evs == sorted(evs, reverse=True) and min(evs) > 0
    for r in rows:
        assert float(r["edge"]) == pytest.approx(
            float(r["probability"]) - 1 / float(r["odds"]), abs=1e-5
        )
        assert float(r["ev"]) == pytest.approx(
            float(r["probability"]) * float(r["odds"]) - 1, abs=1e-5
        )
    _, none = parse_tsv(get(client, "/api/excel/value", day=DAY.isoformat(), min_ev="5"))
    assert none == []


@pytest.mark.parametrize("fmt", ["tsv", "csv"])
@pytest.mark.parametrize(
    ("path", "params", "status"),
    [
        ("/api/excel/predictions", {"day": "25.09.2026"}, 422),
        ("/api/excel/predictions", {}, 422),
        ("/api/excel/predictions", {"day": DAY.isoformat(), "limit": "0"}, 422),
        ("/api/excel/predictions", {"day": DAY.isoformat(), "min_grade": "E"}, 422),
        ("/api/excel/predictions", {"day": DAY.isoformat(), "threshold": "1.5"}, 422),
        ("/api/excel/match/nope", {}, 404),
        ("/api/excel/match/m1", {"section": "bogus"}, 422),
        ("/api/excel/record", {"section": "bogus"}, 422),
        ("/api/excel/value", {"day": "x"}, 422),
    ],
)
def test_errors_are_one_row_tables_with_real_status(client, fmt, path, params, status):
    load_day(client)
    response = get(client, path, fmt, **params)
    header, rows = (parse_tsv if fmt == "tsv" else parse_csv)(response, status)
    assert header == list(xa.ERROR_COLUMNS)
    assert len(rows) == 1 and rows[0]["status"] == str(status) and rows[0]["error"]


def test_unknown_format_and_missing_match_on_post(client):
    header, rows = parse_csv(get(client, "/api/excel/health", "xml"), 422)
    assert "csv" in rows[0]["error"]
    _, rows = parse_tsv(client.post("/api/excel/analyze/nope?format=tsv"), 404)
    assert "Încarcă" in rows[0]["error"]


def test_provider_errors_keep_their_status(tmp_path):
    with make_client(tmp_path, api_key="") as client:
        _, rows = parse_tsv(get(client, "/api/excel/predictions", day=DAY.isoformat()), 503)
        assert "RAPIDAPI_KEY" in rows[0]["error"]
    with make_client(tmp_path, status=429) as client:
        _, rows = parse_tsv(get(client, "/api/excel/value", day=DAY.isoformat()), 429)
        assert "secret" not in rows[0]["error"]


def test_internal_errors_become_500_tables_without_details(client, monkeypatch):
    load_day(client)

    def explode(*args, **kwargs):
        raise RuntimeError("stack detail")

    monkeypatch.setattr(client.app.state.excel_cache, "get", explode)
    _, rows = parse_tsv(get(client, "/api/excel/match/m1"), 500)
    assert "stack detail" not in rows[0]["error"] and "start.ps1" in rows[0]["error"]


def test_foreign_origin_cannot_trigger_analysis(client):
    load_day(client)
    response = client.post(
        "/api/excel/analyze/m1?format=tsv", headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert client.app.state.store.predictions() == []


# ------------------------------------------------------------------ VBA source model

BAS_BYTES = BAS_PATH.read_bytes()
BAS_TEXT = BAS_BYTES.decode("ascii", errors="replace")
BAS_LINES = BAS_TEXT.replace("\r\n", "\n").split("\n")
STRING = re.compile(r'"((?:[^"]|"")*)"')
PROC = re.compile(
    r"^(?:(Public|Private) )?(Sub|Function) (\w+)\((.*)\)(?: As (\w+))?$", re.IGNORECASE
)


def split_code(line):
    """(code with string contents blanked, code with strings kept, [literals]); no comment."""
    blank, keep, literals, i, in_string, buffer = [], [], [], 0, False, []
    while i < len(line):
        char = line[i]
        if in_string:
            if char == '"' and line[i + 1 : i + 2] == '"':
                buffer.append('"')
                keep.append('""')
                i += 2
                continue
            if char == '"':
                in_string = False
                literals.append("".join(buffer))
                blank.append('"')
            else:
                buffer.append(char)
            keep.append(char)
        elif char == '"':
            in_string, buffer = True, []
            blank.append('"')
            keep.append(char)
        elif char == "'":
            break
        else:
            blank.append(char)
            keep.append(char)
        i += 1
    return "".join(blank).rstrip(), "".join(keep).rstrip(), literals


def logical_lines():
    """Joined continuation lines: (first line number, blanked code, kept code, literals)."""
    result, blank, keep, literals, start = [], "", "", [], None
    for number, line in enumerate(BAS_LINES, 1):
        b, k, lits = split_code(line)
        start = start or number
        if b.endswith(" _"):
            blank += b[:-1]
            keep += k[:-1]
            literals += lits
            continue
        result.append((start, (blank + b).strip(), (keep + k).strip(), literals + lits))
        blank, keep, literals, start = "", "", [], None
    return [item for item in result if item[1]]


LOGICAL = logical_lines()


def procedures():
    """name -> dict(kind, scope, params, returns, body=[(line, blank, keep, literals)])."""
    procs, current = {}, None
    for item in LOGICAL:
        blank = item[1]
        match = PROC.match(blank)
        if match:
            scope, kind, name, params, returns = match.groups()
            assert name.lower() not in {p.lower() for p in procs}, f"duplicate {name}"
            current = procs[name] = {
                "kind": kind,
                "scope": scope or "Public",
                "params": params,
                "returns": returns,
                "body": [],
                "line": item[0],
            }
            continue
        if re.match(r"^End (Sub|Function)$", blank):
            current = None
            continue
        if current is not None:
            current["body"].append(item)
    return procs


PROCS = procedures()
MODULE_LINES = [item for item in LOGICAL if item[0] < min(p["line"] for p in PROCS.values())]


def body_of(name):
    return PROCS[name]["body"]


def literals_of(name):
    return [lit for item in body_of(name) for lit in item[3]]


def top_level_split(text, separator=","):
    parts, depth, current = [], 0, ""
    for char in text:
        depth += char == "("
        depth -= char == ")"
        if char == separator and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    return [p.strip() for p in parts + [current] if p.strip()]


def param_names(params):
    names = []
    for piece in top_level_split(params):
        match = re.match(
            r"^(?:Optional )?(?:ByVal |ByRef )?(\w+)(\(\))? As (\w+)(?: = .+)?$", piece
        )
        assert match, f"parameter without explicit type: {piece!r}"
        names.append(match.group(1))
    return names


def declared(names_of, statements):
    for _, blank, _, _ in statements:
        match = re.match(r"^(?:Dim|Private|Public|Static)(?: Const)? (.+)$", blank)
        if match and not PROC.match(blank):
            for piece in top_level_split(match.group(1)):
                decl = re.match(r"^(\w+)(\([^)]*\))? As (\w+)(?: = .+)?$", piece)
                assert decl, f"declaration without explicit type: {piece!r}"
                names_of.add(decl.group(1).lower())
    return names_of


MODULE_NAMES = declared(set(), MODULE_LINES)


def layouts():
    """Layout function -> [(api column, title, format)] parsed from its string literals."""
    result = {}
    for name, proc in PROCS.items():
        if name.startswith("Layout") and not proc["params"]:
            spec = "".join(literals_of(name))
            entries = [e for e in spec.split(";") if e.strip()]
            result[name] = [tuple(e.split("|")) for e in entries]
    return result


LAYOUTS = layouts()
LAYOUT_TABLES = {
    "LayoutPredictions": ("GET", "/api/excel/predictions", {}, xa.PREDICTION_COLUMNS),
    "LayoutPredictionsBasketball": (
        "GET",
        "/api/excel/predictions",
        {"sport": "basketball"},
        xa.PREDICTION_COLUMNS_BY_SPORT["basketball"],
    ),
    "LayoutPredictionsTennis": (
        "GET",
        "/api/excel/predictions",
        {"sport": "tennis"},
        xa.PREDICTION_COLUMNS_BY_SPORT["tennis"],
    ),
    "LayoutMatchCardBasketball": (
        "POST",
        "/api/excel/analyze/KMHepeEM",
        {"sport": "basketball"},
        xa.ANALYZE_COLUMNS_BY_SPORT["basketball"],
    ),
    "LayoutMatchCardTennis": (
        "POST",
        "/api/excel/analyze/KnR6QDo1",
        {"sport": "tennis"},
        xa.ANALYZE_COLUMNS_BY_SPORT["tennis"],
    ),
    "LayoutRecoTickets": (
        "GET",
        "/api/excel/recommendations",
        {"section": "tickets"},
        xa.RECO_TICKET_COLUMNS,
    ),
    "LayoutRecoLegs": (
        "GET",
        "/api/excel/recommendations",
        {"section": "legs"},
        xa.RECO_LEG_COLUMNS,
    ),
    "LayoutRecoSingles": (
        "GET",
        "/api/excel/recommendations",
        {"section": "singles"},
        xa.RECO_SINGLE_COLUMNS,
    ),
    "LayoutLive": ("GET", "/api/excel/live", {"sport": "football"}, xa.LIVE_COLUMNS),
    "LayoutLiveMarkets": (
        "GET",
        "/api/excel/live",
        {"sport": "tennis", "section": "markets"},
        xa.LIVE_MARKET_COLUMNS,
    ),
    "LayoutSimSummary": (
        "GET",
        "/api/excel/simulate",
        {"section": "summary"},
        xa.SIM_SUMMARY_COLUMNS,
    ),
    "LayoutSimLadders": (
        "GET",
        "/api/excel/simulate",
        {"section": "ladders"},
        xa.SIM_LADDER_COLUMNS,
    ),
    "LayoutSimDays": ("GET", "/api/excel/simulate", {"section": "days"}, xa.SIM_DAY_COLUMNS),
    "LayoutSimEquity": ("GET", "/api/excel/simulate", {"section": "equity"}, xa.SIM_EQUITY_COLUMNS),
    "LayoutSimDatasets": ("GET", "/api/excel/simulate/datasets", {}, xa.SIM_DATASET_COLUMNS),
    "LayoutWalletSummary": (
        "GET",
        "/api/excel/wallet",
        {"section": "summary"},
        xa.WALLET_SUMMARY_COLUMNS,
    ),
    "LayoutWalletBets": ("GET", "/api/excel/wallet", {"section": "bets"}, xa.WALLET_BET_COLUMNS),
    "LayoutWalletHistory": (
        "GET",
        "/api/excel/wallet",
        {"section": "history"},
        xa.WALLET_HISTORY_COLUMNS,
    ),
    "LayoutMatchCard": ("POST", "/api/excel/analyze/m1", {}, xa.ANALYZE_COLUMNS),
    "LayoutMarkets": ("GET", "/api/excel/match/m1", {"section": "markets"}, xa.MARKET_COLUMNS),
    "LayoutHtft": ("GET", "/api/excel/match/m1", {"section": "htft"}, xa.HTFT_COLUMNS),
    "LayoutInsights": ("GET", "/api/excel/match/m1", {"section": "insights"}, xa.INSIGHT_COLUMNS),
    "LayoutScores": ("GET", "/api/excel/match/m1", {"section": "scores"}, xa.SCORE_COLUMNS),
    "LayoutGrid": ("GET", "/api/excel/match/m1", {"section": "grid"}, xa.GRID_COLUMNS),
    "LayoutFormStats": (
        "GET",
        "/api/excel/match/m1",
        {"section": "formstats"},
        xa.FORMSTATS_COLUMNS,
    ),
    "LayoutForm": ("GET", "/api/excel/match/m1", {"section": "form"}, xa.FORM_COLUMNS),
    "LayoutH2H": ("GET", "/api/excel/match/m1", {"section": "h2h"}, xa.H2H_COLUMNS),
    "LayoutStandings": (
        "GET",
        "/api/excel/match/m1",
        {"section": "standings"},
        xa.STANDING_COLUMNS,
    ),
    "LayoutValue": ("GET", "/api/excel/value", {}, xa.VALUE_COLUMNS),
    "LayoutMetrics": ("GET", "/api/excel/record", {"section": "metrics"}, xa.METRIC_COLUMNS),
    "LayoutCalibration": (
        "GET",
        "/api/excel/record",
        {"section": "calibration"},
        xa.CALIBRATION_COLUMNS,
    ),
    "LayoutRecord": ("GET", "/api/excel/record", {"section": "rows"}, xa.RECORD_COLUMNS),
}
# Layouts checked on the football app (``client``); the others need the multi-sport app.
FOOTBALL_LAYOUTS = {
    "LayoutPredictions",
    "LayoutMatchCard",
    "LayoutMarkets",
    "LayoutHtft",
    "LayoutInsights",
    "LayoutScores",
    "LayoutGrid",
    "LayoutFormStats",
    "LayoutForm",
    "LayoutH2H",
    "LayoutStandings",
    "LayoutValue",
    "LayoutMetrics",
    "LayoutCalibration",
    "LayoutRecord",
}
# The ladder run shown by the Simulare sheet (strategy "scara" of the multi-sport benchmark).
LADDER_QUERY = {
    "dataset": "football",
    "bankroll": "5",
    "strategy": "ladder",
    "target_odds": "2",
    "reinvest": "1",
    "restart_on_loss": "1",
    "start": "2024-08-03",
    "end": "2025-05-03",
}


def _flat(sections):
    return [c for columns in sections.values() for c in columns]


FORMATS = {
    "txt": None,
    "int": NUMBER,
    "num": NUMBER,
    "odd": NUMBER,
    "pct": NUMBER,
    "pc0": NUMBER,
    "hot": NUMBER,
    "hgr": NUMBER,
    "ev": NUMBER,
    "grd": re.compile(r"^[ABCD]$"),
    "wdl": re.compile(r"^[WDL]$"),
    "yn": re.compile(r"^[01]$"),
    "win": re.compile(r"^[01]$"),
    "mrk": None,
    # Raw statuses of tickets, legs, bets, simulated days and ladders (Romanian in Excel).
    "sts": re.compile(r"^(pending|won|lost|void|unavailable|skipped|open|cashed)$"),
    # Sports of a ticket or a dataset ("multi": the recent days of several sports).
    "spt": re.compile(r"^(football|basketball|tennis|multi)(,(football|basketball|tennis))*$"),
}
ALL_COLUMNS = (
    set(xa.ERROR_COLUMNS)
    | set(xa.HEALTH_COLUMNS)
    | set(xa.COMPETITION_COLUMNS)
    | set(xa.ANALYZE_COLUMNS)
    | set(xa.VALUE_COLUMNS)
    | {c for columns in xa.SECTIONS.values() for c in columns}
    | {c for columns in xa.RECORD_SECTIONS.values() for c in columns}
    | {c for columns in xa.ANALYZE_COLUMNS_BY_SPORT.values() for c in columns}
    | {c for sections in (xa.RECO_SECTIONS, xa.LIVE_SECTIONS) for c in _flat(sections)}
    | {c for sections in (xa.SIM_SECTIONS, xa.WALLET_SECTIONS) for c in _flat(sections)}
    | set(xa.SIM_DATASET_COLUMNS)
    | set(xa.RECENT_COLUMNS)
)


def api_calls():
    """(method, path literal, line) for every ApiTable/ApiTry call."""
    pattern = re.compile(r'\bApi(?:Table|Try)\("(GET|POST)", "(/api/excel/[^"]*)"')
    return [
        (m.group(1), m.group(2), item[0])
        for item in LOGICAL
        for m in pattern.finditer(item[2])
        if not PROC.match(item[1])
    ]


# ------------------------------------------------------------------ cross-contract


def excel_routes(app):
    return [r for r in app.routes if getattr(r, "path", "").startswith("/api/excel/")]


def test_vba_requests_only_served_paths_and_methods(client):
    routes = excel_routes(client.app)
    calls = api_calls()
    assert len(calls) >= 15
    used = set()
    for method, path, line in calls:
        if path.endswith("/"):
            matches = [r for r in routes if r.path == path + "{match_id}"]
        else:
            matches = [r for r in routes if r.path == path]
        assert matches, f"line {line}: {path} is not served"
        assert any(method in r.methods for r in matches), f"line {line}: {method} {path}"
        used.add(matches[0].path)
    # Every table the API offers to Excel is actually used by the client.
    assert used == set(xa.TABLES)


def test_vba_query_parameters_are_accepted_by_the_api(client):
    accepted = {"format"}
    for route in excel_routes(client.app):
        accepted |= {p.name for p in route.dependant.query_params}
    sent = set()
    for item in LOGICAL:
        for literal in item[3]:
            sent |= set(re.findall(r"(?:^|[?&])([a-z_]+)=", literal))
    assert {"day", "min_grade", "threshold", "limit", "section", "enrich", "demo"} <= sent
    assert {"sport", "sports", "targets", "refresh", "dataset", "days", "bankroll"} <= sent
    assert {"strategy", "mode", "target_odds", "reinvest", "restart_on_loss", "stake"} <= sent
    assert sent <= accepted, sent - accepted


def test_vba_sections_exist():
    sections = set(re.findall(r'MatchQuery\("(\w+)", sport\)', BAS_TEXT))
    assert sections == set(xa.SECTIONS) - {"summary"}
    record = set(re.findall(r'"/api/excel/record", "section=(\w+)"', BAS_TEXT))
    assert record == set(xa.RECORD_SECTIONS)
    live = set(re.findall(r'"/api/excel/live", "sport=" & sport & "&section=(\w+)"', BAS_TEXT))
    assert live == set(xa.LIVE_SECTIONS)
    wallet = set(re.findall(r'"/api/excel/wallet", "section=(\w+)"', BAS_TEXT))
    assert wallet <= set(xa.WALLET_SECTIONS) and {"summary", "bets", "history"} <= wallet
    for path, sections in (
        ("recommendations", xa.RECO_SECTIONS),
        ("simulate", xa.SIM_SECTIONS),
    ):
        used = set(re.findall(rf'"/api/excel/{path}", [^\n]*?"&section=(\w+)"', BAS_TEXT))
        assert used and used <= set(sections), (path, used)


def test_every_vba_layout_maps_to_api_columns():
    assert set(LAYOUTS) == set(LAYOUT_TABLES), set(LAYOUTS) ^ set(LAYOUT_TABLES)
    for name, entries in LAYOUTS.items():
        columns = LAYOUT_TABLES[name][3]
        keys = [e[0] for e in entries]
        if name not in FOOTBALL_LAYOUTS:
            assert LAYOUT_TABLES[name][0] == "GET" or "analyze" in LAYOUT_TABLES[name][1]
        assert len(keys) == len(set(keys)), f"{name}: duplicate column"
        for entry in entries:
            assert len(entry) == 3, f"{name}: {entry}"
            key, title, fmt = entry
            assert key in columns, f"{name}: API has no column {key!r}"
            assert fmt in FORMATS, f"{name}: unknown format {fmt!r}"
            assert title.strip(), f"{name}: empty title for {key}"
    # The board row is refreshed in place from the analyze response, in every sport.
    assert {e[0] for e in LAYOUTS["LayoutPredictions"]} <= set(xa.ANALYZE_COLUMNS)
    for sport, name in (("basketball", "Basketball"), ("tennis", "Tennis")):
        board = {e[0] for e in LAYOUTS[f"LayoutPredictions{name}"]}
        assert board <= set(xa.ANALYZE_COLUMNS_BY_SPORT[sport]), sport


def test_vba_field_lookups_exist_in_the_api():
    fields = set(re.findall(r'\b(?:FieldOf|FindCol)\(\w+, "(\w+)"\)', BAS_TEXT))
    assert {"version", "warnings", "competition_id", "error", "sports", "odds_note"} <= fields
    assert {"status", "done", "total", "message", "final", "balance"} <= fields
    assert fields <= ALL_COLUMNS, fields - ALL_COLUMNS
    cells = set(
        re.findall(
            r'\b(?:PredictionCell\(\w+|LayoutPos\(PredictionLayout\(sport\)), "(\w+)"',
            BAS_TEXT,
        )
    )
    assert {"match_id", "grade", "upcoming"} <= cells
    # The Predictii sheet may hold any sport: every board layout has the looked-up cells.
    for name in ("LayoutPredictions", "LayoutPredictionsBasketball", "LayoutPredictionsTennis"):
        board = {e[0] for e in LAYOUTS[name]}
        assert cells <= board, (name, cells - board)
    days = {e[0] for e in LAYOUTS["LayoutSimDays"]}
    chart = set(re.findall(r'LayoutPos\(LayoutSimDays\(\), "(\w+)"\)', BAS_TEXT))
    assert chart == {"bankroll_after", "date"} and chart <= days


def test_live_responses_carry_every_layout_column_with_the_declared_type(client):
    """What the VBA module reads, parsed exactly like ParseTsv, after a full analysis."""
    load_day(client)
    parse_tsv(client.post("/api/excel/analyze/m1?format=tsv&threshold=0.5"))
    store = client.app.state.store
    final = store.match("m1").model_copy(
        update={"status": "finished", "home_goals": 1, "away_goals": 1}
    )
    store.save_matches([final])
    store.settle([final])
    for name, (method, path, params, _) in LAYOUT_TABLES.items():
        if name not in FOOTBALL_LAYOUTS:
            continue
        query = {"format": "tsv", "day": DAY.isoformat(), **params}
        response = client.request(method, path, params=query)
        assert response.status_code == 200, (name, response.text)
        header, rows = vba_parse(response.content.decode("utf-8"))
        assert rows, f"{name}: no data to check"
        for key, _, fmt in LAYOUTS[name]:
            assert key in header, f"{name}: {key} missing from {path}"
            pattern = FORMATS[fmt]
            for row in rows:
                value = row[key]
                assert pattern is None or value == "" or pattern.match(value), (name, key, value)


# ------------------------------------------------------------------ VBA static checks


def test_bas_is_ascii_with_crlf_and_module_header():
    BAS_BYTES.decode("ascii")  # raises on any non-ASCII byte (VBA imports as ANSI)
    assert b"\n" not in BAS_BYTES.replace(b"\r\n", b"")
    assert b"\r" not in BAS_BYTES.replace(b"\r\n", b"")
    assert BAS_BYTES.endswith(b"\r\n")
    assert BAS_LINES[0] == 'Attribute VB_Name = "FootyPreds"'
    assert not any(line.startswith("Attribute") for line in BAS_LINES[1:])


def test_option_explicit_before_any_procedure():
    first = min(p["line"] for p in PROCS.values())
    assert any(item[1] == "Option Explicit" for item in LOGICAL if item[0] < first)


def test_line_length_and_continuations():
    run = 0
    for number, line in enumerate(BAS_LINES, 1):
        assert len(line) < 1000, number
        run = run + 1 if line.rstrip().endswith(" _") else 0
        assert run <= 20, f"line {number}: more than 20 continuations"


def test_blocks_are_balanced_and_properly_nested():
    openers = [
        (re.compile(r"^(?:(?:Public|Private) )?(Sub|Function) \w+\("), None),
        (re.compile(r"^If .+ Then$"), "If"),
        (re.compile(r"^For (?:Each )?\w+"), "For"),
        (re.compile(r"^Do(?: While .+| Until .+)?$"), "Do"),
        (re.compile(r"^With .+"), "With"),
        (re.compile(r"^Select Case .+"), "Select"),
    ]
    closers = {
        "End Sub": "Sub",
        "End Function": "Function",
        "End If": "If",
        "End With": "With",
        "End Select": "Select",
    }
    stack = []
    for number, blank, _, _ in LOGICAL:
        closer = closers.get(blank)
        if closer is None and re.match(r"^Next(?: \w+)?$", blank):
            closer = "For"
        if closer is None and re.match(r"^Loop(?: While .+| Until .+)?$", blank):
            closer = "Do"
        if closer:
            assert stack and stack[-1][0] == closer, f"line {number}: {blank} closes {stack[-1:]}"
            stack.pop()
            continue
        if re.match(r"^(ElseIf .+ Then|Else)$", blank):
            assert stack and stack[-1][0] == "If", f"line {number}: {blank} outside If"
            continue
        if re.match(r"^Case ", blank):
            assert stack and stack[-1][0] == "Select", f"line {number}: Case outside Select"
            continue
        for pattern, kind in openers:
            match = pattern.match(blank)
            if match:
                kind = kind or match.group(1)
                if kind in ("Sub", "Function"):
                    assert not stack, f"line {number}: nested procedure"
                stack.append((kind, number))
                break
    assert not stack, stack


def test_declarations_are_explicit_and_assignments_target_declared_names():
    keywords = {
        "if", "elseif", "else", "for", "next", "do", "loop", "with", "select", "case", "exit",
        "on", "end", "dim", "redim", "set", "call", "resume", "goto", "private", "public",
    }  # fmt: skip
    for name, proc in PROCS.items():
        names = declared(set(MODULE_NAMES), proc["body"])
        names |= {n.lower() for n in param_names(proc["params"])}
        names.add(name.lower())
        for number, blank, _, _ in proc["body"]:
            statements = [blank]
            single = re.match(r"^(?:If|ElseIf) .+? Then (.+)$", blank)
            if single:
                statements = [s.strip() for s in re.split(r" Else ", single.group(1))]
            for statement in statements:
                statement = statement.replace(":=", "~")
                target = None
                for pattern in (
                    r"^Set (\w+)(?:\(.*\))? =",
                    r"^For (\w+) =",
                    r"^ReDim (\w+)\(",
                    r"^(\w+)(?:\([^=]*\))? =",
                ):
                    match = re.match(pattern, statement)
                    if match:
                        target = match.group(1)
                        break
                if target and target.lower() not in keywords:
                    assert target.lower() in names, f"{name} line {number}: {target} undeclared"


def test_no_identifier_shadows_a_vba_keyword_or_excel_member():
    """Names like Width/Name/Day are statements or functions; Value/Count get re-cased."""
    shadowing = {
        "width", "name", "date", "time", "day", "month", "year", "hour", "left", "right",
        "mid", "len", "val", "str", "format", "error", "line", "input", "print", "open",
        "close", "get", "put", "seek", "lock", "write", "type", "property", "string", "cells",
        "rows", "columns", "range", "names", "value", "count", "status", "caption", "sheets",
    }  # fmt: skip
    for name, proc in PROCS.items():
        names = declared(set(), proc["body"]) | {n.lower() for n in param_names(proc["params"])}
        assert not names & shadowing, f"{name}: {names & shadowing}"
    assert not MODULE_NAMES & shadowing


VBA_FUNCTIONS = {
    "Array", "AscW", "CDate", "CDbl", "CLng", "CStr", "Chr$", "ChrW", "CreateObject",
    "Format$", "Hex$", "InStr", "IsArray", "IsEmpty", "IsError", "IsNull", "LBound",
    "LCase$", "Left$", "Len", "Mid$", "MsgBox", "RGB", "Replace", "Right$", "Split", "Str$",
    "TimeSerial", "Trim$", "TypeName", "UBound", "UCase$", "Val", "VarType",
}  # fmt: skip
VBA_STATEMENTS = {"MsgBox", "DoEvents"}


def test_every_called_procedure_exists():
    procs = {p.lower() for p in PROCS}
    builtins = {f.lower() for f in VBA_FUNCTIONS}
    keywords = {
        "dim", "redim", "set", "if", "elseif", "else", "end", "for", "next", "do", "loop",
        "with", "select", "case", "exit", "on", "resume", "option", "private", "public",
    }  # fmt: skip
    for name, proc in PROCS.items():
        local = declared(set(MODULE_NAMES), proc["body"]) | {
            n.lower() for n in param_names(proc["params"])
        }
        for number, blank, _, _ in proc["body"]:
            for call in re.findall(r"(?<![.\w])([A-Za-z_]\w*\$?)\(", blank):
                lowered = call.lower()
                assert lowered in procs | builtins | local | {name.lower()}, (
                    f"{name} line {number}: unknown function {call}"
                )
            statements = [blank]
            single = re.match(r"^(?:If|ElseIf) .+? Then (.+)$", blank)
            if single:
                statements = [s.strip() for s in re.split(r" Else ", single.group(1))]
            for statement in statements:
                first = re.match(r"^([A-Za-z_]\w*)(?=\s|$)", statement)
                if not first or statement.endswith(":"):
                    continue
                word = first.group(1)
                rest = statement[len(word) :].replace(":=", "~")
                if word.lower() in keywords or rest.lstrip().startswith(("=", "(")):
                    continue
                assert word.lower() in procs or word in VBA_STATEMENTS, (
                    f"{name} line {number}: unknown statement {word}"
                )


def test_button_and_run_targets_are_public_macros():
    targets = set(re.findall(r'\bAddButton ws, "(\w+)"', BAS_TEXT))
    targets |= set(re.findall(r'\.OnAction = "(\w+)"', BAS_TEXT))
    assert targets == {
        "Setup",
        "VerificaServer",
        "IncarcaPredictii",
        "AnalizaMeci",
        "AnalizaCompletaTop",
        "IncarcaValoare",
        "IncarcaTrackRecord",
        "IncarcaRecomandari",
        "ActualizeazaLive",
        "RuleazaSimularea",
        "IncarcaSeturiDate",
        "IncarcaPortofel",
    }
    for target in targets:
        assert PROCS[target]["scope"] == "Public" and PROCS[target]["kind"] == "Sub"
        assert PROCS[target]["params"].strip() == "", f"{target} must take no arguments"
    runs = {m for item in LOGICAL for lit in item[3] for m in re.findall(r"!(\w+)", lit)}
    assert runs == {"AnalizaMeciDinRand"}
    for target in runs:
        assert PROCS[target]["scope"] == "Public" and PROCS[target]["kind"] == "Sub"
    public = {n for n, p in PROCS.items() if p["scope"] == "Public"}
    assert public == targets | runs, "every public macro must be reachable from a button"


def test_late_binding_only_and_no_platform_specific_declares():
    code = "\n".join(item[1] for item in LOGICAL)
    assert not re.search(r"\bNew\s+\w", code), "no New: use CreateObject (late binding)"
    assert not re.search(r"\bDeclare\b", code), "Declare breaks 32/64-bit portability"
    types = set(re.findall(r"\bAs\s+([\w.]+)", code))
    assert types <= {
        "String",
        "Long",
        "Double",
        "Boolean",
        "Variant",
        "Object",
        "Worksheet",
        "Range",
    }
    progids = set(re.findall(r'CreateObject\("([^"]+)"\)', BAS_TEXT))
    assert progids == {
        "MSXML2.ServerXMLHTTP.6.0",
        "WinHttp.WinHttpRequest.5.1",
        "ADODB.Stream",
    }
    for forbidden in ("As MSXML2.", "As ADODB.", "As Scripting.", "New Dictionary", "As WinHttp"):
        assert forbidden.lower() not in BAS_TEXT.lower()


def test_numbers_are_parsed_locale_independently():
    code = "\n".join(item[1] for item in LOGICAL)
    assert "Val(" in code
    # CDbl is locale-dependent on text; it may only convert an already numeric cell value.
    cdbl = [item for item in LOGICAL if "CDbl(" in item[1]]
    assert [item[1] for item in cdbl] == ["SheetNumber = CDbl(v)"]
    for forbidden in ("CSng(", "CDec(", "CCur(", "IsNumeric("):
        assert forbidden not in code
    # Numbers sent to the API are built with Str$ (always "."), never Format$ or CStr.
    number_to_text = "".join(item[1] for item in body_of("NumToStr"))
    assert "Str$(" in number_to_text and "Format$(" not in number_to_text


def test_diacritic_markers_are_known_and_decoded():
    markers = {m for item in LOGICAL for lit in item[3] for m in re.findall(r"\{[^}]*\}", lit)}
    known = {"{a}", "{A}", "{a^}", "{A^}", "{i^}", "{I^}", "{s}", "{S}", "{t}", "{T}"}
    assert markers <= known, markers - known
    decoder = " ".join(item[2] for item in body_of("Ro"))
    for marker in known:
        assert f'"{marker}"' in decoder
    # Comma-below letters (U+0219/U+021B), not the legacy cedilla forms.
    assert "ChrW(537)" in decoder and "ChrW(539)" in decoder


def test_excel_constants_are_real_names():
    used = set(re.findall(r"\b(xl\w+)", "\n".join(item[1] for item in LOGICAL)))
    known = {
        "xlUp", "xlNone", "xlCenter", "xlLeft", "xlCellValue", "xlEqual", "xlSheetHidden",
        "xlConditionValueLowestValue", "xlConditionValueHighestValue", "xlConditionValueNumber",
        "xlValidateList", "xlValidAlertStop", "xlBetween", "xlFreeFloating", "xlWait",
        "xlDefault", "xlErrorHandler", "xlInterrupt", "xlLine",
    }  # fmt: skip
    assert used <= known, used - known


def test_private_helpers_are_all_used():
    for name, proc in PROCS.items():
        if proc["scope"] == "Private":
            uses = len(re.findall(rf"\b{name}\b", "\n".join(item[1] for item in LOGICAL)))
            assert uses >= 2, f"{name} is never called"


def test_macros_handle_errors_and_restore_excel_state():
    for name in ("Setup", "VerificaServer", "IncarcaPredictii", "AnalizaMeci",
                 "AnalizaMeciDinRand", "AnalizaCompletaTop", "IncarcaValoare",
                 "IncarcaTrackRecord", "RunAnalysis", "IncarcaRecomandari", "ActualizeazaLive",
                 "RuleazaSimularea", "IncarcaSeturiDate", "IncarcaPortofel"):  # fmt: skip
        body = [item[1] for item in body_of(name)]
        assert "On Error GoTo Fail" in body, name
        handler = body[body.index("Fail:") :]
        assert "EndWork" in handler and any(s.startswith("ShowError") for s in handler), name
        assert body.index("Exit Sub") < body.index("Fail:"), name
    end_work = " ".join(item[1] for item in body_of("EndWork"))
    for reset in ("ScreenUpdating = True", "StatusBar = False", "Cursor = xlDefault"):
        assert reset in end_work
    down = " ".join(item[2] for item in body_of("ServerDownMessage"))
    assert "start.ps1" in down


def test_http_client_sets_timeouts_and_decodes_utf8():
    http = " ".join(item[2] for item in body_of("HttpCall"))
    assert "setTimeouts" in http and "Utf8Decode(http.responseBody)" in http
    decode = " ".join(item[2] for item in body_of("Utf8Decode"))
    assert '"utf-8"' in decode and "ADODB.Stream" not in decode.replace('"ADODB.Stream"', "")
    assert '?format=tsv"' in " ".join(item[2] for item in body_of("ApiTry"))


def test_vba_url_encoder_matches_python():
    """Re-implements UrlEncode's branches to prove the UTF-8 byte math is right."""
    from urllib.parse import quote

    def vba(text):
        out, i, units = [], 0, text.encode("utf-16-le")
        codes = [int.from_bytes(units[j : j + 2], "little") for j in range(0, len(units), 2)]
        while i < len(codes):
            code = codes[i]
            char = chr(code) if code < 0xD800 or code > 0xDFFF else ""
            if (
                48 <= code <= 57
                or 65 <= code <= 90
                or 97 <= code <= 122
                or code in (45, 46, 95, 126)
            ):
                out.append(char)
            elif code < 128:
                out.append(f"%{code:02X}")
            elif code < 2048:
                out.append(f"%{192 + code // 64:02X}%{128 + (code & 63):02X}")
            elif 55296 <= code <= 56319 and i < len(codes) - 1:
                low = codes[i + 1]
                code = 65536 + (code - 55296) * 1024 + (low - 56320)
                out.append(
                    f"%{240 + code // 262144:02X}%{128 + ((code // 4096) & 63):02X}"
                    f"%{128 + ((code // 64) & 63):02X}%{128 + (code & 63):02X}"
                )
                i += 1
            else:
                out.append(
                    f"%{224 + code // 4096:02X}%{128 + ((code // 64) & 63):02X}"
                    f"%{128 + (code & 63):02X}"
                )
            i += 1
        return "".join(out)

    for text in ("england|premier league", "românia|liga 1", "Oțelul Galați", "€ ⚽ 🙂", "a&b=c"):
        assert vba(text) == quote(text, safe="-._~")
    encoder = " ".join(item[1] for item in body_of("UrlEncode"))
    for fragment in ("192 + code \\ 64", "224 + code \\ 4096", "240 + code \\ 262144", "55296"):
        assert fragment in encoder


# ------------------------------------------------------------------ build_xlsm.py


def load_builder():
    spec = importlib.util.spec_from_file_location("build_xlsm", CLIENT_DIR / "build_xlsm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeComponents:
    def __init__(self, calls):
        self.calls = calls

    def Import(self, path):  # noqa: N802 - COM method name
        self.calls.append(("import", path))


class FakeProject:
    def __init__(self, calls):
        self.VBComponents = FakeComponents(calls)


class FakeRange:
    def __init__(self, value):
        self.Value = value


class FakeSheet:
    def __init__(self, name, status=None):
        self.Name, self.status = name, status

    def Range(self, address):  # noqa: N802 - COM method name
        return FakeRange(self.status if address == "B15" else None)


class FakeSheets:
    def __init__(self, sheets):
        self.sheets = sheets

    def __iter__(self):
        return iter(self.sheets)

    def __call__(self, name):
        return next(sheet for sheet in self.sheets if sheet.Name == name)


class FakeWorkbook:
    Name = "Book1"

    def __init__(self, calls, trusted):
        self.calls, self.trusted = calls, trusted
        self.sheets = [FakeSheet("Foaie1")]

    @property
    def Worksheets(self):  # noqa: N802 - COM property name
        return FakeSheets(self.sheets)

    @property
    def VBProject(self):  # noqa: N802 - COM property name
        if not self.trusted:
            raise RuntimeError("Programmatic access to Visual Basic Project is not trusted")
        return FakeProject(self.calls)

    def SaveAs(self, path, FileFormat):  # noqa: N802, N803 - COM names
        self.calls.append(("save", path, FileFormat))

    def Close(self, SaveChanges):  # noqa: N802, N803 - COM names
        self.calls.append(("close", SaveChanges))


class FakeExcel:
    """Setup either builds every sheet (status 'Foile sunt gata') or fails like the VBA
    handler does in a hidden Excel: no MsgBox, only the error text in Panou!B15."""

    def __init__(self, trusted=True, setup_error=None):
        self.calls = []
        self.trusted = trusted
        self.setup_error = setup_error
        self.workbook = None
        outer = self

        class Workbooks:
            def Add(self):  # noqa: N802 - COM method name
                outer.workbook = FakeWorkbook(outer.calls, outer.trusted)
                return outer.workbook

        self.Workbooks = Workbooks()

    def Run(self, macro):  # noqa: N802 - COM method name
        self.calls.append(("run", macro))
        if self.setup_error:
            self.workbook.sheets = [FakeSheet("Panou", f"10:00:00  {self.setup_error}")]
        else:
            self.workbook.sheets = [
                FakeSheet(name, "10:00:00  Foile sunt gata. Porneste serverul.")
                for name in ("Panou", "Predictii", "Meci", "Forma", "ScorCorect", "Valoare",
                             "TrackRecord", "Recomandari", "Live", "Simulare", "Portofel",
                             "Ajutor", "Liste")
            ]  # fmt: skip

    def Quit(self):  # noqa: N802 - COM method name
        self.calls.append(("quit",))


def test_builder_imports_runs_setup_and_saves_xlsm(tmp_path):
    builder = load_builder()
    excel, output = FakeExcel(), tmp_path / "out" / "FootyPreds.xlsm"
    lines = []
    code = builder.main(["--output", str(output)], opener=lambda: (excel, None), out=lines.append)
    assert code == 0
    assert excel.calls == [
        ("import", str(builder.BAS)),
        ("run", "'Book1'!Setup"),
        ("save", str(output.resolve()), 52),
        ("close", False),
        ("quit",),
    ]
    assert any("Gata" in line for line in lines)


def test_builder_reports_a_failed_setup_instead_of_saving_a_broken_workbook(tmp_path):
    """Hidden Excel: Setup's error handler writes to Panou!B15 instead of an unclosable MsgBox."""
    builder = load_builder()
    excel, lines = FakeExcel(setup_error="Eroare neasteptata (1004): Nu se poate redenumi"), []
    output = tmp_path / "x.xlsm"
    code = builder.main(["--output", str(output)], opener=lambda: (excel, None), out=lines.append)
    assert code == builder.EXIT_FAILED
    assert not any(call[0] == "save" for call in excel.calls)
    assert excel.calls[-2:] == [("close", False), ("quit",)]
    text = "\n".join(lines)
    assert "Nu se poate redenumi" in text and "Predictii" in text and "Import File" in text


def test_builder_checks_the_same_sheets_and_status_that_setup_writes():
    builder = load_builder()
    names = set(re.findall(r"SH_\w+ As String = \"(\w+)\"", BAS_TEXT))
    assert names == set(builder.SHEETS)
    assert f'CELL_STATUS As String = "{builder.STATUS_CELL}"' in BAS_TEXT
    setup = " ".join(item[2] for item in body_of("Setup"))
    assert f'EndWork Ro("{builder.READY}.' in setup
    # SetStatus writes the message to B15 even when no MsgBox can be shown.
    show = [item[1] for item in body_of("ShowError")]
    assert show[0] == "SetStatus message"
    assert show.index("If Not Application.Visible Then Exit Sub") < next(
        i for i, line in enumerate(show) if line.startswith("MsgBox")
    )


def test_builder_explains_trust_setting_and_still_quits(tmp_path):
    builder = load_builder()
    excel, lines = FakeExcel(trusted=False), []
    code = builder.main(
        ["--output", str(tmp_path / "x.xlsm")], opener=lambda: (excel, None), out=lines.append
    )
    assert code == builder.EXIT_TRUST
    text = "\n".join(lines)
    assert "Trust access to the VBA project object model" in text and "Alt+F11" in text
    assert excel.calls[-1] == ("quit",)


def test_builder_without_excel_prints_manual_steps(tmp_path):
    builder = load_builder()
    lines = []
    code = builder.main(
        ["--output", str(tmp_path / "x.xlsm")],
        opener=lambda: (None, "Lipsește pywin32."),
        out=lines.append,
    )
    assert code == builder.EXIT_UNAVAILABLE
    text = "\n".join(lines)
    assert "pywin32" in text and "Import File" in text and "Setup" in text and ".xlsm" in text
    assert not (tmp_path / "x.xlsm").exists()


@pytest.mark.skipif(
    importlib.util.find_spec("win32com") is not None, reason="pywin32 is installed here"
)
def test_builder_script_exits_cleanly_on_this_machine(tmp_path):
    output = tmp_path / "never.xlsm"
    result = subprocess.run(
        [sys.executable, str(CLIENT_DIR / "build_xlsm.py"), "--output", str(output)],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 2
    text = result.stdout.decode("utf-8")
    assert "Alt+F11" in text and "Traceback" not in result.stderr.decode("utf-8", "replace")
    assert not output.exists()


def test_client_documentation_covers_every_endpoint_and_macro():
    readme = (CLIENT_DIR / "README.md").read_text(encoding="utf-8")
    power_query = (CLIENT_DIR / "PowerQuery.md").read_text(encoding="utf-8")
    for macro in ("Setup", "IncarcaPredictii", "AnalizaMeci", "AnalizaCompletaTop",
                  "IncarcaValoare", "IncarcaTrackRecord", "VerificaServer",
                  "IncarcaRecomandari", "ActualizeazaLive", "RuleazaSimularea",
                  "IncarcaSeturiDate", "IncarcaPortofel"):  # fmt: skip
        assert macro in readme
    for path in xa.TABLES:
        short = path.replace("/{match_id}", "")
        assert short in readme or short in power_query, short
    for topic in ("start.ps1", "Mark of the Web", "64", "127.0.0.1"):
        assert topic in readme
    assert "Web.Contents" in power_query and "Refresh" in power_query


# ------------------------------------------------------------------ review regressions


def test_demo_matches_can_be_analysed_without_provider_or_ledger(tmp_path):
    """Demo mode is the documented no-key mode: analysis must work, not answer 404."""
    calls = []
    with make_client(tmp_path, calls, api_key="") as client:
        _, board = load_day(client, demo="1")
        match_id = board[0]["match_id"]
        assert match_id.startswith("demo-next-")
        header, rows = parse_tsv(client.post(f"/api/excel/analyze/{match_id}?format=tsv"))
        assert header == list(xa.ANALYZE_COLUMNS)
        row = rows[0]
        assert row["match_id"] == match_id and row["saved"] == "0"
        assert "demo" in row["warnings"].lower()
        # Same numbers as the demo board row the user double-clicked.
        for key in ("p_1", "p_x", "p_2", "grade", "tip_key"):
            assert row[key] == board[0][key], key
        for section, columns in xa.SECTIONS.items():
            header, rows = parse_tsv(get(client, f"/api/excel/match/{match_id}", section=section))
            assert header == list(columns), section
            assert rows or section in ("standings", "h2h"), section
        _, table = parse_tsv(get(client, f"/api/excel/match/{match_id}", section="standings"))
        assert table == []
        # No FlashScore call, nothing stored, nothing in the prospective ledger.
        assert calls == []
        assert client.app.state.store.matches() == []
        assert client.app.state.store.predictions() == []
        for unknown in ("demo-next-99", "demo-3-0", "demo-"):
            parse_tsv(client.post(f"/api/excel/analyze/{unknown}?format=tsv"), 404)


def test_stored_match_wins_over_the_demo_fallback(client):
    load_day(client)
    store = client.app.state.store
    real = store.match("m1").model_copy(update={"id": "demo-next-0"})
    store.save_matches([real])
    _, rows = parse_tsv(get(client, "/api/excel/match/demo-next-0", section="summary"))
    assert rows[0]["home"] == "Strong" and rows[0]["source"] != "synthetic"


def test_predictions_report_the_filtered_total_before_the_limit(client):
    response = get(client, "/api/excel/predictions", day=DAY.isoformat(), limit="1")
    _, rows = parse_tsv(response)
    assert len(rows) == 1 and response.headers["x-total-count"] == "3"
    response = get(client, "/api/excel/predictions", "csv", day=DAY.isoformat(), offset="2")
    assert response.headers["x-total-count"] == "3"
    response = get(
        client, "/api/excel/predictions", day=DAY.isoformat(), upcoming_only="1", limit="1"
    )
    assert response.headers["x-total-count"] == "2"
    response = get(client, "/api/excel/predictions", day=DAY.isoformat(), demo="1", limit="2")
    assert response.headers["x-total-count"] == "6"


def test_vba_announces_a_truncated_day():
    http = [item[2] for item in body_of("HttpCall")]
    assert any('getResponseHeader("X-Total-Count")' in line for line in http)
    assert http.index("m_total = -1") < next(i for i, x in enumerate(http) if "http.send" in x)
    load = [item[2] for item in body_of("IncarcaPredictii")]
    fetch = next(i for i, x in enumerate(load) if '"/api/excel/predictions"' in x)
    # Read before LoadCompetitions (another request) overwrites m_total.
    assert load[fetch + 1] == "totalCount = m_total"
    assert any("totalCount > matchCount" in x for x in load)
    assert any("B9" in lit for item in body_of("IncarcaPredictii") for lit in item[3])


def error_text_for_escape():
    body = body_of("ErrorText")
    index = next(i for i, item in enumerate(body) if item[1] == "Case 18")
    return " ".join(body[index + 1][3])


def test_escape_is_never_reported_as_server_down():
    """Esc = error 18; code under On Error Resume Next / GoTo must re-raise it."""
    http = [item[1] for item in body_of("HttpCall")]
    raises = [i for i, x in enumerate(http) if x.startswith("RaiseHttpFailure")]
    assert len(raises) == 2
    for i in raises:
        # Without On Error GoTo 0 first, HttpCall's Resume Next would swallow the raise.
        assert http[i - 1] == "On Error GoTo 0" and http[i - 3] == "failNumber = Err.Number"
    assert not any("ERR_SERVER" in x for x in http)
    failure = [item[1] for item in body_of("RaiseHttpFailure")]
    assert failure[0] == "If failNumber = 18 Then Err.Raise 18"
    # A receive timeout means "still computing", never "start the server".
    assert failure[1] == "If failNumber = HTTP_TIMEOUT_ERROR Then"
    assert failure[2].startswith("Err.Raise ERR_API") and "ERR_SERVER" in failure[-1]
    for name, label in (("LoadCompetitions", "Quiet:"), ("ErrorFromTable", "Raw:")):
        body = [item[1] for item in body_of(name)]
        assert body[body.index(label) + 1] == "If Err.Number = 18 Then Err.Raise 18", name
    for name, proc in PROCS.items():
        code = [item[1] for item in proc["body"]]
        swallows = any(re.match(r"^On Error (Resume Next|GoTo (?!Fail$|0$)\w+)$", x) for x in code)
        talks = any(re.search(r"\b(ApiTry|ApiTable|HttpCall)\(|http\.send", x) for x in code)
        if swallows and talks:
            assert any("Err.Raise 18" in x or x.startswith("RaiseHttpFailure") for x in code), name
    assert "Esc" in error_text_for_escape()


def test_top_n_failure_keeps_the_progress_counts():
    body = [item[1] for item in body_of("AnalizaCompletaTop")]
    handler = body[body.index("Fail:") :]
    assert "failText = ErrorText(failNumber, Err.Description)" in handler
    assert any("succeeded" in x and "failed" in x for x in handler)
    assert handler[-1] == "ShowError failText"


def test_top_n_moves_on_instead_of_reanalysing_the_same_rows():
    top = [item[1] for item in body_of("AnalizaCompletaTop")]
    assert "If WasAnalyzed(matchId) Then" in top
    # Success and ordinary failures are remembered; 429/503 stops are retried next time.
    marks = [i for i, x in enumerate(top) if x == "MarkAnalyzed matchId"]
    assert len(marks) == 2
    stop = top.index("stopReason = message")
    assert not any(stop - 2 < i < stop + 2 for i in marks)
    assert "MarkAnalyzed matchId" in [item[1] for item in body_of("RunAnalysis")]
    assert 'm_analyzed = ""' in [item[2] for item in body_of("IncarcaPredictii")]
    was = " ".join(item[2] for item in body_of("WasAnalyzed"))
    assert '"|" & matchId & "|"' in was

    # Python twin of WasAnalyzed / MarkAnalyzed: ids never match by prefix.
    seen = ""

    def mark(match_id):
        nonlocal seen
        if match_id and f"|{match_id}|" not in seen:
            seen = (seen or "|") + match_id + "|"

    for match_id in ("abc", "ab", "abc", ""):
        mark(match_id)
    assert seen == "|abc|ab|" and "|a|" not in seen


def test_score_grid_uses_one_colour_scale_for_the_whole_matrix():
    grid = LAYOUTS["LayoutGrid"]
    assert [fmt for key, _, fmt in grid if key.startswith("away_")] == ["hgr"] * xa.GRID_SIZE
    # No per-column rule for hgr: DecorateColumn must not know it.
    decorate = " ".join(item[2] for item in body_of("DecorateColumn"))
    assert '"hgr"' not in decorate
    write = [item[1] for item in body_of("WriteTable")]
    scale = next(x for x in write if x.startswith("AddScale"))
    assert "heatFirst" in scale and "heatLast" in scale and scale.endswith(", True")
    for helper in ("Convert", "NumberFormatOf"):
        assert '"hgr"' in " ".join(item[2] for item in body_of(helper)), helper


def test_updated_row_highlight_matches_the_table_style():
    update = [item[1] for item in body_of("UpdatePredictionRow")]
    marked = [item[1] for item in body_of("HighlightMarked")]
    for style in (".Interior.Color = RGB(255, 250, 205)", ".Font.Bold = True"):
        assert style in update and style in marked
    assert ".Font.Bold = False" in update and ".Interior.Pattern = xlNone" in update


def test_selection_is_labelled_as_a_view_threshold_not_the_ledger():
    for name in ("LayoutPredictions", "LayoutMatchCard", "LayoutMarkets"):
        for key, title, _ in LAYOUTS[name]:
            if key in ("selection_label", "is_selection"):
                assert "registru" not in title and "prag Panou" in title, (name, title)
    saved = {key: title for key, title, _ in LAYOUTS["LayoutMatchCard"]}["saved"]
    assert "registru" in saved
    panel = [lit for item in body_of("BuildPanel") for lit in item[3] if "85%" in lit]
    assert panel and "Registrul" in panel[0]
    assert xa.LEDGER_THRESHOLD == 0.85


def test_panel_button_falls_back_to_the_predictii_selection():
    body = [item[1] for item in body_of("AnalizaMeci")]
    b13 = body.index("If Not onPredictions Then matchId = PanelMatchId()")
    activate = body.index("If Not onPredictions Then ThisWorkbook.Worksheets(SH_PRED).Activate")
    row = body.index("If ActiveCell.Row > PRED_HEADER Then")
    assert b13 < activate < row < body.index("RunAnalysis matchId, rowNumber, sport")
    # A row of the Predictii sheet is analysed in the sport that sheet was loaded with.
    loaded = body.index("sport = LoadedSport()")
    assert row < loaded < body.index("RunAnalysis matchId, rowNumber, sport")


def test_default_day_is_the_server_utc_day(client):
    panel = " ".join(item[2] for item in body_of("BuildPanel"))
    assert '.Formula = "=TODAY()"' not in panel
    assert "If UsesServerDay() Then ws.Range(CELL_DATE).ClearContents" in panel
    day = [item[1] for item in body_of("PanelDay") if not item[1].startswith("Dim ")]
    assert day[:3] == ["If UsesServerDay() Then", "PanelDay = ServerDay()", "Exit Function"]
    uses = " ".join(item[2] for item in body_of("UsesServerDay"))
    assert '"=TODAY()"' in uses and ".Formula" in uses
    server = " ".join(item[2] for item in body_of("ServerDay"))
    assert '"/api/excel/health"' in server and '"server_time_utc"' in server
    assert 'Like "####-##-##"' in server
    before = datetime.now(timezone.utc).date().isoformat()
    _, rows = parse_tsv(get(client, "/api/excel/health"))
    after = datetime.now(timezone.utc).date().isoformat()
    stamp = rows[0]["server_time_utc"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", stamp) and stamp[:10] in (before, after)


def test_double_click_install_finds_the_sheet_module_without_codename():
    install = " ".join(item[1] for item in body_of("InstallDoubleClick"))
    assert "CodeName" not in install
    assert "Set sheetCode = SheetCodeModule(project, ws)" in install
    lookup = " ".join(item[2] for item in body_of("SheetCodeModule"))
    assert "For Each component In project.VBComponents" in lookup
    assert "component.Type = 100" in lookup and "ComponentSheetName(component) = ws.Name" in lookup
    name = " ".join(item[2] for item in body_of("ComponentSheetName"))
    assert 'component.Properties("Name").Value' in name and "On Error Resume Next" in name


def test_message_boxes_only_in_user_triggered_macros():
    """A MsgBox in a hidden Excel (build_xlsm.py runs Setup) would hang forever."""
    with_box = {
        name
        for name, proc in PROCS.items()
        if any(re.match(r"^(?:If .+ Then )?MsgBox\b", item[1]) for item in proc["body"])
    }
    assert with_box == {"ShowError", "VerificaServer", "AnalizaCompletaTop"}


def power_query_function():
    text = (CLIENT_DIR / "PowerQuery.md").read_text(encoding="utf-8")
    section = text[text.index("### 3.") : text.index("### 4.")]
    return section[section.index("```m") : section.rindex("```")]


def test_power_query_reads_raw_tsv_and_explains_foreign_errors(client):
    code = power_query_function()
    assert '[format = "tsv"]' in code and "QuoteStyle.None" in code
    assert 'Delimiter = "#(tab)"' in code and "Encoding = 65001" in code
    # The format is forced last so a caller's record cannot switch back to CSV.
    assert code.index("else parametri") < code.index('[format = "tsv"]')
    assert "try Text.From(Tabel{0}[error])" in code and "otherwise" in code
    statuses = set(re.search(r"ManualStatusHandling = \{([^}]*)\}", code).group(1).split(", "))
    assert {"400", "403", "404", "409", "422", "429", "500", "503"} <= statuses
    # What the M function receives: the TSV value is exact (no formula-guard apostrophe).
    _, rows = load_day(client)
    assert next(r for r in rows if r["match_id"] == "m2")["away"] == "=Steaua București "
    # Every documented text column really exists, so nothing text-like is typed as number.
    listed = code[code.index("ColoaneText") : code.index("Numerice")]
    text_columns = set(re.findall(r'"(\w+)"', listed))
    assert text_columns <= ALL_COLUMNS, text_columns - ALL_COLUMNS


# ------------------------------------------------------------------ multi-sport app

MULTI_DAY = e2e.DAY
BASE = "http://testserver"
TICKET_QUERY = {
    "dataset": "football",
    "bankroll": "1000",
    "mode": "ticket",
    "target_odds": "2",
    "staking": "flat",
    "stake": "10",
    "start": "2024-08-03",
    "end": "2025-05-03",
}


@pytest.fixture(scope="module")
def multi(tmp_path_factory):
    """The e2e app: captured FlashScore payloads for the three sports, frozen clocks, and a
    tmp football benchmark for the simulator. Shared by the module's product-table tests."""
    with pytest.MonkeyPatch.context() as patch:
        counter = itertools.count(start=10_000, step=1_000)
        # Only the provider's request spacing reads this clock: no real sleeps.
        patch.setattr(
            provider_module, "time", types.SimpleNamespace(monotonic=lambda: next(counter))
        )
        patch.setattr(recommend, "utcnow", lambda: e2e.NOW)
        patch.setattr(wallet, "utcnow", lambda: e2e.NOW)
        patch.setattr(sim_datasets, "_MEMO", {})
        tmp = tmp_path_factory.mktemp("excel-multi")
        fake = e2e.Fake()
        settings = Settings(api_key="k", database=tmp / "multi.db")
        app = create_app(settings, httpx.MockTransport(fake))
        app.state.sim_benchmark_dir = write_benchmark(tmp / "bench", football_records())
        app.state.sim_cache_dir = tmp / "sim-cache"
        app.state.sim_workers = 1
        app.state.store.save_matches(e2e.football_history())
        with TestClient(app) as test_client:
            test_client.fake = fake
            for sport in ("football", "basketball", "tennis"):
                parse_tsv(get(test_client, "/api/excel/predictions", day=MULTI_DAY, sport=sport))
            yield test_client


def json_of(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code in (200, 202), response.text
    return response.json()


def logo_cell(value):
    """What an Excel logo cell holds for a JSON logo value (absolute proxy URL or empty)."""
    return BASE + value if value else ""


def assert_number(value, expected, tolerance=1e-5):
    if expected is None:
        assert value == ""
    else:
        assert float(value) == pytest.approx(expected, abs=tolerance)


def is_number_column(column):
    return column.startswith(("p_", "exp_", "odds_", "fair_", "main_")) and not column.endswith(
        ("_key", "_label")
    )


@pytest.mark.parametrize("sport", ["basketball", "tennis"])
def test_sport_boards_have_their_own_columns_and_match_the_web_board(multi, sport):
    response = get(multi, "/api/excel/predictions", day=MULTI_DAY, sport=sport, limit="400")
    header, rows = parse_tsv(response)
    assert header == list(xa.PREDICTION_COLUMNS_BY_SPORT[sport])
    assert response.headers["x-total-count"] == str(len(rows)) == "30"
    web = multi.get(f"/api/predictions?day={MULTI_DAY}&sport={sport}&limit=400").json()["items"]
    assert [r["match_id"] for r in rows] == [item["match"]["id"] for item in web]
    for row, item in zip(rows, web):
        assert row["sport"] == sport and row["grade"] in "ABCD"
        assert row["competition_id"] == item["competition_id"]
        assert row["competition_id"].startswith(f"{sport}:")
        assert row["tip_key"] == item["tip"]["key"]
        assert float(row["p_1"]) + float(row["p_2"]) == pytest.approx(1, abs=1e-5)
        for n, market in enumerate(item["main"][2:4], 3):
            assert row[f"main_{n}_key"] == market["key"]
            assert_number(row[f"main_{n}_p"], market["probability"])
        for column in filter(is_number_column, header):
            assert row[column] == "" or NUMBER.match(row[column]), (column, row[column])
        for field in xa.LOGO_COLUMNS:
            assert row[field] == logo_cell(item.get(field)), field
    assert any(r["home_logo"].startswith(f"{BASE}/api/img?u=https%3A%2F%2F") for r in rows)
    _, competitions = parse_tsv(get(multi, "/api/excel/competitions", day=MULTI_DAY, sport=sport))
    assert {c["competition_id"] for c in competitions} == {r["competition_id"] for r in rows}
    # The football board keeps its historical columns; the new ones are appended at the end.
    header, rows = parse_tsv(get(multi, "/api/excel/predictions", day=MULTI_DAY))
    assert header == list(xa.PREDICTION_COLUMNS)
    assert header[-4:] == ["sport", "home_logo", "away_logo", "league_logo"]
    assert {r["sport"] for r in rows} == {"football"}


def test_demo_is_football_only(multi):
    before = len(multi.fake.calls)
    for path in ("/api/excel/predictions", "/api/excel/competitions", "/api/excel/value"):
        _, rows = parse_tsv(get(multi, path, day=MULTI_DAY, sport="tennis", demo="1"))
        assert rows == [], path
    assert len(multi.fake.calls) == before


@pytest.mark.parametrize(
    ("match_id", "sport"), [("KnR6QDo1", "tennis"), ("KMHepeEM", "basketball")]
)
def test_sport_match_analysis_and_sections(multi, match_id, sport):
    standings_before = multi.fake.count("matches/standings")
    path = f"/api/excel/match/{match_id}"
    header, rows = parse_tsv(
        multi.post(f"/api/excel/analyze/{match_id}", params={"format": "tsv", "sport": sport})
    )
    assert header == list(xa.ANALYZE_COLUMNS_BY_SPORT[sport])
    row = rows[0]
    assert row["match_id"] == match_id and row["sport"] == sport
    assert row["saved"] in ("0", "1") and row["retrospective"] in ("0", "1")
    web = multi.get(f"/api/analysis/{match_id}?sport={sport}").json()["prediction"]
    assert row["grade"] == web["grade"] and row["version"] == web["version"]
    header, _ = parse_tsv(get(multi, path, section="summary", sport=sport))
    assert header == list(xa.SUMMARY_COLUMNS_BY_SPORT[sport])
    for section in ("markets", "form", "formstats", "h2h", "insights", "standings"):
        header, _ = parse_tsv(get(multi, path, section=section, sport=sport))
        assert header == list(xa.SECTIONS[section]), section
    _, markets = parse_tsv(get(multi, path, section="markets", sport=sport))
    assert sum(r["is_tip"] == "1" for r in markets) == 1
    assert {r["key"] for r in markets} == {m["key"] for m in web["markets"]}
    for section in xa.FOOTBALL_SECTIONS:
        _, error = parse_tsv(get(multi, path, section=section, sport=sport), 422)
        assert "fotbal" in error[0]["error"]
    if sport == "tennis":
        _, h2h = parse_tsv(get(multi, path, section="h2h", sport=sport))
        assert h2h and all(r["result"] in "WDL" for r in h2h)
        _, form = parse_tsv(get(multi, path, section="form", sport=sport))
        assert form and {r["side"] for r in form} == {"Gazde", "Oaspeți"}
        # Tennis has no standings: no FlashScore request for them.
        assert multi.fake.count("matches/standings") == standings_before
    # Wrong or missing sport: a 404 that names the right one, never another sport's columns.
    _, error = parse_tsv(get(multi, path), 404)
    assert f"sportul {sport}" in error[0]["error"]
    _, error = parse_tsv(multi.post(f"/api/excel/analyze/{match_id}?format=tsv&enrich=0"), 404)
    assert "sport=" in error[0]["error"]
    parse_tsv(get(multi, "/api/excel/match/fb0", sport=sport), 404)
    parse_tsv(get(multi, path, sport="golf"), 422)


def test_recommendation_tables_flatten_the_json(multi):
    data = multi.get(f"/api/recommendations?day={MULTI_DAY}").json()
    reco = "/api/excel/recommendations"
    header, tickets = parse_tsv(get(multi, reco, day=MULTI_DAY, section="tickets"))
    assert header == list(xa.RECO_TICKET_COLUMNS)
    assert len(tickets) == len(data["tickets"]) == 4
    for row, ticket in zip(tickets, data["tickets"]):
        assert float(row["target"]) == ticket["target"] and row["status"] == ticket["status"]
        assert row["legs"] == str(len(ticket["legs"]))
        assert_number(row["total_odds"], ticket["total_odds"])
        assert row["disclaimer"] == data["disclaimer"]
        if ticket["status"] == "unavailable":
            assert row["reason"] and row["selections"] == ""
        else:
            assert row["selections"].count("@") == len(ticket["legs"])
    header, legs = parse_tsv(get(multi, reco, day=MULTI_DAY))
    assert header == list(xa.RECO_LEG_COLUMNS)
    expected = [(t, leg) for t in data["tickets"] for leg in t["legs"]]
    assert len(legs) == len(expected) > 0
    for row, (ticket, leg) in zip(legs, expected):
        assert (row["match_id"], row["key"]) == (leg["match_id"], leg["key"])
        assert row["label"] == leg["label"] and row["ticket_status"] == ticket["status"]
        assert row["legs_count"] == str(len(ticket["legs"]))
        assert_number(row["odds"], leg["odds"])
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["kickoff_utc"])
        for field in xa.LOGO_COLUMNS:
            assert row[field] == logo_cell(leg.get(field)), field
    for ticket in data["tickets"]:
        rows = [r for r in legs if float(r["target"]) == ticket["target"]]
        if rows:
            product = math.prod(float(r["odds"]) for r in rows)
            assert product == pytest.approx(float(rows[0]["ticket_total_odds"]), rel=1e-4)
    header, singles = parse_tsv(get(multi, reco, day=MULTI_DAY, section="singles"))
    assert header == list(xa.RECO_SINGLE_COLUMNS)
    assert [r["rank"] for r in singles] == [str(n) for n in range(1, len(data["singles"]) + 1)]
    probabilities = [float(r["probability"]) for r in singles]
    assert probabilities == sorted(probabilities, reverse=True)
    # One sport, another target: the same optimizer (no minimum probability anywhere).
    _, tennis = parse_tsv(
        get(multi, reco, day=MULTI_DAY, sports="tennis", targets="3", section="tickets")
    )
    assert len(tennis) == 1 and tennis[0]["sports"] in ("", "tennis")


@pytest.mark.parametrize(
    ("params", "text"),
    [
        ({"sports": "golf"}, "Sport necunoscut"),
        ({"targets": "0.5"}, ""),
        ({"section": "bogus"}, "legs"),
        ({"day": "26.09.2026"}, "day"),
    ],
)
def test_recommendation_errors_are_tables_without_provider_calls(multi, params, text):
    before = len(multi.fake.calls)
    query = {"day": MULTI_DAY, **params}
    for fmt, parse in (("tsv", parse_tsv), ("csv", parse_csv)):
        header, rows = parse(get(multi, "/api/excel/recommendations", fmt, **query), 422)
        assert header == list(xa.ERROR_COLUMNS) and text in rows[0]["error"]
    assert len(multi.fake.calls) == before


@pytest.mark.parametrize("sport", ["football", "basketball", "tennis"])
def test_live_tables_flatten_the_json(multi, sport):
    data = multi.get(f"/api/live?sport={sport}").json()
    header, rows = parse_tsv(get(multi, "/api/excel/live", sport=sport))
    assert header == list(xa.LIVE_COLUMNS)
    assert [r["match_id"] for r in rows] == [item["match"]["id"] for item in data["matches"]]
    for row, item in zip(rows, data["matches"]):
        assert row["sport"] == sport and row["status"] == "live"
        assert row["score_home"] == str(item["score"]["home"])
        assert_number(row["p_1"], item["probabilities"].get("1"))
        assert_number(row["p_x"], item["probabilities"].get("X"))
        assert row["odds_note"] == data["odds_note"] and row["disclaimer"] == data["disclaimer"]
        if item["suggestions"]:
            assert row["suggestion_key"] == item["suggestions"][0]["key"]
            assert row["suggestions"].count(" | ") == len(item["suggestions"]) - 1
        for field in xa.LOGO_COLUMNS:
            shown = item.get(field) or xa.display_url(item["match"].get(field))
            assert row[field] == logo_cell(shown), field
    header, markets = parse_tsv(get(multi, "/api/excel/live", sport=sport, section="markets"))
    assert header == list(xa.LIVE_MARKET_COLUMNS)
    # In-play markets have fair odds only: never a bookmaker price column.
    assert "odds" not in header and "ev" not in header
    assert len(markets) == sum(len(item["markets"]) for item in data["matches"])
    assert {r["reliable"] for r in markets} <= {"0", "1"}


def test_live_errors(multi):
    parse_tsv(get(multi, "/api/excel/live", sport="golf"), 422)
    parse_tsv(get(multi, "/api/excel/live", section="bogus"), 422)


def simulate_json(client, query):
    body = dict(query)
    for key in ("bankroll", "target_odds", "stake", "reinvest"):
        if key in body:
            body[key] = float(body[key])
    if "restart_on_loss" in body:
        body["restart_on_loss"] = body["restart_on_loss"] == "1"
    return json_of(client, "POST", "/api/simulate", json=body)


def test_simulate_ticket_tables_match_the_json(multi):
    data = simulate_json(multi, TICKET_QUERY)
    sim = "/api/excel/simulate"
    header, summary = parse_tsv(get(multi, sim, section="summary", **TICKET_QUERY))
    assert header == list(xa.SIM_SUMMARY_COLUMNS)
    row = summary[0]
    for key in ("initial", "final", "profit", "staked", "roi", "hit_rate", "max_drawdown"):
        assert_number(row[key], data[key])
    assert row["bets"] == str(data["bets"]) and row["dataset"] == "football"
    assert row["baseline_label"] == data["baseline"]["label"]
    assert row["warnings"] == " | ".join(data["warnings"])
    assert row["first_run_days"] == "" and row["ladders"] == ""
    _, days = parse_tsv(get(multi, sim, **TICKET_QUERY))
    assert len(days) == len(data["rows"]) and [d["n"] for d in days[:2]] == ["1", "2"]
    for day, bet in zip(days, data["rows"]):
        assert day["date"] == bet["date"] and day["result"] == bet["result"]
        assert_number(day["bankroll_after"], bet["bankroll_after"])
        assert day["legs_count"] == str(len(bet["legs"]))
    _, legs = parse_tsv(get(multi, sim, section="legs", **TICKET_QUERY))
    assert len(legs) == sum(len(bet["legs"]) for bet in data["rows"])
    _, equity = parse_tsv(get(multi, sim, section="equity", **TICKET_QUERY))
    assert [e["date"] for e in equity] == [h["date"] for h in data["history"]]
    header, ladders = parse_tsv(get(multi, sim, section="ladders", **TICKET_QUERY))
    assert header == list(xa.SIM_LADDER_COLUMNS) and ladders == []


def test_simulate_ladder_tables_match_the_json(multi):
    data = simulate_json(multi, LADDER_QUERY)
    ladder = data["ladder"]
    sim = "/api/excel/simulate"
    _, summary = parse_tsv(get(multi, sim, section="summary", **LADDER_QUERY))
    row = summary[0]
    for key in ("first_run_days", "longest_streak", "restarts", "days_without_ticket"):
        assert row[key] == str(ladder[key]), key
    for key in ("first_run_peak", "total_invested", "total_returned", "net"):
        assert_number(row[key], ladder[key])
    assert row["days"] == str(len(data["days"])) and row["ladders"] == str(len(ladder["ladders"]))
    _, days = parse_tsv(get(multi, sim, **LADDER_QUERY))
    assert len(days) == len(data["days"]) > 0
    for day, entry in zip(days, data["days"]):
        assert day["result"] == entry["result"]
        assert day["ladder_index"] == str(entry["ladder_index"])
        assert_number(day["bankroll_after"], entry["bankroll_after"])
        legs = (entry["ticket"] or {}).get("legs") or []
        assert day["legs_count"] == str(len(legs))
        if entry["result"] == "skipped":
            assert day["selections"] == ""
    _, ladders = parse_tsv(get(multi, sim, section="ladders", **LADDER_QUERY))
    assert len(ladders) == len(ladder["ladders"]) > 0
    assert {r["status"] for r in ladders} <= {"lost", "cashed", "open"}


def canned_leg(**update):
    leg = {
        "match_id": "m\t1",
        "sport": "football",
        "kickoff": "2026-09-20T15:00:00+00:00",
        "competition": "Liga\n1",
        "home": "=cmd|' /C calc'!A0",
        "away": "Away\nFC",
        "market": "Victorie gazde",
        "key": "1",
        "odds": 1.5,
        "probability": 0.7,
        "result": "won",
        "score": "2-0",
        "home_logo": "/api/img?u=https%3A%2F%2Fflagcdn.com%2Fw40%2Fro.png",
        "away_logo": None,
        "league_logo": None,
    }
    return leg | update


def canned_ladder():
    """A ladder answer shaped like the binding contract (won, lost, then a skipped day)."""
    lost = {"index": 1, "start": "2026-09-20", "end": "2026-09-21", "days": 1, "peak": 7.5}
    fresh = {"index": 2, "start": "2026-09-22", "end": "2026-09-22", "days": 0, "peak": 5.0}
    return {
        "dataset": {"id": "recent", "label": "Ultimele zile"},
        "sport": "football",
        "strategy": "ladder",
        "mode": "ladder",
        "target_odds": 2.0,
        "initial": 5.0,
        "final": 2.5,
        "profit": -2.5,
        "bets": 2,
        "won": 1,
        "lost": 1,
        "void": 0,
        "ladder": {
            "first_run_days": 1,
            "first_run_peak": 7.5,
            "longest_streak": 1,
            "longest_streak_peak": 7.5,
            "ladders": [
                lost | {"final": 0.0, "status": "lost"},
                fresh | {"final": 5.0, "status": "open"},
            ],
            "restarts": 1,
            "total_invested": 10.0,
            "total_returned": 5.0,
            "net": -5.0,
            "days_without_ticket": 1,
        },
        "days": [
            {
                "date": "2026-09-20",
                "ticket": {"legs": [canned_leg()], "total_odds": 1.5, "probability": 0.7},
                "stake": 5.0,
                "result": "won",
                "bankroll_after": 7.5,
                "ladder_index": 1,
                "streak_day": 1,
            },
            {
                "date": "2026-09-21",
                "ticket": {"legs": [canned_leg(result="lost", odds=2.0)]},
                "stake": 7.5,
                "result": "lost",
                "bankroll_after": 0.0,
                "ladder_index": 1,
                "streak_day": 2,
            },
            {
                "date": "2026-09-22",
                "ticket": None,
                "stake": 0.0,
                "result": "skipped",
                "bankroll_after": 5.0,
                "ladder_index": 2,
                "streak_day": 0,
                "reason": "Niciun bilet nu atinge cota.",
            },
        ],
        "history": [{"date": "2026-09-20", "bankroll": 7.5}, {"date": "2026-09-21", "bankroll": 0}],
        "warnings": ["Avertisment 1", "Avertisment 2"],
        "disclaimer": "Simulare cu bani virtuali. 18+.",
    }


@pytest.fixture
def fake_api(multi, monkeypatch):
    """Replaces the in-process JSON call: (calls, responses keyed by (method, path))."""
    calls, responses = [], {}

    async def call(request, method, path, params=None, body=None):
        calls.append((method, path, params, body))
        return responses[(method, path)]

    monkeypatch.setattr(xa, "call_api", call)
    multi.app.state.excel_sim_memo = {}
    yield calls, responses
    multi.app.state.excel_sim_memo = {}


def test_simulate_query_maps_to_the_post_body_and_runs_once_per_ttl(multi, fake_api, monkeypatch):
    calls, responses = fake_api
    responses[("POST", "/api/simulate")] = canned_ladder()
    now = [100.0]
    monkeypatch.setattr(xa, "clock", lambda: now[0])
    sim = "/api/excel/simulate"
    query = {
        "dataset": "recent",
        "sports": "tennis,football",
        "days": "7",
        "bankroll": "5",
        "strategy": "ladder",
        "target_odds": "2",
        "reinvest": "0.5",
        "restart_on_loss": "0",
        "max_days": "10",
    }
    _, summary = parse_tsv(get(multi, sim, section="summary", **query))
    body = {
        "dataset": "recent",
        "sports": ["football", "tennis"],
        "days": 7,
        "bankroll": 5.0,
        "strategy": "ladder",
        "target_odds": 2.0,
        "reinvest": 0.5,
        "restart_on_loss": False,
        "max_days": 10,
    }
    assert calls == [("POST", "/api/simulate", None, body)]
    row = summary[0]
    assert (row["dataset"], row["dataset_label"]) == ("recent", "Ultimele zile")
    assert (row["days"], row["ladders"], row["restarts"], row["net"]) == ("3", "2", "1", "-5")
    assert row["warnings"] == "Avertisment 1 | Avertisment 2"
    _, days = parse_tsv(get(multi, sim, **query))
    assert [(d["result"], d["odds"], d["payout"], d["legs_count"]) for d in days] == [
        ("won", "1.5", "7.5", "1"),
        ("lost", "2", "0", "1"),
        ("skipped", "", "", "0"),
    ]
    assert days[0]["selections"] == "=cmd|' /C calc'!A0 - Away FC: Victorie gazde @1.50 (won)"
    assert days[2]["reason"] == "Niciun bilet nu atinge cota."
    _, ladders = parse_tsv(get(multi, sim, section="ladders", **query))
    assert [(r["n"], r["status"]) for r in ladders] == [("1", "lost"), ("2", "open")]
    header, legs = parse_tsv(get(multi, sim, section="legs", **query))
    assert header == list(xa.SIM_LEG_COLUMNS)
    assert [(r["date"], r["n"], r["leg"], r["status"]) for r in legs] == [
        ("2026-09-20", "1", "1", "won"),
        ("2026-09-21", "2", "1", "lost"),
    ]
    # TSV: no tab or newline inside a value, the exact text otherwise. CSV: formula guard.
    first = legs[0]
    assert (first["match_id"], first["competition"], first["away"]) == ("m 1", "Liga 1", "Away FC")
    assert first["home"] == "=cmd|' /C calc'!A0" and first["label"] == "Victorie gazde"
    assert first["home_logo"] == f"{BASE}/api/img?u=https%3A%2F%2Fflagcdn.com%2Fw40%2Fro.png"
    assert first["away_logo"] == "" and first["kickoff_utc"] == "2026-09-20T15:00:00Z"
    _, csv_legs = parse_csv(get(multi, sim, "csv", section="legs", **query))
    assert csv_legs[0]["home"] == "'=cmd|' /C calc'!A0"
    # Five tables, one simulation; a new one only after the TTL.
    assert len(calls) == 1
    now[0] += xa.SIM_TTL + 1
    parse_tsv(get(multi, sim, section="equity", **query))
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"dataset": "nope"}, 422),
        ({"bankroll": "0"}, 422),
        ({"sports": "golf", "dataset": "recent", "days": "3"}, 422),
        ({"days": "61", "dataset": "recent"}, 422),
        ({"reinvest": "1.5", "strategy": "ladder", "target_odds": "2"}, 422),
        ({"section": "bogus"}, 422),
        ({"dataset": "tennis", "bankroll": "100"}, 404),
        ({"strategy": "ladder", "bankroll": "5"}, 422),
    ],
)
def test_simulate_errors_are_tables(multi, params, status):
    header, rows = parse_tsv(get(multi, "/api/excel/simulate", **params), status)
    assert header == list(xa.ERROR_COLUMNS) and rows[0]["error"]
    assert "Errno" not in rows[0]["error"] and "Traceback" not in rows[0]["error"]


def test_simulate_datasets_table(multi):
    data = multi.get("/api/simulate/datasets").json()["datasets"]
    header, rows = parse_tsv(get(multi, "/api/excel/simulate/datasets"))
    assert header == list(xa.SIM_DATASET_COLUMNS)
    assert [r["id"] for r in rows] == [d["id"] for d in data]
    assert {r["available"] for r in rows} <= {"0", "1"}
    assert next(r for r in rows if r["id"] == "football")["available"] == "1"


def test_recent_days_status_and_prepare(multi, fake_api):
    calls, responses = fake_api
    state = {
        "status": "running",
        "done": 1,
        "total": 3,
        "days_loaded": 1,
        "days_total": 3,
        "matches": 4,
        "loaded_matches": 4,
        "requests": 1,
        "message": "Rezultate fotbal 2026-09-24…",
        "sports": ["football"],
    }
    responses[("POST", "/api/simulate/recent/prepare")] = state
    responses[("GET", "/api/simulate/recent/status")] = state | {"status": "done", "done": 3}
    recent = "/api/excel/simulate/recent"
    prepared = multi.post(recent, params={"format": "tsv", "days": 3, "sports": "football"})
    header, rows = parse_tsv(prepared)
    assert header == list(xa.RECENT_COLUMNS) and rows[0]["status"] == "running"
    _, rows = parse_tsv(get(multi, recent, days="3", sports="tennis,football"))
    assert rows[0]["status"] == "done" and rows[0]["done"] == "3"
    assert calls == [
        ("POST", "/api/simulate/recent/prepare", None, {"days": 3, "sports": ["football"]}),
        ("GET", "/api/simulate/recent/status", {"days": 3, "sports": "football,tennis"}, None),
    ]
    parse_tsv(multi.post(f"{recent}?format=tsv&days=0"), 422)
    parse_tsv(multi.post(f"{recent}?format=tsv&sports=golf"), 422)


def test_recent_status_reaches_the_real_loader(multi):
    """No mock: GET /api/excel/simulate/recent answers from the simulator's own loader."""
    header, rows = parse_tsv(get(multi, "/api/excel/simulate/recent"))
    assert header == list(xa.RECENT_COLUMNS)
    assert rows[0]["status"] in ("idle", "running", "done", "partial", "failed", "interrupted")


def place_ai_bet(client):
    client.post("/api/wallet/reset")
    assert client.post("/api/wallet/deposit", json={"amount": 100}).status_code == 200
    placed = client.post("/api/wallet/bet", json={"stake": 20, "day": MULTI_DAY, "target": 2})
    assert placed.status_code == 200, placed.text


def test_wallet_tables(multi):
    place_ai_bet(multi)
    try:
        data = multi.get("/api/wallet").json()
        header, summary = parse_tsv(get(multi, "/api/excel/wallet"))
        assert header == list(xa.WALLET_SUMMARY_COLUMNS)
        assert summary[0]["balance"] == "80" and summary[0]["bets"] == "1"
        assert summary[0]["currency"] == "RON" and "18+" in summary[0]["notice"]
        _, bets = parse_tsv(get(multi, "/api/excel/wallet", section="bets"))
        bet = data["bets"][0]
        assert [(b["id"], b["status"], b["legs_count"]) for b in bets] == [
            (bet["id"], "pending", str(len(bet["legs"])))
        ]
        assert bets[0]["created_utc"] == "2026-09-25T20:00:00Z"
        _, legs = parse_tsv(get(multi, "/api/excel/wallet", section="legs"))
        assert [(r["bet_id"], r["match_id"]) for r in legs] == [
            (bet["id"], leg["match_id"]) for leg in bet["legs"]
        ]
        _, history = parse_tsv(get(multi, "/api/excel/wallet", section="history"))
        assert [h["type"] for h in history] == [h["type"] for h in data["history"]]
        parse_tsv(get(multi, "/api/excel/wallet", section="bogus"), 422)
    finally:
        multi.post("/api/wallet/reset")


def test_logo_links_are_absolute_proxy_urls_of_allowed_hosts_only():
    base = "http://127.0.0.1:8000"
    assert xa.logo_link("/api/img?u=abc", base) == f"{base}/api/img?u=abc"
    upstream = "https://static.flashscore.com/res/image/data/x.png"
    assert xa.logo_link(upstream, base) == (
        f"{base}/api/img?u=https%3A%2F%2Fstatic.flashscore.com%2Fres%2Fimage%2Fdata%2Fx.png"
    )
    for bad in (
        None,
        "",
        "https://evil.example/x.png",
        "http://flagcdn.com/w40/ro.png",
        "https://flagcdn.com.evil.example/x.png",
        42,
    ):
        assert xa.logo_link(bad, base) is None, bad


def test_api_error_details_are_romanian():
    assert xa.api_detail(404, {"detail": "Not Found"}) == xa.MISSING_FEATURE
    assert xa.api_detail(405, None) == xa.MISSING_FEATURE
    live = "Meciul nu este live acum."
    assert xa.api_detail(404, {"detail": live}) == live
    assert xa.api_detail(422, {"detail": [{"loc": ["body"]}]}) == "Parametri invalizi."
    assert "HTTP 500" in xa.api_detail(500, None)


def test_missing_json_feature_becomes_an_update_hint(multi, monkeypatch):
    """A server without a JSON route (an older build) gives a clear Romanian 404 table."""

    async def missing(request, method, path, params=None, body=None):
        raise xa.HTTPException(404, xa.api_detail(404, {"detail": "Not Found"}))

    monkeypatch.setattr(xa, "call_api", missing)
    _, rows = parse_tsv(get(multi, "/api/excel/simulate/recent"), 404)
    assert "start.ps1" in rows[0]["error"]


def test_every_multisport_layout_column_has_the_declared_type(multi):
    """Same check as for football, on the basketball/tennis and product tables."""
    multi.post("/api/excel/analyze/KMHepeEM?format=tsv&sport=basketball")
    multi.post("/api/excel/analyze/KnR6QDo1?format=tsv&sport=tennis")
    place_ai_bet(multi)
    try:
        for name, (method, path, params, _) in LAYOUT_TABLES.items():
            if name in FOOTBALL_LAYOUTS:
                continue
            query = {"format": "tsv", "day": MULTI_DAY, **params}
            if path == "/api/excel/simulate":
                query.update(LADDER_QUERY)
            response = multi.request(method, path, params=query)
            assert response.status_code == 200, (name, response.text)
            header, rows = vba_parse(response.content.decode("utf-8"))
            assert rows, f"{name}: no data to check"
            for key, _, fmt in LAYOUTS[name]:
                assert key in header, f"{name}: {key} missing from {path}"
                pattern = FORMATS[fmt]
                for row in rows:
                    value = row[key]
                    assert pattern is None or value == "" or pattern.match(value), (
                        name,
                        key,
                        value,
                    )
    finally:
        multi.post("/api/wallet/reset")


def test_power_query_types_every_product_text_column_as_text(multi):
    """A text column typed as a number would become Error cells in Power Query."""
    code = power_query_function()
    listed = set(re.findall(r'"(\w+)"', code[code.index("ColoaneText") : code.index("Numerice")]))
    place_ai_bet(multi)
    requests = [
        ("/api/excel/predictions", {"sport": "basketball"}),
        ("/api/excel/predictions", {"sport": "tennis"}),
        ("/api/excel/predictions", {}),
        ("/api/excel/recommendations", {"section": "tickets"}),
        ("/api/excel/recommendations", {"section": "legs"}),
        ("/api/excel/recommendations", {"section": "singles"}),
        ("/api/excel/live", {"sport": "tennis"}),
        ("/api/excel/live", {"sport": "football", "section": "markets"}),
        ("/api/excel/simulate", {**LADDER_QUERY, "section": "summary"}),
        ("/api/excel/simulate", {**LADDER_QUERY, "section": "days"}),
        ("/api/excel/simulate", {**LADDER_QUERY, "section": "legs"}),
        ("/api/excel/simulate", {**LADDER_QUERY, "section": "ladders"}),
        ("/api/excel/simulate", {**LADDER_QUERY, "section": "equity"}),
        ("/api/excel/simulate/datasets", {}),
        ("/api/excel/simulate/recent", {}),
        ("/api/excel/wallet", {"section": "summary"}),
        ("/api/excel/wallet", {"section": "bets"}),
        ("/api/excel/wallet", {"section": "legs"}),
        ("/api/excel/wallet", {"section": "history"}),
        ("/api/excel/health", {}),
    ]
    try:
        for path, params in requests:
            header, rows = parse_tsv(get(multi, path, day=MULTI_DAY, **params))
            assert rows, path
            for column in header:
                values = [r[column] for r in rows if r[column]]
                if any(not NUMBER.match(v) for v in values):
                    assert column in listed, (path, column)
    finally:
        multi.post("/api/wallet/reset")


# --- simulation memo: TTL from the end of the run, one run for parallel sections, store-aware


class _MemoApp:
    def __init__(self):
        from types import SimpleNamespace

        self.state = SimpleNamespace(store=SimpleNamespace(version=1))


class _MemoRequest:
    def __init__(self, app):
        self.app = app


def test_simulation_memo_starts_ttl_after_the_run_and_shares_parallel_runs(monkeypatch):
    import asyncio

    now = [1000.0]
    calls = []

    async def slow(request, method, path, params=None, body=None):
        calls.append(body)
        await asyncio.sleep(0.01)
        now[0] += 70  # a run longer than SIM_TTL
        return {"n": len(calls)}

    monkeypatch.setattr(xa, "clock", lambda: now[0])
    monkeypatch.setattr(xa, "call_api", slow)
    request = _MemoRequest(_MemoApp())
    body = {"dataset": "football", "bankroll": 5}

    async def scenario():
        # Power Query "Refresh All": five sections at once -> one simulation.
        first = await asyncio.gather(*(xa.simulation(request, body) for _ in range(5)))
        assert first == [{"n": 1}] * 5 and len(calls) == 1
        # 70 s after the start but just after the end: still the same run.
        now[0] += 1
        assert await xa.simulation(request, body) == {"n": 1}
        now[0] += xa.SIM_TTL
        assert await xa.simulation(request, body) == {"n": 2}

    asyncio.run(scenario())


def test_simulation_memo_of_local_datasets_follows_the_store(monkeypatch):
    import asyncio

    calls = []

    async def fast(request, method, path, params=None, body=None):
        calls.append(body)
        return {"n": len(calls)}

    monkeypatch.setattr(xa, "clock", lambda: 5.0)
    monkeypatch.setattr(xa, "call_api", fast)
    app = _MemoApp()
    request = _MemoRequest(app)
    recent = {"dataset": "recent", "days": 7, "sports": ["football"]}
    fixed = {"dataset": "football"}

    async def scenario():
        assert await xa.simulation(request, recent) == {"n": 1}
        assert await xa.simulation(request, fixed) == {"n": 2}
        assert await xa.simulation(request, recent) == {"n": 1}
        # More recent days were loaded (store write): the next click runs a new simulation.
        app.state.store.version += 1
        assert await xa.simulation(request, recent) == {"n": 3}
        assert await xa.simulation(request, fixed) == {"n": 2}

    asyncio.run(scenario())


# --- review fixes: busy guard, timeouts, competition per sport, recent prompt, cash-out ------


def test_every_macro_refuses_to_start_while_another_runs():
    for name, proc in PROCS.items():
        if proc["scope"] != "Public":
            continue
        body = [item[1] for item in body_of(name)]
        guard = body.index("If IsBusy() Then Exit Sub")
        assert guard < body.index("On Error GoTo Fail"), name
    begin = [item[1] for item in body_of("BeginWork")]
    end = [item[1] for item in body_of("EndWork")]
    assert begin[0] == "m_busy = True" and end[0] == "m_busy = False"


def test_long_computations_wait_longer_and_timeouts_are_named():
    assert "Private Const HTTP_TIMEOUT_ERROR As Long = -2147012894" in BAS_TEXT
    timeout = " ".join(item[2] for item in body_of("ReceiveTimeout"))
    assert "/api/excel/simulate" in timeout and "/api/excel/recommendations" in timeout
    assert "LONG_RECEIVE_TIMEOUT_MS" in timeout
    http = " ".join(item[1] for item in body_of("HttpCall"))
    assert "ReceiveTimeout(url)" in http
    failure = " ".join(item[2] for item in body_of("RaiseHttpFailure"))
    assert "prima rulare" in failure
    power_query = (CLIENT_DIR / "PowerQuery.md").read_text(encoding="utf-8")
    assert "Timeout = #duration(0, 0, 10, 0)" in power_query


def test_competition_filter_resets_when_the_sport_changes():
    competition = [item[1] for item in body_of("PanelCompetition")]
    assert any("LIST_COMP_SPORT" in x and "PanelSport()" in x for x in competition)
    assert "ResetCompetition" in competition
    loader = " ".join(item[2] for item in body_of("LoadCompetitions"))
    assert "ws.Range(LIST_COMP_SPORT).Value = sport" in loader and "ResetCompetition" in loader
    reset = " ".join(item[1] for item in body_of("ResetCompetition"))
    assert "CELL_COMP" in reset


def test_recent_load_asks_before_spending_requests_and_keeps_loaded_days(multi):
    prepare = [item[2] for item in body_of("PrepareRecentDays")]
    text = " ".join(prepare)
    ask = next(i for i, x in enumerate(prepare) if "MsgBox" in x)
    post = next(i for i, x in enumerate(prepare) if '"POST"' in x)
    assert ask < post and "planned" in text and "vbYesNoCancel" in text
    assert 'loadState = "failed" Or loadState = "interrupted"' in text
    assert "days_loaded" in text
    # The API answers the planned request count the question shows.
    header, rows = parse_tsv(get(multi, "/api/excel/simulate/recent", days="3", sports="football"))
    assert "planned" in header and int(rows[0]["planned"]) >= 0


def test_simulation_sheet_sends_the_cash_out_and_charts_net_for_ladders():
    query = " ".join(item[2] for item in body_of("SimulationQuery"))
    assert "&max_days=" in query and "SimMaxDays()" in query
    run = " ".join(item[2] for item in body_of("RuleazaSimularea"))
    assert "&section=equity" in run and "LayoutSimEquity()" in run
    assert "total_invested" in run and "total_returned" in run
    equity = {e[0] for e in LAYOUTS["LayoutSimEquity"]}
    assert {"date", "net"} <= equity <= set(xa.SIM_EQUITY_COLUMNS)
