"""tenisPrediction v3: model walk-forward fără leakage, cote, calibrare și regula de selecție.

Two layers:

``TennisModel``
    The benchmark protocol (``predict(ctx)``, ``select(ctx, p)``, ``update(row)``, see
    ``tenisPrediction/benchmark.py``). Player state lives in ``engine.FeatureEngine`` and is
    keyed by the TML player key (id, ``wta:`` prefix on the WTA tour). The probability is a
    symmetric logistic regression without intercept on the engine's antisymmetric feature
    vector, refitted at the start of every season (from ``fit_from``) on all stored matches of
    earlier dates. The training vector of a row is computed BEFORE the row's result reaches
    the state, so it is exactly what ``predict`` would have seen.

    Pre-match odds (opt-in, ``odds_weights``): when the context carries both prices
    (``ctx.first_odds`` / ``ctx.second_odds``, benchmark ``--odds``) the logit becomes
    ``w_model * z_model + w_market * logit(p_market)`` with the bookmaker margin removed by
    ``odds_margin``; without prices it stays the pure model. ``odds_weights="fit"`` fits the
    pair at every season start on the out-of-sample pairs of EARLIER seasons only (per tour,
    pooled fallback); a tuple fixes it (the app uses ``PRODUCTION_ODDS_WEIGHTS``). There is no
    intercept, so the blend stays exactly symmetric. With ``odds_track`` the selection window
    and the calibrator see the blended logit of fed rows that carry prices, so they follow the
    probability that is actually used.

    Calibration (``calib_mode``): an antisymmetric S-curve ``g(z) = a z + b z|z|`` (or a
    temperature / symmetrised isotonic map) per tour, refitted every ``calib_step`` results on
    the last ``calib_window`` out-of-sample winner-first logits. Only the DISPLAYED
    probability changes: selection keeps the raw confidence (``calib_select=False``) and the
    reported threshold is mapped through the same monotone curve, so "selectează" means the
    same thing on both scales.

    ``select`` is the validated rule: each tour keeps a rolling window of its last
    ``select_window`` out-of-sample predictions whose results are known (every fed match is
    scored with the weights fitted before its season, before its result is added). Every
    ``select_step`` new results the threshold becomes the lowest confidence whose top slice of
    the window is at least ``select_target`` accurate (``select_target_high`` gives a second,
    stricter "high-precision" threshold). A match is selected when max(p, 1 - p) reaches the
    threshold of ``select_profile`` (never below ``select_floor``).

``TennisPredictor``
    Production wrapper: trains a ``TennisModel`` on the local TML files (optionally joined to
    tennis-data.co.uk closing prices so the window tracks the blend), pickled under the
    git-ignored ``footypreds/data/``; resolves FlashScore names ("De Minaur A.") to player keys
    and predicts from names with the player's last known rank, age, height and hand.

Benchmark factory: ``tenisPrediction.model:benchmark_factory``.
"""

from __future__ import annotations

import hashlib
import math
import os
import pickle
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from . import calibration
from .engine import FeatureEngine, SampleStore, fit_symmetric_logistic, lr_design, sigmoid
from .legacy import CompactTennisModel, _key, _number, serve_record

VERSION = "tenisPrediction-3.0"
CACHE_FILE = "model_v3.pkl"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "tml-data"
CACHE_DIR = ROOT.parent / "footypreds" / "data" / "tenisPrediction"
MINOR_TOURS = ("challenger", "quali")
SELECTED = "selectează"
NO_BET = "fără pariu"
# Margin removal validated on tennis-data.co.uk AVERAGE closing prices (2021-2023): the power
# method beats proportional / Shin / additive for the market alone (see EXPERIMENTS.md, v3).
ODDS_MARGIN = "power"
# Fixed (w_model, w_market) for the app. The weights fitted on CLOSING prices are about
# (0.1, 0.9); FlashScore pre-match prices are earlier and noisier, and with noise on the
# market logit the optimum moves to ~(0.25-0.35, 0.6-0.7). (0.25, 0.70) costs < 0.001 log-loss
# against closing prices and is robust to that noise (EXPERIMENTS.md, v3).
PRODUCTION_ODDS_WEIGHTS = (0.25, 0.70)
# Engine dynamics per tour group (adjustment 3): chosen on 2021-2023 among 8 real benchmark runs
# and confirmed once on 2024. Only the ATP override (faster-decaying K) confirmed; the WTA
# candidate (form_decay 0.95, mov 0, idle_half_life 250, idle_grace 0) lost on 2024 (log-loss
# 0.5962 against 0.5942) and was dropped (confirm-or-drop), so the women's group keeps the v2
# dynamics. Pass ``tour_params={}`` for the v2 dynamics on both groups.
DEFAULT_TOUR_PARAMS: dict[str, dict] = {"atp": {"k_shape": 0.5, "k_base": 200.0}}
# Injury / fatigue differences kept after the 2021-2023 ablation (adjustment 2): only the
# return-from-absence trio helped; retirements, walkovers given, recent minutes and a long
# previous match did not move the log-loss. Pass ``health_features=()`` to switch them off.
DEFAULT_HEALTH_FEATURES: tuple[str, ...] = ("comeback", "comeback_away", "comeback_time")

