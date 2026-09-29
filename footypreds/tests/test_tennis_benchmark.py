"""Walk-forward tennis benchmark: ordering, anti-leakage, orientation, metrics and the lock."""

import csv
import json
import math
import random

import pytest

from tenisPrediction import benchmark as bm
from tenisPrediction.candidates import baseline

HEADER = list(bm.CSV_COLUMNS)
RESULT_FIELDS = {"score", "minutes", "winner_key", "loser_key", "is_walkover"} | {
    name for name in bm.CSV_COLUMNS if name.startswith(("w_", "l_", "winner_", "loser_"))
}


def match(
    tourney_id,
    date,
    number,
    round_name,
    winner,
    loser,
    *,
    score="6-3 6-4",
    surface="Hard",
    winner_rank=10,
    loser_rank=50,
    name="Test Open",
):
    return {
        "tourney_id": tourney_id,
        "tourney_name": name,
        "surface": surface,
        "draw_size": "32",
        "tourney_level": "A",
        "indoor": "O",
        "tourney_date": date,
        "match_num": "" if number is None else str(number),
        "winner_id": winner,
        "winner_name": f"Player {winner}",
        "winner_hand": "R",
        "winner_ht": "185",
        "winner_ioc": "ROU",
        "winner_age": "25.5",
        "winner_rank": str(winner_rank),
        "winner_rank_points": "1000",
        "loser_id": loser,
        "loser_name": f"Player {loser}",
        "loser_hand": "L",
        "loser_ht": "180",
        "loser_ioc": "ESP",
        "loser_age": "27.1",
        "loser_rank": str(loser_rank),
        "loser_rank_points": "500",
        "score": score,
        "best_of": "3",
        "round": round_name,
        "minutes": "95",
        "w_ace": "7",
        "w_svpt": "60",
        "w_1stWon": "30",
        "w_2ndWon": "12",
        "l_ace": "2",
        "l_svpt": "62",
        "l_1stWon": "25",
        "l_2ndWon": "10",
    }


def write(directory, relative, rows):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in HEADER})
    return path


