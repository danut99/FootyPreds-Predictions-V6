"""goals_model: score-matrix candidate for every goal market (FT and HT).

Covers stats ``goals`` (1X2, DC, totals 0.5-4.5, team totals, BTTS, DNB, AH -2.5..+2.5 with
quarter lines, exact score) and ``ht_goals`` (HT 1X2 and HT totals). No corners/cards/SOT.

Model (every default below was tuned on 2223/2324 only, see ``goals_model_tune.py``):

- Batch ratings: one multiplicative attack/defence fit per league (Maher / Dixon-Coles),
  refitted once per match date on rows strictly before that date within ``max_days``, with
  exponential decay ``half_life`` and conjugate Gamma shrinkage ``prior`` towards a PER-TEAM
  prior mean (1 for established teams). League-specific goal level and home advantage.
- xG proxy: the fit target blends goals with shots on target and shots, each converted to the
  goal scale by the league's decayed conversion rate:
  ``t = (1 - w_sot - w_shots) * goals + w_sot * c_sot * sot + w_shots * c_sh * shots``
  (rows without shots use goals only, e.g. the National League after 1516).
- Split total: a second fit with its own decay / shrinkage / shot weights (``total_*``)
  sets the expected goal TOTAL; the first fit sets the home/away split (supremacy). Totals
  want much stronger shrinkage than supremacy.
- Dynamic ratings: an online Poisson state-space model per league (approximate Kalman filter
  on log attack / log defence, random-walk noise ``dyn_q`` per day, initial variance
  ``dyn_p0``) updated match by match on the same xG-proxy target; its rates are pooled
  geometrically with the batch rates (``dyn_weight``).
- Promoted / relegated teams (cross-league strength): a team starting a new spell in a league
  gets a prior mean ``exp(c_dir + beta * log(rating in the previous league))`` for attack and
  defence (``dir`` = up for promoted or unknown origin, down for relegated; the previous rating
  is the origin league's plain fit on the day the team left it).
- ``sup_scale`` stretches log(home/away rate) around the league home advantage (the pooled
  ensemble is slightly under-confident on supremacy).
- Score matrix: Dixon-Coles ``rho`` (+ optional diagonal inflation ``draw_boost``).
- Half-time: the league's decayed first-half share per side scales the FT rates
  (``ht_rho``, ``ht_draw_boost``).
- With ``ctx.odds`` (pre-closing unless the benchmark says closing) the 1X2 is pooled with the
  margin-free market 1X2 and P(over 2.5) with the over/under price; the tuned weights are 1.0
  (the market alone beats every model mix on 1X2 and O/U 2.5), so with odds the model only
  shapes the rest of the matrix (DC/AH/team totals/BTTS/HT/CS) around the market.

Selection (``select`` / ``select_high``): p >= 0.80 / 0.85 and both teams have at least
``min_team_matches`` league matches in ``max_days`` (or a known previous-league rating).
``min_fair`` can exclude trivially short prices. ``online_rule=True`` adds a per-key threshold
learned only from the model's own past settled predictions (it self-predicts fed rows from
``calib_from`` on to warm up); it changed almost nothing on 2223/2324, so it is off.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict

import numpy as np

from fotbalPrediction import data
from fotbalPrediction import markets as mk

PRIOR_MU = 1.35
PRIOR_HOME = 1.2
SIZE = mk.GRID["goals"]
HT_SIZE = mk.GRID["ht_goals"]
GOAL_KEYS = mk.keys_of("goals")
HT_KEYS = mk.keys_of("ht_goals")
SPELL_GAP = 250  # days without a match in the league that start a new spell
ORIGIN_GAP = 400  # the previous league only counts when left at most this many days ago


def _masks(size: int):
    h = np.arange(size)[:, None]
    a = np.arange(size)[None, :]
    return (h > a), (h == a), (h < a), (h + a)


MASKS = {SIZE: _masks(SIZE), HT_SIZE: _masks(HT_SIZE)}


def score_matrix(home_rate: float, away_rate: float, rho: float, boost: float, size: int):
    """Dixon-Coles matrix with a multiplicative draw (diagonal) inflation, normalized."""
    matrix = np.outer(mk.poisson_pmf(home_rate, size), mk.poisson_pmf(away_rate, size))
    if rho:
        low = max(-1 / max(home_rate, 1e-9), -1 / max(away_rate, 1e-9))
        high = min(1 / max(home_rate * away_rate, 1e-9), 1)
        rho = min(high, max(low, rho))
        matrix[0, 0] *= 1 - home_rate * away_rate * rho
        matrix[0, 1] *= 1 + home_rate * rho
        matrix[1, 0] *= 1 + away_rate * rho
        matrix[1, 1] *= 1 - rho
    if boost:
        matrix[np.diag_indices(size)] *= 1 + boost
    return matrix / matrix.sum()


def one_x_two(matrix) -> np.ndarray:
    win, draw, loss, _ = MASKS[matrix.shape[0]]
    return np.array([matrix[win].sum(), matrix[draw].sum(), matrix[loss].sum()])


def reweight(matrix, target) -> np.ndarray:
    win, draw, loss, _ = MASKS[matrix.shape[0]]
    current = one_x_two(matrix)
    factor = np.where(current > 0, np.asarray(target) / np.maximum(current, 1e-300), 0.0)
    out = matrix * (win * factor[0] + draw * factor[1] + loss * factor[2])
    total = out.sum()
    return matrix if total <= 0 else out / total


def over_probability(matrix, line: float = 2.5) -> float:
    return float(matrix[MASKS[matrix.shape[0]][3] > line].sum())


def _logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def market_1x2(odds) -> np.ndarray | None:
    values = [odds.get(k) for k in ("1", "X", "2")]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 1 for v in values):
        return None
    inverse = np.array([1 / v for v in values])
    total = inverse.sum()
    if not 0.98 <= total <= 1.4:
        return None
    return inverse / total


def market_over(odds) -> float | None:
    over, under = odds.get("over25"), odds.get("under25")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 1 for v in (over, under)):
        return None
    total = 1 / over + 1 / under
    if not 0.95 <= total <= 1.4:
        return None
    return (1 / over) / total


def fit_ratings(hi, ai, ht, at, w, n, m_att, m_def, *, prior, iterations, init):
    """(attack, defence, mu, home): conjugate updates with per-team prior means."""
    if init is not None:
        attack = np.ones(n)
        defence = np.ones(n)
        k = min(n, len(init[0]))
        attack[:k], defence[:k] = init[0][:k], init[1][:k]
        mu, home = init[2], init[3]
    else:
        attack, defence = m_att.copy(), m_def.copy()
        mu, home = PRIOR_MU, PRIOR_HOME
    goals = float((w * (ht + at)).sum())
    total_home = float((w * ht).sum())
    scored = np.bincount(hi, w * ht, n) + np.bincount(ai, w * at, n)
    conceded = np.bincount(hi, w * at, n) + np.bincount(ai, w * ht, n)
    pa, pd = prior * m_att, prior * m_def
    for _ in range(iterations):
        previous = attack
        chances = np.bincount(hi, w * mu * home * defence[ai], n) + np.bincount(
            ai, w * mu * defence[hi], n
        )
        attack = (scored + pa) / (chances + prior)
        exposure = np.bincount(hi, w * mu * attack[ai], n) + np.bincount(
            ai, w * mu * home * attack[hi], n
        )
        defence = (conceded + pd) / (exposure + prior)
        base_home = float((w * mu * attack[hi] * defence[ai]).sum())
        home = (total_home + 10 * PRIOR_HOME) / (base_home + 10)
        base = float((w * (home * attack[hi] * defence[ai] + attack[ai] * defence[hi])).sum())
        mu = (goals + 20 * PRIOR_MU) / (base + 20)
        if np.max(np.abs(attack - previous)) < 1e-6:
            break
    return attack, defence, mu, home


class _Dynamic:
    """Online Poisson state-space ratings of one league (approximate Kalman filter on log
    attack / log defence, random-walk process noise per day)."""

    def __init__(self, p0: float, q: float, rate: float):
        self.p0, self.q, self.rate = p0, q, rate
        self.state: dict[str, list[float]] = {}  # team -> [att, def, Pa, Pd, day]
        self.mu = math.log(1.35)
        self.home = math.log(1.2)
        self.n = 0

    def _team(self, name: str, day: int, prior, mutate: bool):
        s = self.state.get(name)
        if s is None:
            m_att, m_def = prior(name)
            s = [math.log(m_att), math.log(m_def), self.p0, self.p0, day]
            if mutate:
                self.state[name] = s
            return s
        gap = max(0, day - s[4])
        pa = min(self.p0, s[2] + self.q * gap)
        pd = min(self.p0, s[3] + self.q * gap)
        if mutate:
            s[2], s[3], s[4] = pa, pd, day
            return s
        return [s[0], s[1], pa, pd, day]

    def rates(self, home: str, away: str, day: int, prior):
        h = self._team(home, day, prior, False)
        a = self._team(away, day, prior, False)
        return (
            math.exp(self.mu + self.home + h[0] + a[1]),
            math.exp(self.mu + a[0] + h[1]),
        )

    def update(self, home: str, away: str, day: int, t_h: float, t_a: float, prior):
        h = self._team(home, day, prior, True)
        a = self._team(away, day, prior, True)
        lam_h = math.exp(self.mu + self.home + h[0] + a[1])
        lam_a = math.exp(self.mu + a[0] + h[1])
        for att, dfn, y, lam in ((h, a, t_h, lam_h), (a, h, t_a, lam_a)):
            s = att[2] + dfn[3] + 1.0 / lam
            z = (y - lam) / lam
            att[0] += att[2] / s * z
            dfn[1] += dfn[3] / s * z
            att[2] -= att[2] * att[2] / s
            dfn[3] -= dfn[3] * dfn[3] / s
        self.mu += self.rate * ((t_h - lam_h) + (t_a - lam_a)) / (lam_h + lam_a)
        self.home += self.rate * 0.5 * ((t_h - lam_h) / lam_h - (t_a - lam_a) / lam_a)
        self.n += 1


class _League:
    def __init__(self, code: str, country: str, tier: int):
        self.code, self.country, self.tier = code, country, tier
        self.teams: dict[str, int] = {}
        self.day: list[int] = []
        self.hi: list[int] = []
        self.ai: list[int] = []
        self.cols = {name: [] for name in ("hg", "ag", "hst", "ast", "hs", "as", "hth", "hta")}
        self.played: dict[int, list[int]] = defaultdict(list)
        # idx -> (spell start day, origin (league code, day left) | None, newcomer flag)
        self.spell: dict[int, tuple] = {}
        self.last_day: dict[int, int] = {}
        self.fits: dict[tuple, tuple] = {}  # (plain, fit day) -> fit
        self.last_fit: dict[str, tuple] = {}
        self.dyn: _Dynamic | None = None
        # decayed sums for the shot -> goal conversion of the online model
        self.conv = [0, 0.0, 0.0, 0.0]  # day, goals, sot, shots

    def team(self, name: str) -> int:
        if name not in self.teams:
            self.teams[name] = len(self.teams)
        return self.teams[name]


def _num(value) -> float:
    return math.nan if value is None else float(value)


class GoalsModel:
    def __init__(
        self,
        half_life: float = 360.0,
        max_days: int = 730,
        prior: float = 4.0,
        rho: float = -0.05,
        draw_boost: float = 0.0,
        w_sot: float = 0.35,
        w_shots: float = 0.1,
        total_half_life: float | None = 270.0,
        total_prior: float | None = 14.0,
        total_w_sot: float | None = None,
        total_w_shots: float | None = 0.25,
        dyn_weight: float = 0.5,
        dyn_p0: float = 0.1,
        dyn_q: float = 0.0004,
        dyn_rate: float = 0.003,
        sup_scale: float = 1.08,
        promo: bool = True,
        c_att_up: float = -0.15,
        c_def_up: float = 0.15,
        c_att_down: float = 0.10,
        c_def_down: float = -0.10,
        beta: float = 0.3,
        ht_half_life: float = 1460.0,
        ht_rho: float = 0.0,
        ht_draw_boost: float = -0.03,
        market_weight: float = 1.0,
        totals_market_weight: float = 1.0,
        threshold: float = 0.80,
        threshold_high: float = 0.85,
        min_team_matches: int = 5,
        min_fair: float = 1.0,
        online_rule: bool = False,
        calib_from: str = "1920",
        rule_half_life: float = 730.0,
        rule_margin: float = 0.01,
        rule_min_picks: float = 60.0,
    ):
        self.half_life = half_life
        self.max_days = max_days
        self.prior = prior
        self.rho = rho
        self.draw_boost = draw_boost
        self.w_sot = w_sot
        self.w_shots = w_shots
        main = (half_life, prior, w_sot, w_shots)
        total = (
            half_life if total_half_life is None else total_half_life,
            prior if total_prior is None else total_prior,
            w_sot if total_w_sot is None else total_w_sot,
            w_shots if total_w_shots is None else total_w_shots,
        )
        self.settings = {"main": main, "plain": main, "total": total}
        self.split_total = total != main
        self.promo = promo
        self.dyn_weight = dyn_weight
        self.dyn_args = (dyn_p0, dyn_q, dyn_rate)
        self.sup_scale = sup_scale
        self.c = {"up": (c_att_up, c_def_up), "down": (c_att_down, c_def_down)}
        self.beta = beta
        self.ht_decay = math.log(2) / ht_half_life
        self.ht_rho = ht_rho
        self.ht_draw_boost = ht_draw_boost
        self.market_weight = market_weight
        self.totals_market_weight = totals_market_weight
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.min_team_matches = min_team_matches
        self.min_fair = min_fair
        self.online_rule = online_rule
        self.calib_from = calib_from
        self.rule_decay = math.log(2) / rule_half_life
        self.rule_margin = rule_margin
        self.rule_min_picks = rule_min_picks
        self.leagues: dict[str, _League] = {}
        # country -> team -> (league code, last day)
        self.registry: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)
        # league -> decayed sums [day, first_h, full_h, first_a, full_a]
        self.ht_share: dict[str, list[float]] = {}
        self._sample: dict[str, tuple] = {}
        self._pending: dict[str, dict[str, float]] = {}
        # online rule: key -> bins of decayed (picks, wins) by p (0.005 wide from 0.70)
        self._rule: dict[str, list] = {}
        self._rule_day: dict[str, int] = {}
        self._cut: dict[tuple[str, float], float] = {}
        self._queue: list = []
        self._origins: dict[tuple, tuple[float, float]] = {}
        self._queue_day: int | None = None

    # ------------------------------------------------------------------ fitting

    def _league(self, code: str, country: str = "", tier: int = 0) -> _League:
        league = self.leagues.get(code)
        if league is None:
            league = self.leagues[code] = _League(code, country, tier)
        return league

    def _prior_mean(self, spell, today: int) -> tuple[float, float]:
        if not self.promo or spell is None:
            return 1.0, 1.0
        start, origin, newcomer, direction = spell
        if not newcomer or start < today - self.max_days:
            return 1.0, 1.0
        c_att, c_def = self.c[direction]
        att_old = def_old = 1.0
        if origin is not None:
            att_old, def_old = self._origin_rating(origin)
        return (
            math.exp(c_att + self.beta * math.log(max(att_old, 1e-3))),
            math.exp(c_def + self.beta * math.log(max(def_old, 1e-3))),
        )

    def _origin_rating(self, origin) -> tuple[float, float]:
        """(attack, defence) of a team in the league it left, on the day it left (memoized;
        a plain fit without newcomer priors, so origins never chain)."""
        rating = self._origins.get(origin)
        if rating is None:
            code, left, name = origin
            rating = (1.0, 1.0)
            other = self.leagues.get(code)
            if other is not None:
                fit = self._fit(other, left + 1, "plain")
                idx = other.teams.get(name)
                if fit is not None and idx is not None and idx < len(fit[0]):
                    rating = (float(fit[0][idx]), float(fit[1][idx]))
            self._origins[origin] = rating
        return rating

    def _fit(self, league: _League, today: int, variant: str = "main"):
        """Ratings on rows strictly before `today`. Variants: "main" (supremacy and default
        rates), "total" (its own decay / shrinkage / shot weights for the goal total only),
        "plain" (main settings without newcomer priors, for origin ratings)."""
        slot = (variant, today)
        if slot in league.fits:
            return league.fits[slot]
        days = league.day
        start = bisect_left(days, today - self.max_days)
        stop = bisect_left(days, today)
        n = len(league.teams)
        if stop <= start:
            league.fits[slot] = None
            return None
        half_life, prior, w_sot, w_shots = self.settings[variant]
        d = np.asarray(days[start:stop], dtype=float)
        w = np.exp(-math.log(2) * (today - d) / half_life)
        hi = np.asarray(league.hi[start:stop], dtype=np.int64)
        ai = np.asarray(league.ai[start:stop], dtype=np.int64)
        cols = {k: np.asarray(v[start:stop], dtype=float) for k, v in league.cols.items()}
        ht, at = self._targets(cols, w, w_sot, w_shots)
        m_att, m_def = np.ones(n), np.ones(n)
        if self.promo and variant != "plain":
            for idx, spell in league.spell.items():
                if spell[2] and spell[0] >= today - self.max_days:
                    m_att[idx], m_def[idx] = self._prior_mean(spell, today)
        init = None if variant == "plain" else league.last_fit.get(variant)
        fit = fit_ratings(
            hi,
            ai,
            ht,
            at,
            w,
            n,
            m_att,
            m_def,
            prior=prior,
            iterations=15 if init is not None else 60,
            init=init,
        )
        league.fits[slot] = fit
        if variant != "plain":
            league.last_fit[variant] = fit
        if len(league.fits) > 96:  # keep the cache small
            for old in sorted(league.fits, key=lambda item: item[1])[:-48]:
                del league.fits[old]
        return fit

    @staticmethod
    def _targets(cols: dict, w: np.ndarray, w_sot: float, w_shots: float):
        hg, ag = cols["hg"], cols["ag"]
        if w_sot <= 0 and w_shots <= 0:
            return hg, ag
        goals = hg + ag
        out_h, out_a = hg * 0.0, ag * 0.0
        weight_sum = np.zeros_like(hg)
        for name_h, name_a, weight in (("hst", "ast", w_sot), ("hs", "as", w_shots)):
            if weight <= 0:
                continue
            sh, sa = cols[name_h], cols[name_a]
            ok = ~(np.isnan(sh) | np.isnan(sa))
            if not ok.any():
                continue
            shots = (sh + sa)[ok]
            conv = float((w[ok] * goals[ok]).sum() / max((w[ok] * shots).sum(), 1e-9))
            out_h[ok] += weight * conv * sh[ok]
            out_a[ok] += weight * conv * sa[ok]
            weight_sum[ok] += weight
        return out_h + (1 - weight_sum) * hg, out_a + (1 - weight_sum) * ag

    def _rates(self, league: _League, ctx, today: int):
        fit = self._fit(league, today)
        if fit is None:
            return None
        attack, defence, mu, home_adv = fit

        def team(name):
            idx = league.teams.get(name)
            if idx is not None and idx < len(attack):
                return attack[idx], defence[idx]
            # First match in this league: prior from the previous league, if any.
            spell = self._new_spell(league, name, today)
            return self._prior_mean(spell, today)

        a_h, d_h = team(ctx.home)
        a_a, d_a = team(ctx.away)
        home_rate = mu * home_adv * a_h * d_a
        away_rate = mu * a_a * d_h
        if self.dyn_weight > 0 and league.dyn is not None and league.dyn.n >= 100:
            dyn_h, dyn_a = league.dyn.rates(ctx.home, ctx.away, today, lambda name: team(name))
            w = self.dyn_weight
            home_rate = home_rate ** (1 - w) * dyn_h**w
            away_rate = away_rate ** (1 - w) * dyn_a**w
        if self.sup_scale != 1.0:
            total, base = home_rate + away_rate, math.log(max(home_adv, 1e-6))
            ratio = math.exp(base + self.sup_scale * (math.log(home_rate / away_rate) - base))
            home_rate, away_rate = total * ratio / (1 + ratio), total / (1 + ratio)
        if self.split_total:
            total_fit = self._fit(league, today, "total")
            attack, defence, mu, home_adv = total_fit
            a_h, d_h = team(ctx.home)
            a_a, d_a = team(ctx.away)
            total = mu * home_adv * a_h * d_a + mu * a_a * d_h
            scale = total / (home_rate + away_rate)
            home_rate, away_rate = home_rate * scale, away_rate * scale
        home_rate = min(5.0, max(0.15, home_rate))
        away_rate = min(5.0, max(0.15, away_rate))
        return home_rate, away_rate

    def _new_spell(self, league: _League, name: str, day: int):
        """Spell descriptor for a team starting (now) a spell in `league`."""
        known = self.registry[league.country].get(name) if league.country else None
        newcomer = bool(league.day) and league.day[0] < day - 200
        origin, direction = None, "up"
        if known is not None:
            code, last = known
            if code != league.code and day - last <= ORIGIN_GAP:
                origin = (code, last, name)
                other = self.leagues.get(code)
                if other is not None and other.tier and league.tier and other.tier < league.tier:
                    direction = "down"
        return (day, origin, newcomer, direction)

    # ------------------------------------------------------------------ protocol

    def _probabilities(self, ctx) -> dict[str, float]:
        league = self._league(ctx.league, ctx.country, ctx.tier)
        today = ctx.date.toordinal()
        rates = self._rates(league, ctx, today)
        if rates is None:
            return {}
        home_rate, away_rate = rates
        matrix = score_matrix(home_rate, away_rate, self.rho, self.draw_boost, SIZE)
        odds = ctx.odds or {}
        market = market_1x2(odds) if odds else None
        final = one_x_two(matrix)
        if market is not None and self.market_weight > 0:
            wgt = self.market_weight
            pooled = final ** (1 - wgt) * market**wgt
            final = pooled / pooled.sum()
            matrix = reweight(matrix, final)
        over_market = market_over(odds) if odds else None
        if over_market is not None and self.totals_market_weight > 0:
            wgt = self.totals_market_weight
            pooled = (1 - wgt) * _logit(over_probability(matrix)) + wgt * _logit(over_market)
            target = _sigmoid(pooled)
            scale, matrix = self._fit_total(home_rate, away_rate, final, target)
            home_rate, away_rate = home_rate * scale, away_rate * scale
        output = mk.probabilities("goals", matrix, GOAL_KEYS)
        share = self.ht_share.get(ctx.league)
        if share is not None and share[2] > 0 and share[4] > 0 and share[5] >= 60:
            ht = score_matrix(
                home_rate * share[1] / share[2],
                away_rate * share[3] / share[4],
                self.ht_rho,
                self.ht_draw_boost,
                HT_SIZE,
            )
            output.update(mk.probabilities("ht_goals", ht, HT_KEYS))
        return output

    def _fit_total(self, home_rate, away_rate, target_1x2, target_over):
        def over_at(x):
            m = score_matrix(
                home_rate * math.exp(x), away_rate * math.exp(x), self.rho, self.draw_boost, SIZE
            )
            m = reweight(m, target_1x2)
            return over_probability(m), m

        lo, hi = -1.0, 1.0
        p_lo, m_lo = over_at(lo)
        if p_lo >= target_over:
            return math.exp(lo), m_lo
        p_hi, m_hi = over_at(hi)
        if p_hi <= target_over:
            return math.exp(hi), m_hi
        x, m = 0.0, m_lo
        for _ in range(40):
            x = (lo + hi) / 2
            p, m = over_at(x)
            if abs(p - target_over) < 1e-7:
                break
            if p < target_over:
                lo = x
            else:
                hi = x
        return math.exp(x), m

    def predict(self, ctx) -> dict[str, float]:
        if self._queue:
            self._flush()
        output = self._probabilities(ctx)
        league = self.leagues[ctx.league]
        today = ctx.date.toordinal()
        recent = today - self.max_days
        played = []
        for name in (ctx.home, ctx.away):
            idx = league.teams.get(name)
            days = league.played.get(idx, []) if idx is not None else []
            count = len(days) - bisect_left(days, recent)
            spell = league.spell.get(idx) if idx is not None else None
            if spell is None:
                spell = self._new_spell(league, name, today)
            known_origin = spell[1] is not None and self.promo
            played.append(count if not known_origin else max(count, self.min_team_matches))
        self._sample[ctx.match_id] = (min(played), today)
        if self.online_rule:
            self._pending[ctx.match_id] = output
        return output

    # ------------------------------------------------------------------ selection

    def _allowed(self, ctx, key: str, p: float) -> bool:
        sample = self._sample.get(ctx.match_id)
        if sample is None or sample[0] < self.min_team_matches:
            return False
        return 1.0 / max(p, 1e-9) >= self.min_fair

    def _threshold(self, key: str, target: float, floor: float, today: int) -> float:
        if not self.online_rule:
            return floor
        cached = self._cut.get((key, target))
        if cached is not None:
            return cached
        bins = self._rule.get(key)
        cut = 1.01  # no history: never select
        if bins is not None:
            factor = math.exp(-self.rule_decay * (today - self._rule_day[key]))
            picks = bins[0][::-1].cumsum() * factor
            wins = bins[1][::-1].cumsum() * factor
            ok = (picks >= self.rule_min_picks) & (
                wins >= (target + self.rule_margin) * np.maximum(picks, 1e-9)
            )
            if ok.any():
                # the lowest cut (largest suffix) that is still accurate enough
                j = int(np.nonzero(ok)[0].max())
                cut = 0.70 + 0.005 * (len(bins[0]) - 1 - j)
        cut = max(cut, floor)
        self._cut[(key, target)] = cut
        return cut

    def select(self, ctx, key: str, p: float) -> bool:
        if not self._allowed(ctx, key, p):
            return False
        return p >= self._threshold(key, 0.80, self.threshold, ctx.date.toordinal()) - 1e-12

    def select_high(self, ctx, key: str, p: float) -> bool:
        if not self._allowed(ctx, key, p):
            return False
        return p >= self._threshold(key, 0.85, self.threshold_high, ctx.date.toordinal()) - 1e-12

    def _learn(self, row, prediction: dict[str, float]) -> None:
        day = row.date.toordinal()
        pairs = {}
        for key, p in prediction.items():
            if p < 0.70:
                continue
            market = mk.CATALOGUE[key]
            if not market.selectable:
                continue
            if market.stat not in pairs:
                pairs[market.stat] = mk.stat_pair(row, market.stat)
            pair = pairs[market.stat]
            if pair is None:
                continue
            y = mk.outcome_of(*market.settle_pair(*pair))
            if y is None:
                continue
            bins = self._rule.get(key)
            if bins is None:
                bins = self._rule[key] = [np.zeros(61), np.zeros(61)]
                self._rule_day[key] = day
            last = self._rule_day[key]
            if day > last:
                factor = math.exp(-self.rule_decay * (day - last))
                bins[0] *= factor
                bins[1] *= factor
                self._rule_day[key] = day
            j = min(60, int((p - 0.70) / 0.005 + 1e-9))
            bins[0][j] += 1
            bins[1][j] += y
        self._cut.clear()

    # ------------------------------------------------------------------ update

    def update(self, row) -> None:
        """Queues the row; a date's rows are ingested together once that date is over."""
        day = row.date.toordinal()
        if self._queue and day != self._queue_day:
            self._flush()
        self._queue.append(row)
        self._queue_day = day

    def _flush(self) -> None:
        rows, self._queue = self._queue, []
        if self.online_rule:
            first = data.season_start(self.calib_from)
            for row in rows:
                prediction = self._pending.pop(row.id, None)
                if prediction is None and data.season_start(row.season) >= first:
                    # Rule warm-up on a fed row: the store still holds only earlier dates.
                    prediction = self._self_predict(row)
                if prediction:
                    self._learn(row, prediction)
        for row in rows:
            self._sample.pop(row.id, None)
            self._ingest(row)

    def _ingest(self, row) -> None:
        day = row.date.toordinal()
        league = self._league(row.league, row.country, row.tier)
        names = (row.home, row.away)
        idx = []
        for name in names:
            known = name in league.teams
            i = league.team(name)
            last = league.last_day.get(i)
            if not known or last is None or day - last > SPELL_GAP:
                league.spell[i] = self._new_spell(league, name, day)
            league.last_day[i] = day
            league.played[i].append(day)
            idx.append(i)
            if row.country:
                self.registry[row.country][name] = (row.league, day)
        if self.dyn_weight > 0:
            self._ingest_dynamic(league, row, day)
        league.day.append(day)
        league.hi.append(idx[0])
        league.ai.append(idx[1])
        cols = league.cols
        cols["hg"].append(float(row.home_goals))
        cols["ag"].append(float(row.away_goals))
        cols["hst"].append(_num(row.home_sot))
        cols["ast"].append(_num(row.away_sot))
        cols["hs"].append(_num(row.home_shots))
        cols["as"].append(_num(row.away_shots))
        if row.ht_home_goals is not None and row.ht_away_goals is not None:
            share = self.ht_share.get(row.league)
            if share is None:
                share = self.ht_share[row.league] = [day, 0.0, 0.0, 0.0, 0.0, 0]
            if day > share[0]:
                factor = math.exp(-self.ht_decay * (day - share[0]))
                for k in range(1, 5):
                    share[k] *= factor
                share[0] = day
            share[1] += row.ht_home_goals
            share[2] += row.home_goals
            share[3] += row.ht_away_goals
            share[4] += row.away_goals
            share[5] += 1

    def _ingest_dynamic(self, league: _League, row, day: int) -> None:
        conv = league.conv
        if day > conv[0]:
            factor = 0.5 ** ((day - conv[0]) / 365.0)
            conv[1:] = [v * factor for v in conv[1:]]
            conv[0] = day
        goals_h, goals_a = float(row.home_goals), float(row.away_goals)
        t_h, t_a = goals_h, goals_a
        extra_h = extra_a = used = 0.0
        pairs = (
            (row.home_sot, row.away_sot, self.w_sot, 2),
            (row.home_shots, row.away_shots, self.w_shots, 3),
        )
        for sh, sa, weight, slot in pairs:
            if sh is None or sa is None:
                continue
            conv[slot] += sh + sa
            if weight > 0 and conv[slot] > 0 and conv[1] > 0:
                rate = conv[1] / conv[slot]
                extra_h += weight * rate * sh
                extra_a += weight * rate * sa
                used += weight
        conv[1] += goals_h + goals_a
        t_h = (1 - used) * goals_h + extra_h
        t_a = (1 - used) * goals_a + extra_a
        if league.dyn is None:
            league.dyn = _Dynamic(*self.dyn_args)
        dyn = league.dyn

        def prior(name):
            idx = league.teams.get(name)
            spell = league.spell.get(idx) if idx is not None else None
            return self._prior_mean(spell, day)

        dyn.update(row.home, row.away, day, t_h, t_a, prior)

    def _self_predict(self, row) -> dict[str, float]:
        """Prediction for a fed (not evaluated) row from strictly earlier data (rule warm-up)."""
        from fotbalPrediction.benchmark import make_context

        ctx = make_context(row, None, True)
        return self._probabilities(ctx)


def factory(**params) -> GoalsModel:
    return GoalsModel(**params)
