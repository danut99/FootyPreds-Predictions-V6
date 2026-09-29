"""Baseline football candidate: per-league time-decayed Dixon-Coles + league-average counts.

Goals: one Maher/Dixon-Coles attack/defence fit per league, refitted once per match date on
the rows of the previous ``max_days`` (exponential decay ``half_life``, Gamma(k, k) shrinkage
``prior``; the same conjugate updates as ``footypreds.engine.ratings.fit``, vectorised with
numpy). The score matrix (``rho`` low-score dependence) prices every goal market, AH quarter
lines included. With ``ctx.odds`` the 1X2 is pooled with the margin-free market 1X2
(``market_weight``) and P(over 2.5) with the over/under price (``totals_market_weight``),
exactly like the core analyzer (``blend`` + ``reweight`` + ``fit_total``).

Half-time: the league's decayed first-half share of goals (per side) scales the full-time
rates; independent Poisson.

Corners, cards, booking points and shots on target: per league and side, decayed mean and
variance of the count (negative binomial when over-dispersed). With ``team_counts=True`` the
means come from a per-league team fit of that count instead (same fit as goals).

Default parameters are NOT tuned on the protocol seasons: the goal parameters are the V8
engine defaults (which were tuned on 2425 under the old protocol); the market weights are
round, untuned values. Re-tune on 2223/2324 only.
"""

from __future__ import annotations

import math
from bisect import bisect_left

import numpy as np

from fotbalPrediction import markets as mk

PRIOR_MU = 1.35
PRIOR_HOME = 1.2
COUNT_STATS = ("corners", "cards", "bookings", "sot")


