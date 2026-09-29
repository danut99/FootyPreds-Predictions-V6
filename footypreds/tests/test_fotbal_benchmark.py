"""fotbalPrediction: data parser, downloader, market settlement and walk-forward benchmark.

Everything runs on tiny synthetic CSVs / rows in tmp_path; nothing is downloaded.
"""

import hashlib
import json
import random
from datetime import date, timedelta

import httpx
import numpy as np
import pytest

from fotbalPrediction import benchmark as bench
from fotbalPrediction import data
from fotbalPrediction import markets as mk
from fotbalPrediction.candidates import baseline

MAIN_HEADER = (
    "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,HS,AS,HST,AST,"
    "HF,AF,HC,AC,HY,AY,HR,AR,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5,AHh,AvgAHH,AvgAHA,"
    "AvgCH,AvgCD,AvgCA,AHCh,AvgCAHH,AvgCAHA,,"
)


def main_csv(rows, header=MAIN_HEADER, bom=True, newline="\r\n", encoding="utf-8"):
    text = newline.join([header] + rows) + newline
    content = text.encode(encoding)
    return (b"\xef\xbb\xbf" + content) if bom else content


def main_line(day, home, away, fthg, ftag, **kw):
    values = {
        "Time": "15:00",
        "HTHG": "0",
        "HTAG": "0",
        "Referee": " M Dean ",
        "HS": "10",
        "AS": "8",
        "HST": "4",
        "AST": "3",
        "HF": "11",
        "AF": "12",
        "HC": "6",
        "AC": "4",
        "HY": "2",
        "AY": "1",
        "HR": "0",
        "AR": "1",
        "AvgH": "2.10",
        "AvgD": "3.40",
        "AvgA": "3.60",
        "Avg>2.5": "1.90",
        "Avg<2.5": "1.95",
        "AHh": "-0.25",
        "AvgAHH": "1.95",
        "AvgAHA": "1.93",
        "AvgCH": "2.00",
        "AvgCD": "3.50",
        "AvgCA": "3.80",
        "AHCh": "-0.5",
        "AvgCAHH": "2.02",
        "AvgCAHA": "1.86",
    }
    values.update(kw)
    cells = [
        "E0",
        day,
        values["Time"],
        home,
        away,
        fthg,
        ftag,
        "H",
        values["HTHG"],
        values["HTAG"],
        "D",
        values["Referee"],
        values["HS"],
        values["AS"],
        values["HST"],
        values["AST"],
        values["HF"],
        values["AF"],
        values["HC"],
        values["AC"],
        values["HY"],
        values["AY"],
        values["HR"],
        values["AR"],
        values["AvgH"],
        values["AvgD"],
        values["AvgA"],
        values["Avg>2.5"],
        values["Avg<2.5"],
        values["AHh"],
        values["AvgAHH"],
        values["AvgAHA"],
        values["AvgCH"],
        values["AvgCD"],
        values["AvgCA"],
        values["AHCh"],
        values["AvgCAHH"],
        values["AvgCAHA"],
        "",
        "",
    ]
    return ",".join(str(c) for c in cells)


# --------------------------------------------------------------------------- parser


def test_parse_main_handles_bom_crlf_empty_headers_sorting_and_abandoned_rows():
    content = main_csv(
        [
            main_line("20/08/23", " Arsenal ", "Chelsea", "2", "1"),
            main_line("12/08/23", "Burnley", "Man City", "0", "3", HC="", AC=""),
            main_line("13/08/23", "Bastia", "Red Star", "", "", HTHG="0"),  # abandoned
            ",,,,,,,,,,",
        ]
    )
    rows = data.parse_main(content, "E0", "2324")
    assert [r.home for r in rows] == ["Burnley", "Arsenal"]  # sorted, abandoned skipped
    first, second = rows
    assert first.date == date(2023, 8, 12) and first.time == "15:00"
    assert first.home_corners is None and first.away_corners is None  # stats optional per row
    assert second.referee == "M Dean" and second.home == "Arsenal"
    assert second.country == "Anglia" and second.tier == 1 and second.season == "2324"
    assert second.home_red == 0 and second.away_red == 1
    avg = second.odds["avg"]
    assert avg["1"] == 2.10 and avg["over25"] == 1.90
    assert avg["ah_1_-0.25"] == 1.95 and avg["ah_2_+0.25"] == 1.93
    closing = second.odds["avg_closing"]
    assert data.is_closing("avg_closing") and not data.is_closing("avg")
    assert closing["1"] == 2.00 and closing["ah_1_-0.5"] == 2.02 and closing["ah_2_+0.5"] == 1.86
    assert second.id == "E0-20230820-Arsenal-Chelsea"


