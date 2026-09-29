"""Market catalogue, settlement and probabilities for fotbalPrediction.

One definition per market drives both settlement and probability: every market is a function
of a (home, away) count pair of one ``stat`` that returns the settled stake fractions
``(win, loss)`` (``win + loss < 1`` means a partial or full refund). Probabilities come from a
joint (home, away) distribution of the same stat, so a model can never price a market in a way
its own settlement contradicts.

Stats (all settled from football-data.co.uk columns of the finished row)::

    goals      FTHG / FTAG                     ht_goals   HTHG / HTAG
    corners    HC / AC                         sot        HST / AST (shots on target)
    cards      HY+HR / AY+AR (plain card count)
    bookings   (HY + 2*HR) / (AY + 2*AR)       (yellow = 1, red = 2 booking points)

Card convention (football-data notes.txt): English and Scottish yellow counts EXCLUDE the
first yellow of a two-yellow sending-off, European leagues count it as yellow + red. Card and
booking markets therefore settle on the football-data definition of each league; a bookmaker
may settle differently.

Keys
----
Goal keys reuse the FootyPreds core spelling (``footypreds.sports.keys`` /
``footypreds.engine.markets.FT_MARKETS``) and settle identically for whole and half lines:
"1" "X" "2", "1X" "X2" "12", "over05".."over45" / "under05".."under45", "btts" / "no_btts",
"home_over05" "home_over15" "home_over_2.5" "home_under_0.5" ..., "dnb_1" "dnb_2",
"ah_1_{signed}" / "ah_2_{signed}" (lines -2.5..+2.5 in 0.25 steps; quarter lines are split
bets: half the stake on each neighbouring line, which the core does not support), "cs_{h}-{a}"
(0..4 goals each) and "cs_other".

Keys outside the core keyspace (settled here only): "ht_1" "ht_X" "ht_2" "ht_over05"
"ht_under05" "ht_over15" "ht_under15" "ht_over25" "ht_under25"; "corners_over_9.5",
"home_corners_over_4.5", "corners_ah_1_-1.5"; "cards_over_3.5", "home_cards_over_1.5";
"bookings_over_4.5"; "sot_over_8.5", "home_sot_over_3.5" (and the matching "under" keys).

Outcome for metrics: ``outcome(key, row)`` is 1.0 when the stake won (fully or half), 0.0
when it lost (fully or half) and None for a full refund or missing data. A model's
probability for a key is P(outcome = 1 | not a full refund); for whole and half lines that is
P(win) / (1 - P(push)), as in the core.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from footypreds.sports.keys import fmt_line, fmt_signed, handicap

STATS = ("goals", "ht_goals", "corners", "cards", "bookings", "sot")
# Grid sizes (0..size-1 per team) used to price each stat.
GRID = {"goals": 13, "ht_goals": 9, "corners": 26, "cards": 13, "bookings": 17, "sot": 23}

# group id -> Romanian label (UI market-type filter).
GROUPS = {
    "1x2": "Rezultat final",
    "dc": "Șansă dublă",
    "goals": "Total goluri",
    "team_goals": "Goluri pe echipă",
    "btts": "Ambele marchează",
    "dnb": "Egal = pariu anulat",
    "ah": "Handicap asiatic",
    "cs": "Scor exact",
    "ht": "Pauză",
    "corners": "Cornere",
    "team_corners": "Cornere pe echipă",
    "corners_ah": "Handicap cornere",
    "cards": "Cartonașe",
    "team_cards": "Cartonașe pe echipă",
    "bookings": "Puncte cartonașe",
    "sot": "Șuturi pe poartă",
    "team_sot": "Șuturi pe poartă pe echipă",
}


@dataclass(frozen=True)
class Market:
    key: str
    label: str
    group: str
    stat: str
    # "result" "dc" "total" "team_total" "handicap" "btts" "exact" "exact_other"
    kind: str
    # (win, loss) stake fractions for a (home, away) count pair of `stat`.
    settle_pair: Callable[[int, int], tuple[float, float]]
    # Exclusive outcome set for multi-class metrics ("1x2", "ht_1x2", "cs") or "".
    question: str = ""
    selectable: bool = True
    # The key exists in the FootyPreds core keyspace and settles identically there.
    core: bool = False
    # football-data.co.uk carries a real price for this key (ROI can be tested).
    priced: bool = False


# --------------------------------------------------------------------------- settlement math


def _parts(line: float) -> tuple[float, ...]:
    """A quarter line is two half-stakes on its neighbours; any other line is one stake."""
    if abs(line * 4 - round(line * 4)) > 1e-9:
        raise ValueError(f"Linie invalidă: {line}")
    if round(line * 4) % 2:  # x.25 / x.75
        return (line - 0.25, line + 0.25)
    return (line,)


def asian(margin: float, line: float) -> tuple[float, float]:
    """(win, loss) of a bet that wins when ``margin + part > 0`` for each part of ``line``."""
    parts = _parts(line)
    share = 1.0 / len(parts)
    win = loss = 0.0
    for part in parts:
        value = margin + part
        if value > 1e-9:
            win += share
        elif value < -1e-9:
            loss += share
    return win, loss


def _bool(flag: bool) -> tuple[float, float]:
    return (1.0, 0.0) if flag else (0.0, 1.0)


def _total(direction: str, line: float):
    if direction == "over":
        return lambda h, a: asian(h + a, -line)
    return lambda h, a: asian(-(h + a), line)


def _team_total(side: str, direction: str, line: float):
    if side == "home":
        if direction == "over":
            return lambda h, a: asian(h, -line)
        return lambda h, a: asian(-h, line)
    if direction == "over":
        return lambda h, a: asian(a, -line)
    return lambda h, a: asian(-a, line)


def _handicap(side: str, line: float):
    if side == "1":
        return lambda h, a: asian(h - a, line)
    return lambda h, a: asian(a - h, line)


# --------------------------------------------------------------------------- catalogue

GOAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
TEAM_GOAL_LINES = (0.5, 1.5, 2.5)
AH_LINES = tuple(x / 4 for x in range(-10, 11))  # -2.5 .. +2.5 in quarter steps
CS_MAX = 4
HT_GOAL_LINES = (0.5, 1.5, 2.5)
CORNER_LINES = (7.5, 8.5, 9.5, 10.5, 11.5, 12.5)
TEAM_CORNER_LINES = (2.5, 3.5, 4.5, 5.5, 6.5)
CORNER_AH_LINES = (-3.5, -2.5, -1.5, -0.5, 0.5, 1.5, 2.5, 3.5)
CARD_LINES = (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)
TEAM_CARD_LINES = (0.5, 1.5, 2.5, 3.5)
BOOKING_LINES = (1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5)
SOT_LINES = (5.5, 6.5, 7.5, 8.5, 9.5, 10.5)
TEAM_SOT_LINES = (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)

_WORD = {"over": "Peste", "under": "Sub"}
_SIDE = {"home": "Gazde", "away": "Oaspeți"}


def _legacy_total(direction: str, line: float) -> str:
    return f"{direction}{round(line * 10):02d}"


def _build() -> dict[str, Market]:
    catalogue: dict[str, Market] = {}

    def add(market: Market) -> None:
        if market.key in catalogue:
            raise ValueError(f"Cheie duplicată: {market.key}")
        catalogue[market.key] = market

    # Full-time result and double chance.
    add(
        Market(
            "1",
            "Victorie gazde",
            "1x2",
            "goals",
            "result",
            lambda h, a: _bool(h > a),
            "1x2",
            core=True,
            priced=True,
        )
    )
    add(
        Market(
            "X",
            "Egal",
            "1x2",
            "goals",
            "result",
            lambda h, a: _bool(h == a),
            "1x2",
            core=True,
            priced=True,
        )
    )
    add(
        Market(
            "2",
            "Victorie oaspeți",
            "1x2",
            "goals",
            "result",
            lambda h, a: _bool(h < a),
            "1x2",
            core=True,
            priced=True,
        )
    )
    add(
        Market(
            "1X",
            "Gazde sau egal",
            "dc",
            "goals",
            "dc",
            lambda h, a: _bool(h >= a),
            core=True,
            priced=True,
        )
    )
    add(
        Market(
            "X2",
            "Egal sau oaspeți",
            "dc",
            "goals",
            "dc",
            lambda h, a: _bool(h <= a),
            core=True,
            priced=True,
        )
    )
    add(
        Market(
            "12",
            "Fără egal",
            "dc",
            "goals",
            "dc",
            lambda h, a: _bool(h != a),
            core=True,
            priced=True,
        )
    )
    # Match goals (legacy core keys).
    for line in GOAL_LINES:
        for direction in ("over", "under"):
            add(
                Market(
                    _legacy_total(direction, line),
                    f"{_WORD[direction]} {fmt_line(line)} goluri",
                    "goals",
                    "goals",
                    "total",
                    _total(direction, line),
                    core=True,
                    priced=line == 2.5,
                )
            )
    # Team goals: legacy spelling where the core has one (home_over05/15), generic otherwise.
    for side in ("home", "away"):
        for line in TEAM_GOAL_LINES:
            for direction in ("over", "under"):
                if direction == "over" and line in (0.5, 1.5):
                    key = f"{side}_over{round(line * 10):02d}"
                else:
                    key = f"{side}_{direction}_{fmt_line(line)}"
                label = f"{_SIDE[side]} {_WORD[direction].lower()} {fmt_line(line)} goluri"
                add(
                    Market(
                        key,
                        label,
                        "team_goals",
                        "goals",
                        "team_total",
                        _team_total(side, direction, line),
                        core=True,
                    )
                )
    add(
        Market(
            "btts",
            "Ambele marchează",
            "btts",
            "goals",
            "btts",
            lambda h, a: _bool(h > 0 and a > 0),
            core=True,
        )
    )
    add(
        Market(
            "no_btts",
            "Nu marchează ambele",
            "btts",
            "goals",
            "btts",
            lambda h, a: _bool(h == 0 or a == 0),
            core=True,
        )
    )
    add(
        Market(
            "dnb_1",
            "Gazde (egal = anulat)",
            "dnb",
            "goals",
            "handicap",
            _handicap("1", 0.0),
            core=True,
        )
    )
    add(
        Market(
            "dnb_2",
            "Oaspeți (egal = anulat)",
            "dnb",
            "goals",
            "handicap",
            _handicap("2", 0.0),
            core=True,
        )
    )
    for side in ("1", "2"):
        who = "gazde" if side == "1" else "oaspeți"
        for line in AH_LINES:
            key = handicap(side, line)
            quarter = bool(round(line * 4) % 2)
            add(
                Market(
                    key,
                    f"Handicap {who} {fmt_signed(line)}",
                    "ah",
                    "goals",
                    "handicap",
                    _handicap(side, line),
                    core=not quarter,
                    priced=True,
                )
            )
    for h in range(CS_MAX + 1):
        for a in range(CS_MAX + 1):
            add(
                Market(
                    f"cs_{h}-{a}",
                    f"Scor exact {h}-{a}",
                    "cs",
                    "goals",
                    "exact",
                    (lambda hh, aa: lambda x, y: _bool((x, y) == (hh, aa)))(h, a),
                    "cs",
                    core=True,
                )
            )
    add(
        Market(
            "cs_other",
            "Alt scor",
            "cs",
            "goals",
            "exact_other",
            lambda h, a: _bool(h > CS_MAX or a > CS_MAX),
            "cs",
            selectable=False,
        )
    )
    # Half-time.
    add(
        Market(
            "ht_1", "Pauză: gazde", "ht", "ht_goals", "result", lambda h, a: _bool(h > a), "ht_1x2"
        )
    )
    add(
        Market(
            "ht_X", "Pauză: egal", "ht", "ht_goals", "result", lambda h, a: _bool(h == a), "ht_1x2"
        )
    )
    add(
        Market(
            "ht_2",
            "Pauză: oaspeți",
            "ht",
            "ht_goals",
            "result",
            lambda h, a: _bool(h < a),
            "ht_1x2",
        )
    )
    for line in HT_GOAL_LINES:
        for direction in ("over", "under"):
            add(
                Market(
                    f"ht_{_legacy_total(direction, line)}",
                    f"Pauză: {_WORD[direction].lower()} {fmt_line(line)} goluri",
                    "ht",
                    "ht_goals",
                    "total",
                    _total(direction, line),
                )
            )

    def count_family(
        stat: str,
        noun: str,
        total_group: str,
        team_group: str,
        lines: Iterable[float],
        team_lines: Iterable[float],
    ) -> None:
        for line in lines:
            for direction in ("over", "under"):
                add(
                    Market(
                        f"{stat}_{direction}_{fmt_line(line)}",
                        f"{_WORD[direction]} {fmt_line(line)} {noun}",
                        total_group,
                        stat,
                        "total",
                        _total(direction, line),
                    )
                )
        for side in ("home", "away"):
            for line in team_lines:
                for direction in ("over", "under"):
                    add(
                        Market(
                            f"{side}_{stat}_{direction}_{fmt_line(line)}",
                            f"{_SIDE[side]} {_WORD[direction].lower()} {fmt_line(line)} {noun}",
                            team_group,
                            stat,
                            "team_total",
                            _team_total(side, direction, line),
                        )
                    )

    count_family("corners", "cornere", "corners", "team_corners", CORNER_LINES, TEAM_CORNER_LINES)
    for side in ("1", "2"):
        who = "gazde" if side == "1" else "oaspeți"
        for line in CORNER_AH_LINES:
            add(
                Market(
                    f"corners_ah_{side}_{fmt_signed(line)}",
                    f"Handicap cornere {who} {fmt_signed(line)}",
                    "corners_ah",
                    "corners",
                    "handicap",
                    _handicap(side, line),
                )
            )
    count_family("cards", "cartonașe", "cards", "team_cards", CARD_LINES, TEAM_CARD_LINES)
    for line in BOOKING_LINES:
        for direction in ("over", "under"):
            add(
                Market(
                    f"bookings_{direction}_{fmt_line(line)}",
                    f"{_WORD[direction]} {fmt_line(line)} puncte cartonașe (galben 1, roșu 2)",
                    "bookings",
                    "bookings",
                    "total",
                    _total(direction, line),
                )
            )
    count_family("sot", "șuturi pe poartă", "sot", "team_sot", SOT_LINES, TEAM_SOT_LINES)
    return catalogue


CATALOGUE: dict[str, Market] = _build()
KEYS = tuple(CATALOGUE)
QUESTIONS = {
    name: tuple(key for key, market in CATALOGUE.items() if market.question == name)
    for name in ("1x2", "ht_1x2", "cs")
}


def keys_of(stat: str) -> tuple[str, ...]:
    return tuple(key for key, market in CATALOGUE.items() if market.stat == stat)


def resolve_keys(spec: str | Iterable[str] | None) -> tuple[str, ...]:
    """Keys selected by a comma list of keys, stats or group ids ("all"/None = everything).

    A stat name ("goals", "corners", "cards", "sot", ...) wins over the group of the same
    name; use "team_corners", "corners_ah", "dc", "ah", ... for a single group.
    """
    if spec is None:
        return KEYS
    items = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if s.strip()]
    if not items or "all" in items:
        return KEYS
    chosen: dict[str, None] = {}
    for item in items:
        if item in CATALOGUE:
            chosen[item] = None
        elif item in STATS:  # "corners" = every corner market (totals, team, handicap)
            chosen.update(dict.fromkeys(keys_of(item)))
        elif item in GROUPS:
            chosen.update(dict.fromkeys(k for k, m in CATALOGUE.items() if m.group == item))
        else:
            raise ValueError(f"Piață necunoscută: {item}")
    return tuple(chosen)


# --------------------------------------------------------------------------- settlement


def stat_pair(row, stat: str) -> tuple[int, int] | None:
    """(home, away) count of `stat` in a finished row, or None when the file lacks it."""
    get = row.get if hasattr(row, "get") else (lambda name: getattr(row, name, None))
    if stat == "goals":
        pair = (get("home_goals"), get("away_goals"))
    elif stat == "ht_goals":
        pair = (get("ht_home_goals"), get("ht_away_goals"))
    elif stat == "corners":
        pair = (get("home_corners"), get("away_corners"))
    elif stat == "sot":
        pair = (get("home_sot"), get("away_sot"))
    elif stat in ("cards", "bookings"):
        values = [get(n) for n in ("home_yellow", "home_red", "away_yellow", "away_red")]
        if any(v is None for v in values):
            return None
        hy, hr, ay, ar = values
        weight = 2 if stat == "bookings" else 1
        pair = (hy + weight * hr, ay + weight * ar)
    else:
        raise ValueError(f"Statistică necunoscută: {stat}")
    if pair[0] is None or pair[1] is None:
        return None
    return int(pair[0]), int(pair[1])


def settle(key: str, row) -> tuple[float, float] | None:
    """(win, loss) stake fractions of `key` on a finished row; None when data is missing."""
    market = CATALOGUE[key]
    pair = stat_pair(row, market.stat)
    if pair is None:
        return None
    return market.settle_pair(*pair)


def settle_score(key: str, home: int, away: int) -> tuple[float, float]:
    """(win, loss) of `key` from a (home, away) count pair of the market's own stat."""
    return CATALOGUE[key].settle_pair(int(home), int(away))


