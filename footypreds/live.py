"""In-play analysis: what can be bet now on a live game (football, basketball, tennis).

Everything here is pure computation on a live `Match` (provider.live) plus pre-match
information: the stored analysis when one exists, otherwise the pre-match 1X2 / winner prices.

- Football: pre-match expected goals -> remaining expected goals scaled by the time left
  (stoppage allowance included; later minutes weigh more), adjusted for red cards and,
  optionally, for live xG / shots momentum within bounds -> Poisson for the rest of the game
  added to the current score -> every final-result market.
- Basketball: pre-match margin and total, blended with the observed scoring pace -> final
  margin ~ current margin + Normal(remaining mean, remaining sd), final total likewise.
- Tennis: per-set win probability solved from the pre-match match-win probability -> exact
  Markov chain over the remaining sets (best of 3 or 5).

FlashScore gives no live prices: the list odds are PRE-MATCH. Markets therefore carry
`odds: None` and a fair price (the minimum odds worth taking) instead.
"""

import math
import re
from functools import lru_cache
from statistics import NormalDist

from footypreds.competitions import competition_name, match_competition
from footypreds.media import match_media, public_match
from footypreds.sports.keys import (
    FOOTBALL_ALIASES,
    fmt_line,
    fmt_signed,
    handicap,
    label,
    over,
    under,
)
from footypreds.sports.settle import is_settleable

NORMAL = NormalDist()
VERSION = "live-0.1"
ODDS_NOTE = (
    "Cotele din lista FlashScore sunt de dinainte de meci (nu există cote live). "
    "Afișăm cota corectă: cota minimă la care pariul merită jucat."
)
DISCLAIMER = "Estimări statistice, nu garanții. 18+."

# --- football -------------------------------------------------------------------------------
# Minutes of play per half including the usual stoppage time, and the share of goals scored
# in each half (more goals come late in games).
FIRST_HALF_MINUTES = 47.0
SECOND_HALF_MINUTES = 50.0
FIRST_HALF_GOALS = 0.45
FULL_TIME_MINUTE = 95.0
MIN_REMAINING = 0.75
DEFAULT_LAMBDAS = (1.45, 1.15)
TYPICAL_TOTAL = 2.6
RHO = -0.12  # the engine's Dixon-Coles low-score dependence (Params.rho default)
MAX_EXTRA = 10
# A red card: the side down to ten scores ~30% less and concedes ~22% more (per card).
RED_ATTACK = 0.70
RED_DEFENCE = 1.22
# Live xG momentum: pre-match expectation weighs MOMENTUM_PRIOR x the observed value.
MOMENTUM_PRIOR = 3.0
MOMENTUM_BOUNDS = (0.8, 1.25)
MOMENTUM_MIN_MINUTE = 15
SHOT_ON_TARGET_XG = 0.3

# --- basketball -----------------------------------------------------------------------------
BASKETBALL_MARGIN_SD = 12.0
BASKETBALL_TOTAL_SD = 17.0
BASKETBALL_DEFAULT_TOTAL = 160.0
# Prior weight of the pre-match numbers, in game fractions, against the observed pace.
PACE_PRIOR_INFORMED = 0.6
PACE_PRIOR_DEFAULT = 0.1
MARGIN_PRIOR_INFORMED = 1.0
MARGIN_PRIOR_DEFAULT = 0.3
OVERTIME_MINUTES = 5.0

# --- suggestions ----------------------------------------------------------------------------
SAFE_RANGE = (0.6, 0.97)
BALANCED_RANGE = (0.4, 0.72)
MAX_SUGGESTIONS = 3


def clamp(value, low, high):
    return min(high, max(low, value))


def market(sport, key, group, probability, why, selectable=None, name=None):
    """A live market: probability of the FINAL result, fair price, no live odds."""
    probability = clamp(float(probability), 0.0, 1.0)
    return {
        "key": key,
        "label": name or label(sport, key),
        "group": group,
        "probability": probability,
        "fair_odds": round(1 / probability, 3) if probability > 0 else None,
        "odds": None,
        "ev": None,
        "selectable": is_settleable(sport, key) if selectable is None else selectable,
        # False when the estimate rests on assumptions only (no pre-match information yet):
        # shown, but never suggested.
        "reliable": True,
        "why": why,
    }


def unreliable(markets, groups=None):
    """Mark markets (all, or those of `groups`) as not reliable enough to suggest."""
    for m in markets:
        if groups is None or m["group"] in groups:
            m["reliable"] = False
    return markets


UNRELIABLE_NOTE = "Fără informații înainte de meci: afișăm estimări, dar nu facem sugestii."


def decided(probability):
    return probability <= 1e-6 or probability >= 1 - 1e-6


# ============================================================================================
# Football
# ============================================================================================


