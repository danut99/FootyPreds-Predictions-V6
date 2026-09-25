"""Ladder (rollover) strategy: arithmetic, restarts, reinvest, voids, skipped days, blindness,
determinism and the API contract (docs/CONTRACTS.md §10.2, strategy "ladder")."""

from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from footypreds import simulator as sim
from footypreds.api import create_app
from footypreds.config import Settings
from footypreds.evaluation import sim_datasets as sd
from footypreds.tests.test_simulator import (
    SEASON,
    Result,
    bet,
    football_dataset,
    football_records,
    poisoned,
)
from footypreds.tests.test_simulator_datasets import write_benchmark

DAYS = [f"2025-01-0{n}" for n in range(1, 10)]


def plan_run(outcomes, *, bankroll=5, odds=2.0, **options):
    """Ladder over one single-leg ticket per day; outcomes: "W", "L", "V" (void) or None/"-"
    (a day without rows) or "0" (rows but no ticket)."""
    by_day, results, plan = {}, {}, {}
    days = DAYS[: len(outcomes)]
    for day, outcome in zip(days, outcomes, strict=True):
        if outcome in (None, "-"):
            continue
        by_day[day] = [day]
        if outcome == "0":
            plan[day] = []
            continue
        match_id = f"m{day}"
        plan[day] = [bet((match_id, "1", odds, 0.55))]
        results[match_id] = {
            "W": Result(2, 0),
            "L": Result(0, 1),
            "V": Result(0, 0, "unavailable"),
        }[outcome]
    return sim.run_ladder(
        days, by_day, results, lambda rows: plan[rows[0]], bankroll=bankroll, **options
    )


def test_all_in_ladder_doubles_until_a_loss_and_restarts_with_the_initial_amount():
    run = plan_run("WWWLW")
    days = run["days"]
    assert [d["stake"] for d in days] == [5, 10, 20, 40, 5]
    assert [d["bankroll_after"] for d in days] == [10, 20, 40, 0, 10]
    assert [d["result"] for d in days] == ["won", "won", "won", "lost", "won"]
    assert [d["ladder_index"] for d in days] == [1, 1, 1, 1, 2]
    assert [d["streak_day"] for d in days] == [1, 2, 3, 4, 1]
    ladder = run["ladder"]
    first, second = ladder["ladders"]
    assert (first["start"], first["end"], first["days"], first["peak"], first["final"]) == (
        DAYS[0],
        DAYS[3],
        3,
        40,
        0,
    )
    assert first["status"] == "lost" and second["status"] == "open"
    assert (second["start"], second["days"], second["final"]) == (DAYS[4], 1, 10)
    assert ladder["first_run_days"] == 3 and ladder["first_run_peak"] == 40
    assert ladder["longest_streak"] == 3 and ladder["longest_streak_peak"] == 40
    assert ladder["restarts"] == 1
    assert ladder["total_invested"] == 10 and ladder["total_returned"] == 10
    assert ladder["net"] == 0 and ladder["days_without_ticket"] == 0
    assert run["final"] == 5 and run["profit"] == 0 and run["staked"] == 80
    assert (run["won"], run["lost"], run["void"], run["bets"]) == (4, 1, 0, 5)
    assert run["longest_losing_streak"] == 1
    # The equity curve is the ladder bankroll; net counts every restart as money invested.
    assert [h["bankroll"] for h in run["history"]] == [10, 20, 40, 0, 10]
    assert [h["net"] for h in run["history"]] == [5, 15, 35, -5, 0]
    assert days[0]["ticket"]["legs"][0]["result"] == "won"
    assert days[0]["ticket"]["total_odds"] == 2.0


def test_ladder_without_restart_stops_at_the_first_loss():
    run = plan_run("WLWW", restart_on_loss=False)
    assert [d["result"] for d in run["days"]] == ["won", "lost"]
    ladder = run["ladder"]
    assert ladder["stopped"] == DAYS[1] and run["stopped"] == DAYS[1]
    assert len(ladder["ladders"]) == 1 and ladder["restarts"] == 0
    assert ladder["total_invested"] == 5 and ladder["net"] == -5 and run["final"] == 0


def test_first_ticket_lost_means_zero_days_survived():
    run = plan_run("LL")
    ladder = run["ladder"]
    assert [x["days"] for x in ladder["ladders"]] == [0, 0]
    assert ladder["first_run_days"] == 0 and ladder["first_run_peak"] == 5
    assert ladder["total_invested"] == 10 and ladder["net"] == -10 and run["final"] == -5
    assert run["longest_losing_streak"] == 2


