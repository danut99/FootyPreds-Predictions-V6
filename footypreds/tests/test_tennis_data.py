"""tennis-data.co.uk loader: workbook parsing, orientation, odds, download (mocked)."""

import io
import json
from datetime import date, datetime

import httpx
import pytest
from openpyxl import Workbook

from footypreds.engine import HistoryIndex
from footypreds.evaluation import tennis_data as td
from footypreds.sports import tennis
from footypreds.sports.settle import settle

ATP_HEADER = [
    "ATP", "Location", "Tournament", "Date", "Series", "Court", "Surface", "Round", "Best of",
    "Winner", "Loser", "WRank", "LRank", "WPts", "LPts", "W1", "L1", "W2", "L2", "W3", "L3",
    "W4", "L4", "W5", "L5", "Wsets", "Lsets", "Comment", "B365W", "B365L", "PSW", "PSL",
    "MaxW", "MaxL", "AvgW", "AvgL",
]  # fmt: skip
WTA_HEADER = [
    "WTA", "Location", "Tournament", "Date", "Tier", "Court", "Surface", "Round", "Best of",
    "Winner", "Loser", "WRank", "LRank", "WPts", "LPts", "W1", "L1", "W2", "L2", "W3", "L3",
    "Wsets", "Lsets", "Comment", "B365W", "B365L", "PSW", "PSL", "MaxW", "MaxL", "AvgW", "AvgL",
]  # fmt: skip
TODAY = date(2026, 9, 25)


def atp_row(winner, loser, day, sets=((6, 3), (6, 4)), comment="Completed", best=3, **extra):
    row = dict(
        ATP=1,
        Location="Madrid",
        Tournament="Mutua Madrid Open",
        Date=datetime(day.year, day.month, day.day),
        Series="Masters 1000",
        Court="Outdoor",
        Surface="Clay",
        Round="1st Round",
        **{"Best of": best},
        Winner=winner,
        Loser=loser,
        WRank=10,
        LRank=40,
        Comment=comment,
        B365W=1.3,
        B365L=3.5,
        PSW=1.32,
        PSL=3.6,
        AvgW=1.29,
        AvgL=3.4,
    )
    for n, (w, lo) in enumerate(sets, 1):
        row[f"W{n}"], row[f"L{n}"] = w, lo
    if "Wsets" not in extra:
        row["Wsets"] = sum(w > lo for w, lo in sets)
        row["Lsets"] = sum(lo > w for w, lo in sets)
    row.update(extra)
    return row


