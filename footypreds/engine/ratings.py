"""Time-weighted attack/defence ratings (Maher/Dixon-Coles style) with Gamma shrinkage.

expected home goals = mu * home * attack[home] * defence[away]
expected away goals = mu * attack[away] * defence[home]

Each rating is the conjugate posterior mean under a Gamma(k, k) prior centred on 1, so a
team with few results stays close to average instead of swinging on two matches.
"""

import math
from dataclasses import dataclass, field

PRIOR_MU = 1.35
PRIOR_HOME = 1.2


@dataclass
class Ratings:
    mu: float = PRIOR_MU
    home: float = PRIOR_HOME
    attack: dict = field(default_factory=dict)
    defence: dict = field(default_factory=dict)
    weight: dict = field(default_factory=dict)
    matches: int = 0
    total_weight: float = 0.0

    def expected(self, home_key, away_key):
        return (
            self.mu * self.home * self.attack.get(home_key, 1.0) * self.defence.get(away_key, 1.0),
            self.mu * self.attack.get(away_key, 1.0) * self.defence.get(home_key, 1.0),
        )


def decay(age_days, half_life):
    return math.exp(-math.log(2) * max(0.0, age_days) / half_life)


def fit(rows, *, prior=4.0, iterations=40, init=None, tolerance=1e-6):
    """rows: iterable of (home_key, away_key, home_goals, away_goals, weight)."""
    rows = [r for r in rows if r[4] > 0]
    ratings = Ratings() if init is None else Ratings(init.mu, init.home)
    if init is not None:
        ratings.attack = dict(init.attack)
        ratings.defence = dict(init.defence)
    if not rows:
        return ratings
    attack, defence = ratings.attack, ratings.defence
    teams = {key for r in rows for key in r[:2]}
    for team in teams:
        attack.setdefault(team, 1.0)
        defence.setdefault(team, 1.0)
    total_weight = sum(r[4] for r in rows)
    goals = sum(r[4] * (r[2] + r[3]) for r in rows)
    home_goals = sum(r[4] * r[2] for r in rows)
    mu, home = ratings.mu, ratings.home
    for _ in range(iterations):
        previous = dict(attack)
        scored = dict.fromkeys(teams, prior)
        chances = dict.fromkeys(teams, prior)
        for h, a, hg, ag, w in rows:
            scored[h] += w * hg
            chances[h] += w * mu * home * defence[a]
            scored[a] += w * ag
            chances[a] += w * mu * defence[h]
        for team in teams:
            attack[team] = scored[team] / chances[team]
        conceded = dict.fromkeys(teams, prior)
        exposure = dict.fromkeys(teams, prior)
        for h, a, hg, ag, w in rows:
            conceded[h] += w * ag
            exposure[h] += w * mu * attack[a]
            conceded[a] += w * hg
            exposure[a] += w * mu * home * attack[h]
        for team in teams:
            defence[team] = conceded[team] / exposure[team]
        # Weak priors keep tiny samples sane without dominating a league season.
        base_home = sum(w * mu * attack[h] * defence[a] for h, a, _, _, w in rows)
        home = (home_goals + 10 * PRIOR_HOME) / (base_home + 10)
        base = sum(
            w * (home * attack[h] * defence[a] + attack[a] * defence[h]) for h, a, *_, w in rows
        )
        mu = (goals + 20 * PRIOR_MU) / (base + 20)
        if max(abs(attack[t] - previous[t]) for t in teams) < tolerance:
            break
    weight = dict.fromkeys(teams, 0.0)
    for h, a, *_, w in rows:
        weight[h] += w
        weight[a] += w
    ratings.mu, ratings.home, ratings.weight = mu, home, weight
    ratings.matches = len(rows)
    ratings.total_weight = total_weight
    return ratings
