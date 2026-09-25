"""Tennis analyzer: surface-aware player Elo, market blend, set and game distributions.

Model (docs/CONTRACTS.md §5 shape):

1. Player Elo from every visible tennis result (strictly before kickoff - 3h). Each player has
   an overall rating and one rating per surface; the K-factor decays with matches played
   (K = k_base / (n + k_offset) ** k_shape, the FiveThirtyEight form), a straight-sets win can
   count a little more (`mov`), retirements count `retired_k` of a full result, and walkovers
   never count. After `idle_grace` days without a match a rating shrinks towards the start
   value with half-life `idle_half_life`. The match rating is a blend of the overall and the
   surface rating (`surface_weight`).
2. Market blend. Tennis prices are efficient, so the Elo probability only moves the
   margin-free price on the logit scale with weight (1 - market_weight), scaled down while
   either player has fewer than `experience` rated matches. Without prices the Elo
   probability is used alone.
3. Sets. The match probability p is inverted into a per-set probability for best of 3/5;
   `set_spread` > 0 makes sets over-dispersed (a mix of a better and a worse day), which puts
   more weight on straight sets. That gives exact set scores, set totals and set handicaps,
   all settleable from the final set score.
4. Games. A point-level Markov model (hold, tiebreak, set, match) with serve-point
   probabilities around the tour average, solved so its match probability equals p, gives the
   total-games distribution. Results only store sets, so games markets are never selectable.

Every parameter default in TennisParams was chosen on the validation year of the walk-forward
benchmark (`python -m footypreds.evaluation.tennis_eval --validate`); the test year is
locked. Do not change them without re-running it.
"""

import math
import threading
import weakref
from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from itertools import product
from math import comb

from footypreds.engine.history import canonical
from footypreds.sports import common as c
from footypreds.sports.keys import fmt_line, handicap, over, under

VERSION = "tennis-1.0-elo"
SPORT = "tennis"
MAX_DAYS = 400  # form, H2H and grade window; Elo uses the whole visible history
START = 1500.0
SURFACES = ("clay", "grass", "hard", "carpet")
SURFACE_NAMES = {"clay": "zgură", "grass": "iarbă", "hard": "hard", "carpet": "mochetă"}
SLAMS = ("australian open", "french open", "roland garros", "wimbledon", "us open")


@dataclass(frozen=True)
class TennisParams:
    # Chosen on the 2024 validation year (evaluation/tennis_eval.py --validate); 2025 is the
    # locked test. The closing market already contains everything Elo knows (market_weight
    # 1.0 won the validation), so Elo decides only when a match has no prices.
    k_base: float = 150.0
    k_offset: float = 5.0
    k_shape: float = 0.4
    surface_weight: float = 0.25
    mov: float = 0.5
    retired_k: float = 1.0
    idle_grace: float = 60.0
    idle_half_life: float = 730.0
    market_weight: float = 1.0
    experience: float = 0.0
    set_spread: float = 0.9
    serve_men: float = 0.64
    serve_women: float = 0.50


PARAMS = TennisParams()


# --- Tournament facts ------------------------------------------------------------------------


def surface_of(league):
    """Surface from FlashScore's "..., hard (indoor)" tournament suffix, else ""."""
    tail = league.rsplit(",", 1)[-1].casefold() if "," in league else ""
    return next((s for s in SURFACES if s in tail), "")


def source_hint(match, name):
    """A "key=value" hint of the match source (tennis-data rows carry best_of, ranks...)."""
    for part in match.source.split(";")[1:]:
        key, _, value = part.partition("=")
        if key == name:
            return value
    return ""


def best_of(match):
    """5 sets only for men's Grand Slam singles; everything else is best of 3.

    A source hint `best_of=5` (historical datasets) wins over the tournament name.
    """
    hinted = source_hint(match, "best_of")
    if hinted in ("3", "5"):
        return int(hinted)
    name = match.league.casefold()
    category = name.split(":", 1)[0]
    men = "atp" in category or ("men" in category and "women" not in category)
    return 5 if men and "singles" in name and any(s in name for s in SLAMS) else 3


def is_women(match):
    category = match.league.casefold().split(":", 1)[0]
    return "wta" in category or "women" in category or source_hint(match, "tour") == "wta"


# --- Elo ---------------------------------------------------------------------------------------


def expected(rating, other):
    return 1 / (1 + 10 ** ((other - rating) / 400))