def workbook_bytes(header, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "2024"
    sheet.append(header)
    for row in rows:
        sheet.append([row.get(column) for column in header])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def parse(rows, header=ATP_HEADER, tour="atp"):
    return td.parse_workbook(io.BytesIO(workbook_bytes(header, rows)), tour, today=TODAY)


def by_names(matches, a, b):
    return next(m for m in matches if {m.home, m.away} == {a, b})


def test_completed_retired_walkover_and_awarded_rows():
    day = date(2024, 4, 25)
    rows = [
        atp_row("Alcaraz C.", "Ruud C.", day),
        atp_row("Sinner J.", "Rune H.", day, sets=((6, 3), (3, 6), (7, 6))),
        atp_row("Zverev A.", "Fritz T.", day, sets=((6, 3), (2, 1)), comment="Retired"),
        atp_row("Medvedev D.", "Paul T.", day, sets=(), comment="Walkover", Wsets=None),
        atp_row("Bublik A.", "Rublev A.", day, sets=((6, 7), (7, 6)), comment="Awarded"),
    ]
    matches, rejected = parse(rows)
    assert rejected == 0 and len(matches) == 5
    straight = by_names(matches, "Alcaraz C.", "Ruud C.")
    assert straight.sport == "tennis" and straight.status == "finished"
    assert straight.league == "ATP - SINGLES: Mutua Madrid Open (Madrid), clay"
    assert straight.kickoff.hour == 12 and straight.kickoff.date() == day
    winner_home = straight.home == "Alcaraz C."
    assert (straight.home_goals, straight.away_goals) == ((2, 0) if winner_home else (0, 2))
    # Odds follow the orientation: the winner's average price is 1.29.
    assert straight.odds["1" if winner_home else "2"] == 1.29
    assert td.extra(straight)["odds"] == "avg" and td.extra(straight)["best_of"] == "3"
    three = by_names(matches, "Sinner J.", "Rune H.")
    games = td.extra(three)["games"]
    assert games == ("6-3 3-6 7-6" if three.home == "Sinner J." else "3-6 6-3 6-7")
    retired = by_names(matches, "Zverev A.", "Fritz T.")
    assert retired.status == "finished" and retired.finish_type == "retired"
    assert settle("tennis", "1", retired.home_goals, retired.away_goals, "retired") is None
    walkover = by_names(matches, "Medvedev D.", "Paul T.")
    assert walkover.status == "unavailable" and walkover.finish_type == "walkover"
    assert walkover.home_goals is None
    awarded = by_names(matches, "Bublik A.", "Rublev A.")
    assert awarded.finish_type == "retired"
    # Walkovers never reach the history; retirements do.
    assert {m.id for m in HistoryIndex(matches).rows} == {m.id for m in matches} - {walkover.id}


def test_bad_rows_are_rejected_and_best_of_is_repaired():
    day = date(2024, 7, 6)
    rows = [
        atp_row("Fritz T.", "Tabilo A.", day, sets=((7, 6), (6, 3), (7, 5))),  # "Best of 3"
        atp_row("Tsonga J.W.", "Bedene A.", day, sets=((6, 7), (3, 4))),  # loser leads
        atp_row("Future P.", "Later P.", date(2027, 1, 1)),
        atp_row("", "Nobody P.", day),
        atp_row("Odd P.", "Case P.", day, comment="Something"),
        atp_row("Same P.", "Same P.", day),
    ]
    matches, rejected = parse(rows)
    assert rejected == 5 and len(matches) == 1
    assert td.extra(matches[0])["best_of"] == "5"
    assert tennis.best_of(matches[0]) == 5


def test_odds_fall_back_to_pinnacle_then_bet365():
    day = date(2024, 5, 2)
    rows = [
        atp_row("A P.", "B P.", day, AvgW=None, AvgL=None),
        atp_row("C P.", "D P.", day, AvgW=None, AvgL=None, PSW=None, PSL=None),
        atp_row("E P.", "F P.", day, AvgW=None, AvgL=None, PSW=None, B365W=None),
    ]
    matches, _ = parse(rows)
    ps, b365, none = (
        by_names(matches, *pair) for pair in (("A P.", "B P."), ("C P.", "D P."), ("E P.", "F P."))
    )
    assert td.extra(ps)["odds"] == "ps" and sorted(ps.odds.values()) == [1.32, 3.6]
    assert td.extra(b365)["odds"] == "b365" and sorted(b365.odds.values()) == [1.3, 3.5]
    assert none.odds == {} and "odds" not in td.extra(none)


def test_orientation_is_deterministic_and_balanced():
    day = date(2024, 3, 10)
    rows = [atp_row(f"Winner{i} W.", f"Loser{i} L.", day) for i in range(200)]
    first, _ = parse(rows)
    second, _ = parse(rows)
    assert [(m.id, m.home) for m in first] == [(m.id, m.home) for m in second]
    home_wins = sum(m.home.startswith("Winner") for m in first)
    assert 70 < home_wins < 130
    for m in first:
        assert (m.home_goals > m.away_goals) == m.home.startswith("Winner")
        assert (m.odds["1"] < m.odds["2"]) == m.home.startswith("Winner")
        extra = td.extra(m)
        ranks = (extra["rank_home"], extra["rank_away"])
        assert ranks == (("10", "40") if m.home.startswith("Winner") else ("40", "10"))


def test_wta_workbook_uses_the_tier_column_and_women_serve_base():
    rows = [atp_row("Swiatek I.", "Gauff C.", date(2024, 6, 8), Tier="Grand Slam")]
    for row in rows:
        row.pop("ATP")
        row["WTA"] = 1
        row["Tournament"] = "French Open"
        row["Location"] = "Paris"
    matches, rejected = parse(rows, WTA_HEADER, "wta")
    assert rejected == 0
    match = matches[0]
    assert match.league == "WTA - SINGLES: French Open (Paris), clay"
    assert td.extra(match)["series"] == "Grand Slam" and tennis.is_women(match)
    assert tennis.best_of(match) == 3 and match.id.startswith("td-wta-2024-")


def test_unknown_schema_and_tour_are_errors():
    with pytest.raises(td.TennisDataError):
        td.parse_workbook(io.BytesIO(workbook_bytes(["Foo", "Bar"], [])), "atp")
    with pytest.raises(td.TennisDataError):
        td.parse_workbook(io.BytesIO(workbook_bytes(ATP_HEADER, [])), "itf")


def test_load_reads_stored_workbooks_in_order(tmp_path):
    early = [atp_row("A P.", "B P.", date(2023, 5, 1))]
    late = [atp_row("C P.", "D P.", date(2024, 5, 1)), atp_row("E P.", "F P.", date(2024, 1, 3))]
    (tmp_path / "atp_2023.xlsx").write_bytes(workbook_bytes(ATP_HEADER, early))
    (tmp_path / "atp_2024.xlsx").write_bytes(workbook_bytes(ATP_HEADER, late))
    matches = td.load_tennis_matches(directory=tmp_path, today=TODAY)
    assert [m.kickoff.date() for m in matches] == sorted(m.kickoff.date() for m in matches)
    assert len(matches) == 3
    assert len(td.load_tennis_matches(years=[2024], directory=tmp_path, today=TODAY)) == 2
    assert td.load_tennis_matches(tours=("wta",), directory=tmp_path, today=TODAY) == []


INDEX_HTML = """
<a HREF="hrjk-secret/2025/2025.xlsx">2025</a>
<A HREF="hrjk-secret/2025w/2025.xlsx">2025 women</A>
<a HREF="hrjk-secret/2012/2012.xls">old</a>
<a href="https://www.tennis-data.co.uk/other/2024/2024.xlsx">2024</a>
"""


def test_discover_reads_the_index_links():
    links = td.discover(INDEX_HTML)
    assert links == {
        ("atp", 2025): "https://www.tennis-data.co.uk/hrjk-secret/2025/2025.xlsx",
        ("wta", 2025): "https://www.tennis-data.co.uk/hrjk-secret/2025w/2025.xlsx",
        ("atp", 2024): "https://www.tennis-data.co.uk/other/2024/2024.xlsx",
    }
    assert td.fallback_url("wta", 2024).endswith("2024w/2024.xlsx")


def test_download_uses_the_index_and_keeps_raw_files(tmp_path):
    content = workbook_bytes(ATP_HEADER, [atp_row("A P.", "B P.", date(2025, 2, 1))])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("alldata.php"):
            return httpx.Response(200, text=INDEX_HTML)
        if request.url.path == "/hrjk-secret/2025/2025.xlsx":
            return httpx.Response(200, content=content)
        return httpx.Response(404)

    raw = tmp_path / "tennis" / "raw"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        entries = td.download([2025], ("atp", "wta"), directory=raw, client=client)
    assert [e["file"] for e in entries] == ["atp_2025.xlsx"]
    assert (raw / "atp_2025.xlsx").read_bytes() == content
    manifest = json.loads((tmp_path / "tennis" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["atp_2025.xlsx"]["url"].endswith("/hrjk-secret/2025/2025.xlsx")
    assert len(manifest["atp_2025.xlsx"]["sha256"]) == 64
    # The women's link is on the index page but answers 404: skipped.
    assert any(url.endswith("2025w/2025.xlsx") for url in calls)
    calls.clear()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert td.download([2025], ("atp",), directory=raw, client=client) == []
    assert all("2025.xlsx" not in url for url in calls)


def test_download_rejects_a_page_that_is_not_a_workbook(tmp_path):
    def handler(request):
        if request.url.path.endswith("alldata.php"):
            return httpx.Response(500)
        return httpx.Response(200, text="<html>not found</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(td.TennisDataError):
            td.download([2025], ("atp",), directory=tmp_path / "raw", client=client)
    assert not (tmp_path / "raw" / "atp_2025.xlsx").exists()
