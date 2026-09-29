"""Rânduri football-data sintetice și un predictor mic pentru testele fotbalPrediction."""

import json
import random
from datetime import date, timedelta

from fotbalPrediction import data
from fotbalPrediction.model import FootballModel, FootballPredictor

E0_TEAMS = [
    "Arsenal",
    "Chelsea",
    "Liverpool",
    "Man United",
    "Man City",
    "Tottenham",
    "Everton",
    "Newcastle",
]
E1_TEAMS = ["Leeds", "Burnley", "Watford", "Norwich", "Stoke", "Hull", "Derby", "Wrexham"]


def synthetic_rows(start=date(2021, 8, 7), weeks=70, seed=5, leagues=("E0", "E1")):
    """Etape săptămânale cu goluri, pauză, cornere, cartonașe, șuturi și cote pre-meci."""
    rng = random.Random(seed)
    rows = []
    for league in leagues:
        teams = E0_TEAMS if league == "E0" else E1_TEAMS
        name, country, tier = data.MAIN_LEAGUES[league]
        for week in range(weeks):
            day = start + timedelta(days=7 * week)
            season = data.season_code(day.year if day.month >= 7 else day.year - 1)
            order = teams[:]
            rng.shuffle(order)
            for home, away in zip(order[::2], order[1::2]):
                edge = (teams.index(away) - teams.index(home)) * 0.12
                hg = max(0, int(rng.gauss(1.5 + edge, 1.1)))
                ag = max(0, int(rng.gauss(1.1 - edge, 1.0)))
                rows.append(
                    data.MatchRow(
                        league=league,
                        season=season,
                        date=day,
                        home=home,
                        away=away,
                        home_goals=hg,
                        away_goals=ag,
                        country=country,
                        tier=tier,
                        ht_home_goals=min(hg, rng.randint(0, 1)),
                        ht_away_goals=min(ag, rng.randint(0, 1)),
                        home_shots=rng.randint(6, 18),
                        away_shots=rng.randint(4, 15),
                        home_sot=rng.randint(1, 8),
                        away_sot=rng.randint(1, 7),
                        home_corners=rng.randint(2, 9),
                        away_corners=rng.randint(1, 7),
                        home_fouls=rng.randint(6, 15),
                        away_fouls=rng.randint(6, 15),
                        home_yellow=rng.randint(0, 3),
                        away_yellow=rng.randint(0, 3),
                        home_red=int(rng.random() < 0.08),
                        away_red=int(rng.random() < 0.08),
                        referee="M Dean",
                        odds={
                            "avg": {
                                "1": 2.0,
                                "X": 3.4,
                                "2": 3.8,
                                "over25": 1.9,
                                "under25": 1.95,
                            },
                            "avg_closing": {"1": 2.1, "X": 3.3, "2": 3.7},
                        },
                    )
                )
    rows.sort(key=data.sort_key)
    return rows


def write_rule(path, select=("1X", "X2", "over15", "under35", "home_over05"), high=("1X",)):
    """Regulă înghețată mică pentru teste (în locul selection_rule.json)."""
    path.write_text(
        json.dumps({"select": list(select), "select_high": list(high)}), encoding="utf-8"
    )
    return str(path)


def tiny_predictor(rows=None, **params):
    rows = synthetic_rows() if rows is None else rows
    model = FootballModel(**params)
    for row in rows:
        model.update(row)
    return FootballPredictor(model, rows, key="test")