def test_parse_main_four_digit_year_cp1252_and_forfeit_without_ht():
    line = main_line(
        "05/12/2021",
        "King’s Lynn",
        "Wrexham",
        "3",
        "0",
        HTHG="",
        HTAG="",
        HS="",
        AS="",
        HST="",
        AST="",
    )
    content = main_csv([line], bom=False, encoding="cp1252")
    with pytest.raises(UnicodeDecodeError):
        content.decode("utf-8")
    (row,) = data.parse_main(content, "EC", "2122")
    assert row.home == "King's Lynn"
    assert row.date == date(2021, 12, 5)
    assert row.ht_home_goals is None and row.home_shots is None
    assert row.home_goals == 3


def test_parse_main_betbrain_fallback_before_1920():
    header = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,BbAvH,BbAvD,BbAvA,BbAv>2.5,BbAv<2.5,BbAHh,"
        "BbAvAHH,BbAvAHA"
    )
    content = main_csv(
        ["D1,05/08/11,Dortmund,Hamburg,3,1,1.52,4.1,6.36,1.77,2.02,-1,1.85,2.02"], header=header
    )
    (row,) = data.parse_main(content, "D1", "1112")
    assert row.time is None and row.date == date(2011, 8, 5)
    assert row.odds["avg"] == {
        "1": 1.52,
        "X": 4.1,
        "2": 6.36,
        "over25": 1.77,
        "under25": 2.02,
        "ah_1_-1": 1.85,
        "ah_2_+1": 2.02,
    }
    assert "avg_closing" not in row.odds


def test_parse_tab_separated_file():
    header = MAIN_HEADER.replace(",", "\t")
    line = main_line("01/09/2026", "Leeds", "Hull", "1", "1").replace(",", "\t")
    (row,) = data.parse_main(main_csv([line], header=header), "E1", "2627")
    assert (row.home, row.away, row.home_goals, row.away_goals) == ("Leeds", "Hull", 1, 1)


def test_parse_extra_filters_competition_maps_seasons_and_aliases():
    header = "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA"
    lines = [
        "Switzerland,Super League ,2020/2021,19/09/2020, 16:00,Basel,Sion,2,0,H,,,,1.5,4,6",
        "Switzerland,Super League,2020/2021,20/09/2020,16:00,Zurich,Servette,1,1,D,,,,2,3.5,3.5",
        "Switzerland,Challenge League,2020/2021,01/06/2021,18:00,Thun,Vaduz,0,1,A,,,,2,3,3",
        "Romania,Superliga,2012/2013,20/07/2012,18:20,Steaua,Din. Bucuresti,1,2,A,2,3,4,,,",
    ]
    rows = data.parse_extra(main_csv(lines, header=header), "SWZ")
    swz = [r for r in rows if r.season_label == "2020/2021"]
    assert {r.home for r in swz} == {"Basel", "Zurich"}  # Challenge League row dropped
    assert all(r.season == "2021" and r.extra for r in swz)
    assert swz[0].time == "16:00"
    assert set(swz[0].odds) == {"avg_closing"}  # only closing prices
    rou = data.parse_extra(main_csv(lines[3:], header=header), "ROU")
    assert rou[0].away == "Dinamo Bucuresti" and rou[0].season == "1213"
    calendar = header + "\r\nBrazil,Serie A,2023,15/04/2023,20:00,Palmeiras,Cuiaba,2,1,H,,,,1.3,5,9"
    calendar += "\r\nBrazil,Serie A,2023,01/10/2023,20:00,Santos,Gremio,0,0,D,,,,2,3,3\r\n"
    bra = data.parse_extra(calendar.encode(), "BRA")
    assert [r.season for r in bra] == ["2223", "2324"]  # July-June season of the date


