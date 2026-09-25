import math
import random
from datetime import datetime, timedelta, timezone

from footypreds.domain import Match


def demo_data():
    """Seeded synthetic examples, never inserted into the real result ledger."""
    rng = random.Random(7)
    teams = [
        "Northbridge",
        "Port Albion",
        "Westford",
        "Riverside",
        "Oakwell",
        "Eastgate",
        "Kingsport",
        "Hillcrest",
        "Southbank",
        "Greenfield",
        "Lakewood",
        "Redhaven",
    ]
    now = datetime.now(timezone.utc).replace(hour=18, minute=0, second=0, microsecond=0)

    def goals(rate):
        product, count = 1.0, 0
        while product > math.exp(-rate):
            product *= rng.random()
            count += 1
        return count - 1

    history = []
    for week in range(45):
        order = list(range(12))
        rng.shuffle(order)
        for i in range(0, 12, 2):
            h, a = order[i : i + 2]
            history.append(
                Match(
                    id=f"demo-{week}-{i}",
                    kickoff=now - timedelta(days=(46 - week) * 7),
                    league="Demo League",
                    home=teams[h],
                    away=teams[a],
                    status="finished",
                    home_goals=goals(2.5 if h < 3 else 0.9),
                    away_goals=goals(1.7 if a < 3 else 0.6),
                    source="synthetic",
                )
            )
    fixtures = [
        Match(
            id=f"demo-next-{i}",
            kickoff=now + timedelta(days=1, hours=i),
            league="Demo League",
            home=teams[i],
            away=teams[11 - i],
            source="synthetic",
        )
        for i in range(6)
    ]
    return history, fixtures