def test_reinvest_half_keeps_the_rest_when_the_ladder_breaks():
    run = plan_run("WWLW", reinvest=0.5)
    days = run["days"]
    # 5 -> stake 2.5 -> 7.5 -> stake 3.75 -> 11.25 -> stake 5.62 lost -> 5.63 kept.
    assert [d["stake"] for d in days] == [2.5, 3.75, 5.62, 2.5]
    assert [d["bankroll_after"] for d in days] == [7.5, 11.25, 5.63, 7.5]
    first, second = run["ladder"]["ladders"]
    assert first["status"] == "lost" and first["final"] == 5.63 and first["peak"] == 11.25
    assert second["status"] == "open" and second["final"] == 7.5
    assert run["ladder"]["total_invested"] == 10
    assert run["ladder"]["total_returned"] == pytest.approx(13.13)
    assert run["ladder"]["net"] == pytest.approx(3.13) and run["final"] == pytest.approx(8.13)


def test_void_ticket_refunds_and_the_ladder_goes_on():
    run = plan_run("WVW")
    days = run["days"]
    assert [d["result"] for d in days] == ["won", "void", "won"]
    assert [d["bankroll_after"] for d in days] == [10, 10, 20]
    assert [d["payout"] for d in days] == [10, 10, 20]
    ladder = run["ladder"]["ladders"][0]
    assert ladder["days"] == 3 and ladder["void"] == 1 and ladder["won"] == 2
    assert run["void"] == 1 and run["hit_rate"] == 1.0


def test_partly_void_ticket_pays_the_decided_legs_only():
    by_day = {DAYS[0]: [0]}
    ticket = bet(("a", "1", 2.0, 0.6), ("b", "1", 1.5, 0.7))
    results = {"a": Result(1, 0), "b": Result(1, 1, "unavailable")}
    run = sim.run_ladder(DAYS[:1], by_day, results, lambda rows: [ticket], bankroll=5)
    day = run["days"][0]
    assert day["result"] == "won" and day["payout"] == 10 and day["bankroll_after"] == 10
    assert [leg["result"] for leg in day["ticket"]["legs"]] == ["won", "void"]


def test_days_without_a_ticket_are_skipped_counted_and_do_not_break_the_ladder():
    run = plan_run("W-0W")
    days = run["days"]
    assert [d["result"] for d in days] == ["won", "skipped", "skipped", "won"]
    assert days[1]["ticket"] is None and days[1]["stake"] == 0
    assert "Nu există meciuri" in days[1]["reason"] and "Niciun bilet" in days[2]["reason"]
    assert [d["bankroll_after"] for d in days] == [10, 10, 10, 20]
    assert [d["streak_day"] for d in days] == [1, 1, 1, 2]
    assert run["ladder"]["days_without_ticket"] == 2
    assert run["ladder"]["ladders"][0]["days"] == 2 and len(run["ladder"]["ladders"]) == 1


def test_a_day_with_only_grade_d_fixtures_says_why_it_was_skipped():
    rows = [{"grade": "D"}, {"grade": "D"}]
    run = sim.run_ladder(DAYS[:1], {DAYS[0]: rows}, {}, lambda rows: [], bankroll=5)
    assert "nota D" in run["days"][0]["reason"]


def test_skipped_day_after_a_loss_waits_for_the_next_ladder():
    run = plan_run("L-W")
    days = run["days"]
    assert days[1]["result"] == "skipped" and days[1]["bankroll_after"] == 0
    assert days[1]["ladder_index"] == 2 and days[1]["streak_day"] == 0
    assert days[2]["ladder_index"] == 2 and days[2]["stake"] == 5


def test_max_days_cashes_the_ladder_out_and_starts_again():
    run = plan_run("WWWL", max_days=2)
    ladder = run["ladder"]
    first, second = ladder["ladders"]
    assert first["status"] == "cashed" and first["final"] == 20 and first["days"] == 2
    assert second["status"] == "lost" and second["final"] == 0
    assert ladder["total_invested"] == 10 and ladder["total_returned"] == 20
    assert ladder["net"] == 10 and ladder["cashed_ladders"] == 1
    assert [d["stake"] for d in run["days"]] == [5, 10, 5, 10]