__all__ = [
    "VERSION",
    "CompactTennisModel",
    "Prediction",
    "TennisModel",
    "TennisPredictor",
    "benchmark_factory",
    "normalize_name",
    "serve_record",
    "_key",
    "_number",
]

_SIDE = ("key", "hand", "ht", "ioc", "age", "rank", "rank_points", "seed", "entry")


def ctx_sides(ctx):
    """(first, second, match) tuples of a benchmark ``MatchContext``."""
    first = tuple(getattr(ctx, f"first_{name}") for name in _SIDE)
    second = tuple(getattr(ctx, f"second_{name}") for name in _SIDE)
    match = (
        ctx.tour,
        ctx.date.toordinal(),
        ctx.tourney_id,
        ctx.tourney_name,
        ctx.tourney_level,
        ctx.draw_size,
        ctx.surface,
        ctx.indoor,
        ctx.best_of,
        ctx.round,
    )
    return first, second, match


def row_sides(row, day: int):
    """(winner, loser, match) tuples of a finished benchmark ``Row``."""
    winner = tuple(row[f"winner_{name}"] for name in _SIDE)
    loser = tuple(row[f"loser_{name}"] for name in _SIDE)
    match = (
        row["tour"],
        day,
        row["tourney_id"],
        row["tourney_name"],
        row["tourney_level"],
        row["draw_size"],
        row["surface"],
        row["indoor"],
        row["best_of"],
        row["round"],
    )
    return winner, loser, match


def _tour_window(tour: str) -> str:
    return "challenger" if tour == "quali" else tour


def _logit(p: float) -> float:
    p = min(1.0 - 1e-12, max(1e-12, p))
    return math.log(p / (1.0 - p))


def rolling_threshold(
    confidences: np.ndarray, hits: np.ndarray, target: float, min_selected: int
) -> float:
    """Lowest confidence whose top slice (cut where confidence changes) is >= target accurate.

    Returns 1.0 (nothing selectable) when no slice of at least ``min_selected`` matches
    reaches the target.
    """
    order = np.argsort(-confidences, kind="stable")
    conf, hit = confidences[order], hits[order]
    count = np.arange(1, len(conf) + 1)
    accuracy = np.cumsum(hit) / count
    boundary = np.ones(len(conf), dtype=bool)
    boundary[:-1] = conf[1:] < conf[:-1]
    ok = np.nonzero(boundary & (count >= min_selected) & (accuracy >= target - 1e-12))[0]
    return float(conf[ok[-1]]) if len(ok) else 1.0