def test_load_rows_uses_cache_keyed_by_mtime(tmp_path, monkeypatch):
    raw, cache = tmp_path / "raw", tmp_path / "cache"
    path = data.main_path(raw, "2324", "E0")
    path.parent.mkdir(parents=True)
    path.write_bytes(main_csv([main_line("12/08/23", "A", "B", "1", "0")]))
    rows = data.load_rows(["E0"], "2324", "2324", raw_dir=raw, cache_dir=cache)
    assert len(rows) == 1 and list(cache.glob("*.pkl"))

    def boom(*args, **kwargs):
        raise AssertionError("cache ignored")

    monkeypatch.setattr(data, "parse_main", boom)
    assert data.load_rows(["E0"], "2324", "2324", raw_dir=raw, cache_dir=cache) == rows
    monkeypatch.undo()
    path.write_bytes(
        main_csv(
            [main_line("12/08/23", "A", "B", "1", "0"), main_line("13/08/23", "C", "D", "0", "0")]
        )
    )
    import os

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert len(data.load_rows(["E0"], "2324", "2324", raw_dir=raw, cache_dir=cache)) == 2


def test_download_writes_manifest_skips_existing_and_refreshes_running_season(tmp_path):
    served = main_csv([main_line("12/08/23", "A", "B", "1", "0")])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "2425" in str(request.url):
            return httpx.Response(404)
        if "2223" in str(request.url):
            return httpx.Response(200, text="<html>nope</html>")
        return httpx.Response(200, content=served)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = tmp_path / "raw"
    seasons = ["2223", "2324", "2425", "2627"]
    counts = data.download(
        ["E0", "ROU"], seasons, raw_dir=raw, client=client, log=lambda m: None, sleep=lambda s: None
    )
    assert counts == {"downloaded": 3, "missing": 1, "failed": 1}
    manifest = data.read_manifest(raw)["files"]
    entry = manifest["main/2324/E0.csv"]
    assert entry["sha256"] == hashlib.sha256(served).hexdigest()
    assert entry["url"] == data.MAIN_URL.format(season="2324", code="E0")
    assert entry["downloaded_at"]
    assert manifest["main/2425/E0.csv"]["status"] == 404
    assert not data.main_path(raw, "2223", "E0").exists()  # HTML is never saved as CSV
    calls.clear()
    counts = data.download(
        ["E0", "ROU"], seasons, raw_dir=raw, client=client, log=lambda m: None, sleep=lambda s: None
    )
    assert counts["skipped"] == 3 and counts["missing"] == 1
    assert calls == [data.MAIN_URL.format(season="2223", code="E0")]
    calls.clear()
    data.download(
        ["E0", "ROU"],
        seasons,
        raw_dir=raw,
        client=client,
        refresh=True,
        log=lambda m: None,
        sleep=lambda s: None,
    )
    assert data.MAIN_URL.format(season="2627", code="E0") in calls
    assert data.EXTRA_URL.format(code="ROU") in calls
    assert data.MAIN_URL.format(season="2324", code="E0") not in calls


# --------------------------------------------------------------------------- markets


def row_with(**kw):
    base = dict(
        league="E0",
        season="2324",
        date=date(2023, 8, 12),
        home="A",
        away="B",
        home_goals=2,
        away_goals=1,
        ht_home_goals=1,
        ht_away_goals=0,
        home_corners=6,
        away_corners=3,
        home_yellow=2,
        away_yellow=1,
        home_red=1,
        away_red=0,
        home_sot=5,
        away_sot=2,
    )
    base.update(kw)
    return data.MatchRow(**base)


