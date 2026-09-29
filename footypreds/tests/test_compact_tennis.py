from datetime import date

from tenisPrediction.model import CompactTennisModel


def test_prediction_uses_only_previous_updates():
    model = CompactTennisModel()
    before = model.probability("A", "B", "Clay", 10, 100)
    model.update("A", "B", "Clay", date(2024, 1, 1), 10, 100)
    after = model.probability("A", "B", "Clay", 10, 100)
    assert before > 0.5
    assert after > before


def test_selector_abstains_without_history():
    model = CompactTennisModel()
    result = model.predict("A", "B", "Hard", 1, 500)
    assert result.winner == "A"
    assert result.decision == "fără pariu"


def test_high_confidence_after_enough_history():
    model = CompactTennisModel()
    for day in range(1, 7):
        model.update("A", "B", "Grass", date(2024, 1, day), 2, 200)
    result = model.predict("A", "B", "Grass", 2, 200, min_probability=0.6)
    assert result.decision == "selectează"
    assert result.experience_1 == 6


def test_flashscore_abbreviation_resolves_to_full_name():
    model = CompactTennisModel()
    for day in range(1, 4):
        model.update("Jannik Sinner", "Carlos Alcaraz", "Hard", date(2024, 1, day), 1, 2)
    assert model.resolve_player("Sinner J.") == "jannik sinner"
    assert model.resolve_player("Alcaraz C.") == "carlos alcaraz"


def test_rolling_serve_return_form_changes_probability_after_minimum_sample():
    model = CompactTennisModel(stats_scale=8)
    for day in range(1, 7):
        model.update(
            "Server", "Returner", "Hard", date(2024, 1, day), 50, 50,
            winner_serve=(70, 100), loser_serve=(45, 100),
        )
    with_stats = model.probability("Server", "Returner", "Hard", 50, 50)
    model.stats_scale = 0
    without_stats = model.probability("Server", "Returner", "Hard", 50, 50)
    assert with_stats > without_stats
