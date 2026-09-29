"""Antrenează (sau încarcă din cache) modelul v3: ``python -m tenisPrediction.train``.

Ca aplicația, folosește cotele de închidere tennis-data.co.uk din ``footypreds/data/benchmark/
tennis/raw`` (dacă există) și ponderile de producție ale amestecului model/piață."""

from __future__ import annotations

from .model import PRODUCTION_ODDS_WEIGHTS, VERSION, TennisPredictor


def main() -> None:
    predictor = TennisPredictor.load_or_train(odds="avg", odds_weights=PRODUCTION_ODDS_WEIGHTS)
    model = predictor.model
    thresholds = {
        tour: (
            round(model.display_threshold(tour), 4),
            round(model.display_threshold(tour, True), 4),
        )
        for tour in ("atp", "wta", "challenger")
    }
    print(
        f"{VERSION}: {predictor.player_count} jucători, date până la "
        f"{predictor.trained_through}, praguri de selecție (standard, precizie înaltă) {thresholds}"
    )


if __name__ == "__main__":
    main()
