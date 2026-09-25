"""Simulator datasets: CSV parsing, availability, local store data, tennis and caching."""

import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from footypreds import simulator as sim
from footypreds.domain import Match
from footypreds.evaluation import sim_datasets as sd
from footypreds.store import Store
from footypreds.tests.test_simulator import football_records

HEADER = "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,AvgH,AvgD,AvgA,Avg>2.5,Avg<2.5\n"


def write_benchmark(directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    content = ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")
    (directory / "matches.jsonl").write_bytes(content)
    manifest = {"dataset_sha256": hashlib.sha256(content).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_parse_csv_reads_results_and_average_odds():
    content = (
        HEADER
        + "E1,09/08/2025,20:00,Leeds,Burnley,2,1,1.9,3.5,4.2,1.8,2.0\n"
        + "E1,10/08/25,15:00,Málaga,Stoke,0,0,2.5,3.1,0.9,,x\n"
        + "E1,11/08/2025,15:00,Hull,Derby,,,2.1,3.3,3.4,1.9,1.9\n"
        + "E1,01/01/2099,15:00,Luton,QPR,1,0,2.1,3.3,3.4,1.9,1.9\n"
        + ",,,,,,,,,,,\n"
    ).encode("cp1252")
    records = sd.parse_csv(content, "E1", "2526", today=date(2026, 9, 25))
    assert [r["match"]["id"] for r in records] == [
        "fd-E1-2025-08-09-Leeds-Burnley",
        "fd-E1-2025-08-10-Málaga-Stoke",
    ]
    first = records[0]
    assert first["reference_odds"] == {"1": 1.9, "X": 3.5, "2": 4.2, "over25": 1.8, "under25": 2.0}
    assert first["match"]["league"] == "Championship" and first["match"]["country"] == "Anglia"
    assert first["season"] == "2526" and first["league_code"] == "E1"
    assert records[1]["reference_odds"] == {"1": 2.5, "X": 3.1}  # 0.9 and "x" are not prices
    with pytest.raises(ValueError, match="necunoscută"):
        sd.parse_csv(content, "XX", "2526")
    with pytest.raises(ValueError, match="Schema"):
        sd.parse_csv(b"Date,Home\n1,2\n", "E1", "2526")


def test_ids_and_odds_are_the_same_as_the_benchmark_parser():
    from footypreds.evaluation.dataset import parse_archive

    rows = "".join(
        f"E0,01/08/2024,15:00,T{i},U{i},{i % 3},1,1.9,3.5,4.2,1.8,2.0\n" for i in range(260)
    )
    content = (HEADER + rows).encode()
    ours = sd.parse_csv(content, "E0", "2425", today=date(2026, 1, 1))
    theirs = parse_archive(content, "E0", "2425")
    assert [r["match"]["id"] for r in ours] == [r["match"]["id"] for r in theirs]
    assert [r["reference_odds"] for r in ours] == [r["reference_odds"] for r in theirs]
    assert ours[0]["match"]["id"] == "fd-E0-2024-08-01-T0-U0"


def test_football_plus_needs_the_extra_download(tmp_path):
    benchmark = write_benchmark(tmp_path / "bench", football_records())
    with pytest.raises(FileNotFoundError):
        sd.load_football_plus(benchmark, benchmark / "sim")
    raw = benchmark / "sim" / "raw"
    raw.mkdir(parents=True)
    (raw / "2425-E1.csv").write_text(
        HEADER + "E1,09/08/2024,20:00,Leeds,Burnley,2,1,1.9,3.5,4.2,1.8,2.0\n", encoding="utf-8"
    )
    (raw / "notes.csv").write_text("ignored", encoding="utf-8")
    plus = sd.load_football_plus(benchmark, benchmark / "sim", today=date(2026, 1, 1))
    assert set(plus.groups) == {"E1", "T1"}
    assert plus.periods["fd-E1-2024-08-09-Leeds-Burnley"] == "2425"
    assert len(plus.matches) == len(football_records()) + 1


def test_availability_lists_every_dataset_with_hints(tmp_path):
    items = sd.availability(None, tmp_path / "nothing")
    assert [i["id"] for i in items] == list(sd.DATASET_IDS)
    for item in items:
        assert item["available"] is False and item["hint"]
        assert set(item) >= {
            "id",
            "sport",
            "label",
            "matches",
            "start",
            "end",
            "source",
            "available",
            "hint",
        }
    benchmark = write_benchmark(tmp_path / "bench", football_records())
    football = sd.availability(None, benchmark)[0]
    assert football["available"] is True
    assert football["matches"] == 240 and football["bettable"] == 240
    assert football["start"] == "2024-06-30" and football["end"] == "2025-05-03"


def test_tennis_is_unavailable_without_its_module_or_files(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "footypreds.evaluation.tennis_data", None)
    with pytest.raises(FileNotFoundError, match="nu este instalat"):
        sd.load_tennis(tmp_path)
    monkeypatch.delitem(sys.modules, "footypreds.evaluation.tennis_data")
    pytest.importorskip("footypreds.evaluation.tennis_data")
    with pytest.raises(FileNotFoundError, match="nu sunt descărcate"):
        sd.load_tennis(tmp_path)


def tennis_matches(days=200):
    """Seeded synthetic ATP season: the stronger name wins most of the time."""
    players = ["Alpha A.", "Beta B.", "Gamma C.", "Delta D.", "Eps E.", "Zeta Z."]
    start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    rows = []
    for day in range(days):
        a, b = players[day % 6], players[(day * 5 + 1) % 6]
        if a == b:
            continue
        strong = players.index(a) < players.index(b)
        upset = day % 7 == 0
        home_wins = strong != upset
        rows.append(
            Match(
                id=f"td-{day}",
                kickoff=start + timedelta(days=day),
                league="ATP - SINGLES: Test (Town), hard",
                home=a,
                away=b,
                status="finished",
                home_goals=2 if home_wins else day % 2,
                away_goals=day % 2 if home_wins else 2,
                odds={"1": 1.5, "2": 2.6} if strong else {"1": 2.6, "2": 1.5},
                sport="tennis",
                source="tennis-data.co.uk;tour=atp;best_of=3",
                finish_type="retired" if day % 23 == 0 else "",
            )
        )
    return rows


def test_tennis_dataset_and_blind_simulation(tmp_path, monkeypatch):
    module = pytest.importorskip("footypreds.evaluation.tennis_data")
    raw = tmp_path / "tennis" / "raw"
    raw.mkdir(parents=True)
    (raw / "atp_2024.xlsx").write_bytes(b"placeholder")
    walkover = Match(
        id="wo",
        kickoff=datetime(2024, 3, 1, 12, tzinfo=timezone.utc),
        league="ATP - SINGLES: Test (Town), hard",
        home="Alpha A.",
        away="Beta B.",
        status="unavailable",
        sport="tennis",
        finish_type="walkover",
    )
    monkeypatch.setattr(
        module,
        "load_tennis_matches",
        lambda years=None, directory=None, **_: tennis_matches() + [walkover],
    )
    dataset = sd.load_tennis(tmp_path / "tennis")
    assert dataset.sport == "tennis" and "wo" not in {m.id for m in dataset.matches}
    assert dataset.periods["td-40"] == "2024-02"
    seen = []
    real = sim.analyze_match

    def spy(fixture, index, threshold, **kw):
        assert fixture.home_goals is None and fixture.status == "scheduled"
        assert all(m.kickoff < fixture.kickoff - timedelta(hours=3) for m in index.rows)
        seen.append(fixture.id)
        return real(fixture, index, threshold, **kw)

    monkeypatch.setattr(sim, "analyze_match", spy)
    result = sim.simulate(
        dataset,
        bankroll=100,
        strategy="singles",
        stake=5,
        cache_dir=None,
        workers=1,
        max_bets_per_day=2,
    )
    assert seen and result["sport"] == "tennis"
    assert result["bets"] == result["won"] + result["lost"] + result["void"]
    for row in result["rows"]:
        if row["match_id"].startswith("td-") and int(row["match_id"][3:]) % 23 == 0:
            assert row["result"] == "void"


def test_local_dataset_uses_stored_finished_matches_with_odds(tmp_path):
    store = Store(tmp_path / "local.db")
    records = football_records()
    matches = [Match.model_validate(r["match"]) for r in records]
    priced = {r["match"]["id"]: r["reference_odds"] for r in records[-60:]}
    store.save_matches(
        [
            m.model_copy(update={"source": "flashscore", "odds": priced.get(m.id, {})})
            for m in matches
        ]
        + [
            Match(
                id="future",
                kickoff=datetime(2025, 6, 1, tzinfo=timezone.utc),
                league="L",
                home="A",
                away="B",
                odds={"1": 2.0, "X": 3.0, "2": 3.5},
            )
        ]
    )
    dataset = sd.load_local(store, "football")
    assert dataset.id == "local-football" and len(dataset.matches) == 240
    assert len(dataset.bettable) == 60 and not dataset.fit_ratings
    assert dataset.period_of(dataset.matches[0]) == "2023-08"
    result = sim.simulate(dataset, strategy="singles", stake=10, cache_dir=None, workers=1)
    assert result["dataset"]["id"] == "local-football"
    assert "baza locală" in result["warning"]
    assert sd.load_local(store, "basketball").describe()["available"] is False


def test_cached_dataset_keeps_a_parsed_copy_on_disk(tmp_path, monkeypatch):
    benchmark = write_benchmark(tmp_path / "bench", football_records())
    parsed = tmp_path / "parsed"
    monkeypatch.setattr(sd, "_MEMO", {})
    first = sd.cached_dataset("football", None, benchmark, parsed)
    assert list(parsed.glob("football-*.json"))
    monkeypatch.setattr(sd, "_MEMO", {})
    monkeypatch.setattr(sd, "load_dataset", lambda *a, **k: pytest.fail("parsed again"))
    second = sd.cached_dataset("football", None, benchmark, parsed)
    assert [m.model_dump() for m in second.matches] == [m.model_dump() for m in first.matches]
    assert second.periods == first.periods and second.fit_ratings is True
    assert sd.cached_dataset("football", None, benchmark, parsed) is second  # in memory


def test_cached_dataset_follows_file_changes(tmp_path, monkeypatch):
    benchmark = write_benchmark(tmp_path / "bench", football_records())
    monkeypatch.setattr(sd, "_MEMO", {})
    first = sd.cached_dataset("football", None, benchmark, tmp_path / "parsed")
    write_benchmark(benchmark, football_records()[:-3])
    second = sd.cached_dataset("football", None, benchmark, tmp_path / "parsed")
    assert len(first.matches) - len(second.matches) == 3


def test_load_dataset_rejects_unknown_ids(tmp_path):
    with pytest.raises(KeyError):
        sd.load_dataset("cricket", None, tmp_path)
    with pytest.raises(FileNotFoundError):
        sd.load_dataset("local-football", None, tmp_path)
