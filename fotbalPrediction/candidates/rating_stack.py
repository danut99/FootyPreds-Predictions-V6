"""rating_stack: online rating features + a machine-learned stack for the goal markets.

Idea
----
A walk-forward feature builder keeps, per team (keyed by country and football-data name):

- three Elo variants: a goal-difference-weighted Elo with a learned per-league home edge,
  and a home-only / away-only Elo pair (the home rating of the host meets the away rating
  of the visitor);
- pi-ratings (Constantinou & Fenton 2013: home and away rating per team, expected goal
  difference, log-damped error updates with cross-learning);
- exponentially weighted form (short and long memory) of goals, shots, shots on target,
  corners and points, for and against;
- rest days, matches played in the league, the current-season points and goal difference
  per game, the rank percentile in the current league table, promoted / relegated / new
  flags, the league tier and the share of the season already played;
- league context: decayed mean home and away goals, draw rate;
- optionally (``use_dc=True``) the harness baseline's Dixon-Coles expected goals
  (``candidates.baseline``, fed through the same deferred update). Off by default: on
  2223/2324 it did not improve any log loss once the ratings and form were present.

Every feature of a match on date D is computed from rows with a date < D: ``update`` only
buffers the rows of the current date and the buffer is applied when a later date arrives
(training features of those rows are frozen BEFORE they are applied), exactly mirroring
the benchmark's walk order.

Models, refitted at the start of every evaluated season (and every ``refit_days``) on the
earlier samples only (seasons from ``train_first``, sample weight halving every
``sample_half_life`` days):

- 1X2: the mean of a multinomial logistic regression (standardised features plus squared
  rating gaps, log form rates and a has-stats flag; L2 ``C``) and a HistGradientBoosting
  classifier (``learner="blend"``; ``"logit"`` / ``"hgb"`` use one of them);
- goals: two Poisson GLMs (home and away goals) give the rates of a Dixon-Coles matrix
  (``rho``); the matrix is re-weighted to the stacked 1X2 and its total is pulled halfway
  (``totals_weight`` 0.5) towards the stacked over-2.5 classifier (``fit_total``), so every
  goal market (DC, totals, team totals, BTTS, AH incl. quarter lines, exact score) comes
  from one coherent matrix;
- with ``ctx.odds`` (``--odds``), a second stack trained on the rows priced by the same
  source adds the margin-free market 1X2 and over 2.5 as features.

Tuned offline on 2223/2324 only (``rating_stack_tune``): C, sample half-life, learner,
feature expansion, totals weight, rho and HGB size.

Half-time, corners, cards, bookings and shots on target are passed through from the
embedded baseline (``passthrough=True``); this candidate does not model them.

Selection rule (frozen on 2223/2324 before the 2425 confirmation): ONE pick per match, the
highest-probability key of ``select_keys`` (goal markets without near-certain lines), taken
when p >= ``threshold`` (0.78, target >= 80% accuracy) or p >= ``threshold_high`` (0.82,
strict target >= 85%) and both teams have >= ``min_team_matches`` league matches. On the tune
seasons this gave 83.4% / 84.7% accuracy at 75.6% / 73.7% match coverage (mean fair odds 1.20)
and 87.3% / 88.5% at 38.5% / 38.9% (fair odds 1.15). Most picks (home/away over 0.5, under
3.5, over 1.5) have no real price in the data: never call them profitable; where priced (1X2,
double chance) the flat-stake ROI at average pre-closing prices is negative (about -3 to -5%).
"""

from __future__ import annotations

import math

import numpy as np

from fotbalPrediction import markets as mk
from fotbalPrediction.candidates.baseline import BaselineModel