class EloBook:
    """Overall and per-surface player ratings, updated one finished match at a time."""

    __slots__ = ("params", "overall", "surface", "played", "surface_played", "last", "position")

    def __init__(self, params=PARAMS):
        self.params = params
        self.overall = {}
        self.surface = {}
        self.played = {}
        self.surface_played = {}
        self.last = {}
        self.position = 0

    def copy(self):
        book = EloBook(self.params)
        book.overall = dict(self.overall)
        book.surface = dict(self.surface)
        book.played = dict(self.played)
        book.surface_played = dict(self.surface_played)
        book.last = dict(self.last)
        book.position = self.position
        return book

    def k(self, n):
        p = self.params
        return p.k_base / (n + p.k_offset) ** p.k_shape

    def idle_factor(self, player, when):
        last = self.last.get(player)
        if last is None or not math.isfinite(self.params.idle_half_life):
            return 1.0
        idle = (when - last).total_seconds() / 86400 - self.params.idle_grace
        return 0.5 ** (idle / self.params.idle_half_life) if idle > 0 else 1.0

    def ratings(self, player, surface, when):
        """(overall, surface) ratings of `player` at `when`, after the inactivity shrink."""
        factor = self.idle_factor(player, when)
        overall = self.overall.get(player, START)
        own = self.surface.get((player, surface), overall) if surface else overall
        return START + (overall - START) * factor, START + (own - START) * factor

    def rating(self, player, surface, when):
        overall, own = self.ratings(player, surface, when)
        w = self.params.surface_weight if surface else 0.0
        return (1 - w) * overall + w * own

    def update(self, match):
        """Add one result; walkovers, missing scores and level scores are ignored."""
        if match.status != "finished" or match.home_goals is None or match.away_goals is None:
            return
        h, a = match.home_goals, match.away_goals
        if h == a or match.finish_type == "walkover":
            return
        weight = self.params.retired_k if match.finish_type == "retired" else 1.0
        if weight <= 0:
            return
        if min(h, a) == 0 and match.finish_type != "retired":
            weight *= 1 + self.params.mov
        home, away = player_key(match.home), player_key(match.away)
        surface = surface_of(match.league)
        result = 1.0 if h > a else 0.0
        when = match.kickoff
        home_overall, home_surface = self.ratings(home, surface, when)
        away_overall, away_surface = self.ratings(away, surface, when)
        change = result - expected(home_overall, away_overall)
        n_home, n_away = self.played.get(home, 0), self.played.get(away, 0)
        self.overall[home] = home_overall + weight * self.k(n_home) * change
        self.overall[away] = away_overall - weight * self.k(n_away) * change
        self.played[home], self.played[away] = n_home + 1, n_away + 1
        if surface:
            change = result - expected(home_surface, away_surface)
            s_home = self.surface_played.get((home, surface), 0)
            s_away = self.surface_played.get((away, surface), 0)
            self.surface[home, surface] = home_surface + weight * self.k(s_home) * change
            self.surface[away, surface] = away_surface - weight * self.k(s_away) * change
            self.surface_played[home, surface] = s_home + 1
            self.surface_played[away, surface] = s_away + 1
        self.last[home] = self.last[away] = when

    def advance(self, rows, position):
        """Process rows[self.position:position] (rows sorted by kickoff)."""
        for match in rows[self.position : position]:
            self.update(match)
        self.position = max(self.position, position)
        return self


def player_key(name):
    return canonical(name)


_BOOKS = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()
BOOK_SLOTS = 16


def book_before(index, cutoff, params=PARAMS):
    """EloBook of every row of `index` strictly before `cutoff` (cached per index)."""
    position = bisect_left(index.times, cutoff)
    if params is not PARAMS:
        return EloBook(params).advance(index.rows, position)
    version = (len(index.rows), index.rows[-1].id if index.rows else None)
    with _LOCK:
        entry = _BOOKS.get(index)
        if entry is None or entry["version"] != version:
            entry = {"version": version, "books": {}}
            _BOOKS[index] = entry
        books = entry["books"]
        if position not in books:
            base = max((p for p in books if p <= position), default=None)
            book = books[base].copy() if base is not None else EloBook(params)
            books[position] = book.advance(index.rows, position)
            while len(books) > BOOK_SLOTS:
                books.pop(next(iter(books)))
        return books[position]