def poisson(rate, size=MAX_EXTRA + 1):
    values = [math.exp(-rate)]
    for n in range(1, size):
        values.append(values[-1] * rate / n)
    total = sum(values)
    return [v / total for v in values]


def one_x_two(home_rate, away_rate, rho=RHO, size=11):
    """Full-game 1X2 of a Dixon-Coles score model (cheap: 1X2 only)."""
    home, away = poisson(home_rate, size), poisson(away_rate, size)
    p1 = px = p2 = 0.0
    for h, hp in enumerate(home):
        for a, ap in enumerate(away):
            p = hp * ap
            if h > a:
                p1 += p
            elif h == a:
                px += p
            else:
                p2 += p
    if rho:
        low = max(-1 / max(home_rate, 1e-9), -1 / max(away_rate, 1e-9))
        high = min(1 / max(home_rate * away_rate, 1e-9), 1)
        r = clamp(rho, low, high)
        px += home[0] * away[0] * (-home_rate * away_rate * r) + home[1] * away[1] * (-r)
        p2 += home[0] * away[1] * home_rate * r
        p1 += home[1] * away[0] * away_rate * r
    total = p1 + px + p2
    return p1 / total, px / total, p2 / total


@lru_cache(maxsize=4096)
def _fit(p1, px, p2):
    ratio = p1 / (p1 + p2)

    def split(total):
        low, high = -total + 0.02, total - 0.02
        for _ in range(24):
            middle = (low + high) / 2
            h, _, a = one_x_two((total + middle) / 2, (total - middle) / 2)
            if h / (h + a) < ratio:
                low = middle
            else:
                high = middle
        middle = (low + high) / 2
        return (total + middle) / 2, (total - middle) / 2

    low, high = 1.0, 5.5
    for _ in range(20):
        total = (low + high) / 2
        draw = one_x_two(*split(total))[1]
        # The draw probability falls as the expected total grows.
        if draw > px:
            low = total
        else:
            high = total
    return split((low + high) / 2)


def fit_lambdas(target):
    """(home, away) expected goals whose Dixon-Coles 1X2 reproduces `target` {1, X, 2}."""
    values = [target.get(k) for k in ("1", "X", "2")]
    if not all(isinstance(v, (int, float)) and 0 < v < 1 for v in values):
        return None
    total = sum(values)
    p1, px, p2 = (round(v / total, 4) for v in values)
    return _fit(p1, px, p2)


def implied_1x2(odds):
    """Margin-free 1X2 from pre-match prices, or None."""
    prices = [odds.get(k) for k in ("1", "X", "2")]
    if not all(isinstance(v, (int, float)) and v > 1 for v in prices):
        return None
    inverse = [1 / v for v in prices]
    total = sum(inverse)
    if not 0.98 <= total <= 1.4:
        return None
    return dict(zip(("1", "X", "2"), (v / total for v in inverse)))


def football_prematch(odds=None, analysis=None):
    """Pre-match expected goals and where they come from ("analysis" | "odds" | "default")."""
    odds = odds or {}
    if analysis and analysis.get("grade") != "D":
        by_key = {m["key"]: m["probability"] for m in analysis.get("markets", [])}
        fitted = fit_lambdas(by_key)
        if fitted:
            return {"home": fitted[0], "away": fitted[1], "source": "analysis"}
    implied = implied_1x2(odds)
    if implied:
        fitted = fit_lambdas(implied)
        if fitted:
            return {"home": fitted[0], "away": fitted[1], "source": "odds"}
    expected = (analysis or {}).get("expected") or {}
    if isinstance(expected.get("home"), (int, float)) and isinstance(
        expected.get("away"), (int, float)
    ):
        return {"home": expected["home"], "away": expected["away"], "source": "analysis"}
    return {"home": DEFAULT_LAMBDAS[0], "away": DEFAULT_LAMBDAS[1], "source": "default"}


def football_clock(period, minute):
    """(share of the game's goals still to come, elapsed minutes, notes) or None (ET/PEN)."""
    notes = []
    if period in ("ET", "PEN"):
        return None
    if period not in ("1H", "HT", "2H"):
        if minute is not None:
            period = "1H" if minute <= 45 else "2H"
        else:
            notes.append("Faza meciului nu este clară: presupunem jumătatea meciului.")
            return 1 - FIRST_HALF_GOALS, 45.0, notes
    if period == "HT":
        return 1 - FIRST_HALF_GOALS, 45.0, notes
    if period == "1H":
        if minute is None:
            minute = 22
            notes.append("Minutul nu este cunoscut: presupunem mijlocul primei reprize.")
        left = max(0.5, FIRST_HALF_MINUTES - clamp(minute, 0, FIRST_HALF_MINUTES))
        share = FIRST_HALF_GOALS * left / FIRST_HALF_MINUTES + (1 - FIRST_HALF_GOALS)
        return share, float(minute), notes
    if minute is None:
        minute = 67
        notes.append("Minutul nu este cunoscut: presupunem mijlocul reprizei a doua.")
    minute = max(45, minute)
    left = max(MIN_REMAINING, FULL_TIME_MINUTE - minute)
    return (
        (1 - FIRST_HALF_GOALS) * min(left, SECOND_HALF_MINUTES) / SECOND_HALF_MINUTES,
        float(minute),
        notes,
    )


