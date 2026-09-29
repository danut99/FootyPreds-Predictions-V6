"""Motorul tenisPrediction v2: stare online a jucătorilor și trăsături pre-meci.

The feature engine was integrated from the audited ``stack_ml`` candidate (see
``tenisPrediction/EXPERIMENTS.md``). It keeps the player state and learns only through
``update(row, day)`` of finished matches:

- an overall and a per-surface Elo (FiveThirtyEight K = k_base / (n + k_offset) ** k_shape,
  straight-sets bonus, Grand Slam boost, retirements down-weighted, idle shrink, start rating
  from the ranking), a fast constant-K Elo and the 365-day peak gap;
- a games Elo, overall and per surface (share of games won);
- a points Elo (share of all points won) and opponent-adjusted serve / return ratings on the
  logit scale of serve points won (overall plus per-surface offsets), turned into a match
  probability by an exact point -> game -> tiebreak -> set -> match Markov chain;
- form, volatility, retirement rate, fatigue (games in the last 7/14 days, matches in the last
  30, matches and minutes in the current event), rank trend, head-to-head, results at the same
  event and a "home" score (share of the event's earlier wildcards from the player's country).

``features(first, second, match)`` turns that state plus the pre-match fields (rank, points,
age, height, hand, seed, entry, surface, level, round, best of) into an antisymmetric
difference vector ``D`` (swapping the players negates it bit for bit) and a symmetric context
vector ``C``. Score, minutes and in-match statistics are read only inside ``update``.
"""

from __future__ import annotations

import math
import re
from collections import deque

import numpy as np

LN10_400 = math.log(10.0) / 400.0
_GAMES = re.compile(r"(\d+)-(\d+)")
_ROUND_INDEX = {
    "Q1": 0,
    "Q2": 1,
    "Q3": 2,
    "Q4": 3,
    "R256": 3,
    "R128": 4,
    "R64": 5,
    "R32": 6,
    "R16": 7,
    "RR": 7,
    "QF": 8,
    "SF": 9,
    "BR": 9,
    "F": 10,
}
_LEVEL_ALIAS = {"1000": "M", "P": "M", "PM": "M", "I": "500", "T1": "M", "T2": "500"}
SERVE_MEAN = {"m": 0.63, "w": 0.555}
_NO_OFFSET = (0.0, 0.0)
_TOUR_GROUPS = {"atp": "m", "wta": "w"}  # tour_params key -> engine tour group
# constructor parameter -> attribute, for the dynamics that tour_params may override
_TUNABLE = {
    name: name
    for name in (
        "k_base",
        "k_offset",
        "k_shape",
        "mov",
        "slam_k",
        "retired_k",
        "idle_grace",
        "idle_half_life",
        "init_top",
        "init_slope",
        "init_default",
        "games_k",
        "serve_eta",
        "form_decay",
        "fast_k",
        "points_k",
        "surface_serve_eta",
        "mu_rate",
    )
}
_TUNABLE["surface_weight"] = "sw"


def score_info(score: str | None) -> tuple[int, int, int, bool]:
    """(games won by the winner, games won by the loser, sets lost by the winner, retired)."""
    if not score:
        return 0, 0, 0, False
    won = lost = sets_lost = 0
    for a, b in _GAMES.findall(score):
        a, b = int(a), int(b)
        won += a
        lost += b
        if b > a:
            sets_lost += 1
    upper = score.upper()
    retired = "RET" in upper or "DEF" in upper or "ABD" in upper or "ABN" in upper
    return won, lost, sets_lost, retired


# --------------------------------------------------------------------------- Markov chain


def _hold(p: float) -> float:
    q = 1.0 - p
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * p * p / (1 - 2 * p * q)


