"""tenisPrediction v3: symmetry, no look-ahead, selection rule, names and FlashScore facts."""

import random
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from footypreds.domain import Match
from tenisPrediction import benchmark as bm
from tenisPrediction.model import (
    SELECTED,
    TennisModel,
    TennisPredictor,
    match_facts,
    normalize_name,
    rolling_threshold,
)


def make_row(**fields):
    values = {name: None for name in bm.FIELDS}
    values.update(
        tourney_id="T",
        tourney_name="Test Open",
        surface="Hard",
        draw_size=32,
        tourney_level="A",
        indoor="O",
        best_of=3,
        round="R32",
        score="6-3 6-4",
        minutes=90,
        winner_hand="R",
        loser_hand="R",
        winner_age=25.0,
        loser_age=25.0,
        tour="atp",
        is_walkover=False,
    )
    values.update(fields)
    day = values.get("date") or date(2020, 1, 6)
    values["date"] = day
    values["tourney_date"] = day.strftime("%Y%m%d")
    if values["season"] is None:
        values["season"] = day.year
    for side in ("winner", "loser"):
        if values[f"{side}_key"] is None:
            values[f"{side}_key"] = values[f"{side}_id"]
    return bm.Row(tuple(values[name] for name in bm.FIELDS))


def synthetic_rows(seed=7, players=30, seasons=(2019, 2020, 2021), per_week=28, tour="atp"):
    """A small tour where a hidden strength decides results (plus serve stats)."""
    rng = random.Random(seed)
    strength = {f"P{i}": rng.gauss(0, 1.0) for i in range(players)}
    order = sorted(strength, key=strength.get, reverse=True)
    rank = {key: position + 1 for position, key in enumerate(order)}
    prefix = "wta:" if tour == "wta" else ""
    rows = []
    for season in seasons:
        start = date(season, 1, 6)
        for week in range(40):
            day = start + timedelta(days=7 * week)
            for number in range(per_week):
                a, b = rng.sample(sorted(strength), 2)
                p = 1 / (1 + np.exp(-(strength[a] - strength[b]) * 1.5))
                winner, loser = (a, b) if rng.random() < p else (b, a)
                rows.append(
                    make_row(
                        tour=tour,
                        season=season,
                        date=day,
                        tourney_id=f"{season}-{week}",
                        match_num=number,
                        winner_id=winner,
                        loser_id=loser,
                        winner_key=prefix + winner,
                        loser_key=prefix + loser,
                        winner_name=f"Player {winner}",
                        loser_name=f"Player {loser}",
                        winner_rank=rank[winner],
                        loser_rank=rank[loser],
                        w_svpt=70,
                        w_1stWon=35,
                        w_2ndWon=12,
                        l_svpt=70,
                        l_1stWon=30,
                        l_2ndWon=10,
                    )
                )
    return rows


def swap(ctx):
    values = ctx.as_dict()
    for name in list(values):
        if name.startswith("first_"):
            other = "second_" + name[len("first_") :]
            values[name], values[other] = values[other], values[name]
    return bm.MatchContext(**values)


def small_model(**params):
    params = {"fit_from": 2020, "train_from": 2019, "select_min": 20, **params}
    return TennisModel(**params)


def test_prediction_is_symmetric_and_pure():
    model = small_model()
    rows = synthetic_rows()
    for row in rows:
        model.update(row)
    assert model.coef is not None and model.fit_count >= 1
    players_before = len(model.engine.players)
    for row in rows[-50:]:
        ctx, _ = bm.make_context(row)
        p = model.predict(ctx)
        q = model.predict(swap(ctx))
        assert 0 < p < 1
        assert abs(p + q - 1) < 1e-12
    unknown = make_row(winner_id="NEW1", loser_id="NEW2", date=date(2021, 12, 20))
    model.predict(bm.make_context(unknown)[0])
    assert len(model.engine.players) == players_before