class TennisModel:
    """Walk-forward model: feature engine + seasonal symmetric logistic + rolling selection."""

    def __init__(
        self,
        *,
        train_from: int = 2005,
        train_tours: tuple[str, ...] = ("atp", "wta", "challenger"),
        fit_from: int = 2014,
        l2: float = 1.0,
        select_mode: str = "rolling",
        select_target: float = 0.82,
        select_target_minor: float = 0.83,
        select_target_high: float = 0.85,
        select_profile: str = "standard",
        select_window: int = 1000,
        select_step: int = 100,
        select_min: int = 100,
        select_floor: float = 0.60,
        threshold: float = 0.72,
        threshold_minor: float = 0.76,
        track_tours: tuple[str, ...] = ("atp", "wta", "challenger"),
        odds_weights: tuple[float, float] | dict | str | None = None,
        odds_margin: str = ODDS_MARGIN,
        odds_track: bool = True,
        odds_fit_min: int = 500,
        calib_mode: str = "none",
        calib_window: int = 20000,
        calib_step: int = 250,
        calib_min: int = 2000,
        calib_half_life: float | None = None,
        calib_select: bool = False,
        tour_params: dict | None = None,
        health_features: tuple[str, ...] | str | None = None,
        **engine_params,
    ):
        if select_mode not in ("rolling", "fixed"):
            raise ValueError("select_mode trebuie să fie 'rolling' sau 'fixed'.")
        if select_profile not in ("standard", "high"):
            raise ValueError("select_profile trebuie să fie 'standard' sau 'high'.")
        if calib_mode not in calibration.MODES:
            raise ValueError(f"calib_mode trebuie să fie unul dintre {calibration.MODES}.")
        if isinstance(odds_weights, str) and odds_weights != "fit":
            raise ValueError("odds_weights: None, (w_model, w_market), {circuit: ...} sau 'fit'.")
        if tour_params is None:
            tour_params = DEFAULT_TOUR_PARAMS
        if health_features is None:
            health_features = DEFAULT_HEALTH_FEATURES
        self.engine = FeatureEngine(
            tour_params=tour_params or None, health_features=health_features, **engine_params
        )
        self.train_from = train_from
        self.train_tours = frozenset(train_tours)
        self.fit_from = fit_from
        self.l2 = l2
        self.select_mode = select_mode
        self.select_target = select_target
        self.select_target_minor = select_target_minor
        self.select_target_high = select_target_high
        self.select_profile = select_profile
        self.select_window = select_window
        self.select_step = select_step
        self.select_min = select_min
        self.select_floor = select_floor
        self.threshold = threshold
        self.threshold_minor = threshold_minor
        self.track_tours = frozenset(track_tours)
        self.odds_weights = tuple(odds_weights) if isinstance(odds_weights, list) else odds_weights
        self.odds_margin = odds_margin
        self.odds_track = odds_track
        self.odds_fit_min = odds_fit_min
        self.calib_mode = calib_mode
        self.calib_window = calib_window
        self.calib_step = calib_step
        self.calib_min = calib_min
        self.calib_half_life = calib_half_life
        self.calib_select = calib_select
        self.samples: SampleStore | None = SampleStore()
        self.coef: np.ndarray | None = None
        self.fitted_season: int | None = None
        self.fit_count = 0
        self.windows: dict[str, deque] = {}
        self.fresh: dict[str, int] = {}
        self.thresholds: dict[str, float] = {}
        self.thresholds_high: dict[str, float] = {}
        # tour window -> (model logit, market logit) of out-of-sample rows with prices, winner
        # first; refitted into odds_coef at every season start (odds_weights="fit")
        self.odds_pairs: dict[str, list[tuple[float, float]]] = {}
        self.odds_coef: dict[str, tuple[float, float]] = {}
        self.calib_buffers: dict[str, deque] = {}
        self.calib_fresh: dict[str, int] = {}
        self.calib: dict[str, tuple] = {}
        # key -> (name, hand, ht, ioc, birth ordinal, rank, rank points, last day, tour)
        self.profiles: dict[str, tuple] = {}
        self.last_day: int | None = None

    # ------------------------------------------------------------------ fitting

    def _roll(self, season: int) -> None:
        if self.fitted_season is None or season > self.fitted_season:
            self.fitted_season = season
            if season >= self.fit_from:
                self.fit()

    def fit(self) -> None:
        """Refit the logistic layer (and the market blend) on everything finished before now."""
        if self.samples is None:
            return
        d, c = self.samples.arrays()
        if len(d) < 1000:
            return
        self.coef = fit_symmetric_logistic(d, c, self.l2, start=self.coef)
        self.fit_count += 1
        self.fit_odds()

    def fit_odds(self) -> None:
        """Blend weights per tour from the stored out-of-sample pairs (earlier seasons only)."""
        if self.odds_weights != "fit":
            return
        from .odds import fit_blend_weights

        pooled: list[tuple[float, float]] = []
        for tour, pairs in self.odds_pairs.items():
            pooled.extend(pairs)
            if len(pairs) >= self.odds_fit_min:
                model_z, market_z = zip(*pairs)
                self.odds_coef[tour] = fit_blend_weights(model_z, market_z)
        if len(pooled) >= self.odds_fit_min:
            model_z, market_z = zip(*pooled)
            self.odds_coef["all"] = fit_blend_weights(model_z, market_z)

    def logit(self, d, c) -> float:
        if self.coef is None:
            return float(d[0])
        return float(lr_design(d, c)[0] @ self.coef)

    # ------------------------------------------------------------------ market

    def market_weights(self, tour: str) -> tuple[float, float] | None:
        """(w_model, w_market) in force for ``tour``, or None (pure model)."""
        weights = self.odds_weights
        if weights is None:
            return None
        if weights == "fit":
            return self.odds_coef.get(_tour_window(tour)) or self.odds_coef.get("all")
        if isinstance(weights, dict):
            return weights.get(_tour_window(tour))
        return weights

    def market_logit(self, odds_1, odds_2) -> float | None:
        """Fair market logit of player 1 from the two prices, None when unusable."""
        from .odds import valid_odds

        if not valid_odds(odds_1, odds_2):
            return None
        from .odds import fair_probability

        return _logit(fair_probability(odds_1, odds_2, self.odds_margin))

    def blend(self, z: float, tour: str, market_logit: float | None) -> float:
        weights = self.market_weights(tour)
        if weights is None or market_logit is None:
            return z
        return weights[0] * z + weights[1] * market_logit

    # ------------------------------------------------------------------ calibration

    def calibrate(self, tour: str, z: float) -> float:
        """Displayed winner-first logit for the tour's current calibrator (identity when off)."""
        if self.calib_mode == "none":
            return z
        params = self.calib.get(_tour_window(tour))
        return z if params is None else float(calibration.apply(self.calib_mode, params, z))

    def _refit_calibration(self, tour: str) -> None:
        buffer = self.calib_buffers.get(tour)
        if buffer is None or len(buffer) < self.calib_min:
            return
        z = np.fromiter(buffer, float, len(buffer))
        self.calib[tour] = calibration.fit(self.calib_mode, z, self.calib_half_life)

    # ------------------------------------------------------------------ selection

    def threshold_for(self, tour: str, high: bool = False) -> float:
        """Selection threshold on the scale of the tracked confidence (raw unless
        ``calib_select``)."""
        fixed = self.threshold_minor if tour in MINOR_TOURS else self.threshold
        if self.select_mode == "fixed":
            return fixed
        window = self.windows.get(_tour_window(tour))
        if window is None or len(window) < max(self.select_min * 5, self.select_step):
            return max(self.select_floor, fixed)
        table = self.thresholds_high if high else self.thresholds
        return max(self.select_floor, table.get(_tour_window(tour), fixed))

    def display_threshold(self, tour: str, high: bool = False) -> float:
        """The threshold on the scale of the displayed (calibrated) probability."""
        raw = self.threshold_for(tour, high)
        if self.calib_mode == "none" or self.calib_select or raw >= 1.0:
            return raw
        return sigmoid(self.calibrate(tour, _logit(raw)))

    def _track(self, tour: str, z: float) -> None:
        """Record one out-of-sample winner-first logit for the tour's rolling rules."""
        tour = _tour_window(tour)
        window = self.windows.get(tour)
        if window is None:
            window = self.windows[tour] = deque(maxlen=self.select_window)
        tracked = self.calibrate(tour, z) if self.calib_select else z
        p = sigmoid(tracked)
        window.append((max(p, 1.0 - p), 1.0 if p > 0.5 else 0.5 if p == 0.5 else 0.0))
        self.fresh[tour] = self.fresh.get(tour, 0) + 1
        if self.fresh[tour] >= self.select_step:
            self.fresh[tour] = 0
            conf = np.fromiter((item[0] for item in window), float, len(window))
            hits = np.fromiter((item[1] for item in window), float, len(window))
            target = self.select_target_minor if tour in MINOR_TOURS else self.select_target
            self.thresholds[tour] = rolling_threshold(conf, hits, target, self.select_min)
            self.thresholds_high[tour] = rolling_threshold(
                conf, hits, max(self.select_target_high, target), self.select_min
            )
        if self.calib_mode != "none":
            buffer = self.calib_buffers.get(tour)
            if buffer is None:
                buffer = self.calib_buffers[tour] = deque(maxlen=self.calib_window)
            buffer.append(z)
            self.calib_fresh[tour] = self.calib_fresh.get(tour, 0) + 1
            if self.calib_fresh[tour] >= self.calib_step:
                self.calib_fresh[tour] = 0
                self._refit_calibration(tour)

    # ------------------------------------------------------------------ protocol

    def predict(self, ctx) -> float:
        self._roll(ctx.season)
        first, second, match = ctx_sides(ctx)
        d, c = self.engine.features(first, second, match, store=False)
        z = self.logit(d, c)
        if self.odds_weights is not None:
            z = self.blend(
                z, ctx.tour, self.market_logit(ctx.get("first_odds"), ctx.get("second_odds"))
            )
        return sigmoid(self.calibrate(ctx.tour, z))

    def select(self, ctx, p: float) -> bool:
        high = self.select_profile == "high"
        return max(p, 1.0 - p) >= self.display_threshold(ctx.tour, high)

    def select_high(self, ctx, p: float) -> bool:
        """The stricter high-precision rule (benchmark column ``select_high``)."""
        return max(p, 1.0 - p) >= self.display_threshold(ctx.tour, high=True)

    def update(self, row) -> None:
        self._roll(row["season"])
        day = row["date"].toordinal()
        tour = row["tour"]
        wkey, lkey = row["winner_key"], row["loser_key"]
        valid = bool(wkey) and bool(lkey) and wkey != lkey
        if valid and not row["is_walkover"]:
            train = (
                self.samples is not None
                and row["season"] >= self.train_from
                and tour in self.train_tours
            )
            track = self.coef is not None and tour in self.track_tours
            if train or track:
                winner, loser, match = row_sides(row, day)
                d, c = self.engine.features(winner, loser, match)
                if track:
                    z = self.logit(d, c)
                    if self.odds_weights is not None:
                        market = self.market_logit(row.get("winner_odds"), row.get("loser_odds"))
                        if market is not None:
                            self.odds_pairs.setdefault(_tour_window(tour), []).append((z, market))
                            if self.odds_track:
                                z = self.blend(z, tour, market)
                    self._track(tour, z)
                if train:
                    self.samples.append(d, c)
        self.engine.update(row, day)
        if valid:
            self._remember(row, "winner", wkey, day)
            self._remember(row, "loser", lkey, day)
        if self.last_day is None or day > self.last_day:
            self.last_day = day

    def _remember(self, row, side: str, key: str, day: int) -> None:
        old = self.profiles.get(key)
        age = row[f"{side}_age"]
        birth = day - age * 365.25 if age else (old[4] if old else None)
        self.profiles[key] = (
            row[f"{side}_name"] or (old[0] if old else key),
            row[f"{side}_hand"] or (old[1] if old else None),
            row[f"{side}_ht"] or (old[2] if old else None),
            row[f"{side}_ioc"] or (old[3] if old else None),
            birth,
            row[f"{side}_rank"],
            row[f"{side}_rank_points"],
            day,
            row["tour"],
        )

    def finalize(self) -> None:
        """Production: refit on everything seen, then drop the training matrices."""
        self.fit()
        self.samples = None
        self.odds_pairs = {}