def test_choose_receives_rows_only_and_results_are_read_after_the_stake(monkeypatch):
    seen, order = [], []
    real = sim.settle_bet

    def spy(bet_, results):
        order.append("settle")
        return real(bet_, results)

    def choose(rows):
        seen.append(rows)
        order.append("choose")
        return [bet((f"m{rows[0]}", "1", 2.0, 0.5))]

    monkeypatch.setattr(sim, "settle_bet", spy)
    by_day = {d: [d] for d in DAYS[:3]}
    results = {f"m{d}": Result(1, 0) for d in DAYS[:3]}
    sim.run_ladder(DAYS[:3], by_day, results, choose, bankroll=5)
    assert seen == [[d] for d in DAYS[:3]]
    assert order == ["choose", "settle"] * 3


# --- full simulation on the synthetic benchmark ---------------------------------------------


def ladder(dataset, **kw):
    options = {
        "bankroll": 5,
        "strategy": "ladder",
        "target_odds": 2,
        "cache_dir": None,
        "workers": 1,
        "start": SEASON[0],
        "end": SEASON[1],
    }
    return sim.simulate(dataset, **(options | kw))


@pytest.fixture(scope="module")
def season_run():
    return ladder(football_dataset())


def test_full_ladder_output_shape(season_run):
    result = season_run
    assert result["mode"] == result["strategy"] == "ladder"
    assert result["reinvest"] == 1.0 and result["restart_on_loss"] is True
    assert result["target_odds"] == 2 and result["sports"] == ["football"]
    for key in (
        "first_run_days",
        "first_run_peak",
        "longest_streak",
        "longest_streak_peak",
        "ladders",
        "restarts",
        "total_invested",
        "total_returned",
        "net",
        "days_without_ticket",
    ):
        assert key in result["ladder"], key
    for item in result["ladder"]["ladders"]:
        assert set(item) >= {"start", "end", "days", "peak", "final", "status"}
        assert item["status"] in sim.LADDER_STATUSES
    assert result["days"] and result["days_count"] == len(result["days"])
    for day in result["days"]:
        assert set(day) >= {
            "date",
            "ticket",
            "stake",
            "result",
            "bankroll_after",
            "ladder_index",
            "streak_day",
        }
        assert day["result"] in ("won", "lost", "void", "skipped")
        if day["ticket"]:
            for leg in day["ticket"]["legs"]:
                assert set(leg) >= {
                    "home",
                    "away",
                    "home_logo",
                    "away_logo",
                    "league_logo",
                    "market",
                    "odds",
                    "probability",
                    "result",
                }
    for key in ("summary", "equity", "history", "warnings", "baseline", "method", "rules"):
        assert key in result
    assert result["baseline"]["ladder"]["total_invested"] >= 0
    assert result["rules"]["source"] == "recommend"
    assert result["bets"] == sum(d["result"] != "skipped" for d in result["days"])
    ladder_ = result["ladder"]
    assert ladder_["net"] == pytest.approx(ladder_["total_returned"] - ladder_["total_invested"])
    assert result["summary"]["final"] == pytest.approx(5 + ladder_["net"])
    assert len(ladder_["ladders"]) == ladder_["restarts"] + 1
    assert any("independente" in w for w in result["warnings"])
    assert any("Bani virtuali" in w for w in result["warnings"])


def test_ladder_arithmetic_holds_on_every_day(season_run):
    previous = None
    for day in season_run["days"]:
        if day["result"] == "skipped":
            continue
        if previous is not None and day["ladder_index"] == previous["ladder_index"]:
            assert day["bankroll_before"] == previous["bankroll_after"]
        else:
            assert day["bankroll_before"] == 5 and day["streak_day"] == 1
        expected = {"won": day["stake"] * day["odds"], "lost": 0.0, "void": day["stake"]}
        assert day["payout"] == pytest.approx(expected[day["result"]], abs=0.011)
        previous = day


def test_ladder_is_deterministic(season_run):
    again = ladder(football_dataset())
    for key in ("days", "ladder", "summary", "baseline", "history"):
        assert again[key] == season_run[key]


def test_poisoned_future_and_same_day_results_never_change_the_tickets(season_run):
    clean_days = {d["date"]: d for d in season_run["days"]}
    ticket_days = [d for d in clean_days if clean_days[d]["ticket"]]
    cut = date.fromisoformat(ticket_days[len(ticket_days) // 2])
    run = ladder(football_dataset(poisoned(football_records(), cut)))
    days = {d["date"]: d for d in run["days"]}

    def picks(day):
        ticket = day["ticket"]
        return [(x["match_id"], x["key"], x["odds"]) for x in ticket["legs"]] if ticket else None

    for key, day in clean_days.items():
        if key < cut.isoformat():
            # Everything before the poisoned day is identical, outcomes included.
            assert days[key] == day
    # On the poisoned day itself the ticket is the same: its results were still hidden.
    assert picks(days[cut.isoformat()]) == picks(clean_days[cut.isoformat()])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_odds": None}, "cota țintă"),
        ({"target_odds": 150}, "între 1.2 și 100"),
        ({"reinvest": 0}, "Partea reinvestită"),
        ({"reinvest": 1.5}, "Partea reinvestită"),
        ({"reinvest": float("nan")}, "număr"),
        ({"bankroll": 0.01, "reinvest": 0.5}, "sub 0.01"),
        ({"max_days": 0}, "Numărul maxim de zile"),
        ({"bankroll": -5}, "Suma inițială"),
        ({"start": SEASON[1], "end": SEASON[0]}, "Data de început"),
    ],
)
def test_invalid_ladders_raise_romanian_errors(kwargs, message):
    with pytest.raises(sim.SimulationError, match=message):
        ladder(football_dataset(), **kwargs)