def outcome_of(win: float, loss: float) -> float | None:
    """1.0 won (fully or half), 0.0 lost (fully or half), None for a full refund."""
    if win + loss <= 1e-12:
        return None
    return 1.0 if win > loss else 0.0


def outcome(key: str, row) -> float | None:
    settled = settle(key, row)
    return None if settled is None else outcome_of(*settled)


def profit(win: float, loss: float, price: float) -> float:
    """Flat 1-unit stake: net return at decimal `price` (quarter lines split the stake)."""
    return win * (price - 1.0) - loss


# --------------------------------------------------------------------------- probabilities


@lru_cache(maxsize=None)
def _weights(stat: str, size: int, keys: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    """(W, L): (len(keys), size*size) win/loss stake fractions on the 0..size-1 grid."""
    win = np.zeros((len(keys), size * size))
    loss = np.zeros((len(keys), size * size))
    for row, key in enumerate(keys):
        settle_pair = CATALOGUE[key].settle_pair
        for h in range(size):
            for a in range(size):
                w, lo = settle_pair(h, a)
                win[row, h * size + a] = w
                loss[row, h * size + a] = lo
    win.setflags(write=False)
    loss.setflags(write=False)
    return win, loss


def expectations(stat: str, matrix, keys: Iterable[str] | None = None) -> dict:
    """key -> (E[win fraction], E[loss fraction]) under a joint (home, away) distribution."""
    grid = np.asarray(matrix, dtype=float)
    size = grid.shape[0]
    if grid.shape != (size, size):
        raise ValueError("Matricea trebuie să fie pătrată.")
    keys = tuple(keys) if keys is not None else keys_of(stat)
    win, loss = _weights(stat, size, keys)
    flat = grid.ravel()
    return dict(zip(keys, zip(win @ flat, loss @ flat)))


def probabilities(stat: str, matrix, keys: Iterable[str] | None = None) -> dict[str, float]:
    """key -> P(outcome = 1 | not a full refund) under a joint (home, away) distribution.

    The ``cs_other`` bucket and every line are consistent with the grid, whose tail beyond
    ``size - 1`` must already be negligible or folded in by the caller.
    """
    output = {}
    for key, (win, loss) in expectations(stat, matrix, keys).items():
        decided = win + loss
        if decided > 1e-12:
            output[key] = min(1.0, max(0.0, float(win / decided)))
    return output


def fair_odds(win: float, loss: float) -> float | None:
    """Price with zero expected value; equals 1/p except on quarter lines."""
    if win <= 1e-12:
        return None
    return 1.0 + loss / win


def poisson_pmf(rate: float, size: int) -> np.ndarray:
    """Poisson pmf on 0..size-1 with the upper tail folded into the last cell."""
    rate = max(float(rate), 1e-9)
    values = np.empty(size)
    values[0] = math.exp(-rate)
    for k in range(1, size):
        values[k] = values[k - 1] * rate / k
    values[-1] += max(0.0, 1.0 - values.sum())
    return values / values.sum()


def negbin_pmf(mean: float, variance: float, size: int) -> np.ndarray:
    """Negative binomial pmf (mean, variance > mean) on 0..size-1, tail folded; Poisson
    when the variance does not exceed the mean."""
    mean = max(float(mean), 1e-9)
    if variance <= mean * 1.0001:
        return poisson_pmf(mean, size)
    r = mean * mean / (variance - mean)
    q = mean / (mean + r)  # success prob of each extra count
    values = np.empty(size)
    values[0] = math.exp(r * math.log(1 - q))
    for k in range(1, size):
        values[k] = values[k - 1] * (k - 1 + r) / k * q
    values[-1] += max(0.0, 1.0 - values.sum())
    return values / values.sum()


def independent(home_pmf, away_pmf) -> np.ndarray:
    return np.outer(np.asarray(home_pmf, dtype=float), np.asarray(away_pmf, dtype=float))


def goal_matrix(home_rate: float, away_rate: float, rho: float = 0.0, size: int = 13):
    """Dixon-Coles score matrix (footypreds.engine.markets.score_matrix) as a numpy array."""
    from footypreds.engine.markets import score_matrix

    return np.asarray(score_matrix(home_rate, away_rate, rho, size - 1), dtype=float)


def count_probabilities(
    stat: str,
    home: tuple[float, float],
    away: tuple[float, float],
    keys: Iterable[str] | None = None,
) -> dict[str, float]:
    """Markets of a count stat from independent (mean, variance) per team (NB or Poisson)."""
    size = GRID[stat]
    matrix = independent(negbin_pmf(*home, size), negbin_pmf(*away, size))
    return probabilities(stat, matrix, keys)


def complete(probs: Mapping[str, float]) -> dict[str, float]:
    """Adds "cs_other" = 1 - sum(cs_*) when the exact-score grid is present without it."""
    output = dict(probs)
    cs = [output[k] for k in QUESTIONS["cs"] if k != "cs_other" and k in output]
    if "cs_other" not in output and len(cs) == len(QUESTIONS["cs"]) - 1:
        output["cs_other"] = max(0.0, 1.0 - sum(cs))
    return output