def _tiebreak(pa: float, pb: float) -> float:
    """P(A wins a tiebreak): A serves point 1, then serve alternates every two points."""
    table = {}
    for total in range(12, -1, -1):
        for i in range(min(total, 6), -1, -1):
            j = total - i
            if j > 6:
                continue
            if i == 6 and j == 6:
                win, lose = pa * (1 - pb), (1 - pa) * pb
                table[i, j] = win / (win + lose)
                continue
            p = pa if ((total + 1) // 2) % 2 == 0 else 1.0 - pb
            up = 1.0 if i == 6 and j <= 5 else table[i + 1, j]
            down = 0.0 if j == 6 and i <= 5 else table[i, j + 1]
            table[i, j] = p * up + (1 - p) * down
    return table[0, 0]


def _set(pa: float, pb: float) -> float:
    """P(A wins a set) when A serves the first game."""
    ga, gb = _hold(pa), 1.0 - _hold(pb)
    tb = _tiebreak(pa, pb)
    table = {}
    for total in range(12, -1, -1):
        for i in range(min(total, 6), -1, -1):
            j = total - i
            if j > 6:
                continue
            if i == 6 and j == 6:
                table[i, j] = tb
                continue
            p = ga if total % 2 == 0 else gb
            if i == 6 and j <= 4:
                table[i, j] = 1.0
                continue
            if j == 6 and i <= 4:
                table[i, j] = 0.0
                continue
            up = table[i + 1, j] if i + 1 <= 6 else 1.0
            down = table[i, j + 1] if j + 1 <= 6 else 0.0
            if i == 6 and j == 5:
                up, down = 1.0, table[6, 6]
            if i == 5 and j == 6:
                up, down = table[6, 6], 0.0
            table[i, j] = p * up + (1 - p) * down
    return table[0, 0]


def match_probability(pa: float, pb: float, best_of_5: bool) -> float:
    """P(A wins the match) from both serve-point probabilities (sets i.i.d.)."""
    s = (_set(pa, pb) + 1.0 - _set(pb, pa)) / 2.0
    if best_of_5:
        return s**3 * (1 + 3 * (1 - s) + 6 * (1 - s) ** 2)
    return s * s * (1 + 2 * (1 - s))


_GRID_LO, _GRID_STEP, _GRID_N = 0.30, 0.01, 66  # serve-point probabilities 0.30 .. 0.95
_MARKOV: dict[bool, list] = {}


def _markov_table(best_of_5: bool) -> list:
    table = _MARKOV.get(best_of_5)
    if table is None:
        grid = [_GRID_LO + _GRID_STEP * i for i in range(_GRID_N)]
        values = np.empty((_GRID_N, _GRID_N))
        for i, pa in enumerate(grid):
            for j, pb in enumerate(grid):
                if j < i:
                    values[i, j] = -values[j, i]
                    continue
                p = min(1 - 1e-9, max(1e-9, match_probability(pa, pb, best_of_5)))
                values[i, j] = math.log(p / (1 - p))
        table = values.tolist()
        _MARKOV[best_of_5] = table
    return table


def _markov_grid(pa: float, pb: float, best_of_5: bool) -> float:
    table = _markov_table(best_of_5)
    top = _GRID_N - 1.000001
    x = min(top, max(0.0, (pa - _GRID_LO) / _GRID_STEP))
    y = min(top, max(0.0, (pb - _GRID_LO) / _GRID_STEP))
    i, j = int(x), int(y)
    fx, fy = x - i, y - j
    a, b = table[i], table[i + 1]
    return (a[j] * (1 - fx) + b[j] * fx) * (1 - fy) + (a[j + 1] * (1 - fx) + b[j + 1] * fx) * fy


def markov_logit(pa: float, pb: float, best_of_5: bool) -> float:
    """Logit of P(A wins) from both serve-point probabilities, exactly antisymmetric."""
    return (_markov_grid(pa, pb, best_of_5) - _markov_grid(pb, pa, best_of_5)) / 2.0


def _logit(p: float) -> float:
    p = min(0.99, max(0.01, p))
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


# --------------------------------------------------------------------------- health signals

# Optional injury / fatigue differences (FeatureEngine(health_features=...)); off by default.
HEALTH_NAMES = (
    "exit_14",  # last appearance was a retirement / default / walkover given, <= 14 days ago
    "exit_30",  # the same, <= 30 days ago
    "wo_60",  # walkovers given in the last 60 days
    "load_48",  # minutes played in the last ~2 days (approximate match days) / 100
    "load_72",  # minutes played in the last ~3 days / 100
    "prev_long",  # the previous match in this event was long (>= 150 min, 5 sets)
    "comeback",  # 1 / (1 + matches since returning from an absence of more than 60 days)
    "comeback_away",  # comeback x log(absence / 60 days)
    "comeback_time",  # exp(-days since that return / 30)
)
_FROM_FINAL = {"F": 0, "BR": 0, "SF": 1, "QF": 2, "R16": 3, "R32": 4, "R64": 5, "R128": 6}
_BACK_SHORT = (0.0, 1.0, 2.0, 3.0, 4.5, 5.5, 6.0)
_BACK_LONG = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0)
_QUALI_BACK = {"Q1": 3.0, "Q2": 2.0, "Q3": 1.0, "Q4": 1.0}


def approx_match_day(day: int, rnd, level, draw) -> float:
    """Approximate day a match was played from the event start day and the round.

    TML dates every match of an event with the event's start. The final is put on the first
    Sunday at least 3 days (10 for Grand Slams and 96+ draws) after the start and earlier
    rounds are counted back from it; qualifying rounds are the days before the start. An
    unknown round (app predictions pass the real match day) keeps ``day``.
    """
    if rnd in _QUALI_BACK:
        return day - _QUALI_BACK[rnd]
    if rnd == "RR":
        return day + 2.0
    back = _FROM_FINAL.get(rnd)
    if back is None:
        return float(day)
    long_event = level == "G" or (draw or 0) >= 96
    first = 10 if long_event else 3
    weekday = (day - 1) % 7  # ordinal 1 is a Monday; 6 = Sunday
    final = day + first + (6 - (weekday + first) % 7) % 7
    table = _BACK_LONG if long_event else _BACK_SHORT
    return max(float(day), final - table[back])


class Health:
    """Per-player injury / fatigue state (kept only when health features are enabled)."""

    __slots__ = ("away", "back", "exits", "last_exit", "last_play", "last_seen", "recent", "since")

    def __init__(self):
        self.recent: deque = deque(maxlen=6)  # (approx day, event key, minutes, long)
        self.exits: deque = deque()  # approx days of walkovers given, last 60 days
        self.last_exit: float | None = None  # approx day of the latest RET/DEF/W.O. given
        self.last_seen: float | None = None  # approx day of the latest appearance
        self.last_play: int | None = None  # event day of the latest played match
        self.back: int | None = None  # event day of the latest return after a long absence
        self.away = 0  # length in days of that absence
        self.since = 0  # matches played since that return (the return match included)

    def __getstate__(self):
        return tuple(getattr(self, name) for name in self.__slots__)

    def __setstate__(self, state):
        for name, value in zip(self.__slots__, state):
            setattr(self, name, value)