def red_card_factors(red_cards):
    """(home factor, away factor) on the remaining scoring rates."""
    home_reds = int((red_cards or {}).get("home") or 0)
    away_reds = int((red_cards or {}).get("away") or 0)
    home = RED_ATTACK**home_reds * RED_DEFENCE**away_reds
    away = RED_ATTACK**away_reds * RED_DEFENCE**home_reds
    return clamp(home, 0.3, 2.0), clamp(away, 0.3, 2.0)


def stat_values(stats, *names):
    """(home, away) of the first stat named like `names` in the whole-match period."""
    rows = (stats or {}).get("match") or next(iter((stats or {}).values()), [])
    for name in names:
        for row in rows or []:
            if str(row.get("name", "")).casefold() == name.casefold():
                home, away = row.get("home_value"), row.get("away_value")
                if isinstance(home, (int, float)) and isinstance(away, (int, float)):
                    return float(home), float(away)
    return None


def momentum(stats, prematch, played_share, minute):
    """Bounded (home, away) factors from live xG (or shots on target) vs the pre-match pace."""
    if not stats or minute is None or minute < MOMENTUM_MIN_MINUTE or played_share <= 0:
        return (1.0, 1.0), None
    observed = stat_values(stats, "Expected goals (xG)")
    source = "xG"
    if observed is None or sum(observed) <= 0:
        shots = stat_values(stats, "Shots on target")
        if shots is None or sum(shots) <= 0:
            return (1.0, 1.0), None
        observed = (shots[0] * SHOT_ON_TARGET_XG, shots[1] * SHOT_ON_TARGET_XG)
        source = "șuturi pe poartă"
    factors = []
    for value, rate in zip(observed, (prematch["home"], prematch["away"])):
        expected = rate * played_share
        if expected <= 0:
            factors.append(1.0)
            continue
        factor = (value + MOMENTUM_PRIOR * expected) / ((1 + MOMENTUM_PRIOR) * expected)
        factors.append(clamp(factor, *MOMENTUM_BOUNDS))
    return tuple(factors), {"source": source, "home": observed[0], "away": observed[1]}


def final_grid(home_goals, away_goals, home_rate, away_rate):
    """{(final_home, final_away): p} = current score + Poisson goals still to come."""
    home, away = poisson(home_rate), poisson(away_rate)
    return {
        (home_goals + h, away_goals + a): hp * ap
        for h, hp in enumerate(home)
        for a, ap in enumerate(away)
    }


def grid_probability(grid, won):
    return sum(p for (h, a), p in grid.items() if won(h, a))


def football_total_key(side, line):
    key = over(line) if side == "over" else under(line)
    return FOOTBALL_ALIASES.get(key, key)


