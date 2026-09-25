"""SQLite store, match cache and the prospective ledger under adversarial use."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from footypreds.api import AnalysisCache
from footypreds.domain import Match
from footypreds.engine import analyze, outcome
from footypreds.engine.backtest import compact
from footypreds.store import Store
from footypreds.tests.helpers import KICKOFF, fixture, result, strong_history

BEFORE = KICKOFF - timedelta(days=1)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "nested" / "dir" / "ledger.db")


def ledger_prediction(match=None, history=None):
    return compact(analyze(match or fixture(), strong_history() if history is None else history))


def finished(match, home, away, **changes):
    return match.model_copy(
        update={"status": "finished", "home_goals": home, "away_goals": away} | changes
    )


# --- snapshots ----------------------------------------------------------------------------


def test_snapshot_is_written_once_and_never_rewritten(store):
    match = fixture()
    first = ledger_prediction()
    assert first["selection"] is not None
    assert store.snapshot(match, first, BEFORE)
    changed = first | {"threshold": 0.5, "selection": first["selection"] | {"key": "2"}}
    later = BEFORE + timedelta(hours=12)
    assert not store.snapshot(match, changed, later)
    assert not store.snapshot(match.model_copy(update={"odds": {"1": 9.0}}), changed, later)
    (row,) = store.predictions()
    assert row["prediction"] == first and row["created"] == BEFORE.timestamp()
    assert store.assessment_count() == 1


@pytest.mark.parametrize(
    "changes,now",
    [
        ({"status": "live"}, BEFORE),
        ({"status": "unavailable"}, BEFORE),
        ({"status": "finished", "home_goals": 1, "away_goals": 0}, BEFORE),
        ({"source": "synthetic"}, BEFORE),
        ({"source": "import"}, BEFORE),
        ({}, KICKOFF),
        ({}, KICKOFF + timedelta(seconds=1)),
        ({}, KICKOFF + timedelta(days=3)),
    ],
)
def test_only_pre_kickoff_scheduled_flashscore_matches_enter_the_ledger(store, changes, now):
    match = fixture().model_copy(update=changes)
    assert not store.snapshot(match, ledger_prediction(), now)
    assert store.predictions() == [] and store.assessment_count() == 0


def test_one_second_before_kickoff_is_still_pre_match(store):
    assert store.snapshot(fixture(), ledger_prediction(), KICKOFF - timedelta(seconds=1))


def test_abstention_counts_as_assessment_but_not_as_prediction(store):
    abstain = ledger_prediction(history=[])
    assert abstain["selection"] is None
    assert not store.snapshot(fixture(), abstain, BEFORE)
    assert not store.snapshot(fixture(), abstain, BEFORE)
    assert store.assessment_count() == 1 and store.predictions() == []


def test_concurrent_snapshots_of_one_match_record_exactly_one(store):
    prediction = ledger_prediction()
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(
            pool.map(lambda _: store.snapshot(fixture(), prediction, BEFORE), range(16))
        )
    assert outcomes.count(True) == 1
    assert len(store.predictions()) == 1 and store.assessment_count() == 1


# --- settlement ---------------------------------------------------------------------------


@pytest.mark.parametrize("score", [(4, 0), (0, 0), (1, 2), (3, 3), (0, 5)])
def test_settlement_uses_the_final_score_once(store, score):
    match = fixture()
    prediction = ledger_prediction()
    store.snapshot(match, prediction, BEFORE)
    final = finished(match, *score)
    assert store.settle([final]) == 1
    assert store.settle([final, final]) == 0
    # A later correction of the score does not rewrite a settled ledger row.
    assert store.settle([finished(match, 9, 9)]) == 0
    (row,) = store.predictions()
    assert row["result"] == {
        "won": outcome(prediction["selection"]["key"], *score),
        "score": f"{score[0]}-{score[1]}",
    }


@pytest.mark.parametrize(
    "changes",
    [{"status": "scheduled"}, {"status": "live"}, {"status": "unavailable"}, {"source": "import"}],
)
def test_only_finished_flashscore_rows_settle(store, changes):
    match = fixture()
    store.snapshot(match, ledger_prediction(), BEFORE)
    candidate = finished(match, 1, 0).model_copy(update=changes)
    assert store.settle([candidate]) == 0
    assert store.predictions()[0]["result"] is None


def test_a_result_older_than_the_prediction_never_settles_it(store):
    match = fixture()
    store.snapshot(match, ledger_prediction(), BEFORE)
    early = finished(match, 2, 0, kickoff=BEFORE - timedelta(minutes=1))
    assert store.settle([early]) == 0
    assert store.settle([finished(match, 2, 0)]) == 1


def test_settle_ignores_unknown_matches_and_empty_input(store):
    assert store.settle([]) == 0
    assert store.settle([finished(fixture(id="other"), 1, 1)]) == 0


# --- save_matches merge rules -------------------------------------------------------------


def test_finished_results_are_never_downgraded(store):
    match = fixture(home_id="h", away_id="a", country="England", odds={"1": 1.5})
    store.save_matches([finished(match, 2, 1)])
    for status in ("scheduled", "live", "unavailable"):
        store.save_matches([match.model_copy(update={"status": status})])
        stored = store.match(match.id)
        assert stored.status == "finished" and (stored.home_goals, stored.away_goals) == (2, 1)
    store.save_matches([finished(match, 3, 1)])  # a corrected final score is accepted
    assert store.match(match.id).home_goals == 3


def test_sparse_rows_keep_ids_odds_and_country(store):
    rich = fixture(home_id="h", away_id="a", country="England", odds={"1": 1.5, "X": 4.0})
    store.save_matches([rich])
    sparse = finished(fixture(), 1, 0)  # H2H rows carry no IDs, odds or country
    store.save_matches([sparse])
    stored = store.match(rich.id)
    assert (stored.home_id, stored.away_id, stored.country) == ("h", "a", "England")
    assert stored.odds == rich.odds and stored.status == "finished"
    fresh_odds = fixture(odds={"1": 1.7})
    store.save_matches([fresh_odds])  # a scheduled row may not downgrade the finished one
    assert store.match(rich.id).odds == rich.odds


def test_new_odds_replace_old_odds_before_kickoff(store):
    store.save_matches([fixture(odds={"1": 1.5, "X": 4.0, "2": 6.0})])
    store.save_matches([fixture(odds={"1": 1.6, "X": 3.9, "2": 5.5})])
    assert store.match("fixture").odds == {"1": 1.6, "X": 3.9, "2": 5.5}


def test_save_matches_accepts_generators_and_empty_input(store):
    version = store.version
    store.save_matches([])
    store.save_matches(m for m in [])
    assert store.version == version
    store.save_matches(m for m in strong_history())
    assert len(store.matches()) == 25 and store.version > version


def test_unchanged_rows_do_not_invalidate_the_cache(store):
    store.save_matches(strong_history())
    version = store.version
    store.save_matches(strong_history())
    assert store.version == version


# --- matches() cache ----------------------------------------------------------------------


def test_mutating_the_returned_list_does_not_corrupt_the_cache(store):
    store.save_matches(strong_history())
    first = store.matches()
    first.clear()
    first.append("garbage")
    assert len(store.matches()) == 25


def test_mutating_a_returned_match_does_not_corrupt_later_reads(store):
    # Store.matches() shares its cached Match objects with every caller (and with the
    # HistoryIndex), so Match is immutable: an in-place edit is refused instead of
    # silently changing every later read.
    store.save_matches(strong_history())
    with pytest.raises(ValidationError):
        store.matches()[0].home_goals = 49
    assert store.matches()[0].home_goals == 4
    edited = store.matches()[0].model_copy(update={"home_goals": 49})
    assert edited.home_goals == 49 and store.matches()[0].home_goals == 4


def test_matches_are_sorted_by_kickoff_and_match_on_filters_by_utc_day(store):
    rows = [
        result("a", 0, "A", "B", 1, 0).model_copy(
            update={"kickoff": datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)}
        ),
        result("b", 0, "C", "D", 1, 0).model_copy(
            update={"kickoff": datetime(2026, 3, 1, 23, 59, 59, tzinfo=timezone.utc)}
        ),
        result("c", 0, "E", "F", 1, 0).model_copy(
            update={"kickoff": datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)}
        ),
        # 01:30 in Bucharest on March 2nd is still March 1st in UTC.
        result("d", 0, "G", "H", 1, 0).model_copy(
            update={"kickoff": datetime(2026, 3, 2, 1, 30, tzinfo=timezone(timedelta(hours=2)))}
        ),
    ]
    store.save_matches(reversed(rows))
    assert [m.id for m in store.matches()] == ["a", "d", "b", "c"]
    assert {m.id for m in store.matches_on(date(2026, 3, 1))} == {"a", "b", "d"}
    assert [m.id for m in store.matches_on(date(2026, 3, 2))] == ["c"]


def gated_writer(store):
    """Pause the writer thread between its INSERT and COMMIT, controlled by events."""
    original = store.connect
    writing, proceed = threading.Event(), threading.Event()
    writer = {}

    @contextmanager
    def connect():
        with original() as db:
            yield db
            if threading.current_thread() is writer.get("thread"):
                writing.set()
                assert proceed.wait(10)

    store.connect = connect
    return writer, writing, proceed


def test_a_reader_during_an_uncommitted_write_never_caches_stale_rows(store):
    store.save_matches(strong_history()[:3])
    assert len(store.matches()) == 3
    writer, writing, proceed = gated_writer(store)
    thread = threading.Thread(target=store.save_matches, args=(strong_history()[3:6],))
    writer["thread"] = thread
    thread.start()
    assert writing.wait(10)
    assert len(store.matches()) == 3  # uncommitted rows are invisible, fine
    proceed.set()
    thread.join(10)
    assert len(store.matches()) == 6, "a stale list was cached under the new version"


def test_analysis_cache_never_serves_analyses_built_on_pre_write_history(store):
    cache = AnalysisCache(store)
    match = fixture()
    assert cache.get(match)["sample"]["home"] == 0
    writer, writing, proceed = gated_writer(store)
    thread = threading.Thread(target=store.save_matches, args=(strong_history(),))
    writer["thread"] = thread
    thread.start()
    assert writing.wait(10)
    cache.get(match)
    proceed.set()
    thread.join(10)
    assert cache.get(match)["sample"]["home"] == 25


def test_concurrent_writers_and_readers(store):
    errors = []

    def write(worker):
        try:
            for n in range(15):
                store.save_matches(
                    [result(f"w{worker}-{n}", 1 + n, f"H{worker}", f"A{n}", n % 4, worker % 3)]
                )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def read(_):
        try:
            for _ in range(15):
                rows = store.matches()
                assert len({m.id for m in rows}) == len(rows)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with ThreadPoolExecutor(12) as pool:
        list(pool.map(write, range(6)))
        list(pool.map(read, range(6)))
        futures = [pool.submit(write, w) for w in range(6, 10)]
        futures += [pool.submit(read, r) for r in range(6)]
        for future in futures:
            future.result()
    assert errors == []
    assert len(store.matches()) == 10 * 15
    assert len({m.id for m in store.matches()}) == 150


# --- cache, plans, synced days ------------------------------------------------------------


def test_json_cache_roundtrip_overwrite_and_expiry(store):
    payload = [{"name": "Ölympiakos ⚽", "n": 1, "none": None}]
    store.put_cache("k", payload, 60)
    assert store.get_cache("k") == payload
    store.put_cache("k", {"v": 2}, 60)
    assert store.get_cache("k") == {"v": 2}
    store.put_cache("gone", [1], -1)
    assert store.get_cache("gone") is None
    assert store.get_cache("missing") is None


def test_plans_are_listed_newest_first_and_capped(store):
    for n in range(35):
        store.save_plan({"id": f"p{n}", "created": n, "status": "ready", "days": []})
    listed = store.plans()
    assert len(listed) == 30 and listed[0]["id"] == "p34"
    assert store.plan("p0")["id"] == "p0" and store.plan("nope") is None
    store.save_plan({"id": "p0", "created": 0, "status": "failed", "days": []})
    assert store.plan("p0")["status"] == "failed"


def test_synced_days_are_idempotent(store):
    day = date(2026, 3, 1)
    store.mark_synced(day, 10)
    store.mark_synced(day, 12)
    assert store.synced_days() == {"2026-03-01"}


def test_store_survives_reopening(tmp_path):
    path = tmp_path / "reopen.db"
    first = Store(path)
    first.save_matches(strong_history())
    first.snapshot(fixture(), ledger_prediction(), BEFORE)
    second = Store(path)
    assert len(second.matches()) == 25 and len(second.predictions()) == 1
    assert isinstance(second.match("past-0"), Match)
