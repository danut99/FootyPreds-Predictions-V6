"""odds_blend: goal model + margin-free market prices, blended with weights learned walk-forward.

Idea
----
The goal model is the shared baseline (per-league time-decayed Dixon-Coles, untuned V8
defaults). The market is the football-data price of the ``--odds`` source: 1X2, over/under
2.5 and the one listed Asian line. Margins are removed with ``demargin`` (proportional, power,
Shin or additive; power is the default: on the market alone it had the lowest or equal log
loss on 1X2 and over/under 2.5 in 2223/2324, see ``odds_blend_tune demargin``).

Three blends are fitted on EARLIER matches only (walk-forward: refitted every ``refit_days``
on the shadow records of the previous ``window_days``):

- 1X2: multinomial logit ``z_k = a*log pm_k + b*log pq_k + c_k`` (softmax, c_2 = 0), pm the
  model and pq the margin-free market; ``blend="sample"`` adds the interactions with "a team
  has fewer than ``new_games`` league games in the model window" (promoted/new teams);
- over/under 2.5: ``logit p = a*logit pm + b*logit pq + c``;
- the listed Asian line (``ah_blend``): ``logit p = a*logit p_matrix + b*logit pq_ah + c``,
  p_matrix being the line's probability under the matrix after the two blends above.

The score matrix is reweighted to the blended 1X2 and its rates rescaled to the blended
P(over 2.5) (the core ``reweight`` + ``fit_total`` recipe, in numpy), so every other goal market
(double chance, lines 0.5..4.5, team totals, BTTS, DNB, every AH line, exact scores, half time)
follows coherently from one matrix. Without a usable market price the model output passes
through its own walk-forward calibration map (1X2 multinomial and over 2.5 Platt).

Shadow records: ``update`` receives every finished row (burn-in included). From
``learn_first`` on, before a date's rows reach the goal model, the model predicts them exactly
as it would pre-match (reading only the row's PRE-MATCH prices of ``train_source``; the goals
only settle the record afterwards) and stores the model/market inputs, the outcomes and the
final probabilities of the selectable goal and half-time keys, with and without prices. Blend
weights and selection thresholds are fitted only on records with an earlier date.

Modes (``mode``): "blend" (default), "model" (raw goal model, prices ignored), "model_cal"
(the model with its walk-forward calibration only), "market" (margin-free market 1X2 and
over/under 2.5 imposed on the model's matrix shape; the listed AH line from its own price).

Selection (``select`` 80% / ``select_high`` 85%), learned walk-forward on the shadow records
with the same price availability:
- a key is eligible when its own past picks at p >= target reach the target (Wilson lower
  bound, ``z``) on at least ``min_picks`` picks;
- one global threshold t >= target is the smallest one for which the POLICY itself (``policy``
  "one": per match only the eligible key with the longest fair odds, p in [t, p_max]; "all":
  every eligible key in [t, p_max]) reached the target (Wilson lower bound) on past matches.
  Learning t on the policy, not per key, removes the bias of picking the lowest-p key;
- ``scope="priced"`` restricts picks to the keys football-data prices (1X2, double chance
  derived from 1X2, over/under 2.5 and the listed AH line), so every pick has a real-price ROI;
  ``scope="all"`` allows every goal and half-time key (fair odds only for most of them).
Corners, cards, bookings and shots on target come from the baseline count model and are never
selected here: the market carries no information on them.

Honesty: accuracy is always reported with coverage and mean fair odds; no selection is called
profitable without the ROI at real (pre-closing average) prices.

Findings on the TUNE seasons 2223/2324 (22 main leagues; see ``odds_blend_tune``):
- the learned weights are about -0.1 (model) and +1.1 (market) for 1X2 and over/under 2.5:
  the model adds nothing once pre-closing prices are known. The blend beats the power-margin
  market by 0.0004 nats on 1X2 (day-bootstrap 95% CI -0.00075..-0.00012), and almost all of
  that is the market's own recalibration (slight sharpening, draw bias); the model's share is
  -0.00008 (CI includes 0);
- power de-margining beats proportional by about 0.001 nats on 1X2 (favourite-longshot bias);
  it makes no difference on over/under 2.5 or on the Asian line;
- the "sample" (new-team) interaction and per-tier weights did not help;
- every selection ROI on hundreds of bets or more is negative (about -3% at average prices).

Production (FlashScore pre-match prices: best-of-books, earlier and noisier than the
football-data averages): do NOT ship the learned sharpening (b > 1 amplifies price errors).
Use ``learn=False`` with a fixed geometric pool, ``default_market_weight=0.9`` (model 0.1),
on 1X2 and over/under 2.5; use the prices only when all legs are present and the overround
is plausible, and otherwise fall back to the model's calibration map.
"""

from __future__ import annotations

import math
import zlib
from bisect import bisect_left

import numpy as np

from fotbalPrediction import data
from fotbalPrediction import markets as mk
from fotbalPrediction.candidates.baseline import BaselineModel