def test_settlement_of_every_family():
    row = row_with()
    expect = {
        "1": 1.0,
        "X": 0.0,
        "2": 0.0,
        "1X": 1.0,
        "X2": 0.0,
        "12": 1.0,
        "over25": 1.0,
        "under25": 0.0,
        "over35": 0.0,
        "under45": 1.0,
        "over05": 1.0,
        "btts": 1.0,
        "no_btts": 0.0,
        "home_over15": 1.0,
        "home_under_1.5": 0.0,
        "away_over05": 1.0,
        "away_under_0.5": 0.0,
        "home_over_2.5": 0.0,
        "dnb_1": 1.0,
        "dnb_2": 0.0,
        "ah_1_-0.5": 1.0,
        "ah_2_+0.5": 0.0,
        "ah_1_-1.5": 0.0,
        "ah_2_+1.5": 1.0,
        "cs_2-1": 1.0,
        "cs_1-2": 0.0,
        "cs_other": 0.0,
        "ht_1": 1.0,
        "ht_X": 0.0,
        "ht_over05": 1.0,
        "ht_under15": 1.0,
        "ht_over15": 0.0,
        "corners_over_8.5": 1.0,
        "corners_under_9.5": 1.0,
        "home_corners_over_5.5": 1.0,
        "away_corners_under_3.5": 1.0,
        "corners_ah_1_-2.5": 1.0,
        "corners_ah_2_+2.5": 0.0,
        "cards_over_3.5": 1.0,
        "cards_under_4.5": 1.0,
        "home_cards_over_2.5": 1.0,
        "away_cards_under_1.5": 1.0,
        "cards_over_4.5": 0.0,
        "bookings_over_4.5": 1.0,
        "bookings_under_5.5": 1.0,
        "sot_over_6.5": 1.0,
        "home_sot_over_4.5": 1.0,
        "away_sot_under_2.5": 1.0,
    }
    for key, value in expect.items():
        assert mk.outcome(key, row) == value, key
    # Pushes: whole lines refund the stake.
    assert mk.settle("ah_1_-1", row) == (0.0, 0.0) and mk.outcome("ah_1_-1", row) is None
    assert mk.outcome("corners_ah_1_-3.5", row_with(home_corners=6, away_corners=3)) == 0.0
    assert mk.outcome("cs_other", row_with(home_goals=5, away_goals=0)) == 1.0
    # Cards: plain count (yellow + red) vs booking points (yellow 1, red 2).
    assert mk.stat_pair(row, "cards") == (3, 1) and mk.stat_pair(row, "bookings") == (4, 1)


def test_quarter_lines_half_win_and_half_loss():
    draw, home_by_one, home_by_two = (1, 1), (2, 1), (3, 1)
    assert mk.settle_score("ah_1_-0.25", *draw) == (0.0, 0.5)  # half lost, half refunded
    assert mk.settle_score("ah_2_+0.25", *draw) == (0.5, 0.0)  # half won, half refunded
    assert mk.settle_score("ah_1_-0.75", *home_by_one) == (0.5, 0.0)
    assert mk.settle_score("ah_1_-0.75", *home_by_two) == (1.0, 0.0)
    assert mk.settle_score("ah_1_-1.25", *home_by_one) == (0.0, 0.5)
    assert mk.settle_score("ah_2_+1.75", *home_by_two) == (0.0, 0.5)  # +1.5 lost, +2 refunded
    assert mk.settle_score("ah_2_+1.75", *home_by_one) == (1.0, 0.0)
    assert mk.outcome_of(0.5, 0.0) == 1.0 and mk.outcome_of(0.0, 0.5) == 0.0
    assert mk.outcome_of(0.0, 0.0) is None
    assert mk.profit(0.5, 0.0, 1.9) == pytest.approx(0.45)
    assert mk.profit(0.0, 0.5, 1.9) == pytest.approx(-0.5)
    # A quarter line is the average of its two neighbouring lines.
    for h in range(5):
        for a in range(5):
            quarter = mk.settle_score("ah_1_-0.75", h, a)
            low, high = mk.settle_score("ah_1_-0.5", h, a), mk.settle_score("ah_1_-1", h, a)
            assert quarter == ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)


def test_missing_stats_are_not_settled():
    row = row_with(home_corners=None, ht_home_goals=None, away_red=None)
    assert mk.settle("corners_over_9.5", row) is None
    assert mk.outcome("ht_1", row) is None
    assert mk.outcome("cards_over_2.5", row) is None
    assert mk.outcome("over25", row) == 1.0


def test_core_keys_settle_like_footypreds_core():
    from footypreds.sports.settle import settle

    for key, market in mk.CATALOGUE.items():
        if not market.core:
            continue
        for h in range(6):
            for a in range(6):
                core = settle("football", key, h, a)
                ours = mk.outcome_of(*market.settle_pair(h, a))
                assert (core is None) == (ours is None), (key, h, a)
                if core is not None:
                    assert bool(ours) == core, (key, h, a)