def benchmark_factory(**params) -> TennisModel:
    """Factory for ``python -m tenisPrediction.benchmark --model tenisPrediction.model``."""
    return TennisModel(**params)


factory = benchmark_factory


def odds_factory(**params) -> TennisModel:
    """v3 with the market blend fitted walk-forward; run the benchmark with ``--odds avg``."""
    params.setdefault("odds_weights", "fit")
    return TennisModel(**params)


def production_factory(**params) -> TennisModel:
    """Exactly the app's configuration (fixed conservative blend); run with ``--odds avg``."""
    params.setdefault("odds_weights", PRODUCTION_ODDS_WEIGHTS)
    return TennisModel(**params)


# --------------------------------------------------------------------------- names


def normalize_name(value: str | None) -> str:
    """Casefolded ASCII name: accents removed, hyphens and dots as spaces, no apostrophes."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("'", "").replace("’", "").replace("`", "")
    for ch in "-.,_":
        text = text.replace(ch, " ")
    return " ".join(text.split())


def _split_query(name: str) -> tuple[list[str], list[str]]:
    """FlashScore "De Minaur A." -> (surname tokens, initials); a full name has no initials."""
    raw = (name or "").replace("’", "'").split()
    initials: list[str] = []
    while len(raw) > 1:
        token = raw[-1]
        stripped = normalize_name(token).split()
        is_initial = token.endswith(".") or (len(stripped) == 1 and len(stripped[0]) == 1)
        if not is_initial or not stripped or any(len(part) > 3 for part in stripped):
            break
        initials = stripped + initials
        raw.pop()
    return normalize_name(" ".join(raw)).split(), initials


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


@dataclass(frozen=True)
class Prediction:
    player_1: str
    player_2: str
    winner: str
    probability_1: float
    confidence: int
    decision: str
    surface: str
    experience_1: int
    experience_2: int
    key_1: str | None = None
    key_2: str | None = None
    tour: str = "atp"
    best_of: int = 3
    threshold: float | None = None
    model_probability_1: float | None = None
    market_probability_1: float | None = None
    decision_high: str = NO_BET
    threshold_high: float | None = None

    def as_dict(self) -> dict:
        return {
            "player_1": self.player_1,
            "player_2": self.player_2,
            "winner": self.winner,
            "probability_1": round(self.probability_1, 4),
            "probability_2": round(1 - self.probability_1, 4),
            "confidence": self.confidence,
            "decision": self.decision,
            "decision_high": self.decision_high,
            "surface": self.surface,
            "experience_1": self.experience_1,
            "experience_2": self.experience_2,
            "known": self.key_1 is not None and self.key_2 is not None,
            "tour": self.tour,
            "best_of": self.best_of,
            "threshold": _rounded(self.threshold),
            "threshold_high": _rounded(self.threshold_high),
            "model_probability_1": _rounded(self.model_probability_1),
            "market_probability_1": _rounded(self.market_probability_1),
        }


SLAM_WORDS = ("australian open", "french open", "roland garros", "wimbledon", "us open")


class TennisPredictor:
    """Trained ``TennisModel`` plus name resolution and name-based predictions."""

    def __init__(self, model: TennisModel | None = None, signature: str = ""):
        self.model = model or TennisModel()
        self.signature = signature
        self._index: dict[str, list[str]] | None = None
        self._full: dict[str, list[str]] | None = None

    # -------------------------------------------------------------- training

    def fit_rows(self, rows) -> TennisPredictor:
        for row in rows:
            self.model.update(row)
        self.model.finalize()
        self._index = self._full = None
        return self

    @classmethod
    def train(
        cls,
        data_dir: str | Path = DATA_DIR,
        first_year: int = 1990,
        last_year: int | None = None,
        tours: tuple[str, ...] = ("atp", "challenger", "quali", "wta"),
        *,
        odds: str | None = None,
        odds_dir: str | Path | None = None,
        **params,
    ) -> TennisPredictor:
        """Train on the TML files; ``odds`` ("avg", "ps", ...) also joins the tennis-data.co.uk
        closing prices found in ``odds_dir`` so the rolling rules track the market blend."""
        from .benchmark import DEFAULT_CACHE_DIR, load_rows

        last_year = last_year or _latest_year(Path(data_dir))
        # the benchmark's parse cache is shared only for the real data folder
        same = Path(data_dir).resolve() == DATA_DIR.resolve()
        cache_dir = DEFAULT_CACHE_DIR if same else None
        rows = load_rows(first_year, last_year, tours, data_dir=data_dir, cache_dir=cache_dir)
        if odds:
            from .odds import DEFAULT_ODDS_DIR, attach_odds

            odds_dir = Path(odds_dir or DEFAULT_ODDS_DIR)
            if odds_dir.is_dir():
                rows, _stats = attach_odds(rows, odds, directory=odds_dir, cache_dir=cache_dir)
        signature = data_signature(
            data_dir, first_year, last_year, tours, params, odds=odds, odds_dir=odds_dir
        )
        return cls(TennisModel(**params), signature).fit_rows(rows)

    @classmethod
    def load_or_train(
        cls,
        data_dir: str | Path = DATA_DIR,
        cache_dir: str | Path | None = CACHE_DIR,
        first_year: int = 1990,
        last_year: int | None = None,
        *,
        odds: str | None = None,
        odds_dir: str | Path | None = None,
        **params,
    ) -> TennisPredictor:
        """Trained predictor from the pickle cache; retrains when any data file, the model
        version or a parameter changed."""
        last_year = last_year or _latest_year(Path(data_dir))
        tours = ("atp", "challenger", "quali", "wta")
        signature = data_signature(
            data_dir, first_year, last_year, tours, params, odds=odds, odds_dir=odds_dir
        )
        cache_file = None if cache_dir is None else Path(cache_dir) / CACHE_FILE
        if cache_file is not None and cache_file.exists():
            try:
                with cache_file.open("rb") as handle:
                    cached = pickle.load(handle)
                if isinstance(cached, cls) and cached.signature == signature:
                    return cached
            except (OSError, pickle.PickleError, EOFError, AttributeError, TypeError):
                pass
        predictor = cls.train(
            data_dir, first_year, last_year, tours, odds=odds, odds_dir=odds_dir, **params
        )
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
                with temporary.open("wb") as handle:
                    pickle.dump(predictor, handle, pickle.HIGHEST_PROTOCOL)
                os.replace(temporary, cache_file)
            except OSError:
                pass  # the cache is only an optimisation
        return predictor

    def __getstate__(self):
        return {"model": self.model, "signature": self.signature}

    def __setstate__(self, state):
        self.model = state["model"]
        self.signature = state["signature"]
        self._index = self._full = None

    # -------------------------------------------------------------- players

    def experience(self, key: str | None) -> int:
        if not key:
            return 0
        state = self.model.engine.players.get(key)
        return state.n if state else 0

    def _build_index(self) -> None:
        index: dict[str, list[str]] = {}
        full: dict[str, list[str]] = {}
        for key, profile in self.model.profiles.items():
            tokens = normalize_name(profile[0]).split()
            if not tokens:
                continue
            full.setdefault(" ".join(tokens), []).append(key)
            for token in set(tokens):
                index.setdefault(token, []).append(key)
        self._index, self._full = index, full

    def _group_ok(self, key: str, tour: str | None) -> bool:
        if tour is None:
            return True
        return key.startswith("wta:") == (tour == "wta")

    def _same_person(self, first: str, second: str) -> bool:
        """Two keys of one player: same tour group and full name, and either a missing-id
        ("name:...") key or birth dates within 60 days."""
        profiles = self.model.profiles
        if first.startswith("wta:") != second.startswith("wta:"):
            return False
        if normalize_name(profiles[first][0]) != normalize_name(profiles[second][0]):
            return False
        synthetic = ("name:", "wta:name:")
        if first.startswith(synthetic) or second.startswith(synthetic):
            return True
        birth_1, birth_2 = profiles[first][4], profiles[second][4]
        return birth_1 is not None and birth_2 is not None and abs(birth_1 - birth_2) <= 60

    def _pick(self, keys: list[str], tour: str | None) -> str | None:
        """One key among name matches: same tour group, then the clearly dominant active one."""
        profiles = self.model.profiles
        # TML sometimes files one player under two keys (a missing id gives "name:...", or a
        # second id with the same birth date): those collapse to the most experienced key.
        # Namesakes with different ids and different (or unknown) birth dates stay separate.
        people: list[list[str]] = []
        for key in dict.fromkeys(keys):
            if not self._group_ok(key, tour):
                continue
            for group in people:
                if self._same_person(group[0], key):
                    group.append(key)
                    break
            else:
                people.append([key])
        keys = [max(group, key=self.experience) for group in people]
        if len(keys) <= 1:
            return keys[0] if keys else None
        latest = self.model.last_day or 0
        active = [key for key in keys if latest - profiles[key][7] <= 400]
        if len(active) == 1:
            return active[0]
        if not active:
            return max(keys, key=lambda key: (profiles[key][7], self.experience(key)))
        ranked = sorted(active, key=lambda key: profiles[key][5] or 5000)
        best, second = profiles[ranked[0]][5] or 5000, profiles[ranked[1]][5] or 5000
        return ranked[0] if best * 2 <= second else None  # truly ambiguous: no guess

    def resolve_player(self, name: str, tour: str | None = None) -> str | None:
        """Player key for a full TML name or a FlashScore "<surname words> <initial>." name.

        Accents, hyphens and apostrophes are ignored ("Auger-Aliassime F.", "O'Connell C.").
        Ambiguous names (several active players, none clearly the best ranked) give None.
        """
        if self._index is None:
            self._build_index()
        if name in self.model.profiles:
            return name if self._group_ok(name, tour) else None
        normal = normalize_name(name)
        if not normal:
            return None
        exact = self._full.get(normal)
        if exact:
            return self._pick(exact, tour)
        surname, initials = _split_query(name)
        if not surname:
            return None
        pool = self._index.get(surname[-1], [])
        size = len(surname)
        candidates = []
        for key in pool:
            tokens = normalize_name(self.model.profiles[key][0]).split()
            if not self._group_ok(key, tour):
                continue
            # surname = the last words of the full name ("Alex De Minaur")
            if len(tokens) > size and tokens[-size:] == surname:
                given = tokens[:-size]
            # or the first words, for names stored surname-first
            elif len(tokens) > size and tokens[:size] == surname:
                given = tokens[size:]
            elif not initials and tokens == surname:
                given = []
            else:
                continue
            if initials and not _initials_match(initials, given):
                continue
            candidates.append(key)
        if not candidates and len(normal.split()) >= 2 and not initials:
            reversed_name = " ".join(reversed(normal.split()))
            candidates = self._full.get(reversed_name, [])
        return self._pick(candidates, tour)

    def search(self, query: str, limit: int = 12) -> list[str]:
        needle = normalize_name(query)
        profiles = self.model.profiles
        names = [
            (profiles[key][0], self.experience(key))
            for key in profiles
            if needle in normalize_name(profiles[key][0])
        ]
        names.sort(key=lambda item: item[1], reverse=True)
        return [name for name, _ in names[:limit]]

    @property
    def player_count(self) -> int:
        return len(self.model.profiles)

    @property
    def trained_through(self) -> str | None:
        day = self.model.last_day
        return date.fromordinal(day).isoformat() if day else None

    # -------------------------------------------------------------- predictions

    def _side(self, key: str | None, name: str, rank: float | None, day: int):
        profile = self.model.profiles.get(key) if key else None
        if profile is None:
            return (
                key or f"name:{normalize_name(name)}",
                None,
                None,
                None,
                None,
                int(rank) if rank else None,
                None,
                None,
                None,
            )
        age = (day - profile[4]) / 365.25 if profile[4] else None
        known_rank = int(rank) if rank else profile[5]
        points = profile[6] if not rank or rank == profile[5] else None
        return (key, profile[1], profile[2], profile[3], age, known_rank, points, None, None)

    def probability(
        self,
        player_1: str,
        player_2: str,
        surface: str = "Hard",
        rank_1: float | None = None,
        rank_2: float | None = None,
        *,
        tour: str | None = None,
        best_of: int | None = None,
        level: str | None = None,
        when: date | None = None,
        indoor: bool | None = None,
        market_probability: float | None = None,
        market_weights: tuple[float, float] | None = PRODUCTION_ODDS_WEIGHTS,
    ) -> float:
        """Displayed probability of player 1 (model, market blend and calibration applied)."""
        z, _key_1, _key_2, tour, _best_of, _surface = self._evaluate(
            player_1, player_2, surface, rank_1, rank_2, tour, best_of, level, when, indoor
        )
        return sigmoid(self.model.calibrate(tour, _blend(z, market_probability, market_weights)))

    def _evaluate(
        self, player_1, player_2, surface, rank_1, rank_2, tour, best_of, level, when, indoor
    ):
        """(raw model logit of player 1, key_1, key_2, tour, best_of, surface)."""
        key_1 = self.resolve_player(player_1, tour)
        key_2 = self.resolve_player(player_2, tour)
        if tour is None:
            tour = "wta" if any(k and k.startswith("wta:") for k in (key_1, key_2)) else "atp"
        day = (when or date.today()).toordinal()
        if self.model.last_day:
            day = max(day, self.model.last_day)
        surface = (surface or "Hard").strip().capitalize()
        if surface not in ("Hard", "Clay", "Grass", "Carpet"):
            surface = "Hard"
        best_of = best_of or 3
        first = self._side(key_1, player_1, rank_1, day)
        second = self._side(key_2, player_2, rank_2, day)
        match = (
            tour,
            day,
            None,
            None,
            level or ("G" if best_of == 5 else "A"),
            None,
            surface,
            "I" if indoor else ("O" if indoor is False else None),
            best_of,
            None,
        )
        d, c = self.model.engine.features(first, second, match, store=False)
        return self.model.logit(d, c), key_1, key_2, tour, best_of, surface

    def predict(
        self,
        player_1: str,
        player_2: str,
        surface: str = "Hard",
        rank_1: float | None = None,
        rank_2: float | None = None,
        min_probability: float | None = None,
        *,
        tour: str | None = None,
        best_of: int | None = None,
        level: str | None = None,
        when: date | None = None,
        indoor: bool | None = None,
        selectable: bool = True,
        market_probability: float | None = None,
        market_weights: tuple[float, float] | None = PRODUCTION_ODDS_WEIGHTS,
    ) -> Prediction:
        """Prediction with the validated selection rule.

        ``market_probability`` (fair probability of player 1 from real pre-match prices) is
        blended with the model on the logit scale with ``market_weights``; the displayed
        probability, the winner and both decisions come from that blend (then calibrated).
        ``decision`` is "selectează" only when both players are known, ``selectable`` is true
        (the competition belongs to a validated tour, see ``match_facts``) and the confidence
        reaches the tour's rolling threshold (``min_probability`` can only make it stricter);
        ``decision_high`` uses the stricter high-precision threshold.
        """
        z_model, key_1, key_2, tour, best_of, surface = self._evaluate(
            player_1, player_2, surface, rank_1, rank_2, tour, best_of, level, when, indoor
        )
        model = self.model
        z = _blend(z_model, market_probability, market_weights)
        p = sigmoid(model.calibrate(tour, z))
        p_model = sigmoid(model.calibrate(tour, z_model))
        threshold = model.display_threshold(tour)
        threshold_high = model.display_threshold(tour, high=True)
        if min_probability is not None:
            threshold = max(threshold, min_probability)
            threshold_high = max(threshold_high, min_probability)
        chosen = max(p, 1 - p)
        known = key_1 is not None and key_2 is not None
        allowed = known and selectable
        decision = SELECTED if allowed and chosen >= threshold else NO_BET
        decision_high = SELECTED if allowed and chosen >= threshold_high else NO_BET
        return Prediction(
            player_1,
            player_2,
            player_1 if p >= 0.5 else player_2,
            p,
            round(chosen * 100),
            decision,
            surface,
            self.experience(key_1),
            self.experience(key_2),
            key_1,
            key_2,
            tour,
            best_of,
            threshold,
            p_model,
            market_probability,
            decision_high,
            threshold_high,
        )

    def predict_api_match(self, match, min_probability: float | None = None) -> dict:
        """Adaptor for a FlashScore ``footypreds.domain.Match`` (surface, tour, best of).

        The match's real 1/2 prices (``match.odds``) enter the blend as the fair market
        probability with the validated margin removal.
        """
        facts = match_facts(match)
        when = getattr(match, "kickoff", None)
        odds = getattr(match, "odds", None) or {}
        result = self.predict(
            match.home,
            match.away,
            facts["surface"],
            min_probability=min_probability,
            tour=facts["tour"],
            best_of=facts["best_of"],
            level=facts["level"],
            when=when.date() if hasattr(when, "date") else None,
            indoor=facts["indoor"],
            selectable=facts["validated"],
            market_probability=market_probability_of(odds.get("1"), odds.get("2")),
        )
        payload = result.as_dict()
        payload["match_id"] = match.id
        payload["validated"] = facts["validated"]
        return payload


def _blend(z_model: float, market_probability, weights) -> float:
    if market_probability is None or not weights:
        return z_model
    return weights[0] * z_model + weights[1] * _logit(float(market_probability))


def market_probability_of(odds_1, odds_2, margin: str = ODDS_MARGIN) -> float | None:
    """Fair probability of player 1 from two decimal prices (None when unusable)."""
    from .odds import fair_probability, valid_odds

    try:
        odds_1, odds_2 = float(odds_1), float(odds_2)
    except (TypeError, ValueError):
        return None
    if not valid_odds(odds_1, odds_2):
        return None
    return fair_probability(odds_1, odds_2, margin)


def _initials_match(initials: list[str], given: list[str]) -> bool:
    """ "a" matches "alex"; "j","m" match "juan","manuel"; "zh" matches "zhizhen"."""
    if not given or len(initials) > len(given):
        return False
    if len(initials) == 1 and len(given) > 1:
        joined = "".join(given)
        return given[0].startswith(initials[0]) or joined.startswith(initials[0])
    return all(name.startswith(initial) for initial, name in zip(initials, given))


def match_facts(match) -> dict:
    """Surface, tour, best of, level and indoor flag of a FlashScore match (league name).

    ``validated`` is False where the selection rule was never validated: ITF events (men and
    women), WTA 125 / "Challenger Women" events (absent from the TML WTA files) and doubles.
    There the probability is still given, but the decision is always "fără pariu".
    """
    from footypreds.sports import tennis

    league = str(getattr(match, "league", "") or "")
    surface = tennis.surface_of(league)
    if not surface:
        lowered = league.casefold()
        surface = next((s for s in ("clay", "grass", "carpet", "hard") if s in lowered), "hard")
    lowered = league.casefold()
    category = lowered.split(":", 1)[0]
    if tennis.is_women(match):
        tour = "wta"
    elif "challenger" in category or "itf" in category:
        tour = "challenger"
    else:
        tour = "atp"
    slam = any(word in lowered for word in SLAM_WORDS) and "qualif" not in lowered
    team_cup = "davis cup" in lowered or "billie jean king" in lowered
    tail = lowered.rsplit(",", 1)[-1] if "," in lowered else ""
    women_minor = tour == "wta" and ("challenger" in category or "125" in category)
    validated = not ("itf" in category or women_minor or "doubles" in category)
    return {
        "surface": surface.capitalize(),
        "tour": tour,
        "best_of": tennis.best_of(match),
        "level": "G" if slam else ("D" if team_cup else None),
        "indoor": True if "indoor" in tail else None,
        "category": category,
        "validated": validated,
    }


def _latest_year(data_dir: Path) -> int:
    years = [int(path.stem) for path in Path(data_dir).glob("[12][0-9][0-9][0-9].csv")]
    return max(years) if years else date.today().year


def data_signature(data_dir, first_year, last_year, tours, params, odds=None, odds_dir=None) -> str:
    """Hash of the model version, parameters and every data (and odds) file's size and mtime."""
    from .benchmark import data_file

    parts = [VERSION, repr(sorted(params.items())), str(first_year), str(last_year)]
    for year in range(first_year, last_year + 1):
        for tour in tours:
            path = data_file(Path(data_dir), tour, year)
            if path.exists():
                stat = path.stat()
                parts.append(f"{tour}:{year}:{stat.st_size}:{stat.st_mtime_ns}")
    if odds:
        from .odds import DEFAULT_ODDS_DIR

        parts.append(f"odds:{odds}")
        for path in sorted(Path(odds_dir or DEFAULT_ODDS_DIR).glob("*_[12][0-9][0-9][0-9].xlsx")):
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=16).hexdigest()