DEMARGIN_METHODS = ("proportional", "power", "shin", "additive")
MODES = ("blend", "model", "model_cal", "market")
SIZE = mk.GRID["goals"]
HT_SIZE = mk.GRID["ht_goals"]
SELECT_GROUPS = ("1x2", "dc", "goals", "team_goals", "btts", "dnb", "ah", "ht")
GOAL_KEYS = mk.keys_of("goals")
HT_KEYS = mk.keys_of("ht_goals")
TRACK_KEYS = tuple(
    k
    for k in GOAL_KEYS + HT_KEYS
    if mk.CATALOGUE[k].selectable and mk.CATALOGUE[k].group in SELECT_GROUPS
)
TRACK_INDEX = {k: i for i, k in enumerate(TRACK_KEYS)}
_GOAL_TRACK = np.array([TRACK_INDEX.get(k, -1) for k in GOAL_KEYS])
_HT_TRACK = np.array([TRACK_INDEX.get(k, -1) for k in HT_KEYS])
ALWAYS_PRICED = ("1", "X", "2", "1X", "X2", "12", "over25", "under25")
GRID_T = np.round(np.arange(0.60, 0.985, 0.01), 2)
_EPS = 1e-9
_OUTCOME_LIMIT = 21


# --------------------------------------------------------------------------- margin removal


def demargin(prices, method: str = "power") -> list[float] | None:
    """Margin-free probabilities of an exclusive, exhaustive outcome set from decimal prices.

    proportional: q_i / S; power: q_i ** k with sum 1 (Newton on k); additive: q_i - (S-1)/n;
    shin: Shin's model (z by bisection). q_i = 1 / price_i, S = sum q_i. Returns None for
    missing or invalid prices or an implausible book (S outside 0.97..1.5).
    """
    try:
        q = [1.0 / float(p) for p in prices]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if len(q) < 2 or any(not (0.0 < v < 1.0) for v in q):
        return None
    total = sum(q)
    if not 0.97 <= total <= 1.5:
        return None
    if method == "proportional" or abs(total - 1.0) < 1e-12:
        return [v / total for v in q]
    if method == "power":
        logs = [math.log(v) for v in q]
        k = 1.0
        for _ in range(30):
            terms = [math.exp(k * lv) for lv in logs]
            f = sum(terms) - 1.0
            if abs(f) < 1e-13:
                break
            k -= f / sum(lv * t for lv, t in zip(logs, terms))
        p = [math.exp(k * lv) for lv in logs]
        s = sum(p)
        return [v / s for v in p]
    if method == "additive":
        p = [v - (total - 1.0) / len(q) for v in q]
        if min(p) <= 0.0:
            return [v / total for v in q]
        s = sum(p)
        return [v / s for v in p]
    if method == "shin":
        if total < 1.0:
            return [v / total for v in q]

        def shin(z: float) -> list[float]:
            return [
                (math.sqrt(z * z + 4.0 * (1.0 - z) * v * v / total) - z) / (2.0 * (1.0 - z))
                for v in q
            ]

        low, high = 0.0, 0.4
        for _ in range(50):
            mid = 0.5 * (low + high)
            if sum(shin(mid)) > 1.0:
                low = mid
            else:
                high = mid
        p = shin(0.5 * (low + high))
        s = sum(p)
        return [v / s for v in p]
    raise ValueError(f"Metodă necunoscută de eliminare a marjei: {method}")


def _logit(p):
    p = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def wilson_low(hits, n, z: float):
    n = np.maximum(np.asarray(n, dtype=float), 1.0)
    p = np.asarray(hits, dtype=float) / n
    centre = p + z * z / (2 * n)
    margin = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


# --------------------------------------------------------------------------- score matrix (numpy)

_H, _A = np.meshgrid(np.arange(SIZE), np.arange(SIZE), indexing="ij")
_REGION = np.where(_H > _A, 0, np.where(_H == _A, 1, 2)).ravel()
_R = np.stack([(_REGION == r).astype(float) for r in range(3)], axis=1)  # (169, 3)
_OVER25 = ((_H + _A) > 2.5).ravel()
_RO = _R * _OVER25[:, None]
_FACT = np.array([math.factorial(k) for k in range(SIZE)], dtype=float)
_K = np.arange(SIZE, dtype=float)
_SCALE_LIMIT = math.log(4.0)
_W_GOALS, _L_GOALS = mk._weights("goals", SIZE, GOAL_KEYS)
_W_HT, _L_HT = mk._weights("ht_goals", HT_SIZE, HT_KEYS)