ELO_BASE = 1500.0
FORM_FIELDS = ("gf", "ga", "sf", "sa", "tf", "ta", "cf", "ca", "pts")
STAT_COLUMNS = {
    "s": ("home_shots", "away_shots"),
    "t": ("home_sot", "away_sot"),
    "c": ("home_corners", "away_corners"),
}
# Selection menu (frozen on 2223/2324): goal markets without the near-certain lines (over 0.5,
# under 4.5, +1.5 handicaps, team under 2.5), whose fair odds (about 1.03-1.10) make a "pick"
# meaningless.
DEFAULT_SELECT_KEYS = (
    "1",
    "X",
    "2",
    "1X",
    "X2",
    "12",
    "dnb_1",
    "dnb_2",
    "over15",
    "over25",
    "over35",
    "under15",
    "under25",
    "under35",
    "btts",
    "no_btts",
    "home_over05",
    "away_over05",
    "home_over15",
    "away_over15",
    "home_under_1.5",
    "away_under_1.5",
)


def _pi_goal(rating: float, c: float = 3.0) -> float:
    """Expected goal difference implied by a pi-rating."""
    return math.copysign(10 ** (abs(rating) / c) - 1.0, rating)


def _elo_margin(gd: int) -> float:
    gd = abs(gd)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


class Team:
    __slots__ = (
        "elo",
        "elo_h",
        "elo_a",
        "pi_h",
        "pi_a",
        "short",
        "long",
        "last_day",
        "league",
        "tier",
        "season",
        "s_pts",
        "s_gd",
        "s_n",
        "n_league",
        "moved",
    )

    def __init__(self):
        self.elo = ELO_BASE
        self.elo_h = ELO_BASE
        self.elo_a = ELO_BASE
        self.pi_h = 0.0
        self.pi_a = 0.0
        # EW sums per field: [weighted sum, weight]
        self.short = {f: [0.0, 0.0] for f in FORM_FIELDS}
        self.long = {f: [0.0, 0.0] for f in FORM_FIELDS}
        self.last_day = None
        self.league = None
        self.tier = 0
        self.season = None
        self.s_pts = 0
        self.s_gd = 0
        self.s_n = 0
        self.n_league = 0
        self.moved = 0  # +1 promoted, -1 relegated, 2 new, 0 same league


class LeagueState:
    __slots__ = ("home_adv", "hg", "ag", "draw", "n", "season", "season_start", "teams")

    def __init__(self):
        self.home_adv = 60.0
        self.hg = [1.5 * 20, 20.0]
        self.ag = [1.15 * 20, 20.0]
        self.draw = [0.26 * 20, 20.0]
        self.n = 0
        self.season = None
        self.season_start = None
        self.teams: set[str] = set()


def _ew(pair) -> float:
    return pair[0] / pair[1] if pair[1] > 0 else math.nan


