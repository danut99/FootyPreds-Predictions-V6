"""Corners / cards / booking points / shots-on-target candidate ("corners_cards").

Only count-stat markets are priced (stats ``corners``, ``cards``, ``bookings``, ``sot``);
goal markets are left to the goal candidates (``include_goals=True`` delegates them to the
shared baseline so the model can also be benchmarked on every market).

Model, per league and stat. All state is updated walk-forward ONE DATE AT A TIME: rows fed
by ``update`` are buffered and only applied when a later date arrives, so every prediction
and every learned residual statistic only sees rows with an earlier date.

1. Level: decayed per-side league means (home, away), half-life ``level_half_life``.
2. Team ratings (online, conjugate style): per team a "for" multiplier (how many of the stat
   the team produces) and an "against" multiplier (how many it concedes or provokes),
   ``rating = (decayed observed + P) / (decayed expected + P)``; "expected" uses the league
   side mean and the opponent's rating at the time of each match and ``P`` is ``prior``
   matches worth of the league mean (shrinkage to 1). Team half-life ``team_half_life``.
   Promoted teams (``newcomer``) start at the league's decayed average rating of previous
   newcomers instead of 1.0. Per-stat overrides: ``cards_prior=...``,
   ``corners_team_half_life=...``, ``sot_level_half_life=...``. The same ratings are kept
   for goals (match strength) and for two unpriced auxiliary stats: total shots (HS/AS,
   feeding corners and shots on target) and fouls (HF/AF, feeding cards).
3. Match context, learned walk-forward by decayed weighted least squares on pre-match
   residuals (pooled over leagues, the split also per league shrunk to the pool):
   - total (``correction``): ``total = base * (1 + x.b)`` with x = month-of-season dummies
     ("month": cards fall from August to May), ``|s|`` ("sup": lopsided games have fewer
     cards), ``log(base / league mean)`` ("shrink": regression to the mean of extreme
     team ratings) and ``log(aux base / aux mean)`` ("aux"); ridge ``corr_ridge``;
   - split (``split``): logit(home share) + b.z with z = goal supremacy
     ``s = log(goals_home / goals_away)`` ("sup": favourites win more corners and shots on
     target, underdogs collect more cards), the auxiliary share deviation ("aux") and the
     plain share deviation ("shrink").
   With ``market_strength=True`` AND ``--odds avg`` the supremacy comes from the margin-free
   PRE-CLOSING 1X2 prices (0.5 * log(price_2 / price_1)); closing prices are never used.
4. Referee (cards and bookings only, ``use_referee``): a shrunk decayed ratio of observed to
   expected match cards for the referee (``referee_prior`` matches of shrinkage). It is
   pre-match information because English and Scottish referees are appointed and
   published several days before kick-off, and fixtures.csv carries the Referee column.
   The benchmark passes ``ctx.referee`` (England and Scotland only). ``use_referee=False``
   or the benchmark ``--no-referee`` is the variant without referee.
5. Joint (home, away) distribution (``joint``):
   - "copula" (default): negative-binomial margins ``var = lam + a*lam^2`` joined by a Frank
     copula whose rank correlation is the decayed residual correlation of home and away
     counts (negative for corners, positive for cards);
   - "split": total ``T ~ NegBin(mu_T, alpha)`` and ``H | T ~ BetaBinomial(T, share, rho)``;
   - "mix": the equal-weight mixture of both.
   ``a``, ``alpha``, ``rho`` and the correlation are method-of-moment estimates on the
   walk-forward pre-match residuals, per league and shrunk towards the pooled estimate
   (``disp_pool`` pseudo-matches). The copula fixes the split model's under-dispersion of
   dominant sides (e.g. strong home teams' corners). Booking points ``B = C + R`` with
   ``R | C ~ Binomial(C, q)`` (q = the league's decayed red share of cards), so the booking
   grid is consistent with the card grid.

Selection rule (frozen on 2223 + 2324 before the 2425 confirm): a key is eligible when
``threshold <= p <= max_p`` (0.80, strict 0.85; ``max_p`` 0.93 drops trivial lines whose
fair odds are below ~1.08), both teams have at least ``min_team_matches`` matches of the
stat in the league in the last 400 days, the league has at least ``min_league_matches``
rows of the stat, the key's group is in ``select_groups`` and the key is in
``SELECTABLE_KEYS`` (``allow="tuned"``). With ``pick="min"`` (default) only ONE key per
market group and match is selected: the eligible key with the lowest p (the longest fair
odds that still clear the threshold); "max" takes the safest, "all" every eligible key.
None of these markets has a real price in football-data: report accuracy, coverage and
fair odds only, never ROI.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import date

import numpy as np
from scipy.special import gammaln

from fotbalPrediction import markets as mk

COUNT_STATS = ("corners", "cards", "sot")
PRICED_STATS = ("corners", "cards", "bookings", "sot")
NEWCOMER_AFTER_ROWS = 300  # a team first seen after this many league rows was promoted
NEWCOMER_MATCHES = 19  # newcomer ratings are sampled after this many league matches
MIN_ROWS = 30  # league rows of a stat before it is priced
SELECT_GROUPS = (
    "corners",
    "team_corners",
    "corners_ah",
    "cards",
    "team_cards",
    "bookings",
    "sot",
    "team_sot",
)
# Auxiliary (unpriced) stats rated like the others: total shots feed corners and shots on
# target, fouls feed cards (log total ratio in the total correction, share in the split).
AUX_STATS = ("shots", "fouls")
AUX_OF = {"corners": "shots", "sot": "shots", "cards": "fouls"}
_AUX_COLUMNS = {"shots": ("home_shots", "away_shots"), "fouls": ("home_fouls", "away_fouls")}
# month-of-season dummies, |supremacy|, log(base / league mean), log(aux base / aux mean)
N_FEATURES = 12 + 3
# Lines "well calibrated enough to be selectable", FROZEN from the TUNE seasons 2223 + 2324
# (joint="copula", rule 0.80 <= p <= 0.93 with the sample gates): >= 30 selected picks,
# pooled accuracy >= 80 %, mean p - accuracy <= 3 points and no tune season below 78 %.
# Excluded for over-confidence: away_corners_under_3.5, corners_ah_1_-2.5,
# home_cards_over_1.5, bookings_under_4.5, sot_under_8.5, home_sot_under_3.5; every other
# line had fewer than 30 picks (never verifiable). Not re-derived after seeing 2425.
SELECTABLE_KEYS = frozenset(
    (
        "corners_over_7.5",
        "corners_over_8.5",
        "corners_under_10.5",
        "corners_under_11.5",
        "corners_under_12.5",
        "home_corners_over_2.5",
        "home_corners_over_3.5",
        "home_corners_over_4.5",
        "home_corners_under_4.5",
        "home_corners_over_5.5",
        "home_corners_under_5.5",
        "home_corners_under_6.5",
        "away_corners_over_2.5",
        "away_corners_over_3.5",
        "away_corners_over_4.5",
        "away_corners_under_4.5",
        "away_corners_under_5.5",
        "away_corners_under_6.5",
        "corners_ah_1_-1.5",
        "corners_ah_1_-0.5",
        "corners_ah_1_+0.5",
        "corners_ah_1_+1.5",
        "corners_ah_1_+2.5",
        "corners_ah_1_+3.5",
        "corners_ah_2_-0.5",
        "corners_ah_2_+0.5",
        "corners_ah_2_+1.5",
        "corners_ah_2_+2.5",
        "corners_ah_2_+3.5",
        "cards_over_1.5",
        "cards_over_2.5",
        "cards_over_3.5",
        "cards_under_4.5",
        "cards_under_5.5",
        "cards_under_6.5",
        "home_cards_over_0.5",
        "home_cards_under_2.5",
        "home_cards_under_3.5",
        "away_cards_over_0.5",
        "away_cards_over_1.5",
        "away_cards_under_2.5",
        "away_cards_under_3.5",
        "bookings_over_1.5",
        "bookings_over_2.5",
        "bookings_over_3.5",
        "bookings_under_5.5",
        "bookings_under_6.5",
        "bookings_under_7.5",
        "sot_over_5.5",
        "sot_over_6.5",
        "sot_over_7.5",
        "sot_over_8.5",
        "sot_under_9.5",
        "sot_under_10.5",
        "home_sot_over_1.5",
        "home_sot_over_2.5",
        "home_sot_over_3.5",
        "home_sot_over_4.5",
        "home_sot_under_4.5",
        "home_sot_over_5.5",
        "home_sot_under_5.5",
        "home_sot_under_6.5",
        "away_sot_over_1.5",
        "away_sot_over_2.5",
        "away_sot_over_3.5",
        "away_sot_under_3.5",
        "away_sot_over_4.5",
        "away_sot_under_4.5",
        "away_sot_under_5.5",
        "away_sot_under_6.5",
    )
)
N_SPLIT = 3  # split features: supremacy, aux share deviation, plain share deviation
RATED_STATS = ("goals",) + COUNT_STATS + AUX_STATS


def pair_of(row, stat: str) -> tuple[int, int] | None:
    """(home, away) count of a priced or auxiliary stat, or None when missing."""
    columns = _AUX_COLUMNS.get(stat)
    if columns is None:
        return mk.stat_pair(row, stat)
    home, away = row.get(columns[0]), row.get(columns[1])
    if home is None or away is None:
        return None
    return int(home), int(away)


def _decay(half_life: float, days: float) -> float:
    return 0.5 ** (days / half_life) if days > 0 else 1.0


# --------------------------------------------------------------------------- distributions


def negbin_vector(mean: float, alpha: float, size: int) -> np.ndarray:
    """NB (var = mean + alpha*mean^2) pmf on 0..size-1, NOT tail folded (caller folds)."""
    mean = max(float(mean), 1e-9)
    k = np.arange(size, dtype=float)
    if alpha <= 1e-6:
        log = k * math.log(mean) - mean - gammaln(k + 1)
    else:
        r = 1.0 / alpha
        log = (
            gammaln(k + r)
            - gammaln(r)
            - gammaln(k + 1)
            + r * math.log(r / (r + mean))
            + k * math.log(mean / (r + mean))
        )
    return np.exp(log)


_LOGC: dict[int, np.ndarray] = {}


def _log_binom(size: int) -> np.ndarray:
    table = _LOGC.get(size)
    if table is None:
        t = np.arange(size, dtype=float)[:, None]
        h = np.arange(size, dtype=float)[None, :]
        table = gammaln(t + 1) - gammaln(h + 1) - gammaln(np.maximum(t - h, 0) + 1)
        table = np.where(h <= t, table, -np.inf)
        _LOGC[size] = table
    return table


def split_matrix(size_total: int, share: float, rho: float) -> np.ndarray:
    """S[t, h] = P(H = h | T = t): beta-binomial (binomial when rho ~ 0)."""
    share = min(max(share, 1e-4), 1 - 1e-4)
    t = np.arange(size_total, dtype=float)[:, None]
    h = np.arange(size_total, dtype=float)[None, :]
    rest = np.maximum(t - h, 0)
    logc = _log_binom(size_total)
    valid = h <= t
    if rho <= 1e-5:
        log = logc + h * math.log(share) + rest * math.log(1 - share)
    else:
        kappa = 1.0 / rho - 1.0
        a, b = share * kappa, (1 - share) * kappa
        log = (
            logc
            + gammaln(h + a)
            + gammaln(rest + b)
            - gammaln(t + a + b)
            - gammaln(a)
            - gammaln(b)
            + gammaln(a + b)
        )
    return np.where(valid, np.exp(np.where(valid, log, 0.0)), 0.0)


def joint_matrix(size: int, mean_total: float, alpha: float, share: float, rho: float):
    """(size, size) joint pmf of (home, away) with the tail folded into the last row/col."""
    size_total = 2 * size - 1
    total = negbin_vector(mean_total, alpha, size_total)
    total[-1] += max(0.0, 1.0 - total.sum())
    split = split_matrix(size_total, share, rho) * total[:, None]  # [t, h]
    t_idx = np.arange(size_total)[:, None]
    h_idx = np.broadcast_to(np.arange(size_total)[None, :], split.shape)
    a_idx = t_idx - h_idx
    valid = a_idx >= 0
    matrix = np.zeros((size, size))
    np.add.at(
        matrix,
        (np.minimum(h_idx, size - 1)[valid], np.minimum(a_idx, size - 1)[valid]),
        split[valid],
    )
    mass = matrix.sum()
    return matrix / mass if mass > 0 else matrix


_KERNELS: dict[tuple[int, int, float], np.ndarray] = {}


def _red_kernel(n: int, size: int, q: float) -> np.ndarray:
    """K[c, b] = P(bookings = b | cards = c) with R | C ~ Binomial(C, q), B = C + R."""
    q = round(min(max(q, 0.0), 0.5), 4)
    kernel = _KERNELS.get((n, size, q))
    if kernel is None:
        kernel = np.zeros((n, size))
        for c in range(n):
            for r in range(c + 1):
                kernel[c, min(c + r, size - 1)] += math.comb(c, r) * q**r * (1 - q) ** (c - r)
        _KERNELS[(n, size, q)] = kernel
    return kernel


def booking_matrix(cards: np.ndarray, q_home: float, q_away: float, size: int) -> np.ndarray:
    """Joint booking points (yellow = 1, red = 2) from a joint card-count grid."""
    n = cards.shape[0]
    return _red_kernel(n, size, q_home).T @ cards @ _red_kernel(n, size, q_away)


def _debye(k: int, t: float) -> float:
    from scipy import integrate

    value, _ = integrate.quad(lambda x: x**k / np.expm1(x) if x else 1.0, 0.0, t)
    return k / t**k * value


def _frank_table() -> tuple[np.ndarray, np.ndarray]:
    """(Spearman rho, theta) of the Frank copula for theta in [-30, 30]."""
    thetas = np.concatenate((np.linspace(-30, -0.05, 300), np.linspace(0.05, 30, 300)))
    rhos = np.array([1.0 - 12.0 / t * (_debye(1, t) - _debye(2, t)) for t in thetas])
    thetas = np.concatenate((thetas[:300], [0.0], thetas[300:]))
    rhos = np.concatenate((rhos[:300], [0.0], rhos[300:]))
    return rhos, thetas


_FRANK: tuple[np.ndarray, np.ndarray] | None = None


def frank_theta(rank_correlation: float) -> float:
    """Frank copula parameter with the given Spearman rank correlation."""
    global _FRANK
    if _FRANK is None:
        _FRANK = _frank_table()
    rhos, thetas = _FRANK
    return float(np.interp(min(max(rank_correlation, -0.85), 0.85), rhos, thetas))


def copula_matrix(
    size: int, lam_home: float, lam_away: float, side_alpha: float, theta: float
) -> np.ndarray:
    """Joint pmf of NB margins (var = lam + side_alpha*lam^2) joined by a Frank copula."""
    home = mk.negbin_pmf(lam_home, lam_home + side_alpha * lam_home**2, size)
    away = mk.negbin_pmf(lam_away, lam_away + side_alpha * lam_away**2, size)
    if abs(theta) < 1e-6:
        return np.outer(home, away)
    u = np.concatenate(([0.0], np.cumsum(home)))
    v = np.concatenate(([0.0], np.cumsum(away)))
    u[-1] = v[-1] = 1.0
    num = np.expm1(-theta * u)[:, None] * np.expm1(-theta * v)[None, :]
    grid = -np.log1p(num / np.expm1(-theta)) / theta
    grid = np.clip(grid, np.maximum(0.0, u[:, None] + v[None, :] - 1), np.minimum.outer(u, v))
    pmf = np.diff(np.diff(grid, axis=0), axis=1)
    pmf = np.maximum(pmf, 0.0)
    return pmf / pmf.sum()


# --------------------------------------------------------------------------- state


class Team:
    __slots__ = ("day", "f_obs", "f_exp", "a_obs", "a_exp", "n", "days", "newcomer")

    def __init__(self, newcomer: bool = False):
        self.newcomer = newcomer
        self.day = None
        self.f_obs = self.f_exp = self.a_obs = self.a_exp = 0.0
        self.n = 0
        self.days: deque[int] = deque()

    def decay_to(self, day: int, half_life: float) -> None:
        if self.day is not None and day > self.day:
            factor = _decay(half_life, day - self.day)
            self.f_obs *= factor
            self.f_exp *= factor
            self.a_obs *= factor
            self.a_exp *= factor
        if self.day is None or day > self.day:
            self.day = day


class Decayed:
    """Decayed sums of any numpy shape (half-life in days)."""

    __slots__ = ("half_life", "day", "s")

    def __init__(self, half_life: float, shape):
        self.half_life = half_life
        self.day = None
        self.s = np.zeros(shape)

    def add(self, day: int, values) -> None:
        if self.day is not None and day > self.day:
            self.s *= _decay(self.half_life, day - self.day)
        if self.day is None or day > self.day:
            self.day = day
        self.s += values


class StatState:
    """One stat in one league: level, team ratings, dispersion, strength and newcomers."""

    def __init__(self, model: CornersCardsModel, stat: str):
        self.stat = stat
        self.prior = model.param(stat, "prior")
        self.team_half_life = model.param(stat, "team_half_life")
        self.level = Decayed(model.param(stat, "level_half_life"), 3)  # w, home, away
        self.teams: dict[str, Team] = {}
        self.rows = 0
        # [w, w*((T-mu)^2 - mu), w*mu^2, w*((H-Ts)^2 - Ts(1-s)), w*T(T-1)s(1-s)]
        self.disp = Decayed(model.disp_half_life, 5)
        # split regression on z = (supremacy, aux share deviation, plain share deviation):
        # [w | Z'WZ (9) | Z'Wr (3)]
        self.strength = Decayed(model.disp_half_life, 1 + N_SPLIT * N_SPLIT + N_SPLIT)
        # newcomers (promoted teams): [w, w*log f, w*log a] after NEWCOMER_MATCHES matches
        self.newcomer = Decayed(model.disp_half_life, 3)
        # side residuals for the copula joint:
        # [sum (r_h^2 - lam_h) + (r_a^2 - lam_a), sum lam_h^2 + lam_a^2, sum r_h r_a,
        #  sum r_h^2, sum r_a^2]
        self.side = Decayed(model.disp_half_life, 5)
        # red share of cards: [reds home, cards home, reds away, cards away]
        self.reds = Decayed(model.disp_half_life, 4)

    def means(self) -> tuple[float, float] | None:
        w, h, a = self.level.s
        if w <= 0:
            return None
        return h / w, a / w

    def pseudo(self) -> float:
        means = self.means()
        return 0.0 if means is None else self.prior * (means[0] + means[1]) / 2


class League:
    def __init__(self, model: CornersCardsModel):
        self.stats = {stat: StatState(model, stat) for stat in RATED_STATS}
        self.referees: dict[str, Decayed] = {}


def features(
    day: int, sup: float | None, base: float, league_mean: float, aux_total: float
) -> np.ndarray:
    """Total-correction features: month of season (July = 0), |supremacy|, log ratios."""
    x = np.zeros(N_FEATURES)
    x[(date.fromordinal(day).month - 7) % 12] = 1.0
    x[12] = abs(sup) if sup is not None else 0.0
    x[13] = math.log(max(base, 1e-6) / max(league_mean, 1e-6))
    x[14] = aux_total
    return x


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


# --------------------------------------------------------------------------- model


class CornersCardsModel:
    PER_STAT = ("prior", "team_half_life", "level_half_life")

    def __init__(
        self,
        level_half_life: float = 365.0,
        team_half_life: float = 180.0,
        disp_half_life: float = 720.0,
        prior: float = 20.0,
        goal_prior: float = 6.0,
        goal_half_life: float = 270.0,
        use_referee: bool = True,
        referee_prior: float = 12.0,
        referee_half_life: float = 540.0,
        newcomer: bool = True,
        correction: str = "month,sup,shrink,aux",
        split: str = "sup,aux,shrink",
        corr_half_life: float = 1500.0,
        corr_ridge: float = 500.0,
        disp_pool: float = 400.0,
        alpha_scale: float = 1.0,
        rho_scale: float = 1.0,
        joint: str = "copula",
        market_strength: bool = False,
        market_source: str = "avg",
        dep_scale: float = 1.0,
        include_goals: bool = False,
        stats: str = "corners,cards,bookings,sot",
        threshold: float = 0.80,
        threshold_high: float = 0.85,
        max_p: float = 0.93,
        pick: str = "min",
        allow: str = "tuned",
        min_team_matches: int = 6,
        min_league_matches: int = 150,
        select_groups: str = ",".join(SELECT_GROUPS),
        **extra,
    ):
        # "<stat>_<name>" overrides a per-stat parameter; "goals_<name>" goes to the baseline.
        self.overrides: dict[tuple[str, str], float] = {}
        goal_params = {}
        for name, value in extra.items():
            stat, _, param = name.partition("_")
            if stat in COUNT_STATS + AUX_STATS and param in self.PER_STAT:
                self.overrides[(stat, param)] = value
            elif name.startswith("goals_"):
                goal_params[name[len("goals_") :]] = value
            else:
                raise TypeError(f"Parametru necunoscut: {name}")
        self.base = {
            "prior": prior,
            "team_half_life": team_half_life,
            "level_half_life": level_half_life,
        }
        self.goal_prior = goal_prior
        self.goal_half_life = goal_half_life
        self.disp_half_life = disp_half_life
        self.use_referee = use_referee
        self.referee_prior = referee_prior
        self.referee_half_life = referee_half_life
        self.use_newcomer = newcomer
        parts = {c.strip() for c in (correction or "").split(",") if c.strip()}
        self.corr_mask = np.zeros(N_FEATURES, dtype=bool)
        self.corr_mask[:12] = "month" in parts
        self.corr_mask[12] = "sup" in parts
        self.corr_mask[13] = "shrink" in parts
        self.corr_mask[14] = "aux" in parts
        # split features in use: supremacy ("sup"), aux share ("aux"), share shrink ("shrink")
        chosen = {c.strip() for c in (split or "").split(",") if c.strip()}
        self.split_mask = np.array([name in chosen for name in ("sup", "aux", "shrink")], float)
        self.corr_ridge = corr_ridge
        self.disp_pool = disp_pool
        self.alpha_scale = alpha_scale
        self.rho_scale = rho_scale
        if joint not in ("split", "copula", "mix"):
            raise ValueError(f"joint necunoscut: {joint}")
        self.joint = joint
        self.dep_scale = dep_scale
        self.market_strength = market_strength
        self.market_source = market_source
        self.stats = tuple(s.strip() for s in stats.split(",") if s.strip())
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.max_p = max_p
        if pick not in ("all", "max", "min"):
            raise ValueError(f"pick necunoscut: {pick}")
        self.pick = pick
        if allow not in ("tuned", "all"):
            raise ValueError(f"allow necunoscut: {allow}")
        self.allowed = SELECTABLE_KEYS if allow == "tuned" else None
        self.min_team_matches = min_team_matches
        self.min_league_matches = min_league_matches
        self.select_groups = {g.strip() for g in select_groups.split(",") if g.strip()}
        self.leagues: dict[str, League] = {}
        self.pooled = {stat: Decayed(disp_half_life, 5) for stat in COUNT_STATS}
        self.pooled_side = {stat: Decayed(disp_half_life, 5) for stat in COUNT_STATS}
        self.pooled_strength = {
            stat: Decayed(disp_half_life, 1 + N_SPLIT * N_SPLIT + N_SPLIT) for stat in COUNT_STATS
        }
        # total correction (weighted least squares pooled over leagues): [X'WX | X'Wr]
        self.corr = {
            stat: Decayed(corr_half_life, (N_FEATURES, N_FEATURES + 1)) for stat in COUNT_STATS
        }
        self._coef: dict[str, np.ndarray | None] = {}
        self.pending: list = []
        self.pending_day: int | None = None
        self._sample: dict[str, dict[str, tuple[int, int, int]]] = {}
        self._chosen: dict[str, tuple[set[str], set[str]]] = {}
        self.goals_model = None
        if include_goals:
            from fotbalPrediction.candidates.baseline import BaselineModel

            self.goals_model = BaselineModel(**goal_params)

    def param(self, stat: str, name: str) -> float:
        if stat == "goals":
            return {
                "prior": self.goal_prior,
                "team_half_life": self.goal_half_life,
                "level_half_life": self.base["level_half_life"],
            }[name]
        return self.overrides.get((stat, name), self.base[name])

    # ------------------------------------------------------------------ ratings

    def _league(self, code: str) -> League:
        league = self.leagues.get(code)
        if league is None:
            league = self.leagues[code] = League(self)
        return league

    def _team(self, state: StatState, name: str, day: int) -> Team:
        team = state.teams.get(name)
        if team is None:
            team = state.teams[name] = Team(newcomer=state.rows >= NEWCOMER_AFTER_ROWS)
            if self.use_newcomer and state.stat != "goals":
                # Shrink a promoted team towards the league's newcomer average, not to 1.
                f, a = self._newcomer(state)
                base = state.pseudo()
                team.f_obs = base * (f - 1.0)
                team.a_obs = base * (a - 1.0)
            team.day = day
        else:
            team.decay_to(day, state.team_half_life)
        return team

    @staticmethod
    def _ratings(team: Team | None, pseudo: float) -> tuple[float, float]:
        if team is None:
            return 1.0, 1.0
        pseudo = max(pseudo, 0.05)
        f = max(team.f_obs + pseudo, 0.01) / (team.f_exp + pseudo)
        a = max(team.a_obs + pseudo, 0.01) / (team.a_exp + pseudo)
        return f, a

    @staticmethod
    def _newcomer(state: StatState) -> tuple[float, float]:
        w, lf, la = state.newcomer.s
        if w <= 5:
            return 1.0, 1.0
        return math.exp(lf / w), math.exp(la / w)

    def _rates(self, state: StatState, home: str, away: str, day: int):
        """Pre-match (home, away) expected counts, or None without a league level."""
        means = state.means()
        if means is None:
            return None
        pseudo = state.pseudo()
        rated = []
        for name in (home, away):
            team = state.teams.get(name)
            if team is not None:
                team.decay_to(day, state.team_half_life)
                rated.append(self._ratings(team, pseudo))
            elif self.use_newcomer and state.stat != "goals":
                rated.append(self._newcomer(state))
            else:
                rated.append((1.0, 1.0))
        (fh, ah), (fa, aa) = rated
        return means[0] * fh * aa, means[1] * fa * ah

    def _market_supremacy(self, prices) -> float | None:
        """Half the log ratio of the margin-free home/away win probabilities (1X2 prices)."""
        if not self.market_strength or not prices:
            return None
        values = [prices.get(k) for k in ("1", "X", "2")]
        if not all(v and v > 1.0 for v in values):
            return None
        return 0.5 * math.log(values[2] / values[0])

    def _supremacy(self, league: League, home: str, away: str, day: int) -> float | None:
        rates = self._rates(league.stats["goals"], home, away, day)
        if rates is None:
            return None
        return math.log(max(rates[0], 0.05) / max(rates[1], 0.05))

    # ------------------------------------------------------------------ learned corrections

    def _beta(self, league: League, stat: str) -> np.ndarray:
        """Split slopes on z (see _parts): league WLS shrunk towards the pooled WLS."""
        pooled = self.pooled_strength[stat].s
        if pooled[0] <= 0:
            return np.zeros(N_SPLIT)
        combined = league.stats[stat].strength.s + pooled * (self.disp_pool / pooled[0])
        end = 1 + N_SPLIT * N_SPLIT
        mask = self.split_mask > 0
        beta = np.zeros(N_SPLIT)
        if mask.any():
            a = combined[1:end].reshape(N_SPLIT, N_SPLIT)[np.ix_(mask, mask)]
            beta[mask] = np.linalg.solve(a + 1e-6 * np.eye(int(mask.sum())), combined[end:][mask])
        return beta

    def _aux(self, league: League, stat: str, home, away, day) -> tuple[float, float]:
        """(log aux total / aux league mean, logit aux share - logit league aux share)."""
        name = AUX_OF.get(stat)
        if name is None:
            return 0.0, 0.0
        state = league.stats[name]
        if state.rows < MIN_ROWS:
            return 0.0, 0.0
        means = state.means()
        lam_h, lam_a = self._rates(state, home, away, day)
        total = lam_h + lam_a
        mean = means[0] + means[1]
        if total <= 0 or mean <= 0:
            return 0.0, 0.0
        return (
            math.log(total / mean),
            _logit(lam_h / total) - _logit(means[0] / mean),
        )

    def _coefficients(self, stat: str) -> np.ndarray | None:
        if stat in self._coef:
            return self._coef[stat]
        coef = None
        mask = self.corr_mask
        if mask.any():
            matrix = self.corr[stat].s
            a = matrix[:, :N_FEATURES][np.ix_(mask, mask)]
            if np.trace(a) > 0:
                b = matrix[:, N_FEATURES][mask]
                coef = np.zeros(N_FEATURES)
                coef[mask] = np.linalg.solve(a + self.corr_ridge * np.eye(len(b)), b)
        self._coef[stat] = coef
        return coef

    def _dispersion(self, league: League, stat: str) -> tuple[float, float]:
        """(alpha, rho) for the stat in the league, shrunk towards the pooled estimate."""
        pooled = self.pooled[stat].s
        if pooled[0] <= 0:
            return 0.0, 0.0
        combined = league.stats[stat].disp.s + pooled * (self.disp_pool / pooled[0])
        alpha = combined[1] / combined[2] if combined[2] > 0 else 0.0
        rho = combined[3] / combined[4] if combined[4] > 0 else 0.0
        alpha = min(max(alpha * self.alpha_scale, 0.0), 1.0)
        rho = min(max(rho * self.rho_scale, 0.0), 0.5)
        return alpha, rho

    def _side(self, league: League, stat: str) -> tuple[float, float]:
        """(side NB alpha, Frank theta) from residuals, league shrunk towards pooled."""
        pooled = self.pooled_side[stat].s
        weight = self.pooled[stat].s[0]
        if weight <= 0 or pooled[1] <= 0:
            return 0.0, 0.0
        combined = league.stats[stat].side.s + pooled * (self.disp_pool / weight)
        side_alpha = min(max(combined[0] / combined[1] * self.alpha_scale, 0.0), 1.0)
        corr = combined[2] / math.sqrt(max(combined[3] * combined[4], 1e-12))
        return side_alpha, frank_theta(corr * self.dep_scale)

    def _joint(self, league: League, stat: str, total: float, share: float) -> np.ndarray:
        """Joint (home, away) pmf: total+split, copula, or their equal-weight mixture."""
        size = mk.GRID[stat]
        parts = []
        if self.joint in ("split", "mix"):
            alpha, rho = self._dispersion(league, stat)
            parts.append(joint_matrix(size, total, alpha, share, rho))
        if self.joint in ("copula", "mix"):
            side_alpha, theta = self._side(league, stat)
            parts.append(copula_matrix(size, total * share, total * (1 - share), side_alpha, theta))
        return parts[0] if len(parts) == 1 else (parts[0] + parts[1]) / 2

    def _referee_factor(self, league: League, referee: str | None) -> float:
        if not self.use_referee or not referee:
            return 1.0
        ref = league.referees.get(referee)
        means = league.stats["cards"].means()
        if ref is None or means is None:
            return 1.0
        _, obs, exp = ref.s
        pseudo = self.referee_prior * (means[0] + means[1])
        return (obs + pseudo) / (exp + pseudo)

    def _parts(self, league: League, stat: str, home, away, day, sup, referee):
        """(base total, corrected total, plain share, share, total features, split features)."""
        state = league.stats[stat]
        lam_h, lam_a = self._rates(state, home, away, day)
        if stat == "cards":
            factor = self._referee_factor(league, referee)
            lam_h, lam_a = lam_h * factor, lam_a * factor
        base = max(lam_h + lam_a, 1e-6)
        means = state.means()
        aux_total, aux_share = self._aux(league, stat, home, away, day)
        x = features(day, sup, base, means[0] + means[1], aux_total)
        coef = self._coefficients(stat)
        total = base * max(0.3, 1.0 + float(x @ coef)) if coef is not None else base
        plain = min(max(lam_h / base, 1e-3), 1 - 1e-3)
        z = np.array(
            [
                sup if sup is not None else 0.0,
                aux_share,
                _logit(plain) - _logit(means[0] / (means[0] + means[1])),
            ]
        )
        logit = _logit(plain) + float(self._beta(league, stat) @ z)
        share = 1.0 / (1.0 + math.exp(-logit))
        return base, total, plain, share, x, z

    # ------------------------------------------------------------------ walk-forward

    def _advance(self, day: int) -> None:
        if self.pending_day is not None and day > self.pending_day:
            self._flush()
            self.pending_day = None

    def _flush(self) -> None:
        rows, self.pending = self.pending, []
        day = self.pending_day
        prepared = []
        for row in rows:
            league = self._league(row.league)
            # Learning uses the row's PRE-CLOSING prices (known before kick-off) only when the
            # market variant is on; predictions then need the same source in ctx.odds.
            sup = self._market_supremacy(row.odds.get(self.market_source))
            if sup is None:
                sup = self._supremacy(league, row.home, row.away, day)
            prepared.append((row, league, sup))
        # 1) residual statistics with the pre-day state (no row of this day applied yet)
        for row, league, sup in prepared:
            for stat in COUNT_STATS:
                pair = mk.stat_pair(row, stat)
                if pair is not None and league.stats[stat].rows >= MIN_ROWS:
                    self._record(league, stat, row, pair, sup, day)
        # 2) state updates
        for row, league, _ in prepared:
            for stat in RATED_STATS:
                pair = pair_of(row, stat)
                if pair is not None:
                    self._apply(league, stat, row, pair, day)
        self._coef.clear()

    def _record(self, league: League, stat: str, row, pair, sup, day: int) -> None:
        state = league.stats[stat]
        h, a = pair
        t = h + a
        base, total, plain, share, x, z = self._parts(
            league, stat, row.home, row.away, day, sup, row.referee
        )
        # total correction: WLS of r = T/base - 1 on x with weight base (Poisson-like)
        update = np.empty((N_FEATURES, N_FEATURES + 1))
        update[:, :N_FEATURES] = base * np.outer(x, x)
        update[:, N_FEATURES] = x * (t - base)
        self.corr[stat].add(day, update)
        # split: WLS of the smoothed observed log-odds share minus the plain share on z
        if t > 0:
            resid = _logit((h + 0.5) / (t + 1.0)) - _logit(plain)
            weight = t / (t + 4.0)
            values = np.concatenate(([weight], weight * np.outer(z, z).ravel(), weight * z * resid))
            state.strength.add(day, values)
            self.pooled_strength[stat].add(day, values)
        values = np.array(
            [
                1.0,
                (t - total) ** 2 - total,
                total * total,
                (h - t * share) ** 2 - t * share * (1 - share),
                t * (t - 1) * share * (1 - share),
            ]
        )
        state.disp.add(day, values)
        self.pooled[stat].add(day, values)
        lam_h, lam_a = total * share, total * (1 - share)
        r_h, r_a = h - lam_h, a - lam_a
        values = np.array(
            [
                r_h * r_h - lam_h + r_a * r_a - lam_a,
                lam_h * lam_h + lam_a * lam_a,
                r_h * r_a,
                r_h * r_h,
                r_a * r_a,
            ]
        )
        state.side.add(day, values)
        self.pooled_side[stat].add(day, values)
        if stat == "cards" and row.referee:
            ref = league.referees.get(row.referee)
            if ref is None:
                ref = league.referees[row.referee] = Decayed(self.referee_half_life, 3)
            expected = self._rates(state, row.home, row.away, day)  # without the referee
            ref.add(day, np.array([1.0, float(t), expected[0] + expected[1]]))

    def _apply(self, league: League, stat: str, row, pair, day: int) -> None:
        state = league.stats[stat]
        h, a = pair
        means = state.means()
        th = self._team(state, row.home, day)
        ta = self._team(state, row.away, day)
        if means is not None:
            pseudo = state.pseudo()
            fh, ah = self._ratings(th, pseudo)
            fa, aa = self._ratings(ta, pseudo)
            # observed vs expected given the opponent's current rating
            th.f_obs += h
            th.f_exp += means[0] * aa
            th.a_obs += a
            th.a_exp += means[1] * fa
            ta.f_obs += a
            ta.f_exp += means[1] * ah
            ta.a_obs += h
            ta.a_exp += means[0] * fh
        for team in (th, ta):
            team.n += 1
            team.days.append(day)
            if team.newcomer and team.n == NEWCOMER_MATCHES and stat in COUNT_STATS:
                f, ag = self._ratings(team, state.pseudo())
                state.newcomer.add(day, np.array([1.0, math.log(f), math.log(ag)]))
        state.level.add(day, np.array([1.0, float(h), float(a)]))
        state.rows += 1
        if stat == "cards":
            reds = (row.home_red or 0, row.away_red or 0)
            state.reds.add(day, np.array([reds[0], h, reds[1], a], dtype=float))

    # ------------------------------------------------------------------ protocol

    def update(self, row) -> None:
        day = row.date.toordinal()
        self._advance(day)
        self.pending.append(row)
        self.pending_day = day
        self._sample.pop(row.id, None)
        self._chosen.pop(row.id, None)
        if self.goals_model is not None:
            self.goals_model.update(row)

    def predict(self, ctx) -> dict[str, float]:
        day = ctx.date.toordinal()
        self._advance(day)
        output: dict[str, float] = {}
        if self.goals_model is not None:
            output.update(self.goals_model.predict(ctx))
        league = self._league(ctx.league)
        sup = self._market_supremacy(ctx.odds) if not ctx.odds_closing else None
        if sup is None:
            sup = self._supremacy(league, ctx.home, ctx.away, day)
        sample: dict[str, tuple[int, int, int]] = {}
        cards_matrix = None
        for stat in COUNT_STATS:
            if stat not in self.stats and not (stat == "cards" and "bookings" in self.stats):
                continue
            state = league.stats[stat]
            if state.rows < MIN_ROWS:
                continue
            _, total, _, share, _, _ = self._parts(
                league, stat, ctx.home, ctx.away, day, sup, ctx.referee
            )
            matrix = self._joint(league, stat, total, share)
            if stat in self.stats:
                output.update(mk.probabilities(stat, matrix))
            sample[stat] = (
                self._recent(state, ctx.home, day),
                self._recent(state, ctx.away, day),
                state.rows,
            )
            if stat == "cards":
                cards_matrix = matrix
        if cards_matrix is not None and "bookings" in self.stats:
            reds = league.stats["cards"].reds.s
            q_home = reds[0] / reds[1] if reds[1] > 0 else 0.03
            q_away = reds[2] / reds[3] if reds[3] > 0 else 0.03
            matrix = booking_matrix(cards_matrix, q_home, q_away, mk.GRID["bookings"])
            output.update(mk.probabilities("bookings", matrix))
            sample["bookings"] = sample["cards"]
        self._sample[ctx.match_id] = sample
        if self.pick != "all":
            self._chosen[ctx.match_id] = (
                self._choose(ctx, output, self.threshold),
                self._choose(ctx, output, self.threshold_high),
            )
        return output

    def _choose(self, ctx, output: dict[str, float], threshold: float) -> set[str]:
        """One key per market group: the highest ("max") or lowest ("min") p in the band."""
        best: dict[str, tuple[float, str]] = {}
        for key, p in output.items():
            market = mk.CATALOGUE[key]
            if market.stat in ("goals", "ht_goals") or not market.selectable:
                continue
            if not threshold <= p <= self.max_p or not self._enough(ctx, key):
                continue
            score = p if self.pick == "max" else -p
            if market.group not in best or score > best[market.group][0]:
                best[market.group] = (score, key)
        return {key for _, key in best.values()}

    @staticmethod
    def _recent(state: StatState, name: str, day: int) -> int:
        """Matches of the stat the team played in this league in the last 400 days."""
        team = state.teams.get(name)
        if team is None:
            return 0
        while team.days and team.days[0] < day - 400:
            team.days.popleft()
        return len(team.days)

    def _enough(self, ctx, key: str) -> bool:
        market = mk.CATALOGUE[key]
        if market.group not in self.select_groups:
            return False
        if self.allowed is not None and key not in self.allowed:
            return False
        sample = self._sample.get(ctx.match_id, {}).get(market.stat)
        if sample is None:
            return False
        home, away, rows = sample
        return min(home, away) >= self.min_team_matches and rows >= self.min_league_matches

    def _goal_rule(self, ctx, key: str, p: float, high: bool) -> bool | None:
        if mk.CATALOGUE[key].stat not in ("goals", "ht_goals"):
            return None
        if self.goals_model is None:
            return False
        rule = self.goals_model.select_high if high else self.goals_model.select
        return bool(rule(ctx, key, p))

    def select(self, ctx, key: str, p: float) -> bool:
        goal = self._goal_rule(ctx, key, p, False)
        if goal is not None:
            return goal
        if self.pick != "all":
            return key in self._chosen.get(ctx.match_id, ((), ()))[0]
        return self.threshold <= p <= self.max_p and self._enough(ctx, key)

    def select_high(self, ctx, key: str, p: float) -> bool:
        goal = self._goal_rule(ctx, key, p, True)
        if goal is not None:
            return goal
        if self.pick != "all":
            return key in self._chosen.get(ctx.match_id, ((), ()))[1]
        return self.threshold_high <= p <= self.max_p and self._enough(ctx, key)


def factory(**params) -> CornersCardsModel:
    return CornersCardsModel(**params)