def test_future_results_never_reach_earlier_predictions():
    rows = synthetic_rows(seasons=(2019, 2020))
    cutoff = date(2020, 6, 1)
    flipped = []
    for row in rows:
        if row["date"] >= cutoff:
            data = dict(row)
            for name in ("id", "key", "name", "rank"):
                data[f"winner_{name}"], data[f"loser_{name}"] = (
                    data[f"loser_{name}"],
                    data[f"winner_{name}"],
                )
            row = bm.Row(tuple(data[name] for name in bm.FIELDS))
        flipped.append(row)

    def records(feed):
        result = bm.run_benchmark(
            small_model, [2020], ("atp",), 2019, rows=feed, return_records=True
        )
        return [r for r in result["records"] if r["ctx"].date < cutoff]

    clean, poisoned = records(rows), records(flipped)
    assert len(clean) == len(poisoned) > 100
    for a, b in zip(clean, poisoned):
        assert a["ctx"].first_key == b["ctx"].first_key
        assert a["p"] == b["p"]


def test_model_learns_the_hidden_strength():
    result = bm.run_benchmark(small_model, [2021], ("atp",), 2019, rows=synthetic_rows())
    metrics = result["metrics"]["atp/2021"]
    assert metrics["accuracy"] > 0.62
    assert metrics["log_loss"] < 0.66
    assert "select" in metrics


def test_rolling_threshold_takes_lowest_confidence_that_keeps_target():
    conf = np.array([0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6])
    hits = np.array([1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0])
    assert rolling_threshold(conf, hits, 0.8, 1) == 0.65  # 5 of 6 correct = 0.833
    assert rolling_threshold(conf, hits, 0.9, 1) == 0.75
    assert rolling_threshold(conf, hits, 0.8, 7) == 1.0  # needs 7: nothing reaches 80%
    ties = np.array([0.9, 0.7, 0.7])
    assert rolling_threshold(ties, np.array([1.0, 1.0, 0.0]), 0.8, 1) == 0.9


def test_select_threshold_follows_the_out_of_sample_window():
    model = small_model(select_window=200, select_step=50, select_min=10)
    assert model.threshold_for("atp") == 0.72  # fixed fallback without history
    rng = random.Random(3)
    for _ in range(200):
        z = rng.uniform(0.0, 3.0)
        confidence = 1 / (1 + np.exp(-z))
        # hits are more likely at high confidence: the threshold lands where accuracy ~ 82%
        model._track("atp", z if rng.random() < confidence else -z)
    threshold = model.threshold_for("atp")
    assert 0.6 <= threshold < 1.0
    window = model.windows["atp"]
    chosen = [hit for conf, hit in window if conf >= threshold]
    assert sum(chosen) / len(chosen) >= 0.82 - 1e-9
    assert model.threshold_for("quali") == model.threshold_for("challenger")
    fixed = small_model(select_mode="fixed")
    assert fixed.threshold_for("atp") == 0.72 and fixed.threshold_for("challenger") == 0.76


# --------------------------------------------------------------------------- names


NAMES = [
    ("A1", "Alex De Minaur", 8),
    ("A2", "Pablo Carreno Busta", 60),
    ("A3", "Alejandro Davidovich Fokina", 20),
    ("A4", "Giovanni Mpetshi Perricard", 35),
    ("A5", "Félix Auger-Aliassime", 25),
    ("A6", "Francisco Cerundolo", 21),
    ("A7", "Juan Manuel Cerundolo", 90),
    ("A8", "Christopher O'Connell", 80),
    ("A9", "Jannik Sinner", 1),
    ("A10", "Alexander Zverev", 3),
    ("A11", "Mischa Zverev", 900),
]