def test_probabilities_match_core_matrix_and_are_consistent():
    from footypreds.engine.markets import full_time, score_matrix, win_push

    matrix = score_matrix(1.6, 1.1, -0.1)
    probs = mk.probabilities("goals", matrix)
    ft = full_time(matrix)
    for key in ("1", "X", "2", "1X", "over25", "under35", "btts", "home_over15"):
        assert probs[key] == pytest.approx(ft[key], abs=1e-12)
    won, push = win_push(matrix, "ah_1_-1")
    assert probs["ah_1_-1"] == pytest.approx(won / (1 - push))
    assert sum(probs[k] for k in mk.QUESTIONS["cs"]) == pytest.approx(1.0)
    assert probs["ah_1_-0.5"] == pytest.approx(probs["1"])
    win, loss = mk.expectations("goals", matrix, ["ah_1_-0.25"])["ah_1_-0.25"]
    assert mk.fair_odds(win, loss) == pytest.approx(1 + loss / win)
    counts = mk.count_probabilities("corners", (5.5, 9.0), (4.5, 7.0))
    assert counts["corners_over_9.5"] + counts["corners_under_9.5"] == pytest.approx(1.0)
    pmf = mk.negbin_pmf(5.0, 9.0, 60)
    mean = sum(k * p for k, p in enumerate(pmf))
    var = sum((k - mean) ** 2 * p for k, p in enumerate(pmf))
    assert mean == pytest.approx(5.0, abs=1e-6) and var == pytest.approx(9.0, abs=1e-3)


def test_catalogue_keys_are_unique_and_resolvable():
    assert len(mk.KEYS) == len(set(mk.KEYS))
    assert "ah_1_-0.75" in mk.resolve_keys("ah") and "1" not in mk.resolve_keys("ah")
    assert set(mk.resolve_keys("corners")) >= {"corners_over_9.5", "corners_ah_1_-1.5"}
    with pytest.raises(ValueError):
        mk.resolve_keys("nope")
    assert not mk.CATALOGUE["cs_other"].selectable


# --------------------------------------------------------------------------- benchmark


TEAMS = [f"T{i}" for i in range(8)]


def synthetic_rows(seasons=("2021", "2122", "2223", "2324"), leagues=("E0", "E1"), seed=3):
    rng = random.Random(seed)
    rows = []
    for league in leagues:
        for season in seasons:
            start = date(data.season_start(season), 8, 1)
            for week in range(14):
                day = start + timedelta(days=7 * week)
                order = TEAMS[:]
                rng.shuffle(order)
                for home, away in zip(order[::2], order[1::2]):
                    strength = TEAMS.index(home) - TEAMS.index(away)
                    hg = max(0, int(rng.gauss(1.5 + 0.2 * strength, 1.1)))
                    ag = max(0, int(rng.gauss(1.1 - 0.2 * strength, 1.0)))
                    rows.append(
                        data.MatchRow(
                            league=league,
                            season=season,
                            date=day,
                            home=f"{league}{home}",
                            away=f"{league}{away}",
                            home_goals=hg,
                            away_goals=ag,
                            ht_home_goals=min(hg, rng.randint(0, 1)),
                            ht_away_goals=min(ag, rng.randint(0, 1)),
                            home_corners=rng.randint(2, 9),
                            away_corners=rng.randint(1, 7),
                            home_yellow=rng.randint(0, 3),
                            away_yellow=rng.randint(0, 3),
                            home_red=int(rng.random() < 0.1),
                            away_red=int(rng.random() < 0.1),
                            home_sot=rng.randint(1, 8),
                            away_sot=rng.randint(1, 7),
                            referee="R X",
                            country="Anglia",
                            tier=1 if league == "E0" else 2,
                            odds={
                                "avg": {
                                    "1": 2.0,
                                    "X": 3.4,
                                    "2": 3.8,
                                    "over25": 1.9,
                                    "under25": 1.95,
                                    "ah_1_-0.25": 1.9,
                                    "ah_2_+0.25": 1.95,
                                },
                                "avg_closing": {"1": 2.1, "X": 3.3, "2": 3.7},
                            },
                        )
                    )
    rows.sort(key=data.sort_key)
    return rows