def football_markets(match, grid, rates, who):
    home_name, away_name = match.home, match.away
    h0, a0 = match.home_goals or 0, match.away_goals or 0
    rest_h, rest_a = rates
    context = f"Scor {h0}-{a0}, {who}; mai estimăm {rest_h:.2f} goluri pentru {home_name} "
    context += f"și {rest_a:.2f} pentru {away_name} până la final."
    p1 = grid_probability(grid, lambda h, a: h > a)
    px = grid_probability(grid, lambda h, a: h == a)
    p2 = max(0.0, 1 - p1 - px)
    out = [
        market("football", "1", "Rezultat final", p1, f"{home_name} câștigă. {context}"),
        market("football", "X", "Rezultat final", px, f"Meciul se termină egal. {context}"),
        market("football", "2", "Rezultat final", p2, f"{away_name} câștigă. {context}"),
        market("football", "1X", "Șansă dublă", p1 + px, f"{home_name} nu pierde. {context}"),
        market("football", "X2", "Șansă dublă", px + p2, f"{away_name} nu pierde. {context}"),
        market("football", "12", "Șansă dublă", p1 + p2, f"Nu se termină egal. {context}"),
    ]
    if p1 + p2 > 0:
        out += [
            market(
                "football",
                "dnb_1",
                "Egal = pariu anulat",
                p1 / (p1 + p2),
                f"{home_name} câștigă; la egal miza se returnează. {context}",
            ),
            market(
                "football",
                "dnb_2",
                "Egal = pariu anulat",
                p2 / (p1 + p2),
                f"{away_name} câștigă; la egal miza se returnează. {context}",
            ),
        ]
    total = h0 + a0
    for step in (0.5, 1.5, 2.5):
        line = total + step
        p_over = grid_probability(grid, lambda h, a, line=line: h + a > line)
        need = int(step + 0.5)
        extra = {1: "Niciun alt gol", 2: "Cel mult încă un gol", 3: "Cel mult încă 2 goluri"}
        out.append(
            market(
                "football",
                football_total_key("over", line),
                "Total goluri",
                p_over,
                f"Mai trebuie cel puțin {need} gol{'uri' if need > 1 else ''}. {context}",
            )
        )
        out.append(
            market(
                "football",
                football_total_key("under", line),
                "Total goluri",
                1 - p_over,
                f"{extra[need]} până la final. {context}",
            )
        )
    if not (h0 and a0):
        p_btts = grid_probability(grid, lambda h, a: h > 0 and a > 0)
        out.append(
            market(
                "football",
                "btts",
                "Ambele marchează",
                p_btts,
                f"Ambele echipe marchează. {context}",
            )
        )
        out.append(
            market(
                "football",
                "no_btts",
                "Ambele marchează",
                1 - p_btts,
                f"Cel puțin o echipă nu marchează. {context}",
            )
        )
    if a0 == 0:
        p = grid_probability(grid, lambda h, a: h > a and a == 0)
        out.append(
            market(
                "football",
                "home_win_nil",
                "Victorie la zero",
                p,
                f"{home_name} câștigă fără gol primit. {context}",
            )
        )
    if h0 == 0:
        p = grid_probability(grid, lambda h, a: a > h and h == 0)
        out.append(
            market(
                "football",
                "away_win_nil",
                "Victorie la zero",
                p,
                f"{away_name} câștigă fără gol primit. {context}",
            )
        )
    rest = rest_h + rest_a
    none = math.exp(-rest)
    scored = 1 - none
    share = rest_h / rest if rest > 0 else 0.5
    for key, name, p, why in (
        ("next_goal_1", f"Următorul gol: {home_name}", scored * share, "Următorul gol"),
        ("next_goal_none", "Niciun alt gol", none, "Nu se mai marchează"),
        ("next_goal_2", f"Următorul gol: {away_name}", scored * (1 - share), "Următorul gol"),
    ):
        out.append(
            market(
                "football",
                key,
                "Următorul gol",
                p,
                f"{why}. Piață doar live, nu se decontează din scorul final. {context}",
                selectable=False,
                name=name,
            )
        )
    return [m for m in out if not (m["selectable"] and decided(m["probability"]))]


def football_live(match, analysis=None, stats=None):
    live = match.live or {}
    notes = []
    prematch = football_prematch(match.odds, analysis)
    if prematch["source"] == "default":
        notes.append("Fără cote sau analiză înainte de meci: folosim valori medii de goluri.")
    clock = football_clock(live.get("period", ""), live.get("minute"))
    minute = live.get("minute")
    base = {
        "prematch": {
            "source": prematch["source"],
            "expected": {"home": prematch["home"], "away": prematch["away"]},
        }
    }
    if clock is None:
        notes.append(
            "Prelungiri sau lovituri de departajare: pariurile pe rezultatul final "
            "se decontează de obicei la 90 de minute, deci nu estimăm piețe."
        )
        return {**base, "markets": [], "notes": notes, "summary": summary_text(match, [])}
    share, elapsed, clock_notes = clock
    notes += clock_notes
    home_factor, away_factor = red_card_factors(live.get("red_cards"))
    if home_factor != 1 or away_factor != 1:
        notes.append(
            "Cartonaș roșu: echipa în inferioritate marchează mai puțin, primește mai mult."
        )
    momentum_factors, observed = momentum(stats, prematch, 1 - share, minute)
    if observed:
        notes.append(
            f"Ajustare după {observed['source']} live: "
            f"{observed['home']:.2f}-{observed['away']:.2f}."
        )
    rest_h = prematch["home"] * share * home_factor * momentum_factors[0]
    rest_a = prematch["away"] * share * away_factor * momentum_factors[1]
    grid = final_grid(match.home_goals or 0, match.away_goals or 0, rest_h, rest_a)
    period = live.get("period", "")
    if period == "HT":
        who = "la pauză"
    elif minute is not None:
        who = f"minutul {live.get('clock') or minute}"
    else:
        who = f"minutul estimat {elapsed:.0f}"
    markets = football_markets(match, grid, (rest_h, rest_a), who)
    if prematch["source"] == "default" and share > 0.5:
        unreliable(markets)
        notes.append(UNRELIABLE_NOTE)
    base["remaining"] = {"home": rest_h, "away": rest_a, "share": share}
    base["adjustments"] = {
        "red_cards": {"home": home_factor, "away": away_factor},
        "momentum": {"home": momentum_factors[0], "away": momentum_factors[1]},
        "observed": observed,
    }
    return {**base, "markets": markets, "notes": notes, "summary": summary_text(match, markets)}