class FeatureBuilder:
    """Online team / league state; features only ever see rows of earlier dates."""

    def __init__(
        self,
        elo_k: float = 20.0,
        elo_side_k: float = 15.0,
        pi_lambda: float = 0.054,
        pi_gamma: float = 0.79,
        short_alpha: float = 0.25,
        long_alpha: float = 0.06,
        league_alpha: float = 0.003,
        promoted_shift: float = -60.0,
        relegated_shift: float = 40.0,
        season_regress: float = 0.2,
    ):
        self.elo_k = elo_k
        self.elo_side_k = elo_side_k
        self.pi_lambda = pi_lambda
        self.pi_gamma = pi_gamma
        self.short_alpha = short_alpha
        self.long_alpha = long_alpha
        self.league_alpha = league_alpha
        self.promoted_shift = promoted_shift
        self.relegated_shift = relegated_shift
        self.season_regress = season_regress
        self.teams: dict[str, Team] = {}
        self.leagues: dict[str, LeagueState] = {}
        self._rank_cache: dict[tuple[str, int], dict[str, float]] = {}

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def team_key(country: str, name: str) -> str:
        return f"{country}|{name}"

    def _league(self, code: str) -> LeagueState:
        state = self.leagues.get(code)
        if state is None:
            state = self.leagues[code] = LeagueState()
        return state

    def _team_for(self, country: str, name: str, league: str, tier: int, season: str) -> Team:
        """Team state as it enters a match of `league`/`season` (league moves applied)."""
        key = self.team_key(country, name)
        team = self.teams.get(key)
        if team is None:
            team = self.teams[key] = Team()
            team.moved = 2
            team.league = league
            team.tier = tier
            team.season = season
            shift = self.promoted_shift
            team.elo += shift
            team.elo_h += shift
            team.elo_a += shift
            return team
        if team.season != season:
            # New season: regress the Elo variants to the mean, reset the season table.
            r = self.season_regress
            team.elo = ELO_BASE + (team.elo - ELO_BASE) * (1 - r)
            team.elo_h = ELO_BASE + (team.elo_h - ELO_BASE) * (1 - r)
            team.elo_a = ELO_BASE + (team.elo_a - ELO_BASE) * (1 - r)
            team.season = season
            team.s_pts = team.s_gd = team.s_n = 0
            team.moved = 0
        if team.league != league:
            moved = 1 if (tier and team.tier and tier < team.tier) else -1
            if not tier or not team.tier:
                moved = 2
            shift = (
                self.promoted_shift
                if moved == 1
                else (self.relegated_shift if moved == -1 else 0.0)
            )
            for name_attr in ("elo", "elo_h", "elo_a"):
                setattr(team, name_attr, ELO_BASE + shift)
            team.pi_h *= 0.3
            team.pi_a *= 0.3
            team.moved = moved
            team.league = league
            team.tier = tier
            team.n_league = 0
        return team

    def _rank(self, league: str, day: int, team_name: str) -> float:
        cache_key = (league, day)
        table = self._rank_cache.get(cache_key)
        if table is None:
            state = self._league(league)
            entries = []
            for key in state.teams:
                t = self.teams[key]
                if t.league == league and t.season == state.season and t.s_n > 0:
                    entries.append((t.s_pts / t.s_n, t.s_gd / t.s_n, key))
            entries.sort(reverse=True)
            n = len(entries)
            table = {k: (i / (n - 1) if n > 1 else 0.5) for i, (_, _, k) in enumerate(entries)}
            if len(self._rank_cache) > 4096:
                self._rank_cache.clear()
            self._rank_cache[cache_key] = table
        return table.get(team_name, 0.5)

    # ------------------------------------------------------------------ features

    def features(self, row_like, day: int) -> tuple[np.ndarray, int, int]:
        """(feature vector, home league matches, away league matches) before `day`."""
        league = self._league(row_like.league)
        season = row_like.season
        tier = row_like.tier or 0
        # The team objects are peeked without mutating their state.
        home = self._peek(row_like.country, row_like.home, row_like.league, tier, season, day)
        away = self._peek(row_like.country, row_like.away, row_like.league, tier, season, day)
        hk = self.team_key(row_like.country, row_like.home)
        ak = self.team_key(row_like.country, row_like.away)
        if league.season == season and league.season_start is not None:
            progress = min(1.5, (day - league.season_start) / 300.0)
        else:
            progress = 0.0
        rank_h = self._rank(row_like.league, day, hk) if home["s_n"] else 0.5
        rank_a = self._rank(row_like.league, day, ak) if away["s_n"] else 0.5
        lh, la, ld = _ew(league.hg), _ew(league.ag), _ew(league.draw)
        values = [
            (home["elo"] + league.home_adv - away["elo"]) / 400.0,
            (home["elo_h"] - away["elo_a"]) / 400.0,
            _pi_goal(home["pi_h"]) - _pi_goal(away["pi_a"]),
            _pi_goal(home["pi_h"]) + _pi_goal(away["pi_a"]),
            league.home_adv / 400.0,
            math.log(lh),
            math.log(la),
            ld,
            float(tier),
            progress,
        ]
        for side, rank in ((home, rank_h), (away, rank_a)):
            values += [
                side["ppg"],
                side["gdpg"],
                min(side["s_n"], 38) / 38.0,
                rank,
                math.log1p(min(side["n_league"], 60)),
                math.log1p(min(side["rest"], 60)),
                1.0 if side["moved"] == 1 else 0.0,
                1.0 if side["moved"] == -1 else 0.0,
                1.0 if side["moved"] == 2 else 0.0,
            ]
            for memory in ("short", "long"):
                form = side[memory]
                values += [form[f] for f in FORM_FIELDS]
        return np.asarray(values, dtype=float), home["n_league"], away["n_league"]

    def _peek(self, country: str, name: str, league: str, tier: int, season: str, day: int) -> dict:
        team = self.teams.get(self.team_key(country, name))
        if team is None:
            elo = ELO_BASE + self.promoted_shift
            return {
                "elo": elo,
                "elo_h": elo,
                "elo_a": elo,
                "pi_h": 0.0,
                "pi_a": 0.0,
                "ppg": math.nan,
                "gdpg": math.nan,
                "s_n": 0,
                "n_league": 0,
                "rest": 60,
                "moved": 2,
                "short": {f: math.nan for f in FORM_FIELDS},
                "long": {f: math.nan for f in FORM_FIELDS},
            }
        elo, elo_h, elo_a = team.elo, team.elo_h, team.elo_a
        pi_h, pi_a = team.pi_h, team.pi_a
        s_n, ppg, gdpg = team.s_n, None, None
        n_league, moved = team.n_league, team.moved
        if team.season != season:
            r = self.season_regress
            elo = ELO_BASE + (elo - ELO_BASE) * (1 - r)
            elo_h = ELO_BASE + (elo_h - ELO_BASE) * (1 - r)
            elo_a = ELO_BASE + (elo_a - ELO_BASE) * (1 - r)
            s_n, moved = 0, 0
        if team.league != league:
            if tier and team.tier:
                moved = 1 if tier < team.tier else -1
            else:
                moved = 2
            shift = (
                self.promoted_shift
                if moved == 1
                else (self.relegated_shift if moved == -1 else 0.0)
            )
            elo = elo_h = elo_a = ELO_BASE + shift
            pi_h, pi_a = pi_h * 0.3, pi_a * 0.3
            n_league = 0
        if s_n:
            ppg, gdpg = team.s_pts / team.s_n, team.s_gd / team.s_n
        return {
            "elo": elo,
            "elo_h": elo_h,
            "elo_a": elo_a,
            "pi_h": pi_h,
            "pi_a": pi_a,
            "ppg": math.nan if ppg is None else ppg,
            "gdpg": math.nan if gdpg is None else gdpg,
            "s_n": s_n,
            "n_league": n_league,
            "rest": 60 if team.last_day is None else max(0, day - team.last_day),
            "moved": moved,
            "short": {f: _ew(team.short[f]) for f in FORM_FIELDS},
            "long": {f: _ew(team.long[f]) for f in FORM_FIELDS},
        }

    # ------------------------------------------------------------------ updates

    def update(self, row) -> None:
        day = row.date.toordinal()
        league = self._league(row.league)
        if league.season != row.season:
            league.season = row.season
            league.season_start = day
        tier = row.tier or 0
        home = self._team_for(row.country, row.home, row.league, tier, row.season)
        away = self._team_for(row.country, row.away, row.league, tier, row.season)
        league.teams.add(self.team_key(row.country, row.home))
        league.teams.add(self.team_key(row.country, row.away))
        hg, ag = row.home_goals, row.away_goals
        gd = hg - ag
        result = 1.0 if gd > 0 else (0.5 if gd == 0 else 0.0)

        # Elo (goal-difference weighted, league home edge learned online).
        expected = 1.0 / (1.0 + 10 ** (-(home.elo + league.home_adv - away.elo) / 400.0))
        delta = self.elo_k * _elo_margin(gd) * (result - expected)
        home.elo += delta
        away.elo -= delta
        league.home_adv += 0.075 * delta
        # Home-only / away-only Elo.
        expected = 1.0 / (1.0 + 10 ** (-(home.elo_h - away.elo_a) / 400.0))
        delta = self.elo_side_k * _elo_margin(gd) * (result - expected)
        home.elo_h += delta
        away.elo_a -= delta

        # Pi-ratings.
        predicted = _pi_goal(home.pi_h) - _pi_goal(away.pi_a)
        error = gd - predicted
        psi = math.copysign(3.0 * math.log10(1.0 + abs(error)), error)
        step_h = psi * self.pi_lambda
        home.pi_h += step_h
        home.pi_a += step_h * self.pi_gamma
        away.pi_a -= step_h
        away.pi_h -= step_h * self.pi_gamma

        # Form.
        stats = {}
        for prefix, (hc, ac) in STAT_COLUMNS.items():
            stats[prefix] = (getattr(row, hc), getattr(row, ac))
        points = (3, 0) if gd > 0 else ((1, 1) if gd == 0 else (0, 3))
        for team, is_home in ((home, True), (away, False)):
            own, other = (0, 1) if is_home else (1, 0)
            sample = {
                "gf": (hg, ag)[own],
                "ga": (hg, ag)[other],
                "pts": points[own],
            }
            for prefix in STAT_COLUMNS:
                pair = stats[prefix]
                if pair[0] is not None and pair[1] is not None:
                    sample[prefix + "f"] = pair[own]
                    sample[prefix + "a"] = pair[other]
            for memory, alpha in ((team.short, self.short_alpha), (team.long, self.long_alpha)):
                for field, value in sample.items():
                    acc = memory[field]
                    acc[0] = acc[0] * (1 - alpha) + alpha * value
                    acc[1] = acc[1] * (1 - alpha) + alpha
            team.last_day = day
            team.s_n += 1
            team.s_pts += points[own]
            team.s_gd += gd if is_home else -gd
            team.n_league += 1
        a = self.league_alpha
        league.hg = [league.hg[0] * (1 - a) + a * hg, league.hg[1] * (1 - a) + a]
        league.ag = [league.ag[0] * (1 - a) + a * ag, league.ag[1] * (1 - a) + a]
        league.draw = [
            league.draw[0] * (1 - a) + a * (1.0 if gd == 0 else 0.0),
            league.draw[1] * (1 - a) + a,
        ]
        league.n += 1