def named_predictor():
    model = TennisModel(fit_from=3000)
    day = date(2024, 1, 8)
    rows = []
    for week in range(12):
        for index, (key, name, rank) in enumerate(NAMES):
            opponent_key, opponent, opponent_rank = NAMES[(index + week + 1) % len(NAMES)]
            if opponent_key == key:
                continue
            winner = (
                (key, name, rank)
                if rank < opponent_rank
                else (opponent_key, opponent, opponent_rank)
            )
            loser = (
                (opponent_key, opponent, opponent_rank) if winner[0] == key else (key, name, rank)
            )
            rows.append(
                make_row(
                    date=day + timedelta(days=7 * week),
                    tourney_id=f"W{week}",
                    winner_id=winner[0],
                    winner_name=winner[1],
                    winner_rank=winner[2],
                    loser_id=loser[0],
                    loser_name=loser[1],
                    loser_rank=loser[2],
                )
            )
    # the same WTA player filed twice (missing id -> name key), plus two active namesakes
    for number, (winner_id, winner_name, loser_id, loser_name) in enumerate(
        [
            ("201662", "Karolina Pliskova", "201697", "Kristyna Pliskova"),
            (None, "Karolina Pliskova", "201697", "Kristyna Pliskova"),
            ("201697", "Kristyna Pliskova", "201662", "Karolina Pliskova"),
            ("216347", "Iga Swiatek", "201662", "Karolina Pliskova"),
            ("216347", "Iga Swiatek", "201697", "Kristyna Pliskova"),
            (None, "Iga Swiatek", "201697", "Kristyna Pliskova"),
        ]
    ):
        winner_key = f"wta:{winner_id}" if winner_id else f"wta:name:{winner_name.casefold()}"
        rows.append(
            make_row(
                tour="wta",
                date=date(2024, 3, 4) + timedelta(days=number),
                tourney_id="WTA1",
                match_num=number,
                winner_id=winner_id,
                winner_key=winner_key,
                winner_name=winner_name,
                winner_rank=40,
                loser_id=loser_id,
                loser_key=f"wta:{loser_id}",
                loser_name=loser_name,
                loser_rank=45,
            )
        )
    predictor = TennisPredictor(model)
    predictor.fit_rows(rows)
    return predictor


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("De Minaur A.", "A1"),
        ("Carreno Busta P.", "A2"),
        ("Carreno-Busta P.", "A2"),
        ("Davidovich Fokina A.", "A3"),
        ("Mpetshi Perricard G.", "A4"),
        ("Auger-Aliassime F.", "A5"),
        ("Auger Aliassime F.", "A5"),
        ("Cerundolo F.", "A6"),
        ("Cerundolo J. M.", "A7"),
        ("Cerundolo J.M.", "A7"),
        ("O'Connell C.", "A8"),
        ("OConnell C.", "A8"),
        ("Sinner J.", "A9"),
        ("Jannik Sinner", "A9"),
        ("jannik  SINNER", "A9"),
        ("Zverev A.", "A10"),
        ("Zverev M.", "A11"),
        ("Felix Auger Aliassime", "A5"),
    ],
)
def test_flashscore_names_resolve_to_player_keys(query, expected):
    assert named_predictor().resolve_player(query, "atp") == expected


def test_name_resolution_handles_duplicates_ambiguity_and_unknowns():
    predictor = named_predictor()
    # two keys of one player collapse to the most experienced one
    assert predictor.resolve_player("Pliskova Ka.", "wta") == "wta:201662"
    assert predictor.resolve_player("Swiatek I.", "wta") == "wta:216347"
    assert predictor.resolve_player("Pliskova Kr.", "wta") == "wta:201697"
    # two active players with the same surname, initial and a similar rank: no guess
    assert predictor.resolve_player("Pliskova K.", "wta") is None
    assert predictor.resolve_player("Nobody X.", "atp") is None
    # the tour group separates men and women
    assert predictor.resolve_player("Sinner J.", "wta") is None
    assert normalize_name("Félix  Auger-Aliassime") == "felix auger aliassime"


# a fixed prediction day: idle decay must not depend on the wall clock
DAY = date(2024, 4, 1)