def _matrices(home_rates: np.ndarray, away_rates: np.ndarray, rho: float) -> np.ndarray:
    """(B, 169) flattened, normalised Dixon-Coles matrices (clamps as footypreds.engine)."""
    lh = np.maximum(home_rates, 1e-9)[:, None]
    la = np.maximum(away_rates, 1e-9)[:, None]
    ph = np.exp(-lh) * lh**_K / _FACT
    pa = np.exp(-la) * la**_K / _FACT
    m = ph[:, :, None] * pa[:, None, :]
    if rho:
        h, a = lh[:, 0], la[:, 0]
        low = np.maximum(-1 / h, -1 / a)
        high = np.minimum(1 / np.maximum(h * a, _EPS), 1.0)
        r = np.clip(rho, low, high)
        m[:, 0, 0] *= 1 - h * a * r
        m[:, 0, 1] *= 1 + h * r
        m[:, 1, 0] *= 1 + a * r
        m[:, 1, 1] *= 1 - r
    flat = m.reshape(len(m), -1)
    return flat / flat.sum(axis=1, keepdims=True)


def dc_matrix(home_rate: float, away_rate: float, rho: float) -> np.ndarray:
    return _matrices(np.array([home_rate]), np.array([away_rate]), rho)[0]


def one_x_two(flat: np.ndarray) -> np.ndarray:
    return flat @ _R


def reweight(flat: np.ndarray, target) -> np.ndarray:
    current = flat @ _R
    factor = np.where(current > 0, np.asarray(target) / np.maximum(current, 1e-300), 0.0)
    out = flat * factor[_REGION]
    total = out.sum()
    return flat if total <= 0 else out / total


def _over_after(home_rate, away_rate, rho, target_1x2, xs):
    """P(over 2.5) after the 1X2 reweight, for rates x exp(xs) (vectorised)."""
    scales = np.exp(xs)
    flat = _matrices(home_rate * scales, away_rate * scales, rho)
    mass = flat @ _R
    over = flat @ _RO
    return (np.asarray(target_1x2) * over / np.maximum(mass, 1e-300)).sum(axis=1)


def fit_total(home_rate, away_rate, rho, target_1x2, target_over):
    """(scale, flat matrix): rates x scale so that, after the 1X2 reweight, P(over 2.5) equals
    the target (two vectorised bracketing passes, then linear interpolation)."""
    lo, hi = -_SCALE_LIMIT, _SCALE_LIMIT
    for points in (17, 9):
        xs = np.linspace(lo, hi, points)
        f = _over_after(home_rate, away_rate, rho, target_1x2, xs) - target_over
        if f[0] >= 0:
            x = xs[0]
            break
        if f[-1] <= 0:
            x = xs[-1]
            break
        i = int(np.argmax(f > 0))
        lo, hi = xs[i - 1], xs[i]
        f_lo, f_hi = f[i - 1], f[i]
        x = lo - f_lo * (hi - lo) / (f_hi - f_lo)
    scale = math.exp(x)
    flat = reweight(dc_matrix(home_rate * scale, away_rate * scale, rho), target_1x2)
    return scale, flat


def _probabilities(win: np.ndarray, loss: np.ndarray, flat: np.ndarray) -> np.ndarray:
    w, lo = win @ flat, loss @ flat
    decided = w + lo
    return np.where(decided > 1e-12, np.clip(w / np.maximum(decided, 1e-300), 0.0, 1.0), np.nan)


# --------------------------------------------------------------------------- blend fitting


def _fit_multinomial(features: list[np.ndarray], y: np.ndarray, init: np.ndarray, l2: float):
    """Softmax over 3 classes of sum_j theta_j * F_j[:, k] + c_k (c_2 = 0)."""
    from scipy.optimize import minimize

    stack = np.ascontiguousarray(np.stack(features, axis=0))  # (J, n, 3)
    n = len(y)
    onehot = np.zeros((n, 3))
    onehot[np.arange(n), y] = 1.0
    j = stack.shape[0]
    prior = np.zeros_like(init)
    prior[:j] = init[:j]

    def loss(theta):
        z = np.tensordot(theta[:j], stack, axes=1)
        z[:, 0] += theta[j]
        z[:, 1] += theta[j + 1]
        z -= z.max(axis=1, keepdims=True)
        logp = z - np.log(np.exp(z).sum(axis=1, keepdims=True))
        value = -(onehot * logp).sum() / n + l2 * np.sum((theta - prior) ** 2)
        resid = np.exp(logp) - onehot
        grad_w = np.tensordot(stack, resid, axes=([1, 2], [0, 1])) / n
        grad_b = resid[:, :2].sum(axis=0) / n
        return value, np.concatenate([grad_w, grad_b]) + 2 * l2 * (theta - prior)

    return minimize(loss, init, jac=True, method="L-BFGS-B").x


def _fit_binary(features: list[np.ndarray], y: np.ndarray, init: np.ndarray, l2: float):
    """Logistic regression logit p = sum_j theta_j * f_j + c (c last)."""
    from scipy.optimize import minimize

    x = np.stack(features + [np.ones(len(y))], axis=1)
    n = len(y)
    prior = np.zeros_like(init)
    prior[:-1] = init[:-1]

    def loss(theta):
        z = x @ theta
        value = np.mean(np.logaddexp(0.0, z) - y * z) + l2 * np.sum((theta - prior) ** 2)
        grad = x.T @ (_sigmoid(z) - y) / n + 2 * l2 * (theta - prior)
        return value, grad

    return minimize(loss, init, jac=True, method="L-BFGS-B").x