# ============================================================================================
# Basketball
# ============================================================================================


def quarter_minutes(match):
    """12-minute quarters in the NBA/WNBA, 10 minutes (FIBA) elsewhere."""
    tokens = re.split(r"\W+", f"{match.league} {match.country}".casefold())
    return 12.0 if "nba" in tokens else 10.0


def basketball_clock(period, minute, quarter, current_total, prior_total):
    """(elapsed fraction of regulation, remaining fraction, notes). Overtime is extra."""
    notes = []
    game = 4 * quarter
    if period == "HT":
        return 0.5, 0.5, notes
    if period == "OT":
        notes.append("Prelungiri: estimăm jumătate de prelungire rămasă.")
        return 1.0, OVERTIME_MINUTES / 2 / game, notes
    if period.startswith("Q") and period[1:].isdigit():
        number = clamp(int(period[1:]), 1, 4)
        if minute is None:
            # No clock: place the game inside the quarter by the score against the
            # expected total (the middle of the quarter when there is no total).
            notes.append("Minutul nu este cunoscut: estimăm progresul din sfert după scor.")
            guess = current_total / prior_total if prior_total > 0 else (number - 0.5) / 4
            elapsed = clamp(guess, (number - 1) / 4 + 0.01, number / 4 - 0.01)
        else:
            # A small value is the minute inside the quarter, otherwise the game minute.
            within = minute if minute <= quarter else minute - (number - 1) * quarter
            elapsed = ((number - 1) * quarter + clamp(within, 0, quarter)) / game
        elapsed = clamp(elapsed, 0.0, 1.0)
        return elapsed, max(1 - elapsed, 0.2 / game), notes
    # Break, interruption or unknown stage: estimate the finished quarters from the score.
    guess = current_total / prior_total if prior_total > 0 else 0.5
    elapsed = clamp(round(guess * 4) / 4, 0.25, 0.75)
    notes.append("Pauză între sferturi: estimăm faza meciului după scor.")
    return elapsed, 1 - elapsed, notes


def basketball_prematch(odds=None, analysis=None):
    """{margin, total, margin_sd, total_sd, source, total_known}."""
    odds = odds or {}
    expected = (analysis or {}).get("expected") or {}
    if analysis and all(isinstance(expected.get(k), (int, float)) for k in ("home", "away")):
        return {
            "margin": expected["home"] - expected["away"],
            "total": expected["home"] + expected["away"],
            "margin_sd": float(expected.get("margin_sd") or BASKETBALL_MARGIN_SD),
            "total_sd": float(expected.get("total_sd") or BASKETBALL_TOTAL_SD),
            "source": "analysis",
            "informed": analysis.get("grade") != "D",
        }
    pair = two_way(odds)
    margin = 0.0
    source = "default"
    if pair:
        margin = NORMAL.inv_cdf(clamp(pair[0], 0.01, 0.99)) * BASKETBALL_MARGIN_SD
        source = "odds"
    return {
        "margin": margin,
        "total": BASKETBALL_DEFAULT_TOTAL,
        "margin_sd": BASKETBALL_MARGIN_SD,
        "total_sd": BASKETBALL_TOTAL_SD,
        "source": source,
        "informed": False,
    }


def two_way(odds):
    a, b = odds.get("1"), odds.get("2")
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 1 and b > 1):
        return None
    total = 1 / a + 1 / b
    if not 0.98 <= total <= 1.4:
        return None
    return (1 / a) / total, (1 / b) / total