def test_last_days_window_ends_on_the_datasets_last_day():
    result = ladder(football_dataset(), start=None, end=None, last_days=30)
    assert result["end"] == SEASON[1].isoformat()
    assert result["start"] == (SEASON[1] - timedelta(days=29)).isoformat()


# --- API ------------------------------------------------------------------------------------


def offline(request):
    return httpx.Response(500, json={"message": "offline"})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "_MEMO", {})
    app = create_app(
        Settings(api_key="", database=tmp_path / "sim.db"), httpx.MockTransport(offline)
    )
    app.state.sim_benchmark_dir = write_benchmark(tmp_path / "bench", football_records())
    app.state.sim_cache_dir = tmp_path / "cache"
    app.state.sim_workers = 1
    with TestClient(app) as test_client:
        yield test_client


BODY = {
    "dataset": "football",
    "strategy": "ladder",
    "bankroll": 5,
    "target_odds": 2,
    "start": "2024-08-03",
    "end": "2025-05-03",
}


def test_ladder_via_api_matches_the_contract(client):
    response = client.post("/api/simulate", json=BODY)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["strategy"] == "ladder" and isinstance(data["days"], list)
    assert data["ladder"]["total_invested"] >= 5
    assert data["summary"]["start"] == 5 and data["equity"] == data["history"]
    assert data["disclaimer"] and data["warnings"]
    half = client.post("/api/simulate", json=BODY | {"reinvest": 0.5, "max_days": 3}).json()
    assert half["reinvest"] == 0.5 and half["max_days"] == 3
    assert all(x["days"] <= 3 for x in half["ladder"]["ladders"])
    alone = client.post("/api/simulate", json=BODY | {"restart_on_loss": False}).json()
    assert len(alone["ladder"]["ladders"]) == 1


@pytest.mark.parametrize(
    ("body", "text"),
    [
        (BODY | {"reinvest": 2}, "Partea reinvestită"),
        (BODY | {"target_odds": None}, "cota țintă"),
        (BODY | {"max_days": 1000}, "Numărul maxim de zile"),
        (BODY | {"reinvest": "tot"}, "Parametri invalizi"),
        (BODY | {"sports": ["golf"]}, "Parametri invalizi"),
    ],
)
def test_invalid_ladder_requests_are_422(client, body, text):
    response = client.post("/api/simulate", json=body)
    assert response.status_code == 422, response.text
    assert text in response.json()["detail"]


def test_wallet_legs_stored_without_logos_get_display_urls(tmp_path):
    from datetime import datetime, timezone

    from footypreds import wallet
    from footypreds.domain import Match
    from footypreds.store import Store

    store = Store(tmp_path / "wallet.db")
    crest = "https://static.flashscore.com/res/image/data/crest-1.png"
    store.save_matches(
        [
            Match(
                id="w1",
                kickoff=datetime(2030, 1, 1, tzinfo=timezone.utc),
                league="L",
                home="Home",
                away="Away",
                odds={"1": 2.0, "X": 3.0, "2": 4.0},
                home_logo=crest,
            )
        ]
    )
    wallet.deposit(store, 10)
    old_leg = {"match_id": "w1", "sport": "football", "home": "Home", "away": "Away"}
    old_leg |= {"key": "1", "label": "1", "odds": 2.0, "probability": 0.5, "status": "pending"}
    wallet.place_bet(store, 5, [old_leg], "vechi")
    leg = wallet.wallet(store)["bets"][0]["legs"][0]
    assert (
        leg["home_logo"]
        == "/api/img?u=https%3A%2F%2Fstatic.flashscore.com%2Fres%2Fimage%2Fdata%2Fcrest-1.png"
    )
    assert leg["away_logo"] is None and leg["league_logo"] is None