def elo_view(book, fixture):
    """Elo probability that the home player wins, with the ratings behind it."""
    surface = surface_of(fixture.league)
    home, away = player_key(fixture.home), player_key(fixture.away)
    when = fixture.kickoff
    home_rating = book.rating(home, surface, when)
    away_rating = book.rating(away, surface, when)
    return {
        "probability": expected(home_rating, away_rating),
        "home_rating": home_rating,
        "away_rating": away_rating,
        "home_overall": book.ratings(home, surface, when)[0],
        "away_overall": book.ratings(away, surface, when)[0],
        "home_played": book.played.get(home, 0),
        "away_played": book.played.get(away, 0),
        "home_surface_played": book.surface_played.get((home, surface), 0),
        "away_surface_played": book.surface_played.get((away, surface), 0),
        "surface": surface,
    }


def model_weight(view, params=PARAMS):
    """Share of the Elo view in the logit blend with the market price."""
    experience = min(view["home_played"], view["away_played"])
    ramp = min(1.0, experience / params.experience) if params.experience > 0 else 1.0
    return (1 - params.market_weight) * ramp


def blend(model, market, weight):
    """logit-scale blend: weight on the model, the rest on the market; no market -> model."""
    if market is None:
        return model
    return c.logistic(weight * c.logit(model) + (1 - weight) * c.logit(market))


def win_probability(book, fixture, params=PARAMS):
    """(p_home, details): the probability the analyzer and the evaluation both use."""
    view = elo_view(book, fixture)
    priced = c.two_way(fixture.odds, "1", "2")
    weight = model_weight(view, params)
    p = blend(view["probability"], priced[0] if priced else None, weight)
    return p, {**view, "market": priced[0] if priced else None, "model_weight": weight}


# --- Sets ------------------------------------------------------------------------------------


def iid_scores(q, sets):
    """{(home_sets, away_sets): probability} when every set is won with probability q."""
    need = sets // 2 + 1
    scores = {}
    for k in range(need):
        scores[need, k] = comb(need - 1 + k, k) * q**need * (1 - q) ** k
        scores[k, need] = comb(need - 1 + k, k) * (1 - q) ** need * q**k
    return scores


def set_scores(q, sets, spread=0.0):
    """Final set-score distribution; `spread` mixes a better and a worse day (logit units)."""
    if spread <= 0:
        return iid_scores(q, sets)
    x = c.logit(q)
    high = iid_scores(c.logistic(x + spread), sets)
    low = iid_scores(c.logistic(x - spread), sets)
    return {score: (high[score] + low[score]) / 2 for score in high}


def match_probability(q, sets, spread=0.0):
    """P(win the match) when each set is won with probability q."""
    need = sets // 2 + 1
    return sum(p for (h, _), p in set_scores(q, sets, spread).items() if h == need)


def set_probability(p, sets, spread=0.0):
    """Per-set probability q whose match probability is p (bisection)."""
    low, high = 1e-9, 1 - 1e-9
    for _ in range(80):
        middle = (low + high) / 2
        if match_probability(middle, sets, spread) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def live_set_scores(q, sets, home_sets, away_sets, spread=0.0):
    """Final set-score distribution from a current set score (sets already won).

    Helper for in-play use: the remaining sets are independent trials with probability q
    (the spread mixture is applied to the remaining sets only).
    """
    need = sets // 2 + 1
    if home_sets >= need or away_sets >= need:
        return {(home_sets, away_sets): 1.0}

    def one(qq):
        out = {}
        for k in range(need - away_sets):
            wins = need - home_sets
            out[need, away_sets + k] = comb(wins - 1 + k, k) * qq**wins * (1 - qq) ** k
        for k in range(need - home_sets):
            wins = need - away_sets
            out[home_sets + k, need] = comb(wins - 1 + k, k) * (1 - qq) ** wins * qq**k
        return out

    if spread <= 0:
        return one(q)
    x = c.logit(q)
    high, low = one(c.logistic(x + spread)), one(c.logistic(x - spread))
    return {score: (high[score] + low[score]) / 2 for score in high}


# --- Games (point-level Markov model) ------------------------------------------------------


def hold(p):
    """P(the server wins a game) when each service point is won with probability p."""
    q = 1 - p
    deuce = p * p / (p * p + q * q)
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * deuce


