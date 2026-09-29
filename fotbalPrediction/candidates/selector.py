"""Selector candidate: online calibration + per-market selection rules on top of a base model.

The base model (default: ``fotbalPrediction.candidates.baseline``) prices every market. This
wrapper does three things, all walk-forward and leak-free:

1. Out-of-sample stream. Every finished match that reaches ``update`` is paired with the base
   prediction made BEFORE any row of its date was fed: the benchmark's own predictions in the
   evaluated seasons, and "shadow" predictions (``base.predict`` on the pre-match context of
   the row, made before the date is fed) for the burn-in seasons from ``shadow_from``. The
   stream is kept as exponentially decayed histograms (``half_life`` days), so memory and
   refit cost do not grow with time.

2. Calibration. Per market "pair" (a key and its exact complement, e.g. over25/under25,
   1/X2, ah_1_-0.75/ah_2_+0.75, corners_ah_1_-1.5/corners_ah_2_+1.5) ONE map is fitted on the
   primary key and the complement is 1 - p (symmetrised). ``method``: "platt" (logistic on
   logit p) or "beta" (logistic on ln p and ln(1 - p)), ridge-shrunk toward the identity, plus
   an optional per-league intercept shrunk toward 0 (``league_offsets``) and a per history
   bucket map shrunk toward the pooled one (``bucket_calibration``, see 3). 1X2 and HT 1X2 are
   renormalised after calibration; double chance is the complement of the opposite outcome.
   Exact scores are not calibrated. Refit every ``refit_days`` days.

3. Selection. For every key and "history bucket" (0 = both teams have >= ``early_matches``
   league matches this season; 1 = early season; 2 = a team with < ``min_team`` league
   matches in the last 730 days, i.e. promoted/new) the stream of ISSUED (calibrated)
   probabilities gives the lowest threshold whose picks above it reach ``target`` with a
   Wilson lower bound (``z``) and at least ``min_picks`` effective picks. A pick needs
   p >= max(threshold, ``floor``). ``league_veto`` additionally drops a (key, league) whose own
   stream accuracy above the threshold is clearly (Wilson upper bound, ``veto_z``) below the
   target. ``select_high`` is the same with ``target_high`` / ``floor_high``.
   ``min_fair_odds`` (default 1.0 = off) refuses picks priced shorter than that fair price;
   ``agree_slack`` (odds runs only) also needs the margin-free market probability of a priced
   key to be >= threshold - slack; ``league_thresholds`` lets a league with its own long
   stream use its own (lower) threshold.

   The floors (0.80 / 0.85) are deliberate: every selected pick has a calibrated chance of at
   least the target, so any subset (one pick per match, the longest odds, a ticket) keeps the
   target in expectation. With ``floor=0`` the rule becomes the pure "largest prefix" rule:
   more picks per key, but its marginal picks sit near 70-75% and a one-pick-per-match subset
   fell to ~73% accuracy on 2223.

Frozen on 2223 only (see ``selector_tune``): z=2, min_picks=300, floors 0.80/0.85, league
veto, history buckets, Platt + league offsets + bucket maps, half-life 365 days, shadow
stream from 1920.

Nothing here reads a result before it is final: histograms get a match only in ``update``
(after every prediction of its date), and the thresholds / maps used for a date were fitted
on earlier dates only.

Safe markets have short odds: report accuracy WITH coverage and the mean fair odds; only the
1X2, over/under 2.5 and the listed Asian line have real prices (ROI).
"""

from __future__ import annotations

import importlib
from bisect import bisect_left

import numpy as np

from fotbalPrediction import data
from fotbalPrediction import markets as mk

KEYS = mk.KEYS
K = len(KEYS)
INDEX = {key: i for i, key in enumerate(KEYS)}
SELECTABLE = np.array([mk.CATALOGUE[k].selectable for k in KEYS], dtype=bool)
LEAGUES = tuple(data.ALL_LEAGUES)
LEAGUE_INDEX = {code: i for i, code in enumerate(LEAGUES)}
L = len(LEAGUES)
B_CAL = 320  # logit bins on [-LOGIT_MAX, LOGIT_MAX]
LOGIT_MAX = 8.0
B_SEL = 200  # issued-probability bins on [0, 1]
N_BUCKETS = 3
RECENT_DAYS = 730
EPS = 1e-6