def _feature_names() -> list[str]:
    names = [
        "elo_diff",
        "elo_side_diff",
        "pi_diff",
        "pi_sum",
        "league_home_adv",
        "league_log_hg",
        "league_log_ag",
        "league_draw",
        "tier",
        "progress",
    ]
    for side in ("h", "a"):
        names += [
            f"{side}_ppg",
            f"{side}_gdpg",
            f"{side}_played",
            f"{side}_rank",
            f"{side}_log_n",
            f"{side}_log_rest",
            f"{side}_promoted",
            f"{side}_relegated",
            f"{side}_new",
        ]
        for memory in ("s", "l"):
            names += [f"{side}_{memory}_{f}" for f in FORM_FIELDS]
    return names + ["dc_log_home", "dc_log_away"]


FEATURE_NAMES = _feature_names()
ODDS_FEATURES = ["mkt_log_ha", "mkt_draw", "mkt_logit_over"]


def market_features(prices) -> list[float] | None:
    """Margin-free market features from 1X2 (+ over/under 2.5) prices, or None."""
    if not prices:
        return None
    try:
        inv = [1.0 / float(prices[k]) for k in ("1", "X", "2")]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    total = sum(inv)
    if not 0.98 <= total <= 1.4:
        return None
    ph, pd, pa = (v / total for v in inv)
    over = math.nan
    o, u = prices.get("over25"), prices.get("under25")
    if o and u:
        io, iu = 1.0 / o, 1.0 / u
        if 0.98 <= io + iu <= 1.3:
            q = io / (io + iu)
            over = math.log(q / (1 - q))
    return [math.log(ph / pa), pd, over]