_NO_HEALTH = Health()


def health_vector(
    state: Health | None,
    day: int,
    now: float,
    event_key,
    wanted: tuple[str, ...],
    gap: float = 60.0,
    window: float = 120.0,
):
    """Health values of one player (read only) for a match on event day ``day``.

    ``now`` is the approximate match day (``approx_match_day``). A player whose last match is
    more than ``gap`` days old is on his first match back (0 matches since the return).
    """
    state = state or _NO_HEALTH
    exit_gap = None
    if state.last_exit is not None and state.last_exit == state.last_seen:
        exit_gap = now - state.last_exit
    load_48 = load_72 = 0.0
    for when, _event, minutes, _long in reversed(state.recent):
        since_then = now - when
        if since_then > 3.0:
            break
        load_72 += minutes
        if since_then <= 2.0:
            load_48 += minutes
    last = state.recent[-1] if state.recent else None
    back, since, away = state.back, state.since, state.away
    if state.last_play is not None and day - state.last_play > gap:
        back, since, away = day, 0, day - state.last_play
    comeback = away_len = back_time = 0.0
    if back is not None and day - back <= window:
        comeback = 1.0 / (1.0 + since)
        away_len = comeback * math.log(away / gap)
        back_time = math.exp(-(day - back) / 30.0)
    values = {
        "exit_14": 1.0 if exit_gap is not None and exit_gap <= 14.0 else 0.0,
        "exit_30": 1.0 if exit_gap is not None and exit_gap <= 30.0 else 0.0,
        "wo_60": float(sum(1 for when in state.exits if now - when <= 60.0)),
        "load_48": load_48 / 100.0,
        "load_72": load_72 / 100.0,
        "prev_long": 1.0 if last is not None and last[1] == event_key and last[3] else 0.0,
        "comeback": comeback,
        "comeback_away": away_len,
        "comeback_time": back_time,
    }
    return [values[name] for name in wanted]


# --------------------------------------------------------------------------- player state


class Player:
    __slots__ = (
        "elo",
        "n",
        "surf",
        "games",
        "sv",
        "rt",
        "sv_n",
        "last",
        "log",
        "form",
        "form_n",
        "ranks",
        "wins",
        "win_sum",
        "event",
        "fast",
        "peak",
        "peak_day",
        "tour_n",
        "gsurf",
        "pts",
        "svs",
        "ret",
        "vol",
    )

    def __init__(self, elo: float):
        self.elo = elo
        self.n = 0
        self.surf: dict[str, tuple[float, int]] = {}
        self.games = elo
        self.sv = 0.0
        self.rt = 0.0
        self.sv_n = 0
        self.last: int | None = None
        self.log: deque = deque(maxlen=16)  # (day, event key, minutes, games)
        self.form = 0.0
        self.form_n = 0.0
        self.ranks: deque = deque()  # (day, rank), at most one per week, last ~120 days
        self.wins: deque = deque()  # (day, won), last 365 days
        self.win_sum = 0
        self.event: dict[str, list[int]] = {}
        self.fast = elo
        self.peak = elo
        self.peak_day = 0
        self.tour_n = 0
        self.gsurf: dict[str, float] = {}
        self.pts = elo
        self.svs: dict[str, list[float]] = {}  # surface -> [serve offset, return offset]
        self.ret = 0.0  # moving rate of retirements (as the loser)
        self.vol = 0.0  # moving excess of squared surprise over its expectation

    def __getstate__(self):
        return tuple(getattr(self, name) for name in self.__slots__)

    def __setstate__(self, state):
        for name, value in zip(self.__slots__, state):
            setattr(self, name, value)