class _Shadow:
    """Append-only columnar store of shadow records (one per finished match)."""

    FIELDS = (
        "day",
        "group",
        "new",
        "y1x2",
        "yover",
        "ah_y",
        "lpm1",
        "lpmX",
        "lpm2",
        "lpq1",
        "lpqX",
        "lpq2",
        "lom",
        "loq",
        "lah_m",
        "lah_q",
        "ah_home_index",
        "ah_away_index",
    )

    def __init__(self):
        self.cols = {name: [] for name in self.FIELDS}
        self.final_odds: list[np.ndarray] = []
        self.final_none: list[np.ndarray] = []
        self.outcomes: list[np.ndarray] = []

    def __len__(self) -> int:
        return len(self.cols["day"])

    def add(self, values: dict, final_odds, final_none, outcomes) -> None:
        for name in self.FIELDS:
            self.cols[name].append(values.get(name, math.nan))
        self.final_odds.append(final_odds)
        self.final_none.append(final_none)
        self.outcomes.append(outcomes)

    def window(self, start_day: int, end_day: int) -> tuple[int, int]:
        day = self.cols["day"]
        return bisect_left(day, start_day), bisect_left(day, end_day)

    def arrays(self, lo: int, hi: int) -> dict:
        return {name: np.asarray(self.cols[name][lo:hi], dtype=float) for name in self.FIELDS}


# --------------------------------------------------------------------------- the model


