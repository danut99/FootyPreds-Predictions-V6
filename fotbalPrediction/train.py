"""Antrenează (sau încarcă din cache) modelul de producție: ``python -m fotbalPrediction.train``.

Folosește toate ligile principale football-data din ``footypreds/data/fotbal/raw`` (de la 0506
până la sezonul curent 2627, parțial) și salvează pickle-ul în
``footypreds/data/fotbalPrediction/`` cu cheia VERSION + parametri + regulă + fișierele de date.
Datele se descarcă/actualizează cu ``python -m fotbalPrediction.data --download [--refresh]``.
"""

from __future__ import annotations

import sys
import time

from .model import MAX_P, THRESHOLD, THRESHOLD_HIGH, VERSION, FootballPredictor, load_rule


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    try:
        predictor = FootballPredictor.load_or_train()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    rule = load_rule()
    print(
        f"{VERSION}: {predictor.team_count} echipe în {predictor.league_count} ligi, date până la "
        f"{predictor.trained_through}, {time.perf_counter() - started:.0f} s. Regula: "
        f"{THRESHOLD:.2f} <= p <= {MAX_P:.2f} ({len(rule['select'])} piețe de goluri/pauză), "
        f"strictă {THRESHOLD_HIGH:.2f} ({len(rule['select_high'])})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