class FeatureEngine:
    """Online player state and the pre-match feature vector."""

    D_NAMES = (
        "elo_logit",
        "elo_overall",
        "elo_surface",
        "elo_games",
        "serve",
        "return",
        "log_rank",
        "log_points",
        "rank_missing",
        "age",
        "age2",
        "height",
        "lefty",
        "log_n",
        "log_surf_n",
        "form",
        "win_rate_365",
        "rest_days",
        "event_matches",
        "event_minutes",
        "games_7",
        "games_14",
        "matches_30",
        "idle_long",
        "rank_trend",
        "entry_q",
        "entry_wc",
        "entry_ll",
        "entry_pr",
        "seeded",
        "log_seed",
        "log_sv_n",
        "home",
        "event_record",
        "inv_sqrt_n",
        "elo_fast",
        "peak_gap",
        "log_tour_n",
        "games_surface",
        "elo_points",
        "markov",
        "serve_surface",
        "return_surface",
        "retire_rate",
        "volatility",
        "h2h",
    )
    C_NAMES = (
        "tour_atp",
        "tour_wta",
        "tour_ch",
        "tour_q",
        "bo5",
        "surf_hard",
        "surf_clay",
        "surf_grass",
        "surf_carpet",
        "indoor",
        "round",
        "log_draw",
        "level_G",
        "level_M",
        "level_500",
        "level_250",
        "level_D",
        "level_F",
        "level_O",
        "min_log_n",
        "abs_elo_logit",
        "log_min_rank",
        "log_max_rank",
        "retire_max",
        "volatility_sum",
    )

    def __init__(
        self,
        k_base: float = 250.0,
        k_offset: float = 5.0,
        k_shape: float = 0.4,
        surface_weight: float = 0.5,
        mov: float = 0.25,
        slam_k: float = 1.1,
        retired_k: float = 0.5,
        idle_grace: float = 60.0,
        idle_half_life: float = 730.0,
        init_top: float = 2100.0,
        init_slope: float = 80.0,
        init_default: float = 1450.0,
        games_k: float = 2.0,
        serve_eta: float = 0.08,
        form_decay: float = 0.9,
        fast_k: float = 45.0,
        points_k: float = 4.0,
        surface_serve_eta: float = 0.04,
        mu_rate: float = 0.002,
        health_features: tuple[str, ...] | str = (),
        health_gap: float = 60.0,
        health_window: float = 120.0,
        tour_params: dict | None = None,
    ):
        self.k_base = k_base
        self.k_offset = k_offset
        self.k_shape = k_shape
        self.sw = surface_weight
        self.mov = mov
        self.slam_k = slam_k
        self.retired_k = retired_k
        self.idle_grace = idle_grace
        self.idle_half_life = idle_half_life
        self.init_top = init_top
        self.init_slope = init_slope
        self.init_default = init_default
        self.games_k = games_k
        self.serve_eta = serve_eta
        self.form_decay = form_decay
        self.fast_k = fast_k
        self.points_k = points_k
        self.surface_serve_eta = surface_serve_eta
        self.mu_rate = mu_rate
        self.mu: dict[tuple[str, str], float] = {}  # (tour group, surface) -> serve logit
        self.players: dict[str, Player] = {}
        self.h2h: dict[tuple[str, str], list[int]] = {}
        self.home: dict[tuple[str, str], dict[str, int]] = {}  # (group, event) -> {ioc: n}
        self._k_cache = [k_base / (n + k_offset) ** k_shape for n in range(4000)]
        self.health = _health_names(health_features)
        self.health_state: dict[str, Health] = {}
        self.health_gap = health_gap
        self.health_window = health_window
        self.d_names = self.D_NAMES + self.health
        self._dynamics: dict[str, dict] = {}
        self._active: str | None = None
        if tour_params:
            self._set_tour_params(tour_params)

    # ------------------------------------------------------------------ per-tour dynamics

    def _set_tour_params(self, tour_params: dict) -> None:
        """Per tour-group overrides of the dynamics ({"atp": {...}, "wta": {...}}).

        "atp" covers the men's group (ATP, Challenger, qualifying: they share players), "wta"
        the women's. Player, serve-mean and home states never cross groups, so each group
        can run its own K, surface weight, MOV, idle decay, serve learning rates and so on.
        """
        base = {attr: getattr(self, attr) for attr in _TUNABLE.values()}
        for name, values in tour_params.items():
            if name not in _TOUR_GROUPS:
                raise ValueError(f"tour_params: grup necunoscut {name!r} (atp sau wta).")
            unknown = set(values) - set(_TUNABLE)
            if unknown:
                raise ValueError(f"tour_params: parametri necunoscuți {sorted(unknown)}.")
        for name, group in _TOUR_GROUPS.items():
            values = dict(base)
            for param, value in (tour_params.get(name) or {}).items():
                values[_TUNABLE[param]] = float(value)
            values["_k_cache"] = [
                values["k_base"] / (n + values["k_offset"]) ** values["k_shape"]
                for n in range(4000)
            ]
            self._dynamics[group] = values
        self._use("m")

    def _use(self, group: str) -> None:
        """Switch the dynamics attributes to one tour group (no-op without overrides)."""
        if self._dynamics and group != self._active:
            self.__dict__.update(self._dynamics[group])
            self._active = group

    # ------------------------------------------------------------------ state helpers

    def _init_elo(self, rank: int | None) -> float:
        if rank and rank > 0:
            return max(1300.0, self.init_top - self.init_slope * math.log(rank))
        return self.init_default

    def player(self, key: str, rank: int | None) -> Player:
        state = self.players.get(key)
        if state is None:
            state = Player(self._init_elo(rank))
            self.players[key] = state
        return state

    def peek(self, key: str, rank: int | None) -> Player:
        """The player's state, or a fresh one that is NOT stored (prediction for a newcomer)."""
        return self.players.get(key) or Player(self._init_elo(rank))

    def _mu(self, tour_group: str, surface: str) -> float:
        mu = self.mu.get((tour_group, surface))
        return _logit(SERVE_MEAN[tour_group]) if mu is None else mu

    def k(self, n: int) -> float:
        cache = self._k_cache
        return cache[n] if n < len(cache) else self.k_base / (n + self.k_offset) ** self.k_shape

    def ratings(self, state: Player, surface: str, day: int, rank: int | None):
        """(overall, surface, games Elo, surface matches) after the idle shrink."""
        entry = state.surf.get(surface)
        overall = state.elo
        own = entry[0] if entry else overall
        games = state.games
        if state.last is not None:
            idle = day - state.last - self.idle_grace
            if idle > 0:
                f = 0.5 ** (idle / self.idle_half_life)
                base = self._init_elo(rank)
                overall = base + (overall - base) * f
                own = base + (own - base) * f
                games = base + (games - base) * f
        return overall, own, games, (entry[1] if entry else 0)

    # ------------------------------------------------------------------ features

    def _profile(self, state, side, surface, day, event_key, tour_group, tname):
        """Per-player part of the difference vector (without the pairwise h2h)."""
        _key, hand, ht, ioc, age, rank, points, seed, entry = side
        overall, own, games, surf_n = self.ratings(state, surface, day, rank)
        n = state.n
        form = state.form / (state.form_n + 2.0)
        last = state.last
        rest = 60.0 if last is None else min(60.0, max(0.0, day - last))
        ev_matches = ev_minutes = g7 = g14 = m30 = 0.0
        for d, ev, minutes, played in reversed(state.log):
            gap = day - d
            if gap > 30:
                break
            m30 += 1.0
            if gap <= 14:
                g14 += played
                if gap <= 7:
                    g7 += played
            if ev == event_key:
                ev_matches += 1.0
                ev_minutes += minutes
        wins = state.wins
        win_sum = state.win_sum
        drop = 0
        # read-only view: expired results are skipped, not removed (predict stays pure)
        while drop < len(wins) and day - wins[drop][0] > 365:
            win_sum -= wins[drop][1]
            drop += 1
        win_rate = (win_sum + 2.0) / (len(wins) - drop + 4.0) - 0.5
        trend = 0.0
        if rank:
            for when, old in state.ranks:
                if day - when <= 120:
                    trend = math.log(old) - math.log(rank)
                    break
        lrank = math.log(rank) if rank else 7.3
        a = (age if age else 26.0) - 26.0
        h = ((ht if ht else (185.0 if tour_group == "m" else 172.0)) - 180.0) / 10.0
        home = 0.0
        if ioc:
            home_map = self.home.get((tour_group, tname))
            if home_map:
                home = home_map.get(ioc, 0) / (sum(home_map.values()) + 2.0)
        rec = state.event.get(tname)
        event_record = (rec[0] - rec[1]) / (rec[0] + rec[1] + 4.0) if rec else 0.0
        return (
            overall,
            own,
            games,
            state.sv,
            state.rt,
            -lrank,
            math.log1p(points) if points else 0.0,
            0.0 if rank else 1.0,
            a,
            a * a / 10.0,
            h,
            1.0 if hand == "L" else 0.0,
            math.log1p(n),
            math.log1p(surf_n),
            form,
            win_rate,
            math.log1p(rest),
            ev_matches,
            ev_minutes / 100.0,
            g7 / 20.0,
            g14 / 20.0,
            m30,
            1.0 if rest >= 45.0 and last is not None else 0.0,
            trend,
            1.0 if entry == "Q" else 0.0,
            1.0 if entry == "WC" else 0.0,
            1.0 if entry == "LL" else 0.0,
            1.0 if entry in ("PR", "SE", "ALT") else 0.0,
            1.0 if seed else 0.0,
            -math.log(seed) if seed else 0.0,
            math.log1p(state.sv_n),
            home,
            event_record,
            1.0 / math.sqrt(n + 1.0),
            LN10_400 * state.fast,
            LN10_400 * ((state.peak if day - state.peak_day <= 365 else state.elo) - state.elo),
            math.log1p(state.tour_n),
            LN10_400 * state.gsurf.get(surface, games),
            LN10_400 * state.pts,
        )

    def features(self, first, second, match, *, store: bool = True):
        """(D, C): antisymmetric difference vector and symmetric context.

        ``store=False`` never creates player entries (used for predictions, so a prediction
        cannot change the state).
        """
        tour, day, tourney_id, tname, level, draw, surface, indoor, best_of, rnd = match
        tour_group = "w" if tour == "wta" else "m"
        self._use(tour_group)
        surface = surface or "Hard"
        event_key = (tour_group, tourney_id)
        get = self.player if store else self.peek
        sa = get(first[0], first[5])
        sb = get(second[0], second[5])
        pa = self._profile(sa, first, surface, day, event_key, tour_group, tname)
        pb = self._profile(sb, second, surface, day, event_key, tour_group, tname)
        sw = self.sw
        elo_logit = LN10_400 * ((1 - sw) * (pa[0] - pb[0]) + sw * (pa[1] - pb[1]))
        diffs = [
            elo_logit,
            LN10_400 * (pa[0] - pb[0]),
            LN10_400 * (pa[1] - pb[1]),
            LN10_400 * (pa[2] - pb[2]),
        ]
        diffs.extend([x - y for x, y in zip(pa[3:], pb[3:])])
        mu = self._mu(tour_group, surface)
        oa = sa.svs.get(surface, _NO_OFFSET)
        ob = sb.svs.get(surface, _NO_OFFSET)
        sva, rta = sa.sv + oa[0], sa.rt + oa[1]
        svb, rtb = sb.sv + ob[0], sb.rt + ob[1]
        diffs.append(markov_logit(sigmoid(mu + sva - rtb), sigmoid(mu + svb - rta), best_of == 5))
        diffs.append(sva - svb)
        diffs.append(rta - rtb)
        diffs.append(sa.ret - sb.ret)
        diffs.append(sa.vol - sb.vol)
        pair = self.h2h.get((first[0], second[0]))
        diffs.append((pair[0] - pair[1]) / (pair[0] + pair[1] + 3.0) if pair else 0.0)
        if self.health:
            now = approx_match_day(day, rnd, level, draw)
            cfg = (event_key, self.health, self.health_gap, self.health_window)
            ha = health_vector(self.health_state.get(first[0]), day, now, *cfg)
            hb = health_vector(self.health_state.get(second[0]), day, now, *cfg)
            diffs.extend([x - y for x, y in zip(ha, hb)])
        lvl = _LEVEL_ALIAS.get(level, level)
        if tour == "challenger":
            lvl = "C"
        elif tour == "quali":
            lvl = "Q"
        r1, r2 = first[5], second[5]
        lo = math.log(min(r1, r2)) if r1 and r2 else math.log(r1 or r2 or 1500.0)
        hi = math.log(max(r1, r2)) if r1 and r2 else 7.3
        context = [
            1.0 if tour == "atp" else 0.0,
            1.0 if tour == "wta" else 0.0,
            1.0 if tour == "challenger" else 0.0,
            1.0 if tour == "quali" else 0.0,
            1.0 if best_of == 5 else 0.0,
            1.0 if surface == "Hard" else 0.0,
            1.0 if surface == "Clay" else 0.0,
            1.0 if surface == "Grass" else 0.0,
            1.0 if surface == "Carpet" else 0.0,
            1.0 if indoor == "I" else 0.0,
            float(_ROUND_INDEX.get(rnd, 7)),
            math.log(draw) if draw else 3.5,
            1.0 if lvl == "G" else 0.0,
            1.0 if lvl == "M" else 0.0,
            1.0 if lvl == "500" else 0.0,
            1.0 if lvl == "250" else 0.0,
            1.0 if lvl == "D" else 0.0,
            1.0 if lvl == "F" else 0.0,
            1.0 if lvl == "O" else 0.0,
            math.log1p(min(sa.n, sb.n)),
            abs(elo_logit),
            lo,
            hi,
            max(sa.ret, sb.ret),
            sa.vol + sb.vol,
        ]
        return diffs, context

    # ------------------------------------------------------------------ update

    def update(self, row, day: int) -> None:
        """Add one finished match (walkovers only feed the home map)."""
        tour = row["tour"]
        tour_group = "w" if tour == "wta" else "m"
        self._use(tour_group)
        tname = row["tourney_name"]
        for side in ("winner", "loser"):
            if row[f"{side}_entry"] == "WC" and row[f"{side}_ioc"]:
                counts = self.home.setdefault((tour_group, tname), {})
                ioc = row[f"{side}_ioc"]
                counts[ioc] = counts.get(ioc, 0) + 1
        wkey, lkey = row["winner_key"], row["loser_key"]
        if self.health and wkey and lkey and wkey != lkey:
            self._update_health(row, day)
        if not wkey or not lkey or wkey == lkey or row["is_walkover"]:
            return
        wrank, lrank = row["winner_rank"], row["loser_rank"]
        w = self.player(wkey, wrank)
        lo = self.player(lkey, lrank)
        surface = row["surface"] or "Hard"
        w_overall, w_own, w_games, w_sn = self.ratings(w, surface, day, wrank)
        l_overall, l_own, l_games, l_sn = self.ratings(lo, surface, day, lrank)
        gw, gl, sets_lost, retired = score_info(row["score"])
        weight = self.retired_k if retired else 1.0
        if not retired and sets_lost == 0:
            weight *= 1.0 + self.mov
        if row["tourney_level"] == "G":
            weight *= self.slam_k
        k_w, k_l = self.k(w.n), self.k(lo.n)
        change = 1.0 - sigmoid(LN10_400 * (w_overall - l_overall))
        w.elo = w_overall + weight * k_w * change
        lo.elo = l_overall - weight * k_l * change
        f_change = 1.0 - sigmoid(LN10_400 * (w.fast - lo.fast))
        w.fast += self.fast_k * f_change
        lo.fast -= self.fast_k * f_change
        for state in (w, lo):
            if state.elo >= state.peak or day - state.peak_day > 365:
                state.peak, state.peak_day = state.elo, day
        if tour in ("atp", "wta"):
            w.tour_n += 1
            lo.tour_n += 1
        s_change = 1.0 - sigmoid(LN10_400 * (w_own - l_own))
        w.surf[surface] = (w_own + weight * self.k(w_sn) * s_change, w_sn + 1)
        lo.surf[surface] = (l_own - weight * self.k(l_sn) * s_change, l_sn + 1)
        if gw + gl >= 6:
            g_change = gw / (gw + gl) - sigmoid(LN10_400 * (w_games - l_games))
            w.games = w_games + self.games_k * k_w * g_change
            lo.games = l_games - self.games_k * k_l * g_change
            wg, lg = w.gsurf.get(surface, w_games), lo.gsurf.get(surface, l_games)
            g_change = gw / (gw + gl) - sigmoid(LN10_400 * (wg - lg))
            w.gsurf[surface] = wg + self.games_k * self.k(w_sn) * g_change
            lo.gsurf[surface] = lg - self.games_k * self.k(l_sn) * g_change
        else:
            w.games, lo.games = w_games, l_games
        blend = (1 - self.sw) * (w_overall - l_overall) + self.sw * (w_own - l_own)
        residual = 1.0 - sigmoid(LN10_400 * blend)
        excess = residual * residual - residual * (1.0 - residual)
        w.vol = 0.95 * w.vol + 0.05 * excess
        lo.vol = 0.95 * lo.vol + 0.05 * excess
        w.ret *= 0.97
        lo.ret = 0.97 * lo.ret + (0.03 if retired else 0.0)
        if not retired:
            decay = self.form_decay
            w.form = w.form * decay + residual
            w.form_n = w.form_n * decay + 1.0
            lo.form = lo.form * decay - residual
            lo.form_n = lo.form_n * decay + 1.0
        wsv, lsv = row["w_svpt"], row["l_svpt"]
        if wsv and lsv and wsv >= 20 and lsv >= 20 and not retired:
            mu = self._mu(tour_group, surface)
            w_obs = ((row["w_1stWon"] or 0) + (row["w_2ndWon"] or 0)) / wsv
            l_obs = ((row["l_1stWon"] or 0) + (row["l_2ndWon"] or 0)) / lsv
            ow = w.svs.get(surface)
            if ow is None:
                ow = w.svs[surface] = [0.0, 0.0]
            ol = lo.svs.get(surface)
            if ol is None:
                ol = lo.svs[surface] = [0.0, 0.0]
            r_w = _logit(w_obs) - (mu + w.sv + ow[0] - lo.rt - ol[1])
            r_l = _logit(l_obs) - (mu + lo.sv + ol[0] - w.rt - ow[1])
            eta_w = max(self.serve_eta, 1.0 / (w.sv_n + 3.0))
            eta_l = max(self.serve_eta, 1.0 / (lo.sv_n + 3.0))
            w.sv += eta_w * r_w
            lo.rt -= eta_l * r_w
            lo.sv += eta_l * r_l
            w.rt -= eta_w * r_l
            se = self.surface_serve_eta
            ow[0] += se * r_w
            ol[1] -= se * r_w
            ol[0] += se * r_l
            ow[1] -= se * r_l
            self.mu[(tour_group, surface)] = mu + self.mu_rate * (r_w + r_l) / 2.0
            w.sv_n += 1
            lo.sv_n += 1
            share = (w_obs * wsv + (1.0 - l_obs) * lsv) / (wsv + lsv)
            p_change = share - sigmoid(LN10_400 * (w.pts - lo.pts))
            w.pts += self.points_k * k_w * p_change
            lo.pts -= self.points_k * k_l * p_change
        minutes = row["minutes"]
        games = gw + gl
        if not minutes or minutes <= 0 or minutes > 400:
            minutes = games * 4.4
        event_key = (tour_group, row["tourney_id"])
        for state, won in ((w, 1), (lo, 0)):
            state.log.append((day, event_key, minutes, games))
            wins = state.wins
            while wins and day - wins[0][0] > 365:
                state.win_sum -= wins.popleft()[1]
            wins.append((day, won))
            state.win_sum += won
            state.n += 1
            state.last = day
            rec = state.event.get(tname)
            if rec is None:
                rec = state.event[tname] = [0, 0]
            rec[1 - won] += 1
        for state, rank in ((w, wrank), (lo, lrank)):
            if rank:
                ranks = state.ranks
                while ranks and day - ranks[0][0] > 120:
                    ranks.popleft()
                if not ranks or day - ranks[-1][0] >= 7:
                    ranks.append((day, rank))
        pair = self.h2h.get((wkey, lkey))
        if pair is None:
            pair = self.h2h[(wkey, lkey)] = [0, 0]
            self.h2h[(lkey, wkey)] = [0, 0]
        pair[0] += 1
        self.h2h[(lkey, wkey)][1] += 1

    def _update_health(self, row, day: int) -> None:
        """Health state after one row: walkovers given, retirements and the minutes played."""
        when = approx_match_day(day, row["round"], row["tourney_level"], row["draw_size"])
        states = []
        for key in (row["winner_key"], row["loser_key"]):
            state = self.health_state.get(key)
            if state is None:
                state = self.health_state[key] = Health()
            states.append(state)
        loser = states[1]
        if row["is_walkover"]:
            if "W/O" in (row["score"] or "").upper():
                loser.exits.append(when)
                while loser.exits and when - loser.exits[0] > 60.0:
                    loser.exits.popleft()
                loser.last_exit = loser.last_seen = when
            return
        gw, gl, _sets_lost, _retired = score_info(row["score"])
        upper = (row["score"] or "").upper()
        if "RET" in upper or "DEF" in upper:
            loser.last_exit = when
        minutes = row["minutes"]
        games = gw + gl
        if not minutes or minutes <= 0 or minutes > 400:
            minutes = games * 4.4
        sets = len(_GAMES.findall(row["score"] or ""))
        long_match = minutes >= 150 or sets >= 5
        event_key = ("w" if row["tour"] == "wta" else "m", row["tourney_id"])
        for state in states:
            state.recent.append((when, event_key, minutes, long_match))
            state.last_seen = when
            if state.last_play is not None and day - state.last_play > self.health_gap:
                state.back, state.since, state.away = day, 1, day - state.last_play
            elif state.back is not None:
                state.since += 1
            state.last_play = day