def test_experience_and_selection_use_the_resolved_player():
    predictor = named_predictor()
    result = predictor.predict("Sinner J.", "Zverev M.", "Hard", tour="atp", when=DAY)
    assert result.key_1 == "A9" and result.key_2 == "A11"
    assert result.experience_1 > 0 and result.experience_2 > 0
    assert result.winner == "Sinner J."
    assert result.probability_1 > 0.8
    assert result.decision == SELECTED
    stricter = predictor.predict("Sinner J.", "Zverev M.", "Hard", min_probability=0.999, when=DAY)
    assert stricter.decision == "fără pariu"
    unknown = predictor.predict("Sinner J.", "Nobody X.", "Hard", when=DAY)
    assert unknown.decision == "fără pariu" and unknown.as_dict()["known"] is False
    unvalidated = predictor.predict("Sinner J.", "Zverev M.", "Hard", when=DAY, selectable=False)
    assert unvalidated.probability_1 == result.probability_1
    assert unvalidated.decision == "fără pariu"


def test_prediction_on_a_fixed_day_does_not_follow_the_wall_clock():
    predictor = named_predictor()
    first = predictor.probability("Sinner J.", "Zverev A.", "Hard", tour="atp", when=DAY)
    again = predictor.probability("Sinner J.", "Zverev A.", "Hard", tour="atp", when=DAY)
    assert first == again


def namesake_predictor():
    """Two real players named John Smith (different ids and ages), one player filed under two
    ids with the same birth date (Tom Brown), and a regular opponent."""
    rows = []
    players = [
        ("S1", "John Smith", 25.0, 60),
        ("S2", "John Smith", 31.0, 70),
        ("B1", "Tom Brown", 22.0, 90),
        ("B2", "Tom Brown", 22.0, 95),
    ]
    day = date(2024, 1, 8)
    for number, (key, name, age, rank) in enumerate(players * 3):
        rows.append(
            make_row(
                date=day,
                tourney_id="N1",
                match_num=number,
                winner_id=key,
                winner_name=name,
                winner_age=age,
                winner_rank=rank,
                loser_id="O1",
                loser_name="Other Opponent",
                loser_rank=500,
            )
        )
    rows.append(
        make_row(
            date=day,
            tourney_id="N1",
            match_num=99,
            winner_id="B1",
            winner_name="Tom Brown",
            winner_age=22.0,
            winner_rank=90,
            loser_id="O1",
            loser_name="Other Opponent",
            loser_rank=500,
        )
    )
    return TennisPredictor(TennisModel(fit_from=3000)).fit_rows(rows)


def test_real_namesakes_stay_ambiguous_but_duplicate_ids_collapse():
    predictor = namesake_predictor()
    # different ids and birth dates: two people, similar rank -> no guess
    assert predictor.resolve_player("Smith J.", "atp") is None
    assert predictor.resolve_player("John Smith", "atp") is None
    # same name and birth date under two ids: one player, the most experienced key
    assert predictor.resolve_player("Brown T.", "atp") == "B1"
    assert predictor.resolve_player("Tom Brown", "atp") == "B1"


def test_exact_key_lookup_respects_the_tour_group():
    predictor = named_predictor()
    assert predictor.resolve_player("A9", "atp") == "A9"
    assert predictor.resolve_player("A9", "wta") is None
    assert predictor.resolve_player("wta:216347", "wta") == "wta:216347"
    assert predictor.resolve_player("wta:216347", "atp") is None


def api_match(league, home="Sinner J.", away="Zverev M."):
    return Match(
        id="m1",
        kickoff=datetime(2024, 6, 1, 12, tzinfo=UTC),
        league=league,
        home=home,
        away=away,
        sport="tennis",
    )


@pytest.mark.parametrize(
    ("league", "surface", "tour", "best_of", "level"),
    [
        ("ATP - SINGLES: Madrid (Spain), clay", "Clay", "atp", 3, None),
        ("ATP - SINGLES: Wimbledon (United Kingdom), grass", "Grass", "atp", 5, "G"),
        ("WTA - SINGLES: Wimbledon (United Kingdom), grass", "Grass", "wta", 3, "G"),
        ("WTA - SINGLES: Wuhan (China), hard", "Hard", "wta", 3, None),
        ("ATP - SINGLES: Metz (France), hard (indoor)", "Hard", "atp", 3, None),
        ("CHALLENGER MEN - SINGLES: Genoa (Italy), clay", "Clay", "challenger", 3, None),
        ("ATP - SINGLES: Davis Cup, hard", "Hard", "atp", 3, "D"),
        ("ATP - SINGLES: Grass Court Open Hard Rock", "Grass", "atp", 3, None),
        ("ATP - SINGLES: Somewhere", "Hard", "atp", 3, None),
    ],
)
def test_match_facts_follow_the_flashscore_league(league, surface, tour, best_of, level):
    facts = match_facts(api_match(league))
    assert (facts["surface"], facts["tour"], facts["best_of"], facts["level"]) == (
        surface,
        tour,
        best_of,
        level,
    )