class SpyModel:
    """Asserts the no-leak protocol from inside the model."""

    forbidden = ("home_goals", "away_goals", "home_corners", "home_yellow", "ht_home_goals")

    def __init__(self):
        self.seen = []
        self.predicted = set()
        self.contexts = []

    def predict(self, ctx):
        for name in self.forbidden:
            assert not hasattr(ctx, name), name
        assert all(day < ctx.date for day in self.seen), "a same-day or later row leaked"
        assert ctx.match_id not in {r for r in self.predicted}
        self.predicted.add(ctx.match_id)
        self.contexts.append(ctx)
        return {"1": 0.5, "X": 0.25, "2": 0.25, "over05": 0.9, "ah_1_-0.25": 0.55}

    def update(self, row):
        self.seen.append(row.date)


def test_walk_forward_never_leaks_and_feeds_same_date_after_predictions():
    rows = synthetic_rows()
    spy = SpyModel()
    result = bench.run_benchmark(
        lambda: spy, ["2223", "2324"], ["E0"], "2021", ["E0", "E1"], rows=rows
    )
    evaluated = [r for r in rows if r.league == "E0" and r.season in ("2223", "2324")]
    assert result["meta"]["matches_evaluated"] == len(evaluated) == len(spy.contexts)
    assert result["meta"]["rows_fed"] == len(rows)
    assert all(ctx.odds is None and ctx.odds_source is None for ctx in spy.contexts)
    assert all(ctx.referee == "R X" for ctx in spy.contexts)
    assert result["meta"]["default_select"] == bench.DEFAULT_SELECT


def test_odds_only_when_requested_and_closing_is_flagged():
    rows = synthetic_rows()
    spy = SpyModel()
    bench.run_benchmark(lambda: spy, ["2223"], ["E0"], "2021", rows=rows, odds="avg", referee=False)
    ctx = spy.contexts[0]
    assert ctx.odds["1"] == 2.0 and ctx.odds_source == "avg" and not ctx.odds_closing
    assert ctx.referee is None
    with pytest.raises(TypeError):
        ctx.odds["1"] = 5.0  # read-only
    spy = SpyModel()
    result = bench.run_benchmark(lambda: spy, ["2223"], ["E0"], "2021", rows=rows, odds="closing")
    assert spy.contexts[0].odds_closing and spy.contexts[0].odds_source == "avg_closing"
    assert result["meta"]["odds_closing"] is True


def test_metrics_roi_quarter_lines_and_selection():
    rows = synthetic_rows()
    result = bench.run_benchmark(
        SpyModel, ["2223", "2324"], ["E0"], "2021", rows=rows, return_records=True
    )
    metrics = result["metrics"]
    over = metrics["keys"]["over05"]["2223"]
    assert over["select"]["n_selected"] == over["n"] + over["n_push"]  # p = 0.9 >= 0.80
    assert over["select"]["fair_odds"] == pytest.approx(1 / 0.9)
    assert over["select"]["roi"] is None  # no real price for over 0.5
    home = metrics["keys"]["1"]["2223"]
    assert home["select"]["n_selected"] == 0 and home["thresholds"]["0.70"]["n_selected"] == 0
    # ROI of the AH quarter line at the real price, from the stored records.
    records = [r for r in result["records"].iter_dicts() if r["key"] == "ah_1_-0.25"]
    expected = sum(r["win"] * (1.9 - 1) - r["loss"] for r in records) / len(records)
    got = bench._pick_stats(
        *(
            np.array(v)
            for v in (
                [r["p"] for r in records],
                [(-1 if r["y"] is None else r["y"]) for r in records],
                [r["win"] for r in records],
                [r["loss"] for r in records],
                [r["price"] for r in records],
                [True] * len(records),
            )
        ),
        len(records),
    )
    assert got["roi"] == pytest.approx(expected, abs=1e-6)
    assert metrics["questions"]["1x2"]["2223"]["n"] > 0
    assert "transfer_80" in metrics["groups"]["goals"]["2324"]


