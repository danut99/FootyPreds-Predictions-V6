"""Seeded pseudo-random generators for property tests (no hypothesis dependency)."""

import math
from datetime import timedelta

from footypreds.domain import Match
from footypreds.tests.helpers import KICKOFF


def poisson_sample(rng, rate):
    """Knuth's algorithm; fine for the small rates used in football."""
    limit, product, count = math.exp(-rate), 1.0, 0
    while True:
        product *= rng.random()
        if product <= limit:
            return count
        count += 1


def random_target(rng, floor=0.01):
    values = [floor + rng.random() for _ in range(3)]
    total = sum(values)
    return {k: v / total for k, v in zip(("1", "X", "2"), values)}


def random_league(
    rng,
    teams=None,
    matches=120,
    days=500,
    league="Test",
    prefix="r",
    reference=KICKOFF,
    min_age_days=1.0,
):
    """Finished matches between random pairs, all strictly older than `reference - min_age`."""
    teams = teams or [f"Team{i}" for i in range(rng.randint(4, 10))]
    strength = {t: (0.5 + rng.random() * 1.5, 0.5 + rng.random() * 1.5) for t in teams}
    rows = []
    for n in range(matches):
        home, away = rng.sample(teams, 2)
        age = min_age_days + rng.random() * days
        rows.append(
            Match(
                id=f"{prefix}{n}",
                kickoff=reference - timedelta(days=age),
                league=league,
                home=home,
                away=away,
                status="finished",
                home_goals=min(
                    15, poisson_sample(rng, 1.2 * strength[home][0] / strength[away][1])
                ),
                away_goals=min(15, poisson_sample(rng, strength[away][0] / strength[home][1])),
            )
        )
    return rows


def random_odds(rng):
    """A plausible bookmaker 1X2 book with a 2-12% margin."""
    target = random_target(rng, floor=0.05)
    margin = 1.02 + rng.random() * 0.1
    return {k: max(1.01, round(1 / (p * margin), 3)) for k, p in target.items()}