class OddsBlendModel:
    def __init__(
        self,
        mode: str = "blend",
        demargin: str = "power",
        train_source: str = "avg",
        learn_first: str = "1718",
        window_days: int = 1460,
        refit_days: int = 30,
        min_train: int = 3000,
        blend: str = "basic",
        groups: str = "none",
        new_games: int = 10,
        ah_blend: bool = True,
        l2: float = 1e-4,
        default_market_weight: float = 0.8,
        target: float = 0.80,
        target_high: float = 0.85,
        z: float = 1.0,
        min_picks: int = 150,
        policy: str = "one",
        scope: str = "priced",
        p_max: float = 0.95,
        select_groups: str = ",".join(SELECT_GROUPS),
        min_team_matches: int = 5,
        odds_noise: float = 0.0,
        noise_at: str = "predict",
        counts: bool = True,
        learn: bool = True,
        **base_params,
    ):
        if mode not in MODES:
            raise ValueError(f"mode trebuie să fie unul din {MODES}")
        if demargin not in DEMARGIN_METHODS:
            raise ValueError(f"demargin trebuie să fie unul din {DEMARGIN_METHODS}")
        if train_source not in data.ODDS_SOURCES:
            raise ValueError(f"train_source necunoscut: {train_source}")
        if policy not in ("one", "all"):
            raise ValueError("policy trebuie să fie 'one' sau 'all'")
        if scope not in ("priced", "all"):
            raise ValueError("scope trebuie să fie 'priced' sau 'all'")
        if groups not in ("none", "tier", "top"):
            raise ValueError("groups trebuie să fie none, tier sau top")
        if blend not in ("basic", "sample"):
            raise ValueError("blend trebuie să fie basic sau sample")
        self.base = BaselineModel(min_team_matches=min_team_matches, **base_params)
        self.mode = mode
        self.method = demargin
        self.train_source = train_source
        self.learn_first = data.season_start(str(learn_first))
        self.window_days = window_days
        self.refit_days = refit_days
        self.min_train = min_train
        self.blend = blend
        self.groups = groups
        self.new_games = new_games
        self.ah_blend = ah_blend
        self.l2 = l2
        self.default_market_weight = default_market_weight
        self.targets = (target, target_high)
        self.z = z
        self.min_picks = min_picks
        self.policy = policy
        self.scope = scope
        self.p_max = p_max
        self.min_team_matches = min_team_matches
        self.odds_noise = odds_noise
        if noise_at not in ("predict", "both"):
            raise ValueError("noise_at trebuie să fie predict sau both")
        self.noise_at = noise_at
        self.counts = counts
        self.learn = learn
        wanted = {g.strip() for g in str(select_groups).split(",") if g.strip()}
        self._eligible = np.array([mk.CATALOGUE[k].group in wanted for k in TRACK_KEYS])
        self._always_priced = np.array([k in ALWAYS_PRICED for k in TRACK_KEYS])
        self.shadow = _Shadow()
        self._pending: list = []
        self._pending_day: int | None = None
        self._last_fit: int | None = None
        self.params: dict = {}
        # kind ("odds" | "none") -> ((t80, keys80), (t85, keys85))
        self.rules: dict = {"odds": ((None, np.zeros(0)),) * 2, "none": ((None, np.zeros(0)),) * 2}
        self.history: list[dict] = []
        self._chosen: dict[str, tuple[set, set]] = {}
        self._settle = self._settle_table()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _settle_table() -> np.ndarray:
        """outcome[key, h, a] (1 won, 0 lost, -1 refund) for scores 0..20."""
        table = np.full((len(TRACK_KEYS), _OUTCOME_LIMIT, _OUTCOME_LIMIT), -1, dtype=np.int8)
        for i, key in enumerate(TRACK_KEYS):
            settle_pair = mk.CATALOGUE[key].settle_pair
            for h in range(_OUTCOME_LIMIT):
                for a in range(_OUTCOME_LIMIT):
                    y = mk.outcome_of(*settle_pair(h, a))
                    table[i, h, a] = -1 if y is None else int(y)
        return table

    def _group_of(self, tier: int) -> int:
        if self.groups == "tier":
            return min(int(tier or 1), 3)
        if self.groups == "top":
            return 1 if int(tier or 1) == 1 else 2
        return 0

    def _state(self, league_code: str, home_name: str, away_name: str, today: int):
        """(home rate, away rate, min games) from the goal fit on rows strictly before today."""
        base = self.base
        league = base._league(league_code)
        home = league.teams.get(home_name)
        away = league.teams.get(away_name)
        fit = base._fit(league, "goals", today, 1.35, 1.2)
        if fit is None:
            return None
        recent = today - base.max_days
        days_home = league.played.get(home, [])
        days_away = league.played.get(away, [])
        played_home = len(days_home) - bisect_left(days_home, recent)
        played_away = len(days_away) - bisect_left(days_away, recent)
        home_rate, away_rate = base._expected(fit, home, away)
        home_rate = min(5.0, max(0.15, float(home_rate)))
        away_rate = min(5.0, max(0.15, float(away_rate)))
        return home_rate, away_rate, min(played_home, played_away)

    def _market(self, prices) -> dict:
        """Margin-free market pieces from a {key: price} mapping."""
        out: dict = {}
        if not prices:
            return out
        p = demargin([prices.get("1"), prices.get("X"), prices.get("2")], self.method)
        if p is not None:
            out["1x2"] = np.array(p)
        p = demargin([prices.get("over25"), prices.get("under25")], self.method)
        if p is not None:
            out["over"] = p[0]
        home_key = next((k for k in prices if k.startswith("ah_1_")), None)
        away_key = next((k for k in prices if k.startswith("ah_2_")), None)
        if home_key and away_key and home_key in mk.CATALOGUE and away_key in mk.CATALOGUE:
            p = demargin([prices[home_key], prices[away_key]], self.method)
            if p is not None:
                out["ah"] = (home_key, away_key, p[0])
        return out

    def _noisy(self, prices, match_id: str):
        """Deterministic multiplicative log-normal noise on the prices (robustness test only:
        ``noise_at="predict"`` perturbs only the prices seen at prediction time, like weights
        learned on football-data averages applied to noisier production prices)."""
        if not prices or self.odds_noise <= 0:
            return prices
        rng = np.random.default_rng(zlib.crc32(match_id.encode("utf-8")))
        return {
            k: max(1.01, v * float(np.exp(rng.normal(0.0, self.odds_noise))))
            for k, v in prices.items()
        }

    # ------------------------------------------------------------------ blends

    def _param(self, name: str, group: int):
        found = self.params.get((name, group))
        return self.params.get((name, 0)) if found is None else found

    def _blend_1x2(self, pm: np.ndarray, pq, new: int, group: int) -> np.ndarray:
        if self.mode == "model":
            return pm
        if self.mode == "market" and pq is not None:
            return pq
        lpm = np.log(np.maximum(pm, 1e-9))
        if pq is None or self.mode == "model_cal":
            theta = self._param("cal_1x2", group)
            if theta is None:
                return pm
            z = theta[0] * lpm + np.array([theta[1], theta[2], 0.0])
        else:
            lpq = np.log(np.maximum(pq, 1e-9))
            theta = self._param("blend_1x2", group)
            if theta is None:
                w = self.default_market_weight
                z = (1 - w) * lpm + w * lpq
            elif len(theta) == 6:
                z = (
                    (theta[0] + theta[2] * new) * lpm
                    + (theta[1] + theta[3] * new) * lpq
                    + np.array([theta[4], theta[5], 0.0])
                )
            else:
                z = theta[0] * lpm + theta[1] * lpq + np.array([theta[2], theta[3], 0.0])
        z = z - z.max()
        p = np.exp(z)
        return p / p.sum()

    def _blend_over(self, pm: float, pq, new: int, group: int) -> float:
        if self.mode == "model":
            return pm
        if self.mode == "market" and pq is not None:
            return pq
        lm = float(_logit(pm))
        if pq is None or self.mode == "model_cal":
            theta = self._param("cal_over", group)
            return pm if theta is None else float(_sigmoid(theta[0] * lm + theta[1]))
        lq = float(_logit(pq))
        theta = self._param("blend_over", group)
        if theta is None:
            w = self.default_market_weight
            return float(_sigmoid((1 - w) * lm + w * lq))
        if len(theta) == 5:
            z = (theta[0] + theta[2] * new) * lm + (theta[1] + theta[3] * new) * lq + theta[4]
        else:
            z = theta[0] * lm + theta[1] * lq + theta[2]
        return float(_sigmoid(z))

    def _outputs(self, league_code: str, state, market: dict, group: int):
        """(tracked vector, goal probabilities, ht probabilities, record pieces)."""
        home_rate, away_rate, played = state
        new = 1 if played < self.new_games else 0
        rho = self.base.rho
        m0 = dc_matrix(home_rate, away_rate, rho)
        pm = one_x_two(m0)
        pm_over = float(m0[_OVER25].sum())
        pq = market.get("1x2")
        pq_over = market.get("over")
        final = self._blend_1x2(pm, pq, new, group)
        target_over = self._blend_over(pm_over, pq_over, new, group)
        if abs(target_over - pm_over) > 1e-9 or np.max(np.abs(final - pm)) > 1e-12:
            scale, flat = fit_total(home_rate, away_rate, rho, final, target_over)
        else:
            scale, flat = 1.0, m0
        goals = _probabilities(_W_GOALS, _L_GOALS, flat)
        record = {
            "new": new,
            "lpm1": math.log(max(pm[0], 1e-9)),
            "lpmX": math.log(max(pm[1], 1e-9)),
            "lpm2": math.log(max(pm[2], 1e-9)),
            "lom": float(_logit(pm_over)),
        }
        if pq is not None:
            record["lpq1"], record["lpqX"], record["lpq2"] = np.log(np.maximum(pq, 1e-9))
        if pq_over is not None:
            record["loq"] = float(_logit(pq_over))
        ah = market.get("ah")
        if ah is not None and self.mode in ("blend", "market"):
            home_key, away_key, p_q = ah
            i_home, i_away = GOAL_INDEX[home_key], GOAL_INDEX[away_key]
            p_m = goals[i_home]
            if np.isfinite(p_m):
                record.update(
                    lah_m=float(_logit(p_m)),
                    lah_q=float(_logit(p_q)),
                    ah_home_index=TRACK_INDEX.get(home_key, -1),
                    ah_away_index=TRACK_INDEX.get(away_key, -1),
                    ah_key=home_key,
                )
                p_final = None
                if self.mode == "market":
                    p_final = p_q
                elif self.ah_blend:
                    theta = self._param("blend_ah", group)
                    if theta is not None:
                        z = theta[0] * record["lah_m"] + theta[1] * record["lah_q"] + theta[2]
                        p_final = float(_sigmoid(z))
                if p_final is not None:
                    goals[i_home] = p_final
                    goals[i_away] = 1.0 - p_final
        ht = self._half_time(league_code, (home_rate * scale, away_rate * scale))
        tracked = np.full(len(TRACK_KEYS), np.nan, dtype=np.float32)
        mask = _GOAL_TRACK >= 0
        tracked[_GOAL_TRACK[mask]] = goals[mask]
        if ht is not None:
            mask = _HT_TRACK >= 0
            tracked[_HT_TRACK[mask]] = ht[mask]
        return tracked, goals, ht, record

    def _half_time(self, league_code: str, rates):
        league = self.base._league(league_code)
        first_home, full_home = league.ht_first["home"], league.ht_full["home"]
        first_away, full_away = league.ht_first["away"], league.ht_full["away"]
        if full_home.n < self.base.min_league_matches or full_home.s1 <= 0 or full_away.s1 <= 0:
            return None
        share_home = first_home.s1 / full_home.s1
        share_away = first_away.s1 / full_away.s1
        ht = mk.independent(
            mk.poisson_pmf(rates[0] * share_home, HT_SIZE),
            mk.poisson_pmf(rates[1] * share_away, HT_SIZE),
        )
        return _probabilities(_W_HT, _L_HT, ht.ravel())

    # ------------------------------------------------------------------ walk-forward plumbing

    def _flush(self) -> None:
        """Shadow-predict the pending date's rows, then feed them to the goal model."""
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        day = self._pending_day
        for row in rows:
            if row.extra or data.season_start(row.season) < self.learn_first:
                continue
            state = self._state(row.league, row.home, row.away, day)
            if state is None:
                continue
            group = self._group_of(row.tier)
            market = {}
            if self.mode != "model":
                prices = row.odds.get(self.train_source)
                if self.noise_at == "both":
                    prices = self._noisy(prices, row.id)
                market = self._market(prices)
            tracked, _, _, record = self._outputs(row.league, state, market, group)
            none = tracked
            if market:
                none = self._outputs(row.league, state, {}, group)[0]
            h, a = row.home_goals, row.away_goals
            record["day"] = day
            record["group"] = group
            record["y1x2"] = 0 if h > a else (1 if h == a else 2)
            record["yover"] = 1 if h + a > 2 else 0
            if "ah_key" in record:
                y = mk.outcome_of(*mk.settle_score(record["ah_key"], h, a))
                record["ah_y"] = math.nan if y is None else y
            cap = _OUTCOME_LIMIT - 1
            outcomes = self._settle[:, min(h, cap), min(a, cap)].copy()
            ht = mk.stat_pair(row, "ht_goals")
            ht_rows = _HT_TRACK[_HT_TRACK >= 0]
            if ht is None:
                outcomes[ht_rows] = -2
            else:
                outcomes[ht_rows] = self._settle[ht_rows, min(ht[0], cap), min(ht[1], cap)]
            self.shadow.add(record, tracked, none, outcomes)
        for row in rows:
            self.base.update(row)

    def _maybe_refit(self, today: int) -> None:
        if self._last_fit is not None and today - self._last_fit < self.refit_days:
            return
        lo, hi = self.shadow.window(today - self.window_days, today)
        if hi - lo < self.min_train:
            return
        self._last_fit = today
        arr = self.shadow.arrays(lo, hi)
        fitted = {}
        group_ids = [0] if self.groups == "none" else sorted(set(arr["group"].astype(int)))
        for group in group_ids:
            mask = np.ones(hi - lo, dtype=bool) if group == 0 else arr["group"] == group
            if mask.sum() >= self.min_train:
                fitted.update(self._fit_group(arr, mask, group))
        self.params.update(fitted)
        self._fit_rules(arr, lo, hi)
        self.history.append(
            {
                "day": today,
                "n": hi - lo,
                **{f"{k[0]}@{k[1]}": np.round(v, 4).tolist() for k, v in fitted.items()},
                "rules": {
                    kind: [
                        (t, int(len(keys))) for t, keys in ((r[0], r[1]) for r in self.rules[kind])
                    ]
                    for kind in self.rules
                },
            }
        )

    def _init(self, name: str, group: int, default: np.ndarray) -> np.ndarray:
        previous = self.params.get((name, group))
        return default if previous is None or len(previous) != len(default) else previous

    def _fit_group(self, arr: dict, rows: np.ndarray, group: int) -> dict:
        out = {}
        lpm = np.stack([arr["lpm1"], arr["lpmX"], arr["lpm2"]], axis=1)
        lpq = np.stack([arr["lpq1"], arr["lpqX"], arr["lpq2"]], axis=1)
        y = arr["y1x2"].astype(int)
        new = arr["new"]
        out[("cal_1x2", group)] = _fit_multinomial(
            [lpm[rows]], y[rows], self._init("cal_1x2", group, np.array([1.0, 0.0, 0.0])), self.l2
        )
        out[("cal_over", group)] = _fit_binary(
            [arr["lom"][rows]],
            arr["yover"][rows],
            self._init("cal_over", group, np.array([1.0, 0.0])),
            self.l2,
        )
        if not self.learn:
            # Fixed geometric pool (default_market_weight): only the model-only maps are learned.
            return out
        has = rows & np.isfinite(lpq).all(axis=1)
        if has.sum() >= self.min_train:
            feats = [lpm[has], lpq[has]]
            default = np.array([0.2, 0.8, 0.0, 0.0])
            if self.blend == "sample":
                nn = new[has][:, None]
                feats += [nn * lpm[has], nn * lpq[has]]
                default = np.array([0.2, 0.8, 0.0, 0.0, 0.0, 0.0])
            out[("blend_1x2", group)] = _fit_multinomial(
                feats, y[has], self._init("blend_1x2", group, default), self.l2
            )
        has = rows & np.isfinite(arr["loq"])
        if has.sum() >= self.min_train:
            lm, lq = arr["lom"][has], arr["loq"][has]
            feats, default = [lm, lq], np.array([0.2, 0.8, 0.0])
            if self.blend == "sample":
                nn = new[has]
                feats += [nn * lm, nn * lq]
                default = np.array([0.2, 0.8, 0.0, 0.0, 0.0])
            out[("blend_over", group)] = _fit_binary(
                feats, arr["yover"][has], self._init("blend_over", group, default), self.l2
            )
        has = rows & np.isfinite(arr["lah_q"]) & np.isfinite(arr["ah_y"])
        if self.ah_blend and has.sum() >= self.min_train:
            out[("blend_ah", group)] = _fit_binary(
                [arr["lah_m"][has], arr["lah_q"][has]],
                arr["ah_y"][has],
                self._init("blend_ah", group, np.array([0.2, 0.8, 0.0])),
                self.l2,
            )
        return out

    def _scope_mask(self, home_index: np.ndarray, away_index: np.ndarray) -> np.ndarray:
        """(n, K) keys that may be picked under the scope (priced: real football-data prices)."""
        n = len(home_index)
        if self.scope == "all":
            return np.ones((n, len(TRACK_KEYS)), dtype=bool)
        mask = np.zeros((n, len(TRACK_KEYS)), dtype=bool)
        mask[:, self._always_priced] = True
        for index in (home_index, away_index):
            ok = np.isfinite(index) & (index >= 0)
            mask[np.nonzero(ok)[0], index[ok].astype(int)] = True
        return mask

    def _policy(self, p: np.ndarray, allowed: np.ndarray, t: float):
        """(row index, key index) of the picks of the policy at global threshold t."""
        ok = allowed & (p >= t - 1e-9) & (p <= self.p_max + 1e-9)
        if self.policy == "all":
            return np.nonzero(ok)
        rows = np.nonzero(ok.any(axis=1))[0]
        masked = np.where(ok[rows], p[rows], np.inf)
        return rows, masked.argmin(axis=1)

    def _fit_rules(self, arr: dict, lo: int, hi: int) -> None:
        outcomes = np.stack(self.shadow.outcomes[lo:hi]).astype(np.int16)
        scope = self._scope_mask(arr["ah_home_index"], arr["ah_away_index"])
        has_odds = np.isfinite(arr["lpq1"])
        for kind, finals in (("odds", self.shadow.final_odds), ("none", self.shadow.final_none)):
            p = np.stack(finals[lo:hi]).astype(float)
            if kind == "odds":
                p, y, sc = p[has_odds], outcomes[has_odds], scope[has_odds]
            else:
                y = outcomes
                sc = self._scope_mask(np.full(len(p), -1.0), np.full(len(p), -1.0))
            valid = np.isfinite(p) & (y >= 0)
            p = np.where(valid, p, -1.0)
            rules = []
            for target in self.targets:
                chosen = p >= target - 1e-9
                n_key = chosen.sum(axis=0)
                hits_key = (chosen & (y == 1)).sum(axis=0)
                key_ok = (
                    self._eligible
                    & (n_key >= self.min_picks)
                    & (wilson_low(hits_key, n_key, self.z) >= target)
                )
                allowed = sc & key_ok[None, :] & valid
                best = None
                for t in GRID_T[GRID_T >= target - 1e-9]:
                    rows, keys = self._policy(p, allowed, t)
                    n = len(rows)
                    if n < self.min_picks:
                        break
                    hits = int((y[rows, keys] == 1).sum())
                    if wilson_low(hits, n, self.z) >= target:
                        best = float(t)
                        break
                rules.append((best, np.nonzero(key_ok)[0]))
            self.rules[kind] = tuple(rules)

    # ------------------------------------------------------------------ protocol

    def predict(self, ctx) -> dict[str, float]:
        today = ctx.date.toordinal()
        if self._pending and self._pending_day is not None and today > self._pending_day:
            self._flush()
        self._maybe_refit(today)
        output: dict[str, float] = {}
        if self.counts:
            # Corners, cards, bookings, shots on target: the baseline count model, no prices.
            fields = {name: getattr(ctx, name) for name in ctx.__dataclass_fields__}
            base_out = self.base.predict(ctx.__class__(**{**fields, "odds": None}))
            for key, p in base_out.items():
                if mk.CATALOGUE[key].stat not in ("goals", "ht_goals"):
                    output[key] = p
        state = self._state(ctx.league, ctx.home, ctx.away, today)
        if state is None:
            return output
        group = self._group_of(ctx.tier)
        market = {}
        if ctx.odds and self.mode != "model":
            market = self._market(self._noisy(dict(ctx.odds), ctx.match_id))
        tracked, goals, ht, record = self._outputs(ctx.league, state, market, group)
        for key, p in zip(GOAL_KEYS, goals):
            if np.isfinite(p):
                output[key] = float(p)
        if ht is not None:
            for key, p in zip(HT_KEYS, ht):
                if np.isfinite(p):
                    output[key] = float(p)
        kind = "odds" if "lpq1" in record else "none"
        enough = kind == "odds" or state[2] >= self.min_team_matches
        picks = (set(), set())
        if enough:
            scope = self._scope_mask(
                np.array([record.get("ah_home_index", -1.0)], dtype=float),
                np.array([record.get("ah_away_index", -1.0)], dtype=float),
            )[0]
            if kind == "none" and self.scope == "priced":
                scope = self._always_priced.copy()
            picks = tuple(self._pick(tracked, scope, rule) for rule in self.rules[kind])
        self._chosen[ctx.match_id] = picks
        return output

    def _pick(self, tracked: np.ndarray, scope: np.ndarray, rule) -> set:
        t, keys = rule
        if t is None or len(keys) == 0:
            return set()
        allowed = np.zeros(len(TRACK_KEYS), dtype=bool)
        allowed[keys] = True
        allowed &= scope & np.isfinite(tracked)
        p = np.where(allowed, tracked.astype(float), -1.0)
        rows, chosen = self._policy(p[None, :], allowed[None, :], t)
        return {TRACK_KEYS[int(k)] for k in chosen}

    def select(self, ctx, key: str, p: float) -> bool:
        return key in self._chosen.get(ctx.match_id, (set(), set()))[0]

    def select_high(self, ctx, key: str, p: float) -> bool:
        return key in self._chosen.get(ctx.match_id, (set(), set()))[1]

    def update(self, row) -> None:
        day = row.date.toordinal()
        if self._pending_day is not None and day != self._pending_day:
            self._flush()
        self._pending_day = day
        self._pending.append(row)
        self._chosen.pop(row.id, None)


GOAL_INDEX = {k: i for i, k in enumerate(GOAL_KEYS)}


def factory(**params) -> OddsBlendModel:
    return OddsBlendModel(**params)