def test_coverage_at_accuracy_and_real_price():
    p = np.array([0.95] * 60 + [0.7] * 40)
    y = np.array([1] * 55 + [0] * 5 + [0] * 40)
    detail = bench.coverage_at_accuracy(p, y, 0.8)
    assert detail["n_selected"] == 60 and detail["threshold"] == 0.95
    assert bench.coverage_at_accuracy(p, y, 0.95)["n_selected"] == 0
    assert bench.real_price("1X", {"1": 2.0, "X": 4.0}) == pytest.approx(1 / 0.75)
    assert bench.real_price("btts", {"1": 2.0}) is None


def test_locked_and_running_seasons_are_refused(capsys):
    with pytest.raises(bench.LockedTestError):
        bench.check_seasons(["2324", "2526"], locked_test=False)
    assert bench.check_seasons(["2526"], locked_test=True) == ["2526"]
    with pytest.raises(bench.LockedTestError):
        bench.check_seasons(["2627"], locked_test=True)
    with pytest.raises(bench.LockedTestError):
        bench.run_benchmark(SpyModel, ["2526"], ["E0"], rows=[])
    assert bench.main(["--seasons", "2526"]) == 2
    assert "REFUZAT" in capsys.readouterr().err


def test_baseline_runs_end_to_end_on_synthetic_rows():
    rows = synthetic_rows()
    result = bench.run_benchmark(
        lambda: baseline.factory(min_league_matches=20),
        ["2223", "2324"],
        ["E0", "E1"],
        "2021",
        rows=rows,
        odds="avg",
    )
    keys = result["metrics"]["keys"]
    for key in (
        "1",
        "1X",
        "over25",
        "ah_1_-0.75",
        "ht_over05",
        "corners_over_9.5",
        "cards_over_2.5",
        "bookings_over_3.5",
        "sot_over_6.5",
        "cs_1-0",
    ):
        assert keys[key]["all"]["n"] > 0, key
    question = result["metrics"]["questions"]["1x2"]["all"]
    assert 0 < question["log_loss"] < 1.5
    groups = result["metrics"]["groups"]
    assert groups["all"]["all"]["matches"] == result["meta"]["matches_evaluated"]


def test_baseline_is_deterministic_and_uses_only_fed_rows():
    rows = synthetic_rows(seasons=("2122", "2223"))
    target = next(r for r in rows if r.season == "2223")
    history = [r for r in rows if r.date < target.date]
    ctx = bench.make_context(target)
    first = baseline.factory(min_league_matches=10)
    second = baseline.factory(min_league_matches=10)
    for row in history:
        first.update(row)
        second.update(row)
    assert first.predict(ctx) == second.predict(ctx)
    assert baseline.factory(min_league_matches=10).predict(ctx) == {}  # nothing fed


def test_v8_core_adapter_runs_on_synthetic_rows():
    from fotbalPrediction.candidates import v8core

    rows = synthetic_rows(seasons=("2122", "2223"), leagues=("E0",))
    result = bench.run_benchmark(v8core.factory, ["2223"], ["E0"], "2122", ["E0"], rows=rows)
    keys = result["metrics"]["keys"]
    assert keys["1"]["2223"]["n"] > 0 and keys["ht_over05"]["2223"]["n"] > 0
    assert "corners_over_9.5" not in keys


def test_cli_writes_json(tmp_path, monkeypatch):
    rows = synthetic_rows()
    monkeypatch.setattr(data, "load_rows", lambda *a, **k: rows)
    out = tmp_path / "m.json"
    code = bench.main(
        [
            "--model",
            "fotbalPrediction.candidates.baseline:factory",
            "--param",
            "min_league_matches=10",
            "--seasons",
            "2223",
            "--leagues",
            "E0",
            "--first-season",
            "2021",
            "--json",
            str(out),
            "--records",
            str(tmp_path / "r.jsonl"),
            "--markets",
            "1x2,goals",
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload["metrics"]["keys"]) <= set(mk.resolve_keys("1x2,goals"))
    assert payload["meta"]["params"] == {"min_league_matches": 10}
    assert (tmp_path / "r.jsonl").read_text(encoding="utf-8").count("\n") > 0