def fit_ratings(
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    weight: np.ndarray,
    n_teams: int,
    *,
    prior: float,
    prior_mu: float = PRIOR_MU,
    prior_home: float = PRIOR_HOME,
    iterations: int = 40,
    init: tuple | None = None,
    tolerance: float = 1e-6,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """(attack, defence, mu, home) with the conjugate updates of footypreds.engine.ratings."""
    if init is not None and len(init[0]) >= n_teams:
        attack, defence = init[0][:n_teams].copy(), init[1][:n_teams].copy()
        mu, home = init[2], init[3]
    else:
        attack, defence = np.ones(n_teams), np.ones(n_teams)
        if init is not None:
            attack[: len(init[0])] = init[0]
            defence[: len(init[1])] = init[1]
            mu, home = init[2], init[3]
        else:
            mu, home = prior_mu, prior_home
    if len(weight) == 0:
        return attack, defence, mu, home
    w = weight
    goals = float((w * (home_goals + away_goals)).sum())
    total_home = float((w * home_goals).sum())
    scored = np.bincount(home_idx, w * home_goals, n_teams) + np.bincount(
        away_idx, w * away_goals, n_teams
    )
    conceded = np.bincount(home_idx, w * away_goals, n_teams) + np.bincount(
        away_idx, w * home_goals, n_teams
    )
    for _ in range(iterations):
        previous = attack
        chances = np.bincount(home_idx, w * mu * home * defence[away_idx], n_teams) + np.bincount(
            away_idx, w * mu * defence[home_idx], n_teams
        )
        attack = (scored + prior) / (chances + prior)
        exposure = np.bincount(home_idx, w * mu * attack[away_idx], n_teams) + np.bincount(
            away_idx, w * mu * home * attack[home_idx], n_teams
        )
        defence = (conceded + prior) / (exposure + prior)
        base_home = float((w * mu * attack[home_idx] * defence[away_idx]).sum())
        home = (total_home + 10 * prior_home) / (base_home + 10)
        base = float(
            (
                w
                * (
                    home * attack[home_idx] * defence[away_idx]
                    + attack[away_idx] * defence[home_idx]
                )
            ).sum()
        )
        mu = (goals + 20 * prior_mu) / (base + 20)
        if np.max(np.abs(attack - previous)) < tolerance:
            break
    return attack, defence, mu, home


class Moments:
    """Exponentially decayed mean/variance of a count (half-life in days)."""

    __slots__ = ("half_life", "s0", "s1", "s2", "day", "n")

    def __init__(self, half_life: float):
        self.half_life = half_life
        self.s0 = self.s1 = self.s2 = 0.0
        self.day = None
        self.n = 0

    def add(self, day: int, value: float) -> None:
        if self.day is not None and day > self.day:
            factor = 0.5 ** ((day - self.day) / self.half_life)
            self.s0 *= factor
            self.s1 *= factor
            self.s2 *= factor
        if self.day is None or day > self.day:
            self.day = day
        self.s0 += 1.0
        self.s1 += value
        self.s2 += value * value
        self.n += 1

    def mean_var(self) -> tuple[float, float] | None:
        if self.s0 <= 0:
            return None
        mean = self.s1 / self.s0
        var = max(self.s2 / self.s0 - mean * mean, 0.0)
        return mean, var


class League:
    def __init__(self, count_half_life: float):
        self.teams: dict[str, int] = {}
        self.day: list[int] = []
        self.home: list[int] = []
        self.away: list[int] = []
        self.values: dict[str, tuple[list, list]] = {
            stat: ([], []) for stat in ("goals",) + COUNT_STATS
        }
        self.fit_day: dict[str, int] = {}
        self.fits: dict[str, tuple] = {}
        self.played: dict[int, list[int]] = {}
        self.moments = {
            (stat, side): Moments(count_half_life)
            for stat in COUNT_STATS
            for side in ("home", "away")
        }
        self.ht_first = {"home": Moments(count_half_life), "away": Moments(count_half_life)}
        self.ht_full = {"home": Moments(count_half_life), "away": Moments(count_half_life)}

    def team(self, name: str) -> int:
        if name not in self.teams:
            self.teams[name] = len(self.teams)
        return self.teams[name]


class BaselineModel:
    def __init__(
        self,
        half_life: float = 540.0,
        max_days: int = 730,
        prior: float = 8.0,
        rho: float = -0.12,
        market_weight: float = 0.8,
        totals_market_weight: float = 0.8,
        count_half_life: float = 365.0,
        count_prior: float = 8.0,
        team_counts: bool = False,
        threshold: float = 0.80,
        threshold_high: float = 0.85,
        min_team_matches: int = 5,
        min_league_matches: int = 60,
    ):
        self.half_life = half_life
        self.max_days = max_days
        self.prior = prior
        self.rho = rho
        self.market_weight = market_weight
        self.totals_market_weight = totals_market_weight
        self.count_half_life = count_half_life
        self.count_prior = count_prior
        self.team_counts = team_counts
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.min_team_matches = min_team_matches
        self.min_league_matches = min_league_matches
        self.leagues: dict[str, League] = {}
        self._sample: dict[str, tuple[int, int, int]] = {}

    # ------------------------------------------------------------------ fitting

    def _league(self, code: str) -> League:
        if code not in self.leagues:
            self.leagues[code] = League(self.count_half_life)
        return self.leagues[code]

    def _fit(self, league: League, stat: str, today: int, prior_mu: float, prior_home: float):
        if league.fit_day.get(stat) == today:
            return league.fits.get(stat)
        league.fit_day[stat] = today
        days = np.asarray(league.day, dtype=np.int64)
        home_values, away_values = league.values[stat]
        hv = np.asarray(home_values, dtype=float)
        av = np.asarray(away_values, dtype=float)
        start = int(np.searchsorted(days, today - self.max_days, side="left"))
        keep = np.arange(start, len(days))
        keep = keep[~(np.isnan(hv[keep]) | np.isnan(av[keep]))] if len(keep) else keep
        if len(keep) == 0:
            league.fits[stat] = None
            return None
        age = (today - days[keep]).astype(float)
        weight = np.exp(-math.log(2) * age / self.half_life)
        home_idx = np.asarray(league.home, dtype=np.int64)[keep]
        away_idx = np.asarray(league.away, dtype=np.int64)[keep]
        previous = league.fits.get(stat)
        fit = fit_ratings(
            home_idx,
            away_idx,
            hv[keep],
            av[keep],
            weight,
            len(league.teams),
            prior=self.prior if stat == "goals" else self.count_prior,
            prior_mu=prior_mu,
            prior_home=prior_home,
            iterations=12 if previous is not None else 40,
            init=previous,
        )
        league.fits[stat] = fit
        return fit

    @staticmethod
    def _expected(fit, home: int | None, away: int | None) -> tuple[float, float]:
        attack, defence, mu, home_adv = fit
        a_h = attack[home] if home is not None and home < len(attack) else 1.0
        d_h = defence[home] if home is not None and home < len(defence) else 1.0
        a_a = attack[away] if away is not None and away < len(attack) else 1.0
        d_a = defence[away] if away is not None and away < len(defence) else 1.0
        return mu * home_adv * a_h * d_a, mu * a_a * d_h

    # ------------------------------------------------------------------ protocol

    def predict(self, ctx) -> dict[str, float]:
        from footypreds.engine import markets as core
        from footypreds.engine.analyzer import blend, market_probabilities

        league = self._league(ctx.league)
        today = ctx.date.toordinal()
        home = league.teams.get(ctx.home)
        away = league.teams.get(ctx.away)
        output: dict[str, float] = {}
        fit = self._fit(league, "goals", today, PRIOR_MU, PRIOR_HOME)
        recent = today - self.max_days
        days_home = league.played.get(home, [])
        days_away = league.played.get(away, [])
        played_home = len(days_home) - bisect_left(days_home, recent)
        played_away = len(days_away) - bisect_left(days_away, recent)
        self._sample[ctx.match_id] = (played_home, played_away, len(league.day))
        if fit is None:
            return output
        home_rate, away_rate = self._expected(fit, home, away)
        home_rate = min(5.0, max(0.15, home_rate))
        away_rate = min(5.0, max(0.15, away_rate))
        matrix = core.score_matrix(home_rate, away_rate, self.rho)
        odds = ctx.odds or {}
        market = market_probabilities(odds) if odds else None
        final = core.one_x_two(matrix)
        if market and self.market_weight > 0:
            final = blend(final, market, self.market_weight)
            matrix = core.reweight(matrix, final)
        over_market = core.two_way_probability(odds, "over25", "under25") if odds else None
        if over_market is not None and self.totals_market_weight > 0:
            target = core.pool_binary(
                core.over_probability(matrix), over_market, self.totals_market_weight
            )
            scale, matrix = core.fit_total(home_rate, away_rate, self.rho, final, target)
            home_rate, away_rate = home_rate * scale, away_rate * scale
        output.update(mk.probabilities("goals", matrix))

        # Half-time: league first-half share per side.
        first_home, full_home = league.ht_first["home"], league.ht_full["home"]
        first_away, full_away = league.ht_first["away"], league.ht_full["away"]
        if full_home.n >= self.min_league_matches and full_home.s1 > 0 and full_away.s1 > 0:
            share_home = first_home.s1 / full_home.s1
            share_away = first_away.s1 / full_away.s1
            size = mk.GRID["ht_goals"]
            ht = mk.independent(
                mk.poisson_pmf(home_rate * share_home, size),
                mk.poisson_pmf(away_rate * share_away, size),
            )
            output.update(mk.probabilities("ht_goals", ht))

        for stat in COUNT_STATS:
            home_m = league.moments[(stat, "home")]
            away_m = league.moments[(stat, "away")]
            if home_m.n < self.min_league_matches:
                continue
            (mean_h, var_h), (mean_a, var_a) = home_m.mean_var(), away_m.mean_var()
            if self.team_counts:
                team_fit = self._fit(
                    league, stat, today, (mean_h + mean_a) / 2, mean_h / max(mean_a, 1e-6)
                )
                if team_fit is not None:
                    lam_h, lam_a = self._expected(team_fit, home, away)
                    # Keep the league dispersion ratio (variance / mean) per side.
                    var_h, var_a = var_h / mean_h * lam_h, var_a / mean_a * lam_a
                    mean_h, mean_a = lam_h, lam_a
            output.update(mk.count_probabilities(stat, (mean_h, var_h), (mean_a, var_a)))
        return output

    def _enough(self, ctx, key: str) -> bool:
        sample = self._sample.get(ctx.match_id)
        if sample is None:
            return False
        home, away, league_rows = sample
        stat = mk.CATALOGUE[key].stat
        if stat in ("goals", "ht_goals"):
            return min(home, away) >= self.min_team_matches
        return league_rows >= self.min_league_matches

    def select(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold and self._enough(ctx, key)

    def select_high(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold_high and self._enough(ctx, key)

    def update(self, row) -> None:
        self._sample.pop(row.id, None)
        league = self._league(row.league)
        day = row.date.toordinal()
        home, away = league.team(row.home), league.team(row.away)
        league.day.append(day)
        league.home.append(home)
        league.away.append(away)
        league.played.setdefault(home, []).append(day)
        league.played.setdefault(away, []).append(day)
        for stat, (home_values, away_values) in league.values.items():
            pair = mk.stat_pair(row, stat)
            home_values.append(math.nan if pair is None else pair[0])
            away_values.append(math.nan if pair is None else pair[1])
            if pair is not None and stat in COUNT_STATS:
                league.moments[(stat, "home")].add(day, pair[0])
                league.moments[(stat, "away")].add(day, pair[1])
        ht = mk.stat_pair(row, "ht_goals")
        if ht is not None:
            league.ht_first["home"].add(day, ht[0])
            league.ht_first["away"].add(day, ht[1])
            league.ht_full["home"].add(day, row.home_goals)
            league.ht_full["away"].add(day, row.away_goals)


def factory(**params) -> BaselineModel:
    return BaselineModel(**params)