# --------------------------------------------------------------------------- key structure


def _stat_tables() -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """stat -> (key indices, win, loss, outcome) on the stat's grid (outcome 1/0/-1 refund)."""
    tables = {}
    for stat in mk.STATS:
        keys = mk.keys_of(stat)
        size = mk.GRID[stat]
        win = np.zeros((len(keys), size * size))
        loss = np.zeros((len(keys), size * size))
        for r, key in enumerate(keys):
            settle = mk.CATALOGUE[key].settle_pair
            for h in range(size):
                for a in range(size):
                    win[r, h * size + a], loss[r, h * size + a] = settle(h, a)
        outcome = np.where(win + loss <= 1e-12, -1, np.where(win > loss, 1, 0)).astype(np.int8)
        index = np.array([INDEX[k] for k in keys], dtype=np.int64)
        tables[stat] = (index, win, loss, outcome)
    return tables


TABLES = _stat_tables()


def _complements() -> np.ndarray:
    """complement[k] = index of the key whose settlement is k's with win and loss swapped."""
    complement = np.full(K, -1, dtype=np.int64)
    for index, win, loss, _ in TABLES.values():
        n = len(index)
        for i in range(n):
            if complement[index[i]] >= 0:
                continue
            for j in range(i + 1, n):
                if complement[index[j]] >= 0:
                    continue
                if np.allclose(win[i], loss[j]) and np.allclose(loss[i], win[j]):
                    complement[index[i]] = index[j]
                    complement[index[j]] = index[i]
                    break
    return complement


COMPLEMENT = _complements()
# Primary keys: one per complementary pair (the first in catalogue order) and every unpaired key.
PRIMARY = np.array([k for k in range(K) if COMPLEMENT[k] < 0 or k < COMPLEMENT[k]])
P = len(PRIMARY)
PRIMARY_OF = np.empty(K, dtype=np.int64)  # key -> position in PRIMARY
FLIP = np.zeros(K, dtype=bool)  # key is the complement of its primary
for _pos, _key in enumerate(PRIMARY):
    PRIMARY_OF[_key] = _pos
    if COMPLEMENT[_key] >= 0:
        PRIMARY_OF[COMPLEMENT[_key]] = _pos
        FLIP[COMPLEMENT[_key]] = True
NO_CALIBRATION = np.array(
    [mk.CATALOGUE[KEYS[k]].question == "cs" for k in PRIMARY], dtype=bool
)  # exact scores stay as the base priced them
TRIPLES = tuple(
    np.array([PRIMARY_OF[INDEX[k]] for k in mk.QUESTIONS[q]])
    for q in ("1x2", "ht_1x2")
    if all(INDEX[k] == PRIMARY[PRIMARY_OF[INDEX[k]]] for k in mk.QUESTIONS[q])
)


def outcomes(row) -> np.ndarray:
    """Outcome of every key on a finished row: 1 won, 0 lost, -1 refund or missing data."""
    y = np.full(K, -1, dtype=np.int8)
    for stat, (index, win, loss, outcome) in TABLES.items():
        pair = mk.stat_pair(row, stat)
        if pair is None:
            continue
        h, a = pair
        size = mk.GRID[stat]
        if h < size and a < size:
            y[index] = outcome[:, h * size + a]
        else:
            for r, k in enumerate(index):
                w, lo = mk.CATALOGUE[KEYS[k]].settle_pair(h, a)
                y[k] = -1 if w + lo <= 1e-12 else (1 if w > lo else 0)
    return y


def market_probabilities(odds) -> dict[int, float]:
    """Margin-free market probabilities of the priced keys (1X2 + double chance, O/U 2.5 and
    the listed Asian line), by proportional normalisation of the decimal prices."""
    output: dict[int, float] = {}
    if all(odds.get(k) for k in ("1", "X", "2")):
        inverse = {k: 1.0 / odds[k] for k in ("1", "X", "2")}
        total = sum(inverse.values())
        p = {k: v / total for k, v in inverse.items()}
        for key, value in (*p.items(), ("1X", p["1"] + p["X"]), ("X2", p["X"] + p["2"])):
            output[INDEX[key]] = value
        output[INDEX["12"]] = p["1"] + p["2"]
    pairs = [("over25", "under25")]
    pairs += [
        (key, KEYS[COMPLEMENT[INDEX[key]]])
        for key in odds
        if key.startswith("ah_1_") and key in INDEX and COMPLEMENT[INDEX[key]] >= 0
    ]
    for a, b in pairs:
        if odds.get(a) and odds.get(b):
            total = 1.0 / odds[a] + 1.0 / odds[b]
            output[INDEX[a]] = 1.0 / odds[a] / total
            output[INDEX[b]] = 1.0 / odds[b] / total
    return output


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1.0 - EPS)
    return np.log(p) - np.log1p(-p)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -40.0, 40.0)))