def basketball_live(match, analysis=None, stats=None):
    live = match.live or {}
    notes = []
    prematch = basketball_prematch(match.odds, analysis)
    h0, a0 = match.home_goals or 0, match.away_goals or 0
    quarter = quarter_minutes(match)
    elapsed, remaining, clock_notes = basketball_clock(
        live.get("period", ""), live.get("minute"), quarter, h0 + a0, prematch["total"]
    )
    notes += clock_notes
    if prematch["source"] == "default":
        notes.append("Fără cote sau analiză înainte de meci: totalul vine din ritmul de joc.")
    informed = prematch["informed"] or prematch["source"] == "analysis"
    pace_prior = PACE_PRIOR_INFORMED if informed else PACE_PRIOR_DEFAULT
    margin_prior = (
        MARGIN_PRIOR_INFORMED if prematch["source"] != "default" else (MARGIN_PRIOR_DEFAULT)
    )
    # Per-game scoring rate and margin rate, blending the prior with what has been observed.
    played = min(elapsed, 1.0)
    total_rate = (pace_prior * prematch["total"] + (h0 + a0)) / (pace_prior + played)
    margin_rate = (margin_prior * prematch["margin"] + (h0 - a0)) / (margin_prior + played)
    rest_total = total_rate * remaining
    rest_margin = margin_rate * remaining
    margin_sd = max(0.5, prematch["margin_sd"] * math.sqrt(remaining))
    total_sd = max(0.5, prematch["total_sd"] * math.sqrt(remaining))
    final_margin = h0 - a0 + rest_margin
    final_total = h0 + a0 + rest_total
    p_home = NORMAL.cdf(final_margin / margin_sd)
    context = (
        f"Scor {h0}-{a0}; estimăm final {(final_total + final_margin) / 2:.0f}-"
        f"{(final_total - final_margin) / 2:.0f} (diferență ±{margin_sd:.0f} puncte)."
    )
    markets = [
        market(
            "basketball",
            "1",
            "Câștigător (incl. prelungiri)",
            p_home,
            f"{match.home} câștigă meciul. {context}",
        ),
        market(
            "basketball",
            "2",
            "Câștigător (incl. prelungiri)",
            1 - p_home,
            f"{match.away} câștigă meciul. {context}",
        ),
    ]
    base = round(final_margin) + 0.5
    for line in sorted({-base + d for d in (-6, -3, 0, 3, 6)}):
        p = NORMAL.cdf((final_margin + line) / margin_sd)
        markets.append(
            market(
                "basketball",
                handicap("1", line),
                "Handicap",
                p,
                f"{match.home} cu handicap {fmt_signed(line)}. {context}",
            )
        )
        markets.append(
            market(
                "basketball",
                handicap("2", -line),
                "Handicap",
                1 - p,
                f"{match.away} cu handicap {fmt_signed(-line)}. {context}",
            )
        )
    if prematch["source"] == "default" and elapsed < 0.5:
        unreliable(markets)
        notes.append(UNRELIABLE_NOTE)
    # The observed pace is only trustworthy when the clock is known (or the total was
    # predicted before the game); without both, the elapsed time is itself guessed.
    period = live.get("period", "")
    pace_known = prematch["source"] == "analysis" or period in ("HT", "OT")
    pace_known = pace_known or (live.get("minute") is not None and elapsed >= 0.25)
    totals = []
    middle = round(final_total) + 0.5
    for line in sorted({middle + d for d in (-10, -5, 0, 5, 10)}):
        if line <= h0 + a0:
            continue
        p = 1 - NORMAL.cdf((line - final_total) / total_sd)
        totals.append(
            market(
                "basketball",
                over(line),
                "Total puncte",
                p,
                f"Total final peste {fmt_line(line)} (±{total_sd:.0f}). {context}",
            )
        )
        totals.append(
            market(
                "basketball",
                under(line),
                "Total puncte",
                1 - p,
                f"Total final sub {fmt_line(line)} (±{total_sd:.0f}). {context}",
            )
        )
    if not pace_known:
        unreliable(totals)
        notes.append("Ritmul de joc este incert: totalurile de puncte nu sunt sugerate.")
    markets += totals
    return {
        "prematch": {
            "source": prematch["source"],
            "expected": {"margin": prematch["margin"], "total": prematch["total"]},
        },
        "remaining": {
            "fraction": remaining,
            "elapsed": elapsed,
            "margin": rest_margin,
            "total": rest_total,
            "margin_sd": margin_sd,
            "total_sd": total_sd,
        },
        "projected": {"margin": final_margin, "total": final_total},
        "markets": markets,
        "notes": notes,
        "summary": summary_text(match, markets),
    }


# ============================================================================================
# Tennis
# ============================================================================================


def sets_needed(best_of):
    return best_of // 2 + 1


def tennis_match_win(q, home_sets, away_sets, best_of=3):
    """P(home wins the match) from the current set score when each set is won with q."""
    return sum(
        p for (h, a), p in tennis_final_scores(q, home_sets, away_sets, best_of).items() if h > a
    )


def tennis_final_scores(q, home_sets, away_sets, best_of=3):
    """{(home_sets, away_sets): p} of every reachable final set score (exact Markov chain)."""
    need = sets_needed(best_of)
    if home_sets >= need or away_sets >= need:
        return {(home_sets, away_sets): 1.0}
    out = {}
    home_left, away_left = need - home_sets, need - away_sets
    # Home wins the last set: away takes k of the sets before it (k < away_left).
    for k in range(away_left):
        ways = math.comb(home_left - 1 + k, k)
        out[need, away_sets + k] = ways * q**home_left * (1 - q) ** k
    for k in range(home_left):
        ways = math.comb(away_left - 1 + k, k)
        out[home_sets + k, need] = ways * (1 - q) ** away_left * q**k
    return out