@pytest.mark.parametrize(
    ("league", "validated"),
    [
        ("ATP - SINGLES: Madrid (Spain), clay", True),
        ("WTA - SINGLES: Wuhan (China), hard", True),
        ("CHALLENGER MEN - SINGLES: Genoa (Italy), clay", True),
        ("ITF MEN - SINGLES: M25 Monastir (Tunisia), hard", False),
        ("ITF WOMEN - SINGLES: W35 Sharm (Egypt), hard", False),
        ("CHALLENGER WOMEN - SINGLES: Cali (Colombia), clay", False),
        ("WTA 125 - SINGLES: Parma (Italy), clay", False),
        ("ATP - DOUBLES: Madrid (Spain), clay", False),
    ],
)
def test_match_facts_flag_competitions_without_a_validated_rule(league, validated):
    assert match_facts(api_match(league))["validated"] is validated


def test_predict_api_match_uses_league_surface_tour_and_names():
    predictor = named_predictor()
    payload = predictor.predict_api_match(
        api_match("ATP - SINGLES: Roland Garros (France), clay", "De Minaur A.", "Zverev M.")
    )
    assert payload["match_id"] == "m1"
    assert payload["surface"] == "Clay"
    assert payload["best_of"] == 5 and payload["tour"] == "atp"
    assert payload["known"] is True and payload["experience_1"] > 0
    assert payload["decision"] in {"selectează", "fără pariu"}
    assert payload["validated"] is True


def test_predict_api_match_never_selects_on_unvalidated_competitions():
    predictor = named_predictor()
    validated = predictor.predict_api_match(api_match("ATP - SINGLES: Madrid (Spain), hard"))
    itf = predictor.predict_api_match(api_match("ITF MEN - SINGLES: M25 Monastir (Tunisia), hard"))
    assert validated["decision"] == SELECTED
    assert itf["validated"] is False and itf["decision"] == "fără pariu"
    assert itf["known"] is True


def tiny_data_dir(tmp_path):
    header = list(bm.CSV_COLUMNS)
    rows = synthetic_rows(players=12, seasons=(2022, 2023), per_week=6)
    path = tmp_path / "data"
    path.mkdir()
    for season in (2022, 2023):
        with (path / f"{season}.csv").open("w", encoding="utf-8", newline="") as handle:
            handle.write(",".join(header) + "\n")
            for row in rows:
                if row["season"] != season:
                    continue
                cells = []
                for name in header:
                    value = row[name]
                    cells.append("" if value is None else str(value))
                handle.write(",".join(cells) + "\n")
    return path


def test_trained_predictor_is_cached_and_invalidated_by_data_changes(tmp_path):
    data = tiny_data_dir(tmp_path)
    cache = tmp_path / "cache"
    first = TennisPredictor.load_or_train(data, cache, first_year=2022, fit_from=2023)
    assert (cache / "model_v3.pkl").exists()
    assert first.player_count == 12 and first.model.samples is None
    again = TennisPredictor.load_or_train(data, cache, first_year=2022, fit_from=2023)
    assert again is not first and again.signature == first.signature
    assert again.resolve_player("Player P1") == "P1"
    text = (data / "2023.csv").read_text(encoding="utf-8")
    (data / "2023.csv").write_text(text + text.splitlines()[-1] + "\n", encoding="utf-8")
    changed = TennisPredictor.load_or_train(data, cache, first_year=2022, fit_from=2023)
    assert changed.signature != first.signature