def tiebreak(pa, pb):
    """P(A wins a 7-point tiebreak) when A serves first; pa, pb = service-point wins."""
    grid = {(0, 0): 1.0}
    won = 0.0
    for total in range(12):
        nxt = {}
        for (i, j), prob in grid.items():
            if i + j != total:
                continue
            serving_a = ((total + 1) // 2) % 2 == 0
            point = pa if serving_a else 1 - pb
            for di, dj, pr in ((1, 0, point), (0, 1, 1 - point)):
                ni, nj = i + di, j + dj
                if ni == 7 and nj <= 5:
                    won += prob * pr
                elif nj == 7 and ni <= 5:
                    continue
                else:
                    nxt[ni, nj] = nxt.get((ni, nj), 0.0) + prob * pr
        grid = nxt
    at_six = grid.get((6, 6), 0.0)
    both_a = pa * (1 - pb)
    both_b = (1 - pa) * pb
    return won + at_six * both_a / (both_a + both_b)


@lru_cache(maxsize=4096)
def set_outcomes(pa, pb):
    """[(a_won, games, probability)] of one set in which A serves the first game."""
    ha, hb = hold(pa), hold(pb)
    tb = tiebreak(pa, pb)
    grid = {(0, 0): 1.0}
    outcomes = {}
    for total in range(12):
        nxt = {}
        for (i, j), prob in grid.items():
            win = ha if total % 2 == 0 else 1 - hb
            for ni, nj, pr in ((i + 1, j, win), (i, j + 1, 1 - win)):
                if (ni == 6 and nj <= 4) or (ni == 7 and nj == 5):
                    outcomes[True, ni + nj] = outcomes.get((True, ni + nj), 0.0) + prob * pr
                elif (nj == 6 and ni <= 4) or (nj == 7 and ni == 5):
                    outcomes[False, ni + nj] = outcomes.get((False, ni + nj), 0.0) + prob * pr
                else:
                    nxt[ni, nj] = nxt.get((ni, nj), 0.0) + prob * pr
        grid = nxt
    at_six = grid.get((6, 6), 0.0)
    outcomes[True, 13] = outcomes.get((True, 13), 0.0) + at_six * tb
    outcomes[False, 13] = outcomes.get((False, 13), 0.0) + at_six * (1 - tb)
    return tuple((won, games, prob) for (won, games), prob in outcomes.items())


def games_distribution(pa, pb, sets):
    """({(a_won_match, total_games): probability}) averaged over who serves first."""
    need = sets // 2 + 1
    first = set_outcomes(pa, pb)
    # B serves first: swap roles, then map back to A's perspective.
    second = tuple((not won, g, pr) for won, g, pr in set_outcomes(pb, pa))
    result = {}
    # (sets_a, sets_b, a_serves_first, games) -> probability, one set at a time.
    frontier = {(0, 0, True, 0): 0.5, (0, 0, False, 0): 0.5}
    while frontier:
        nxt = {}
        for (sa, sb, a_serves, games), prob in frontier.items():
            for won, g, pr in first if a_serves else second:
                na, nb, total = sa + won, sb + (not won), games + g
                if na == need or nb == need:
                    key = (na == need, total)
                    result[key] = result.get(key, 0.0) + prob * pr
                    continue
                # After an odd number of games the other player serves first in the next set.
                state = (na, nb, a_serves if g % 2 == 0 else not a_serves, total)
                nxt[state] = nxt.get(state, 0.0) + prob * pr
        frontier = nxt
    return result


def serve_split(p, sets, base):
    """Service-point probabilities (pa, pb) = (base + d, base - d) whose match win is p."""
    low, high = -0.3, 0.3
    for _ in range(30):
        middle = (low + high) / 2
        dist = games_distribution(round(base + middle, 6), round(base - middle, 6), sets)
        won = sum(prob for (a_won, _), prob in dist.items() if a_won)
        if won < p:
            low = middle
        else:
            high = middle
    d = (low + high) / 2
    return round(base + d, 6), round(base - d, 6)


def single_totals(p, sets, base):
    pa, pb = serve_split(p, sets, base)
    totals = {}
    for (_, games), prob in games_distribution(pa, pb, sets).items():
        totals[games] = totals.get(games, 0.0) + prob
    return totals, (pa, pb)


def games_totals(p, sets, base, spread=0.0):
    """({total_games: probability}, (pa, pb)) for a match the home player wins with p.

    With `spread` > 0 the games follow the same better-day / worse-day mix as the sets: one
    point model for each of the two set strengths, averaged; (pa, pb) is then the mean split.
    """
    if spread <= 0:
        parts = [single_totals(p, sets, base)]
    else:
        x = c.logit(set_probability(p, sets, spread))
        parts = [
            single_totals(match_probability(c.logistic(x + d), sets), sets, base)
            for d in (spread, -spread)
        ]
    totals = {}
    for part, _ in parts:
        for games, prob in part.items():
            totals[games] = totals.get(games, 0.0) + prob / len(parts)
    norm = sum(totals.values())
    pa = sum(split[0] for _, split in parts) / len(parts)
    pb = sum(split[1] for _, split in parts) / len(parts)
    return {g: prob / norm for g, prob in sorted(totals.items())}, (pa, pb)


def games_lines(totals, odds):
    """x.5 lines around the mean plus every games line that has a price."""
    mean = sum(g * p for g, p in totals.items())
    lines = {math.floor(mean) + 0.5 + k for k in range(-2, 3)}
    for key in odds:
        if key.startswith(("games_over_", "games_under_")):
            try:
                lines.add(float(key.rsplit("_", 1)[1]))
            except ValueError:
                continue
    return sorted(lines)


# --- Analysis ---------------------------------------------------------------------------------


def surface_record(rows, surface):
    played = [(m, side) for m, side in rows if surface and surface_of(m.league) == surface]
    wins = sum(c.perspective(m, side)[2] == "W" for m, side in played)
    return wins, len(played)


def rank_of(match, side):
    value = source_hint(match, f"rank_{side}")
    return int(value) if value.isdigit() else None


def analyze(fixture, history, threshold=0.85, params=None, **_):
    params = params or PARAMS
    index = c.as_index(history, SPORT)
    home_rows, away_rows = c.team_rows(index, fixture, MAX_DAYS)
    cutoff = fixture.kickoff - timedelta(hours=c.CUTOFF_HOURS)
    book = book_before(index, cutoff, params)
    p, details = win_probability(book, fixture, params)
    sets = best_of(fixture)
    q = set_probability(p, sets, params.set_spread)
    scores = set_scores(q, sets, params.set_spread)
    odds = fixture.odds

    markets = [
        c.market(SPORT, "1", "Câștigător", p, odds),
        c.market(SPORT, "2", "Câștigător", 1 - p, odds),
    ]
    for (h, a), prob in sorted(scores.items(), key=lambda item: (-item[0][0], item[0][1])):
        markets.append(c.market(SPORT, f"sets_{h}-{a}", "Scor la seturi", prob, odds))
    for line in [x + 0.5 for x in range(sets // 2 + 1, sets)]:
        above = sum(prob for (h, a), prob in scores.items() if h + a > line)
        markets.append(c.market(SPORT, over(line), "Total seturi", above, odds))
        markets.append(c.market(SPORT, under(line), "Total seturi", 1 - above, odds))
    for line in [x + 0.5 for x in range(1, sets // 2 + 1)]:
        for side, sign in product(("1", "2"), (-1, 1)):
            own = 0 if side == "1" else 1
            won = sum(
                prob for score, prob in scores.items() if score[own] + sign * line > score[1 - own]
            )
            markets.append(
                c.market(SPORT, handicap(side, sign * line), "Handicap seturi", won, odds)
            )
    base = params.serve_women if is_women(fixture) else params.serve_men
    totals, (serve_home, serve_away) = games_totals(p, sets, base, params.set_spread)
    expected_games = sum(g * prob for g, prob in totals.items())
    for line in games_lines(totals, odds):
        above = sum(prob for g, prob in totals.items() if g > line)
        below = sum(prob for g, prob in totals.items() if g < line)
        text = fmt_line(line)
        for key, prob in ((f"games_over_{text}", above), (f"games_under_{text}", below)):
            markets.append(c.market(SPORT, key, "Total game-uri", prob, odds, selectable=False))

    priced = details["market"] is not None
    confidence, grade = c.confidence_of(home_rows, away_rows, fixture.kickoff, priced)
    sufficient = grade != "D"
    selection, reason = c.choose(markets, threshold, sufficient)
    home_form = c.team_form(home_rows, fixture.kickoff)
    away_form = c.team_form(away_rows, fixture.kickoff)
    h2h = c.head_to_head(home_rows, fixture)
    by_key = {m["key"]: m for m in markets}
    favourite = by_key["1"] if p >= 0.5 else by_key["2"]
    exact = max(
        (m for m in markets if m["key"].startswith("sets_")), key=lambda m: m["probability"]
    )
    tips = [
        c.pick("Câștigător", favourite),
        c.pick("Scor la seturi", exact),
        c.pick("Total seturi", by_key[over(sets // 2 + 1.5)]),
    ]
    value = c.value_tip([m for m in markets if m["selectable"]])
    if value:
        tips.append(value)

    name = fixture.home if p >= 0.5 else fixture.away
    surface = details["surface"]
    summary = (
        f"Modelul favorizează {name} ({max(p, 1 - p):.0%}). "
        f"Cel mai probabil scor la seturi: {exact['label'].rsplit(' ', 1)[-1]}; "
        f"aproximativ {expected_games:.0f} game-uri."
    )
    if grade == "D":
        summary += " Atenție: date insuficiente, încrederea este scăzută."
    insights = insights_of(fixture, details, home_rows, away_rows, home_form, away_form, h2h)
    insights.insert(
        0,
        (f"Suprafață: {SURFACE_NAMES[surface]}; " if surface else "")
        + f"meci în cel mult {sets} seturi.",
    )
    return {
        "version": VERSION,
        "sport": SPORT,
        "threshold": threshold,
        "calibrated": False,
        "grade": grade,
        "confidence": confidence,
        "quality": "sufficient" if sufficient else "insufficient",
        "expected": {
            "home_win": p,
            "best_of": sets,
            "set_win": q,
            "surface": surface,
            "games": expected_games,
        },
        "markets": markets,
        "selection": selection,
        "reason": reason,
        "tips": tips,
        "summary": summary,
        "insights": insights,
        "form": {"home": home_form, "away": away_form},
        "h2h": h2h,
        "sample": {"home": len(home_rows), "away": len(away_rows), "h2h": h2h["played"]},
        "components": {
            "model_home_win": details["probability"],
            "market_home_win": details["market"],
            "model_weight": details["model_weight"],
            "elo_home": round(details["home_rating"], 1),
            "elo_away": round(details["away_rating"], 1),
            "elo_home_overall": round(details["home_overall"], 1),
            "elo_away_overall": round(details["away_overall"], 1),
            "elo_matches": {"home": details["home_played"], "away": details["away_played"]},
            "serve_points": {"home": serve_home, "away": serve_away},
            "set_spread": params.set_spread,
        },
    }


def insights_of(fixture, details, home_rows, away_rows, home_form, away_form, h2h):
    insights = []
    if details["home_played"] or details["away_played"]:
        insights.append(
            f"Rating Elo: {fixture.home} {details['home_rating']:.0f} "
            f"({details['home_played']} meciuri), {fixture.away} "
            f"{details['away_rating']:.0f} ({details['away_played']} meciuri)."
        )
    market = details["market"]
    if market is not None and details["model_weight"] > 0:
        gap = details["probability"] - market
        if abs(gap) >= 0.05:
            insights.append(
                f"Elo dă {details['probability']:.0%} pentru {fixture.home}, "
                f"cotele {market:.0%}; predicția le combină."
            )
    surface = details["surface"]
    for player, form, rows in (
        (fixture.home, home_form, home_rows),
        (fixture.away, away_form, away_rows),
    ):
        last10 = form["last10"]
        if not last10:
            insights.append(f"{player}: niciun rezultat recent disponibil.")
            continue
        text = f"{player}: {last10['wins']}/{last10['played']} victorii în ultimele meciuri"
        wins, played = surface_record(rows, surface)
        if played:
            text += f", {wins}/{played} pe {SURFACE_NAMES[surface]}"
        insights.append(text + ".")
        idle = form["days_since_last"]
        if idle is not None and idle > 45:
            insights.append(f"{player} nu a mai jucat de {idle} zile.")
    ranks = rank_of(fixture, "home"), rank_of(fixture, "away")
    if all(ranks):
        insights.append(f"Clasament: {fixture.home} #{ranks[0]}, {fixture.away} #{ranks[1]}.")
    if h2h["played"]:
        insights.append(f"Meciuri directe: {h2h['home_wins']}-{h2h['away_wins']}.")
    return insights