def set_probability(p, best_of=3):
    """Per-set win probability q with P(win match from 0-0) == p."""
    p = clamp(p, 1e-4, 1 - 1e-4)
    low, high = 1e-6, 1 - 1e-6
    for _ in range(60):
        middle = (low + high) / 2
        if tennis_match_win(middle, 0, 0, best_of) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def best_of_match(match):
    try:
        from footypreds.sports.tennis import best_of

        return best_of(match)
    except (ImportError, AttributeError):  # pragma: no cover - model agents keep best_of
        return 3


def tennis_prematch(odds=None, analysis=None, best_of=3):
    odds = odds or {}
    expected = (analysis or {}).get("expected") or {}
    if analysis and isinstance(expected.get("home_win"), (int, float)):
        p = float(expected["home_win"])
        return {"home_win": p, "set_win": set_probability(p, best_of), "source": "analysis"}
    pair = two_way(odds)
    if pair:
        return {"home_win": pair[0], "set_win": set_probability(pair[0], best_of), "source": "odds"}
    return {"home_win": 0.5, "set_win": 0.5, "source": "default"}


def tennis_live(match, analysis=None, stats=None):
    live = match.live or {}
    notes = []
    sets = best_of_match(match)
    prematch = tennis_prematch(match.odds, analysis, sets)
    if prematch["source"] == "default":
        notes.append("Fără cote înainte de meci: considerăm jucătorii egali.")
    q = prematch["set_win"]
    h0, a0 = match.home_goals or 0, match.away_goals or 0
    need = sets_needed(sets)
    period = live.get("period", "")
    current_set = h0 + a0 + 1
    if period.startswith("S") and period[1:].isdigit() and int(period[1:]) != current_set:
        notes.append("Numărul setului din flux nu se potrivește cu scorul; folosim scorul.")
    if not period:
        notes.append("Setul curent nu este cunoscut; calculăm din scorul la seturi.")
    notes.append(
        "Scorul pe game-uri din setul curent nu este disponibil: setul în desfășurare "
        "este estimat cu șansa pe set de dinainte de meci."
    )
    scores = tennis_final_scores(q, h0, a0, sets)
    p_home = sum(p for (h, a), p in scores.items() if h > a)
    context = (
        f"Seturi {h0}-{a0} (cel mai bun din {sets}); șansa pe set a lui {match.home}: {q:.0%}."
    )
    markets = [
        market("tennis", "1", "Câștigător", p_home, f"{match.home} câștigă meciul. {context}"),
        market("tennis", "2", "Câștigător", 1 - p_home, f"{match.away} câștigă meciul. {context}"),
    ]
    for (h, a), p in sorted(scores.items(), key=lambda item: (-item[0][0], item[0][1])):
        markets.append(
            market(
                "tennis",
                f"sets_{h}-{a}",
                "Scor la seturi",
                p,
                f"Meciul se termină {h}-{a} la seturi. {context}",
            )
        )
    for line in [x + 0.5 for x in range(need, sets)]:
        if line < h0 + a0:
            continue
        above = sum(p for (h, a), p in scores.items() if h + a > line)
        markets.append(
            market(
                "tennis",
                over(line),
                "Total seturi",
                above,
                f"Se joacă cel puțin {int(line + 0.5)} seturi. {context}",
            )
        )
        markets.append(
            market(
                "tennis",
                under(line),
                "Total seturi",
                1 - above,
                f"Se joacă cel mult {int(line - 0.5)} seturi. {context}",
            )
        )
    for line in [x + 0.5 for x in range(1, need)]:
        for side, sign in (("1", -1), ("1", 1), ("2", -1), ("2", 1)):
            own = 0 if side == "1" else 1
            won = sum(p for s, p in scores.items() if s[own] + sign * line > s[1 - own])
            name = match.home if side == "1" else match.away
            markets.append(
                market(
                    "tennis",
                    handicap(side, sign * line),
                    "Handicap seturi",
                    won,
                    f"{name} cu handicap {'+' if sign > 0 else '-'}{fmt_line(line)} seturi. "
                    + context,
                )
            )
    if h0 < need and a0 < need:
        for key, name, p in (
            ("next_set_1", f"Setul {current_set}: {match.home}", q),
            ("next_set_2", f"Setul {current_set}: {match.away}", 1 - q),
        ):
            markets.append(
                market(
                    "tennis",
                    key,
                    "Setul curent",
                    p,
                    f"Câștigătorul setului {current_set}. Piață doar live, nu se decontează "
                    f"din scorul final. {context}",
                    selectable=False,
                    name=name,
                )
            )
    markets = [m for m in markets if not (m["selectable"] and decided(m["probability"]))]
    if prematch["source"] == "default":
        unreliable(markets)
        notes.append(UNRELIABLE_NOTE)
    return {
        "prematch": {
            "source": prematch["source"],
            "expected": {"home_win": prematch["home_win"], "set_win": q, "best_of": sets},
        },
        "markets": markets,
        "notes": notes,
        "summary": summary_text(match, markets),
    }