def synthetic_season(
    directory, year, players=40, events=30, seed=3, tour_file="{year}.csv", prefix="P"
):
    """Knockout events of 16 players with fixed strengths; the stronger usually wins."""
    rng = random.Random(seed + year)
    strength = {f"{prefix}{i}": i / players * 4 for i in range(players)}
    rows = []
    for event in range(events):
        day = 1 + (event % 4) * 7
        month = 1 + (event // 4) % 12
        date = f"{year}{month:02d}{day:02d}"
        entrants = rng.sample(sorted(strength), 16)
        number = 0
        for round_name in ("R16", "QF", "SF", "F"):
            winners = []
            for a, b in zip(entrants[::2], entrants[1::2]):
                p = 1 / (1 + math.exp(strength[b] - strength[a]))
                winner, loser = (a, b) if rng.random() < p else (b, a)
                number += 1
                rows.append(
                    match(
                        f"{year}-{event}",
                        date,
                        number,
                        round_name,
                        winner,
                        loser,
                        winner_rank=100 - int(strength[winner] * 20),
                        loser_rank=100 - int(strength[loser] * 20),
                    )
                )
                winners.append(winner)
            entrants = winners
    write(directory, tour_file.format(year=year), rows)
    return rows


def load(directory, first, last, tours=bm.TOURS):
    return bm.load_rows(first, last, tours, data_dir=directory, cache_dir=None)


# ----------------------------------------------------------------------------- ordering


def test_quali_before_main_draw_and_round_fallback_for_empty_match_num(tmp_path):
    write(
        tmp_path,
        "2024.csv",
        [
            match("2024-1", "20240108", 3, "F", "A", "B"),
            match("2024-1", "20240108", 1, "SF", "A", "C"),
            match("2024-1", "20240108", 2, "SF", "B", "D"),
            match("2024-2", "20240115", None, "F", "E", "F"),
            match("2024-2", "20240115", None, "QF", "E", "G"),
            match("2024-2", "20240115", None, "SF", "E", "H"),
            match("2024-2", "20240115", 7, "R32", "E", "I"),
        ],
    )
    write(
        tmp_path,
        "atp_quali/2024_atp_quali.csv",
        [
            match("2024-1", "20240108", 2, "Q2", "C", "X"),
            match("2024-1", "20240108", 1, "Q1", "C", "Y"),
        ],
    )
    rows = load(tmp_path, 2024, 2024)
    order = [(row["tour"], row["tourney_id"], row["round"]) for row in rows]
    assert order == [
        ("quali", "2024-1", "Q1"),
        ("quali", "2024-1", "Q2"),
        ("atp", "2024-1", "SF"),
        ("atp", "2024-1", "SF"),
        ("atp", "2024-1", "F"),
        ("atp", "2024-2", "R32"),
        ("atp", "2024-2", "QF"),
        ("atp", "2024-2", "SF"),
        ("atp", "2024-2", "F"),
    ]
    assert sum(row["match_num"] is None for row in rows) == 3  # kept, not dropped
    assert rows[0]["season"] == 2024 and rows[0]["date"].isoformat() == "2024-01-08"


def test_round_beats_out_of_order_match_num_and_split_event_dates(tmp_path):
    write(
        tmp_path,
        "2024.csv",
        [
            match("2024-9", "20240819", 1, "SF", "A", "B"),  # numbered/dated before its QF
            match("2024-9", "20240818", 5, "QF", "A", "C"),
            match("2024-9", "20240819", 2, "F", "A", "D"),
        ],
    )
    write(
        tmp_path,
        "atp_quali/2024_atp_quali.csv",
        [
            match("2024-9", "20240821", 1, "Q1", "D", "E"),  # dated after the main draw start
        ],
    )
    rows = load(tmp_path, 2024, 2024)
    assert [row["round"] for row in rows] == ["Q1", "QF", "SF", "F"]


def test_impossible_dates_are_repaired_and_walkovers_flagged(tmp_path):
    write(
        tmp_path,
        "2024_challenger.csv",
        [
            match("2024-2205", "20071231", 1, "R32", "A", "B", score="W/O"),
            match("2024-2205", "20071231", 2, "R32", "C", "D", score="6-4 2-1 RET"),
        ],
    )
    rows = load(tmp_path, 2024, 2024)
    assert {row["tourney_date"] for row in rows} == {"20231231"}
    assert [row["is_walkover"] for row in rows] == [True, False]
    assert rows[0]["winner_rank"] == 10 and rows[0]["winner_age"] == 25.5


def test_wta_keys_are_namespaced(tmp_path):
    write(tmp_path, "2024.csv", [match("2024-1", "20240108", 1, "F", "100", "200")])
    write(tmp_path, "2024_wta.csv", [match("2024-5", "20240108", 1, "F", "100", "300")])
    rows = load(tmp_path, 2024, 2024)
    assert {row["winner_key"] for row in rows} == {"100", "wta:100"}


def test_cache_is_reused_and_invalidated_by_file_changes(tmp_path):
    data, cache = tmp_path / "data", tmp_path / "cache"
    path = write(data, "2024.csv", [match("2024-1", "20240108", 1, "F", "A", "B")])
    first = bm.load_rows(2024, 2024, ("atp",), data_dir=data, cache_dir=cache)
    assert (cache / "atp_2024.pkl").exists()
    again = bm.load_rows(2024, 2024, ("atp",), data_dir=data, cache_dir=cache)
    assert [dict(row) for row in again] == [dict(row) for row in first]
    write(
        data,
        "2024.csv",
        [
            match("2024-1", "20240108", 1, "F", "A", "B"),
            match("2024-1", "20240108", 2, "F", "C", "D"),
        ],
    )
    stat = path.stat()
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    changed = bm.load_rows(2024, 2024, ("atp",), data_dir=data, cache_dir=cache)
    assert len(changed) == 2


# ----------------------------------------------------------------------------- leakage


class Spy:
    def __init__(self):
        self.events = []

    def predict(self, ctx):
        names = set(bm.CONTEXT_FIELDS)
        assert not names & RESULT_FIELDS
        assert all(not hasattr(ctx, name) for name in RESULT_FIELDS)
        self.events.append(("predict", ctx.first_key, ctx.second_key, ctx.round))
        return 0.5

    def update(self, row):
        self.events.append(("update", *sorted((row["winner_key"], row["loser_key"])), row["round"]))


def test_predict_sees_only_pre_match_fields_and_update_follows(tmp_path):
    synthetic_season(tmp_path, 2023, events=4)
    synthetic_season(tmp_path, 2024, events=4)
    spies = []

    def factory():
        spies.append(Spy())
        return spies[-1]

    result = bm.run_benchmark(factory, [2024], data_dir=tmp_path, cache_dir=None, first_year=2023)
    events = spies[0].events
    predicted = [index for index, event in enumerate(events) if event[0] == "predict"]
    assert len(predicted) == result["metrics"]["atp/2024"]["n"] == 4 * 15
    for index in predicted:
        _, first, second, round_name = events[index]
        assert events[index + 1] == ("update", *sorted((first, second)), round_name)
    assert all(event[0] == "update" for event in events[: 4 * 15])  # 2023 is only fed


class Oracle:
    """Remembers every fed result; it could only beat 50% if a result leaked early."""

    def __init__(self):
        self.seen = {}

    def predict(self, ctx):
        key = (ctx.tourney_id, ctx.round, *sorted((ctx.first_key, ctx.second_key)))
        winner = self.seen.get(key)
        if winner is None:
            return 0.5
        return 1.0 if winner == ctx.first_key else 0.0

    def update(self, row):
        key = (row["tourney_id"], row["round"], *sorted((row["winner_key"], row["loser_key"])))
        self.seen[key] = row["winner_key"]


def test_no_result_reaches_its_own_prediction(tmp_path):
    synthetic_season(tmp_path, 2024, events=6)
    metrics = bm.run_benchmark(Oracle, [2024], data_dir=tmp_path, cache_dir=None, first_year=2024)[
        "metrics"
    ]["atp/2024"]
    assert metrics["accuracy"] == 0.5
    assert metrics["log_loss"] == pytest.approx(math.log(2))


def test_walkovers_are_fed_but_never_evaluated(tmp_path):
    write(
        tmp_path,
        "2024.csv",
        [
            match("2024-1", "20240108", 1, "SF", "A", "B", score="W/O"),
            match("2024-1", "20240108", 2, "SF", "C", "D", score=""),
            match("2024-1", "20240108", 3, "F", "A", "C", score="6-1 3-0 RET"),
        ],
    )
    fed = []

    class Model(Spy):
        def update(self, row):
            fed.append(row["round"])

    result = bm.run_benchmark(Model, [2024], data_dir=tmp_path, cache_dir=None, first_year=2024)
    assert result["metrics"]["atp/2024"]["n"] == 1
    assert fed == ["SF", "SF", "F"]


# ----------------------------------------------------------------------------- orientation


def test_orientation_is_deterministic_balanced_and_blind_to_the_winner(tmp_path):
    synthetic_season(tmp_path, 2024, events=60, players=60)
    rows = [row for row in load(tmp_path, 2024, 2024) if bm.is_evaluable(row)]
    flags = [bm.make_context(row)[1] for row in rows]
    assert flags == [bm.first_is_winner(row) for row in rows]
    assert 0.45 <= sum(flags) / len(flags) <= 0.55
    salted = [bm.first_is_winner(row, "other") for row in rows]
    assert salted != flags

    class FirstWins:
        def predict(self, ctx):
            return 0.9

        def update(self, row):
            pass

    metrics = bm.run_benchmark(
        FirstWins, [2024], data_dir=tmp_path, cache_dir=None, first_year=2024
    )["metrics"]["atp/2024"]
    assert 0.45 <= metrics["accuracy"] <= 0.55


def test_swapping_winner_and_loser_columns_keeps_the_same_first_player():
    row = {
        "tour": "atp",
        "tourney_id": "T",
        "match_num": 4,
        "round": "QF",
        "winner_key": "A",
        "loser_key": "B",
    }
    swapped = dict(row, winner_key="B", loser_key="A")
    assert bm.first_is_winner(row) != bm.first_is_winner(swapped)


# ----------------------------------------------------------------------------- metrics


def test_coverage_at_80_takes_largest_valid_prefix():
    confidences = [0.95] * 60 + [0.6] * 40
    correct = [1.0] * 54 + [0.0] * 6 + [1.0] * 20 + [0.0] * 20
    detail = bm.coverage_at_accuracy(confidences, correct)
    assert detail["n_selected"] == 60 and detail["coverage"] == 0.6
    assert detail["accuracy"] == pytest.approx(0.9) and detail["threshold"] == 0.95


def test_coverage_at_80_needs_fifty_matches_and_cuts_only_between_confidences():
    assert bm.coverage_at_accuracy([0.9] * 40, [1.0] * 40)["coverage"] == 0.0
    # 60 tied predictions at 75% accuracy cannot be split into an 80% subset.
    tied = bm.coverage_at_accuracy([0.8] * 60, [1.0] * 45 + [0.0] * 15)
    assert tied["coverage"] == 0.0


def test_compute_metrics_basic_values():
    metrics = bm.compute_metrics([0.5, 0.5], [1, 0])
    assert metrics["log_loss"] == pytest.approx(math.log(2))
    assert metrics["brier"] == pytest.approx(0.25)
    assert metrics["accuracy"] == 0.5 and metrics["ece"] == 0.0
    sharp = bm.compute_metrics([0.9, 0.2, 0.7, 0.4], [1, 0, 0, 0], [True, True, False, False])
    assert sharp["accuracy"] == 0.75
    assert sharp["thresholds"]["0.80"] == {"accuracy": 1.0, "coverage": 0.5, "n_selected": 2}
    assert sharp["select"] == {"accuracy": 1.0, "coverage": 0.5, "n_selected": 2}


def test_select_rule_and_transfer_are_reported(tmp_path):
    for year in (2022, 2023, 2024):
        synthetic_season(tmp_path, year, events=40)
    result = bm.run_benchmark(
        baseline.factory,
        [2023, 2024],
        data_dir=tmp_path,
        cache_dir=None,
        first_year=2022,
        return_records=True,
    )
    metrics = result["metrics"]
    assert set(metrics) == {"atp/2023", "atp/2024", "atp/all"}
    assert "select" in metrics["atp/2024"]
    assert metrics["atp/2024"]["transfer_80"]["from_year"] == 2023
    assert "transfer_80" not in metrics["atp/2023"]
    assert metrics["atp/all"]["n"] == len(result["records"]) == 2 * 40 * 15
    assert metrics["atp/2024"]["log_loss"] < math.log(2)  # the baseline learns online


def test_bad_probability_is_rejected(tmp_path):
    synthetic_season(tmp_path, 2024, events=1)

    class Broken(Spy):
        def predict(self, ctx):
            return float("nan")

    with pytest.raises(ValueError):
        bm.run_benchmark(Broken, [2024], data_dir=tmp_path, cache_dir=None, first_year=2024)


# ----------------------------------------------------------------------------- lock and CLI


def test_locked_test_year_needs_explicit_flag(tmp_path):
    synthetic_season(tmp_path, 2025, events=2)
    with pytest.raises(bm.LockedTestError):
        bm.run_benchmark(Spy, [2024, 2025], data_dir=tmp_path, cache_dir=None, first_year=2024)
    result = bm.run_benchmark(
        Spy, [2025], data_dir=tmp_path, cache_dir=None, first_year=2025, locked_test=True
    )
    assert result["meta"]["locked_test"] is True


def test_cli_refuses_locked_year_and_warns_when_forced(tmp_path, capsys):
    synthetic_season(tmp_path, 2025, events=2)
    common = [
        "--model",
        "tenisPrediction.candidates.baseline:factory",
        "--years",
        "2025",
        "--data-dir",
        str(tmp_path),
        "--no-cache",
        "--first-year",
        "2025",
    ]
    assert bm.main(common) == 2
    assert "REFUZAT" in capsys.readouterr().err
    out = tmp_path / "metrics.json"
    assert bm.main(common + ["--locked-test", "--json", str(out)]) == 0
    captured = capsys.readouterr()
    assert "TESTUL BLOCAT" in captured.err
    assert json.loads(out.read_text(encoding="utf-8"))["metrics"]["atp/2025"]["n"] == 30


def test_cli_passes_factory_params_and_writes_records(tmp_path, capsys):
    synthetic_season(tmp_path, 2024, events=4)
    records = tmp_path / "records.json"
    code = bm.main(
        [
            "--years",
            "2024",
            "--data-dir",
            str(tmp_path),
            "--no-cache",
            "--first-year",
            "2024",
            "--param",
            "threshold=0.6",
            "--param",
            "min_history=0",
            "--records",
            str(records),
        ]
    )
    assert code == 0
    assert "atp/2024" in capsys.readouterr().out
    saved = json.loads(records.read_text(encoding="utf-8"))
    assert len(saved) == 60 and {"p", "y", "first_key", "date"} <= set(saved[0])