def _features(x: np.ndarray, method: str) -> np.ndarray:
    """Calibration features of a logit array (..., d)."""
    if method == "platt":
        return np.stack([x, np.ones_like(x)], axis=-1)
    if method == "beta":
        # ln p and -ln(1 - p): identity when both coefficients are 1.
        log_p = -np.logaddexp(0.0, -x)
        neg_log_q = np.logaddexp(0.0, x)
        return np.stack([log_p, neg_log_q, np.ones_like(x)], axis=-1)
    raise ValueError(f"Metodă de calibrare necunoscută: {method}")


def _identity(method: str) -> np.ndarray:
    return np.array([1.0, 0.0]) if method == "platt" else np.array([1.0, 1.0, 0.0])


def _tail_sums(values: np.ndarray) -> np.ndarray:
    """Sum of the bins at or above each bin (along the last axis)."""
    return np.cumsum(values[..., ::-1], axis=-1)[..., ::-1]


def wilson_bound(acc: np.ndarray, n: np.ndarray, z: float, upper: bool = False) -> np.ndarray:
    n = np.maximum(n, 1e-9)
    centre = acc + z * z / (2 * n)
    spread = z * np.sqrt(np.maximum(acc * (1 - acc) / n + z * z / (4 * n * n), 0.0))
    value = (centre + spread) if upper else (centre - spread)
    return value / (1 + z * z / n)


# --------------------------------------------------------------------------- model