def _health_names(value) -> tuple[str, ...]:
    """Enabled health features in canonical order ("all", a comma string or a sequence)."""
    if not value:
        return ()
    if isinstance(value, str):
        value = HEALTH_NAMES if value == "all" else [v.strip() for v in value.split(",")]
    unknown = set(value) - set(HEALTH_NAMES)
    if unknown:
        raise ValueError(f"Trăsături de sănătate necunoscute: {sorted(unknown)}")
    return tuple(name for name in HEALTH_NAMES if name in value)


# --------------------------------------------------------------------------- the stack

_D = {name: i for i, name in enumerate(FeatureEngine.D_NAMES)}
_C = {name: i for i, name in enumerate(FeatureEngine.C_NAMES)}
N_D, N_C = len(FeatureEngine.D_NAMES), len(FeatureEngine.C_NAMES)


def lr_design(d: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Logistic inputs: the differences plus differences x context interactions.

    Every column is a difference times a symmetric quantity, so swapping the players negates
    the whole row and the model (no intercept) is exactly symmetric.
    """
    d = np.asarray(d, dtype=float)
    c = np.asarray(c, dtype=float)
    if d.ndim == 1:
        d, c = d[None, :], c[None, :]
    elo = d[:, 0:1]
    rank = d[:, _D["log_rank"] : _D["log_rank"] + 1]
    col = {name: c[:, i : i + 1] for name, i in _C.items()}
    inter = [
        elo * col["bo5"],
        elo * col["tour_wta"],
        elo * col["tour_ch"],
        elo * col["tour_q"],
        elo / np.sqrt(1.0 + np.expm1(col["min_log_n"])),
        elo * col["level_G"],
        elo * col["level_D"],
        rank * col["tour_wta"],
        rank * col["bo5"],
        elo * np.abs(elo),
        d * col["tour_wta"],
        d * col["tour_ch"],
        d * col["min_log_n"],
        d * col["volatility_sum"],
    ]
    return np.hstack([d] + inter)


def fit_symmetric_logistic(
    d: np.ndarray,
    c: np.ndarray,
    l2: float = 1.0,
    start: np.ndarray | None = None,
    chunk: int = 65536,
    max_iter: int = 30,
    tol: float = 1e-7,
) -> np.ndarray:
    """Ridge logistic regression without intercept on winner-first rows, by Newton steps.

    Training on (x, 1) and (-x, 0) is the same as ``min 0.5 |w|^2 + 2 C sum log(1 + e^{-x w})``
    with columns scaled to unit RMS (the sklearn ``C = 1/l2`` convention of the audited
    candidate). The design is rebuilt chunk by chunk to keep memory small. Returns the
    weights on the unscaled design.
    """
    with _blas_threads(4):
        return _newton(d, c, l2, start, chunk, max_iter, tol)


def _newton(d, c, l2, start, chunk, max_iter, tol):
    n = len(d)
    width = lr_design(d[:1], c[:1]).shape[1]
    square = np.zeros(width)
    for lo in range(0, n, chunk):
        x = lr_design(d[lo : lo + chunk], c[lo : lo + chunk])
        square += (x * x).sum(axis=0)
    scale = np.sqrt(square / max(n, 1)) + 1e-9
    strength = 2.0 / l2
    w = np.zeros(width) if start is None else start * scale

    def evaluate(weights):
        value = 0.5 * float(weights @ weights)
        grad = weights.copy()
        hess = np.eye(width)
        for lo in range(0, n, chunk):
            x = lr_design(d[lo : lo + chunk], c[lo : lo + chunk]) / scale
            z = x @ weights
            value += strength * float(np.logaddexp(0.0, -z).sum())
            s = 0.5 * (1.0 + np.tanh(0.5 * z))  # stable logistic
            grad -= strength * (x.T @ (1.0 - s))
            weighted = x * (s * (1.0 - s))[:, None]
            hess += strength * (weighted.T @ x)
        return value, grad, hess

    value, grad, hess = evaluate(w)
    if start is not None:
        # a warm start can be far off when a rare column's scale changed: keep the better one
        cold = evaluate(np.zeros(width))
        if cold[0] < value:
            w = np.zeros(width)
            value, grad, hess = cold
    for _ in range(max_iter):
        step = np.linalg.solve(hess, grad)
        size = 1.0
        while True:  # damped Newton: halve the step until the objective does not increase
            trial = w - size * step
            trial_value, trial_grad, trial_hess = evaluate(trial)
            if trial_value <= value + 1e-9 * abs(value) or size < 1e-3:
                break
            size *= 0.5
        w, value, grad, hess = trial, trial_value, trial_grad, trial_hess
        if float(np.max(np.abs(size * step))) < tol:
            break
    return w / scale


class _blas_threads:
    """Cap BLAS threads during a fit (many idle OpenBLAS threads were 20x slower here)."""

    def __init__(self, limit: int):
        self.limit = limit
        self._context = None

    def __enter__(self):
        try:
            from threadpoolctl import threadpool_limits
        except ImportError:  # optional dependency: fits are only slower without it
            return self
        self._context = threadpool_limits(self.limit)
        return self

    def __exit__(self, *exc):
        if self._context is not None:
            self._context.restore_original_limits()
        return False


class SampleStore:
    """Growing float64 matrices of D and C rows (compact compared with lists of lists)."""

    def __init__(self, block: int = 8192):
        self.block = block
        self._d_blocks: list[np.ndarray] = []
        self._c_blocks: list[np.ndarray] = []
        self._d_open: list[list[float]] = []
        self._c_open: list[list[float]] = []

    def __len__(self) -> int:
        return sum(len(b) for b in self._d_blocks) + len(self._d_open)

    def append(self, d: list[float], c: list[float]) -> None:
        self._d_open.append(d)
        self._c_open.append(c)
        if len(self._d_open) >= self.block:
            self._seal()

    def _seal(self) -> None:
        if self._d_open:
            self._d_blocks.append(np.asarray(self._d_open, dtype=float))
            self._c_blocks.append(np.asarray(self._c_open, dtype=float))
            self._d_open, self._c_open = [], []

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        self._seal()
        if not self._d_blocks:
            return np.empty((0, N_D)), np.empty((0, N_C))
        d = np.concatenate(self._d_blocks)
        c = np.concatenate(self._c_blocks)
        self._d_blocks, self._c_blocks = [d], [c]
        return d, c