# ============================================================================================
# Shared
# ============================================================================================

ANALYZERS = {"football": football_live, "basketball": basketball_live, "tennis": tennis_live}


def minimum_odds(fair):
    """The fair price rounded UP to 2 decimals: the lowest odds worth taking."""
    return math.ceil(fair * 100 - 1e-9) / 100


def suggestions(markets, limit=MAX_SUGGESTIONS):
    """Up to `limit` bets worth considering now: the safest options plus a balanced one.

    Only settleable, reliable final-result markets; one per group; never two markets that
    are the same event right now (equal probability, e.g. "2" and "sets_1-2" at 1-1), and
    never an already near-certain bet (a fair price under 1.03 is not worth a stake).
    """
    playable = [
        m
        for m in markets
        if m["selectable"] and m.get("reliable", True) and not decided(m["probability"])
    ]
    safe = sorted(
        (m for m in playable if SAFE_RANGE[0] <= m["probability"] <= SAFE_RANGE[1]),
        key=lambda m: (-m["probability"], m["key"]),
    )
    balanced = sorted(
        (m for m in playable if BALANCED_RANGE[0] <= m["probability"] <= BALANCED_RANGE[1]),
        key=lambda m: (abs(m["probability"] - 0.6), m["key"]),
    )
    chosen = []

    def fits(m):
        return all(
            m["group"] != c["group"] and abs(m["probability"] - c["probability"]) > 1e-9
            for c, _ in chosen
        )

    def take(candidates, kind, room):
        for m in candidates:
            if len(chosen) >= room:
                return
            if fits(m):
                chosen.append((m, kind))
                if kind == "echilibrat":
                    return

    take(safe, "sigur", limit - 1)
    take(balanced, "echilibrat", limit)
    take(safe, "sigur", limit)
    output = []
    for m, kind in chosen:
        prefix = "Opțiune sigură" if kind == "sigur" else "Opțiune echilibrată, cotă mai mare"
        output.append(
            {
                "key": m["key"],
                "label": m["label"],
                "group": m["group"],
                "probability": m["probability"],
                "fair_odds": m["fair_odds"],
                "min_odds": minimum_odds(m["fair_odds"]),
                "kind": kind,
                "why": (
                    f"{prefix}: {m['probability']:.0%}. Merită doar la o cotă de cel puțin "
                    f"{minimum_odds(m['fair_odds']):.2f}. {m['why']}"
                ),
            }
        )
    return output


def summary_text(match, markets):
    if not markets:
        return f"{match.home} - {match.away}: nu estimăm piețe live în această fază."
    by_key = {m["key"]: m for m in markets}
    h0, a0 = match.home_goals or 0, match.away_goals or 0
    p1 = by_key.get("1", {}).get("probability")
    p2 = by_key.get("2", {}).get("probability")
    if p1 is None or p2 is None:
        return f"{match.home} - {match.away}, scor {h0}-{a0}."
    px = by_key.get("X", {}).get("probability", 0.0)
    if px >= max(p1, p2):
        lead = f"cel mai probabil rezultat final este egal ({px:.0%})"
    elif p1 >= p2:
        lead = f"{match.home} are {p1:.0%} șanse să câștige"
    else:
        lead = f"{match.away} are {p2:.0%} șanse să câștige"
    return f"Scor {h0}-{a0}: {lead}."


def live_item(match, analysis=None, stats=None):
    """LiveItem (docs/CONTRACTS.md §10.2) of a live match; `analysis` is the pre-match one."""
    result = ANALYZERS[match.sport](match, analysis, stats)
    live = match.live or {}
    markets = result["markets"]
    item = {
        "match": public_match(match),
        # Display logos also at the top level (same values as in `match`).
        **match_media(match),
        "sport": match.sport,
        "competition": competition_name(match.league),
        "competition_id": match_competition(match),
        "minute": live.get("minute"),
        "period": live.get("period", ""),
        "stage": live.get("stage", ""),
        "clock": live.get("clock", ""),
        "score": {"home": match.home_goals, "away": match.away_goals},
        "markets": markets,
        "probabilities": {m["key"]: m["probability"] for m in markets},
        "suggestions": suggestions(markets),
        "summary": result["summary"],
        "pre_match": {
            **result["prematch"],
            "odds": {k: v for k, v in match.odds.items() if k in ("1", "X", "2")},
            "grade": (analysis or {}).get("grade"),
        },
        "notes": [*result["notes"], ODDS_NOTE],
        "model": {
            k: v for k, v in result.items() if k not in ("markets", "notes", "summary", "prematch")
        },
        "version": VERSION,
    }
    return item