class SelectorModel:
    def __init__(
        self,
        base: str = "fotbalPrediction.candidates.baseline:factory",
        base_params: dict | None = None,
        method: str = "platt",
        half_life: float = 365.0,
        calib_min: float = 300.0,
        ridge: float = 20.0,
        league_offsets: bool = True,
        league_ridge: float = 150.0,
        bucket_calibration: bool = True,
        bucket_ridge: float = 300.0,
        refit_days: int = 7,
        shadow_from: str = "1920",
        shadow_odds: str | None = None,
        target: float = 0.80,
        target_high: float = 0.85,
        z: float = 2.0,
        min_picks: float = 300.0,
        floor: float = 0.80,
        floor_high: float = 0.85,
        early_matches: int = 6,
        min_team: int = 5,
        buckets: bool = True,
        refuse_new: bool = False,
        league_veto: bool = True,
        league_thresholds: bool = False,
        thin_floor: float | None = None,
        veto_z: float = 1.0,
        veto_min: float = 30.0,
        min_fair_odds: float = 1.0,
        calibrate: bool = True,
        agree_slack: float | None = None,
        log_stream: bool = False,
        **base_kwargs,
    ):
        module_name, _, attribute = base.partition(":")
        factory = getattr(importlib.import_module(module_name), attribute or "factory")
        self.base = factory(**{**(base_params or {}), **base_kwargs})
        self.method = method
        self.half_life = float(half_life)
        self.calib_min = calib_min
        self.ridge = ridge
        self.league_offsets = league_offsets
        self.league_ridge = league_ridge
        self.bucket_calibration = bucket_calibration
        self.bucket_ridge = bucket_ridge
        self.refit_days = refit_days
        self.shadow_start = data.season_start(shadow_from) if shadow_from else None
        self.shadow_odds = shadow_odds
        self.targets = (target, target_high)
        self.floors = (floor, floor_high)
        self.z = z
        self.min_picks = min_picks
        self.early_matches = early_matches
        self.min_team = min_team
        self.buckets = buckets
        self.refuse_new = refuse_new
        self.use_veto = league_veto
        self.veto_z = veto_z
        self.veto_min = veto_min
        self.max_p = 1.0 / min_fair_odds if min_fair_odds > 1.0 else 1.0 + 1e-9
        self.calibrate = calibrate
        self.agree_slack = agree_slack
        self.league_thresholds = league_thresholds
        self.thin_floor = thin_floor
        # Optional log of the issued stream (tuning only: replays of the selection rule).
        self.stream_log: list | None = [] if log_stream else None
        self.evaluated: set[str] = set()

        dim = len(_identity(method))
        self.theta = np.tile(_identity(method), (P, 1))
        self.theta_bucket = np.tile(_identity(method), (P, N_BUCKETS, 1))
        self.offset = np.zeros((P, L))
        # Calibration stream (primary keys): decayed weight, hits, sum of logits per bin.
        self.cal_w = np.zeros((P, L, B_CAL))
        self.cal_h = np.zeros((P, L, B_CAL))
        self.cal_x = np.zeros((P, L, B_CAL))
        self.cal_bw = np.zeros((P, N_BUCKETS, B_CAL))  # the same stream per history bucket
        self.cal_bh = np.zeros((P, N_BUCKETS, B_CAL))
        self.cal_bx = np.zeros((P, N_BUCKETS, B_CAL))
        # Selection stream (every key): issued probability bins per bucket and per league.
        self.sel_w = np.zeros((K, N_BUCKETS, B_SEL))
        self.sel_h = np.zeros((K, N_BUCKETS, B_SEL))
        self.lg_w = np.zeros((K, L, B_SEL))
        self.lg_h = np.zeros((K, L, B_SEL))
        self.threshold = np.full((2, K, N_BUCKETS), np.inf)
        self.veto = np.zeros((2, K, L), dtype=bool)
        self.league_threshold = np.full((2, K, L), np.inf)
        self.thin = np.zeros((2, K, N_BUCKETS), dtype=bool)
        self._dim = dim
        self.day0: int | None = None
        self.last_refit: int | None = None
        self.buffer: list = []
        self.buffer_day = None
        self.pending: dict[str, tuple] = {}
        self.chosen: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.played: dict[tuple[str, str], list[int]] = {}
        self.season_played: dict[tuple[str, str, str], int] = {}

    # ------------------------------------------------------------------ helpers

    def _weight(self, day: int) -> float:
        if self.day0 is None:
            self.day0 = day
        return 2.0 ** ((day - self.day0) / self.half_life)

    def _bucket(self, league: str, season: str, home: str, away: str, day: int) -> int:
        recent = day - RECENT_DAYS
        counts = []
        for team in (home, away):
            days = self.played.get((league, team), [])
            counts.append(len(days) - bisect_left(days, recent))
        if min(counts) < self.min_team:
            return 2
        if not self.buckets:
            return 0
        season_counts = [self.season_played.get((league, season, t), 0) for t in (home, away)]
        return 1 if min(season_counts) < self.early_matches else 0

    def _raw_vector(self, prediction: dict) -> np.ndarray:
        raw = np.full(K, np.nan)
        for key, value in prediction.items():
            raw[INDEX[key]] = value
        return raw

    def _calibrated(self, raw: np.ndarray, league: int, bucket: int = 0) -> np.ndarray:
        """Issued probability of every key (NaN where the base gave none)."""
        primary = raw[PRIMARY]
        other = COMPLEMENT[PRIMARY]
        missing = np.isnan(primary) & (other >= 0)
        if missing.any():
            primary = primary.copy()
            primary[missing] = 1.0 - raw[other[missing]]
        present = ~np.isnan(primary)
        q = primary.copy()
        if self.calibrate:
            x = _logit(np.where(present, primary, 0.5))
            theta = self.theta_bucket[:, bucket] if self.bucket_calibration else self.theta
            z = np.einsum("pd,pd->p", _features(x, self.method), theta)
            if self.league_offsets and league >= 0:
                z = z + self.offset[:, league]
            fitted = _sigmoid(z)
            use = present & ~NO_CALIBRATION
            q[use] = fitted[use]
            for triple in TRIPLES:
                values = q[triple]
                if not np.isnan(values).any():
                    q[triple] = values / values.sum()
        issued = np.where(FLIP, 1.0 - q[PRIMARY_OF], q[PRIMARY_OF])
        return np.where(np.isnan(raw) & np.isnan(issued), np.nan, issued)

    # ------------------------------------------------------------------ protocol

    def predict(self, ctx) -> dict[str, float]:
        day = ctx.date.toordinal()
        if self.buffer and self.buffer_day is not None and day > self.buffer_day:
            self._flush()
        prediction = self.base.predict(ctx)
        league = LEAGUE_INDEX.get(ctx.league, -1)
        bucket = self._bucket(ctx.league, ctx.season, ctx.home, ctx.away, day)
        raw = self._raw_vector(prediction)
        issued = self._calibrated(raw, league, bucket)
        self.pending[ctx.match_id] = (raw, issued, bucket, league)
        if self.stream_log is not None:
            self.evaluated.add(ctx.match_id)
        market = None
        if self.agree_slack is not None and ctx.odds:
            market = np.full(K, np.nan)
            for k, value in market_probabilities(ctx.odds).items():
                market[k] = value
        self.chosen[ctx.match_id] = (
            self.choose(0, issued, bucket, league, market),
            self.choose(1, issued, bucket, league, market),
        )
        output = {}
        for key in prediction:
            value = float(issued[INDEX[key]])
            output[key] = min(1.0, max(0.0, value))
        return output

    def choose(self, mode: int, p: np.ndarray, bucket: int, league: int, market=None):
        """Selection rule over the whole key vector ``p`` (NaN = no prediction).

        mode 0 = ``select`` (target), 1 = ``select_high`` (target_high).
        """
        if bucket == 2 and self.refuse_new:
            return np.zeros(K, dtype=bool)
        floor = self.floors[mode]
        threshold = np.maximum(self.threshold[mode, :, bucket], floor)
        if self.thin_floor is not None:
            # Too few past picks above the floor to test the key: calibration alone, stricter.
            thin = self.thin[mode, :, bucket]
            threshold = np.where(thin, max(self.thin_floor, floor), threshold)
        if league >= 0:
            vetoed = self.veto[mode, :, league] if self.use_veto else np.zeros(K, dtype=bool)
            if self.league_thresholds:
                own = np.maximum(self.league_threshold[mode, :, league], floor)
                relaxed = np.minimum(threshold, own) if bucket == 0 else threshold
                threshold = np.where(vetoed, np.maximum(own, threshold), relaxed)
            else:
                threshold = np.where(vetoed, np.inf, threshold)
        with np.errstate(invalid="ignore"):
            chosen = SELECTABLE & (p >= threshold - 1e-12) & (p <= self.max_p)
            if market is not None:
                chosen &= np.isnan(market) | (market >= threshold - self.agree_slack)
        return chosen

    def select(self, ctx, key: str, p: float) -> bool:
        chosen = self.chosen.get(ctx.match_id)
        return bool(chosen[0][INDEX[key]]) if chosen is not None else False

    def select_high(self, ctx, key: str, p: float) -> bool:
        chosen = self.chosen.get(ctx.match_id)
        return bool(chosen[1][INDEX[key]]) if chosen is not None else False

    def update(self, row) -> None:
        day = row.date.toordinal()
        if self.buffer and day > self.buffer_day:
            self._flush()
        self.buffer.append(row)
        self.buffer_day = day

    # ------------------------------------------------------------------ stream

    def _shadow(self, row, day: int):
        from fotbalPrediction.benchmark import make_context

        ctx = make_context(row, self.shadow_odds)
        prediction = self.base.predict(ctx)
        league = LEAGUE_INDEX.get(row.league, -1)
        bucket = self._bucket(row.league, row.season, row.home, row.away, day)
        raw = self._raw_vector(prediction)
        return raw, self._calibrated(raw, league, bucket), bucket, league

    def _flush(self) -> None:
        rows, self.buffer = self.buffer, []
        day = self.buffer_day
        # 1) predictions of the date, all made before any row of the date is fed.
        stream = []
        for row in rows:
            item = self.pending.pop(row.id, None)
            if item is None and self.shadow_start is not None:
                if data.season_start(row.season) >= self.shadow_start:
                    item = self._shadow(row, day)
            self.chosen.pop(row.id, None)
            if item is not None:
                stream.append((row, item))
        # 2) the results of the date enter the stream.
        if stream:
            weight = self._weight(day)
            for row, (raw, issued, bucket, league) in stream:
                y = outcomes(row)
                self._add(y, raw, issued, bucket, league, weight)
                if self.stream_log is not None:
                    evaluated = row.id in self.evaluated
                    self.stream_log.append(
                        (day, row.season, league, bucket, evaluated, issued.astype(np.float32), y)
                    )
        # 3) the rows reach the base model and the history counters.
        for row in rows:
            self.base.update(row)
            for team in (row.home, row.away):
                self.played.setdefault((row.league, team), []).append(day)
                key = (row.league, row.season, team)
                self.season_played[key] = self.season_played.get(key, 0) + 1
        if stream and (self.last_refit is None or day - self.last_refit >= self.refit_days):
            self._refit(day)
            self.last_refit = day

    def _add(self, y, raw, issued, bucket, league, weight) -> None:
        if league < 0:
            return
        # Calibration: primary raw probability and its outcome.
        primary_raw = raw[PRIMARY]
        other = COMPLEMENT[PRIMARY]
        y_primary = y[PRIMARY].astype(np.int64)
        missing = np.isnan(primary_raw) & (other >= 0)
        if missing.any():
            primary_raw = primary_raw.copy()
            primary_raw[missing] = 1.0 - raw[other[missing]]
        ok = ~np.isnan(primary_raw) & (y_primary >= 0)
        if ok.any():
            pos = np.nonzero(ok)[0]
            x = np.clip(_logit(primary_raw[pos]), -LOGIT_MAX, LOGIT_MAX)
            bins = np.minimum(B_CAL - 1, ((x + LOGIT_MAX) / (2 * LOGIT_MAX) * B_CAL).astype(int))
            self.cal_w[pos, league, bins] += weight
            self.cal_h[pos, league, bins] += weight * y_primary[pos]
            self.cal_x[pos, league, bins] += weight * x
            self.cal_bw[pos, bucket, bins] += weight
            self.cal_bh[pos, bucket, bins] += weight * y_primary[pos]
            self.cal_bx[pos, bucket, bins] += weight * x
        # Selection: issued probability of every key and its outcome.
        ok = ~np.isnan(issued) & (y >= 0)
        if ok.any():
            keys = np.nonzero(ok)[0]
            bins = np.minimum(B_SEL - 1, (issued[keys] * B_SEL).astype(int))
            hits = y[keys].astype(float)
            self.sel_w[keys, bucket, bins] += weight
            self.sel_h[keys, bucket, bins] += weight * hits
            self.lg_w[keys, league, bins] += weight
            self.lg_h[keys, league, bins] += weight * hits

    # ------------------------------------------------------------------ refits

    def _refit(self, day: int) -> None:
        scale = 1.0 / self._weight(day)  # weights relative to today (effective counts)
        if self.calibrate:
            self._fit_calibration(scale)
        self._fit_thresholds(scale)

    def _newton(self, w, h, x, theta, prior, ridge, fit) -> np.ndarray:
        """Ridge-penalised logistic calibration fits, batched over the leading axes.

        w, h, x: (..., B) bin weights, hits and mean logits; theta, prior: (..., d).
        """
        feats = _features(x, self.method)  # (..., B, d)
        eye = np.eye(self._dim)
        for _ in range(12):
            q = _sigmoid(np.einsum("...bd,...d->...b", feats, theta))
            grad = np.einsum("...b,...bd->...d", w * q - h, feats) + ridge * (theta - prior)
            curvature = w * q * (1 - q)
            hess = np.einsum("...b,...bd,...be->...de", curvature, feats, feats) + ridge * eye
            step = np.linalg.solve(hess, grad[..., None])[..., 0]
            step = np.where(fit[..., None], step, 0.0)
            theta = theta - step
            if np.max(np.abs(step)) < 1e-7:
                break
        return np.where(fit[..., None], theta, prior)

    def _fit_calibration(self, scale: float) -> None:
        w = self.cal_w.sum(axis=1) * scale  # (P, B)
        h = self.cal_h.sum(axis=1) * scale
        xs = self.cal_x.sum(axis=1) * scale
        x = np.where(w > 0, xs / np.maximum(w, 1e-12), 0.0)
        prior = np.tile(_identity(self.method), (P, 1))
        fit = (w.sum(axis=1) >= self.calib_min) & ~NO_CALIBRATION
        if not fit.any():
            return
        self.theta = self._newton(w, h, x, self.theta.copy(), prior, self.ridge, fit)
        if self.bucket_calibration:
            # Per history bucket, shrunk toward the pooled map (bucket_ridge pseudo-matches).
            bw, bh = self.cal_bw * scale, self.cal_bh * scale
            bx = np.where(bw > 0, self.cal_bx * scale / np.maximum(bw, 1e-12), 0.0)
            pooled = np.repeat(self.theta[:, None, :], N_BUCKETS, axis=1)
            bucket_fit = fit[:, None] & (bw.sum(axis=2) > 0)
            self.theta_bucket = self._newton(
                bw, bh, bx, pooled.copy(), pooled, self.bucket_ridge, bucket_fit
            )
        if not self.league_offsets:
            return
        # Per-league intercepts on the non-empty bins only (sparse Newton).
        p_idx, l_idx, b_idx = np.nonzero(self.cal_w)
        wl = self.cal_w[p_idx, l_idx, b_idx]
        xl = self.cal_x[p_idx, l_idx, b_idx] / wl
        hl = self.cal_h[p_idx, l_idx, b_idx] * scale
        wl = wl * scale
        base = np.einsum("md,md->m", _features(xl, self.method), self.theta[p_idx])
        cell = p_idx * L + l_idx
        offset = self.offset.copy().ravel()
        for _ in range(5):
            q = _sigmoid(base + offset[cell])
            grad = np.bincount(cell, wl * q - hl, P * L) + self.league_ridge * offset
            hess = np.bincount(cell, wl * q * (1 - q), P * L) + self.league_ridge
            offset -= grad / hess
        offset = offset.reshape(P, L)
        offset[~fit] = 0.0
        self.offset = offset

    def _prefix_threshold(self, w: np.ndarray, h: np.ndarray, target: float) -> np.ndarray:
        """Lowest bin edge whose picks above it reach `target` (Wilson), per leading index."""
        cum_w, cum_h = _tail_sums(w), _tail_sums(h)
        acc = np.where(cum_w > 0, cum_h / np.maximum(cum_w, 1e-12), 0.0)
        ok = (cum_w >= self.min_picks) & (wilson_bound(acc, cum_w, self.z) >= target)
        first = np.argmax(ok, axis=-1)
        edges = first / B_SEL
        return np.where(ok.any(axis=-1), edges, np.inf)

    def _fit_thresholds(self, scale: float) -> None:
        w, h = self.sel_w * scale, self.sel_h * scale
        if self.use_veto:
            pad = np.zeros((K, L, 1))
            cum_lw = np.concatenate([_tail_sums(self.lg_w) * scale, pad], axis=2)
            cum_lh = np.concatenate([_tail_sums(self.lg_h) * scale, pad], axis=2)
        for mode, target in enumerate(self.targets):
            threshold = self._prefix_threshold(w, h, target)  # (K, buckets)
            threshold[~SELECTABLE] = np.inf
            self.threshold[mode] = threshold
            start = min(B_SEL - 1, int(np.ceil(self.floors[mode] * B_SEL - 1e-9)))
            self.thin[mode] = SELECTABLE[:, None] & (_tail_sums(w)[..., start] < self.min_picks)
            if self.league_thresholds:
                own = self._prefix_threshold(self.lg_w * scale, self.lg_h * scale, target)
                own[~SELECTABLE] = np.inf
                self.league_threshold[mode] = own
            if not self.use_veto:
                continue
            # League veto at the bucket-0 threshold (the bulk of the picks).
            edge = np.maximum(threshold[:, 0], self.floors[mode])
            start = np.where(np.isfinite(edge), np.ceil(edge * B_SEL - 1e-9), B_SEL).astype(int)
            start = np.minimum(start, B_SEL)
            rows = np.arange(K)
            n = cum_lw[rows, :, start]
            hits = cum_lh[rows, :, start]
            acc = np.where(n > 0, hits / np.maximum(n, 1e-12), 1.0)
            upper = wilson_bound(acc, n, self.veto_z, upper=True)
            self.veto[mode] = (n >= self.veto_min) & (upper < target)


def factory(**params) -> SelectorModel:
    return SelectorModel(**params)