# --------------------------------------------------------------------------- learners


SQUARED = ("elo_diff", "elo_side_diff", "pi_diff")
LOGGED = tuple(
    f"{side}_{memory}_{field}"
    for side in ("h", "a")
    for memory in ("s", "l")
    for field in ("gf", "ga", "sf", "sa", "tf", "ta", "cf", "ca")
)


def expand(x: np.ndarray, names: list[str]) -> np.ndarray:
    """Linear-model extras: squared rating gaps, log form rates, a has-stats indicator."""
    index = {name: i for i, name in enumerate(names)}
    extra = [x[:, index[n]] ** 2 for n in SQUARED if n in index]
    extra += [np.log(np.maximum(x[:, index[n]], 0.0) + 0.1) for n in LOGGED if n in index]
    if "h_s_tf" in index:
        extra.append(np.isnan(x[:, index["h_s_tf"]]).astype(float))
    return np.hstack([x, np.column_stack(extra)]) if extra else x


THREADS = 4  # BLAS / OpenMP threads per fit (the machine is shared with other runs)


class Stack:
    """Fitted models of one refit: 1X2 classifier, two goal GLMs, over-2.5 classifier.

    ``learner``: "logit" (multinomial / binary logistic regression), "hgb"
    (HistGradientBoosting) or "blend" (the mean of both probabilities). The goal rates always
    come from Poisson GLMs on the standardised (optionally expanded) features.
    """

    def __init__(
        self,
        learner: str,
        c: float,
        hgb: dict,
        names: list[str] | None = None,
        expand_features: bool = True,
    ):
        self.learner = learner
        self.c = c
        self.hgb = hgb
        self.names = names
        self.expand_features = expand_features and names is not None

    def _linear(self, x: np.ndarray) -> np.ndarray:
        return expand(x, self.names) if self.expand_features else x

    def fit(self, x: np.ndarray, hg: np.ndarray, ag: np.ndarray, weight: np.ndarray) -> Stack:
        from threadpoolctl import threadpool_limits

        with threadpool_limits(THREADS):
            return self._fit(x, hg, ag, weight)

    def _fit(self, x: np.ndarray, hg: np.ndarray, ag: np.ndarray, weight: np.ndarray) -> Stack:
        from sklearn.linear_model import LogisticRegression, PoissonRegressor

        lin = self._linear(x)
        self.fill = np.nanmedian(lin, axis=0)
        self.fill = np.where(np.isnan(self.fill), 0.0, self.fill)
        z = self._impute(lin)
        self.mean = z.mean(axis=0)
        self.scale = z.std(axis=0)
        self.scale[self.scale < 1e-9] = 1.0
        z = (z - self.mean) / self.scale
        outcome = np.where(hg > ag, 0, np.where(hg == ag, 1, 2))
        over = (hg + ag > 2).astype(int)
        w = weight / weight.mean()
        if self.learner in ("hgb", "blend"):
            from sklearn.ensemble import HistGradientBoostingClassifier

            self.hgb_clf = HistGradientBoostingClassifier(random_state=7, **self.hgb)
            self.hgb_clf.fit(x, outcome, sample_weight=w)
            self.hgb_over = HistGradientBoostingClassifier(random_state=7, **self.hgb)
            self.hgb_over.fit(x, over, sample_weight=w)
        if self.learner in ("logit", "blend"):
            self.clf = LogisticRegression(C=self.c, max_iter=500)
            self.clf.fit(z, outcome, sample_weight=w)
            self.over = LogisticRegression(C=self.c, max_iter=500)
            self.over.fit(z, over, sample_weight=w)
        alpha = 1.0 / (self.c * len(z))
        self.home_glm = PoissonRegressor(alpha=alpha, max_iter=500).fit(z, hg, sample_weight=w)
        self.away_glm = PoissonRegressor(alpha=alpha, max_iter=500).fit(z, ag, sample_weight=w)
        return self

    def _impute(self, x: np.ndarray) -> np.ndarray:
        z = np.array(x, dtype=float, copy=True)
        missing = np.isnan(z)
        if missing.any():
            z[missing] = np.take(self.fill, np.nonzero(missing)[1])
        return z

    def predict(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(1X2 probabilities, P(over 2.5), home rate, away rate) for rows of x."""
        z = (self._impute(self._linear(x)) - self.mean) / self.scale
        parts_1x2, parts_over = [], []
        if self.learner in ("hgb", "blend"):
            parts_1x2.append(self.hgb_clf.predict_proba(x))
            parts_over.append(self.hgb_over.predict_proba(x)[:, 1])
        if self.learner in ("logit", "blend"):
            parts_1x2.append(self.clf.predict_proba(z))
            parts_over.append(self.over.predict_proba(z)[:, 1])
        probs = np.mean(parts_1x2, axis=0)
        over = np.mean(parts_over, axis=0)
        return probs, over, self.home_glm.predict(z), self.away_glm.predict(z)


def goal_markets(
    probs: np.ndarray,
    over: float | None,
    home_rate: float,
    away_rate: float,
    rho: float,
    totals_weight: float,
) -> dict[str, float]:
    """Every goal market from a DC matrix re-weighted to `probs` (1X2) and `over` (2.5)."""
    from footypreds.engine import markets as core

    home_rate = min(5.0, max(0.15, float(home_rate)))
    away_rate = min(5.0, max(0.15, float(away_rate)))
    target_1x2 = {"1": float(probs[0]), "X": float(probs[1]), "2": float(probs[2])}
    matrix = core.score_matrix(home_rate, away_rate, rho)
    matrix = core.reweight(matrix, target_1x2)
    if over is not None and totals_weight > 0:
        target = core.pool_binary(core.over_probability(matrix), float(over), totals_weight)
        _, matrix = core.fit_total(home_rate, away_rate, rho, target_1x2, target)
    return mk.probabilities("goals", np.asarray(matrix, dtype=float))


class RatingStackModel:
    def __init__(
        self,
        learner: str = "blend",
        c: float = 0.1,
        train_first: str = "0809",
        sample_half_life: float = 2920.0,
        refit_days: int = 0,
        rho: float = -0.12,
        totals_weight: float = 0.5,
        use_dc: bool = False,
        expand: bool = True,
        passthrough: bool = True,
        threshold: float = 0.78,
        threshold_high: float = 0.82,
        min_team_matches: int = 5,
        select_keys: tuple[str, ...] | str | None = DEFAULT_SELECT_KEYS,
        one_per_match: bool = True,
        hgb_max_iter: int = 200,
        hgb_lr: float = 0.05,
        hgb_leaf: int = 15,
        hgb_min_leaf: int = 400,
        hgb_l2: float = 1.0,
        **feature_params,
    ):
        from fotbalPrediction import data

        self.learner = learner
        self.c = c
        self.train_first = data.season_start(train_first)
        self.sample_half_life = sample_half_life
        self.refit_days = refit_days
        self.rho = rho
        self.totals_weight = totals_weight
        self.use_dc = use_dc
        self.expand = expand
        self.passthrough = passthrough
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.min_team_matches = min_team_matches
        if isinstance(select_keys, str):
            select_keys = tuple(k for k in select_keys.split(",") if k)
        self.select_keys = None if select_keys is None else frozenset(select_keys)
        self.one_per_match = one_per_match
        self._best: dict[str, str] = {}
        self.hgb = {
            "max_iter": hgb_max_iter,
            "learning_rate": hgb_lr,
            "max_leaf_nodes": hgb_leaf,
            "min_samples_leaf": hgb_min_leaf,
            "l2_regularization": hgb_l2,
        }
        self._season_start = data.season_start
        self.builder = FeatureBuilder(**feature_params)
        self.baseline = BaselineModel()
        self.pending: list = []
        self.pending_day: int | None = None
        # Training samples (features frozen before their date was applied).
        self.x: list[np.ndarray] = []
        self.rows: list = []
        self.days: list[int] = []
        self.stack: Stack | None = None
        self.odds_stack: Stack | None = None
        self.fit_season: str | None = None
        self.fit_day: int | None = None
        self._sample: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------ features

    def _dc(self, row_like, day: int) -> tuple[float, float]:
        if not self.use_dc:
            return math.nan, math.nan
        base = self.baseline
        league = base._league(row_like.league)
        fit = base._fit(league, "goals", day, 1.35, 1.2)
        if fit is None:
            return math.nan, math.nan
        home, away = league.teams.get(row_like.home), league.teams.get(row_like.away)
        lh, la = base._expected(fit, home, away)
        return math.log(max(lh, 0.05)), math.log(max(la, 0.05))

    def _features(self, row_like, day: int) -> tuple[np.ndarray, int, int]:
        x, n_home, n_away = self.builder.features(row_like, day)
        if not self.use_dc:
            return x, n_home, n_away
        return np.concatenate([x, self._dc(row_like, day)]), n_home, n_away

    def _flush(self) -> None:
        if not self.pending:
            return
        day = self.pending_day
        for row in self.pending:
            if self._season_start(row.season) >= self.train_first:
                x, _, _ = self._features(row, day)
                self.x.append(x)
                self.rows.append(row)
                self.days.append(day)
        for row in self.pending:
            self.builder.update(row)
            self.baseline.update(row)
        self.pending = []
        self.pending_day = None

    # ------------------------------------------------------------------ fitting

    def _refit(self, day: int, season: str, odds_source: str | None) -> None:
        stale = self.stack is None or self.fit_season != season
        if not stale and self.refit_days and day - self.fit_day >= self.refit_days:
            stale = True
        if not stale:
            return
        n = len(self.x)
        if n < 500:
            return
        x = np.vstack(self.x)
        days = np.asarray(self.days, dtype=float)
        hg = np.array([r.home_goals for r in self.rows], dtype=float)
        ag = np.array([r.away_goals for r in self.rows], dtype=float)
        weight = np.exp(-math.log(2) * (day - days) / self.sample_half_life)
        names = FEATURE_NAMES if self.use_dc else FEATURE_NAMES[:-2]
        self.stack = Stack(self.learner, self.c, self.hgb, names, self.expand).fit(
            x, hg, ag, weight
        )
        self.odds_stack = None
        if odds_source:
            extra, keep = [], []
            for i, row in enumerate(self.rows):
                feats = market_features(row.odds.get(odds_source))
                if feats is not None:
                    extra.append(feats)
                    keep.append(i)
            if len(keep) >= 500:
                keep = np.asarray(keep)
                xo = np.hstack([x[keep], np.asarray(extra)])
                self.odds_stack = Stack(
                    self.learner, self.c, self.hgb, names + ODDS_FEATURES, self.expand
                ).fit(xo, hg[keep], ag[keep], weight[keep])
        self.fit_season, self.fit_day = season, day

    # ------------------------------------------------------------------ protocol

    def predict(self, ctx) -> dict[str, float]:
        day = ctx.date.toordinal()
        if self.pending_day is not None and self.pending_day < day:
            self._flush()
        output: dict[str, float] = {}
        if self.passthrough:
            base = self.baseline.predict(ctx)
            output.update({k: p for k, p in base.items() if mk.CATALOGUE[k].stat != "goals"})
        self._refit(day, ctx.season, ctx.odds_source)
        x, n_home, n_away = self._features(ctx, day)
        self._sample[ctx.match_id] = (n_home, n_away)
        if self.stack is None:
            return output
        stack = self.stack
        features = x
        market = market_features(ctx.odds) if ctx.odds else None
        if market is not None and self.odds_stack is not None:
            stack = self.odds_stack
            features = np.concatenate([x, market])
        probs, over, home_rate, away_rate = stack.predict(features[None, :])
        goals = goal_markets(
            probs[0], over[0], home_rate[0], away_rate[0], self.rho, self.totals_weight
        )
        output.update(goals)
        menu = [k for k in goals if self.select_keys is None or k in self.select_keys]
        menu = [k for k in menu if mk.CATALOGUE[k].selectable]
        if menu:
            self._best[ctx.match_id] = max(menu, key=lambda k: (goals[k], -menu.index(k)))
        return output

    def _enough(self, ctx, key: str) -> bool:
        if self.select_keys is not None and key not in self.select_keys:
            return False
        if self.one_per_match and self._best.get(ctx.match_id) != key:
            return False
        sample = self._sample.get(ctx.match_id)
        if sample is None:
            return False
        return min(sample) >= self.min_team_matches

    def select(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold and self._enough(ctx, key)

    def select_high(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold_high and self._enough(ctx, key)

    def update(self, row) -> None:
        day = row.date.toordinal()
        if self.pending_day is not None and day != self.pending_day:
            self._flush()
        self._sample.pop(row.id, None)
        self._best.pop(row.id, None)
        self.pending.append(row)
        self.pending_day = day


def factory(**params) -> RatingStackModel:
    return RatingStackModel(**params)


__all__ = ["FEATURE_NAMES", "FeatureBuilder", "RatingStackModel", "factory"]
