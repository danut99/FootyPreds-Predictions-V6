"""Score matrix and every market derived from it.

All full-time markets come from ONE normalized score matrix, so 1X2, goals, BTTS and
correct score can never contradict each other. Half-time markets come from a split of
the same expected goals and are rescaled to agree with the full-time 1X2.
"""

import math

MAX_GOALS = 12
FIRST_HALF_SHARE = 0.44

# key: (label, group, full-time predicate). Every key here can be settled from a final score.
FT_MARKETS = {
    "1": ("Victorie gazde", "Rezultat final", lambda h, a: h > a),
    "X": ("Egal", "Rezultat final", lambda h, a: h == a),
    "2": ("Victorie oaspeți", "Rezultat final", lambda h, a: h < a),
    "1X": ("Gazde sau egal", "Șansă dublă", lambda h, a: h >= a),
    "X2": ("Egal sau oaspeți", "Șansă dublă", lambda h, a: h <= a),
    "12": ("Fără egal", "Șansă dublă", lambda h, a: h != a),
    "over05": ("Peste 0.5 goluri", "Total goluri", lambda h, a: h + a > 0.5),
    "under05": ("Sub 0.5 goluri", "Total goluri", lambda h, a: h + a < 0.5),
    "over15": ("Peste 1.5 goluri", "Total goluri", lambda h, a: h + a > 1.5),
    "under15": ("Sub 1.5 goluri", "Total goluri", lambda h, a: h + a < 1.5),
    "over25": ("Peste 2.5 goluri", "Total goluri", lambda h, a: h + a > 2.5),
    "under25": ("Sub 2.5 goluri", "Total goluri", lambda h, a: h + a < 2.5),
    "over35": ("Peste 3.5 goluri", "Total goluri", lambda h, a: h + a > 3.5),
    "under35": ("Sub 3.5 goluri", "Total goluri", lambda h, a: h + a < 3.5),
    "over45": ("Peste 4.5 goluri", "Total goluri", lambda h, a: h + a > 4.5),
    "under45": ("Sub 4.5 goluri", "Total goluri", lambda h, a: h + a < 4.5),
    "btts": ("Ambele marchează", "Ambele marchează", lambda h, a: h > 0 and a > 0),
    "no_btts": ("Nu marchează ambele", "Ambele marchează", lambda h, a: h == 0 or a == 0),
    "home_over05": ("Gazdele marchează", "Goluri pe echipă", lambda h, a: h > 0),
    "home_over15": ("Gazdele peste 1.5", "Goluri pe echipă", lambda h, a: h > 1),
    "away_over05": ("Oaspeții marchează", "Goluri pe echipă", lambda h, a: a > 0),
    "away_over15": ("Oaspeții peste 1.5", "Goluri pe echipă", lambda h, a: a > 1),
    "home_win_nil": ("Gazdele câștigă la zero", "Goluri pe echipă", lambda h, a: h > 0 and a == 0),
    "away_win_nil": ("Oaspeții câștigă la zero", "Goluri pe echipă", lambda h, a: a > 0 and h == 0),
    "btts_over25": ("GG și peste 2.5", "Combinate", lambda h, a: h > 0 and a > 0 and h + a > 2),
    "1_over15": ("1 și peste 1.5", "Combinate", lambda h, a: h > a and h + a > 1),
    "2_over15": ("2 și peste 1.5", "Combinate", lambda h, a: a > h and h + a > 1),
    "1X_under35": ("1X și sub 3.5", "Combinate", lambda h, a: h >= a and h + a < 4),
    "X2_under35": ("X2 și sub 3.5", "Combinate", lambda h, a: a >= h and h + a < 4),
}
# The prospective ledger keeps the original, comparable market set; trivial lines such as
# "over 0.5" would otherwise dominate every selection at meaningless odds.
SELECTABLE = (
    "1",
    "X",
    "2",
    "1X",
    "X2",
    "12",
    "over15",
    "under15",
    "over25",
    "under25",
    "over35",
    "under35",
    "btts",
    "no_btts",
)
HT_MARKETS = {
    "ht_1": ("Pauză: gazde", "Pauză"),
    "ht_X": ("Pauză: egal", "Pauză"),
    "ht_2": ("Pauză: oaspeți", "Pauză"),
    "ht_over05": ("Pauză: peste 0.5 goluri", "Pauză"),
    "ht_over15": ("Pauză: peste 1.5 goluri", "Pauză"),
}
RESULTS = ("1", "X", "2")
LABELS = {key: value[0] for key, value in FT_MARKETS.items()} | {
    key: value[0] for key, value in HT_MARKETS.items()
}
for _ht in RESULTS:
    for _ft in RESULTS:
        LABELS[f"{_ht}/{_ft}"] = f"Pauză {_ht} / Final {_ft}"


def outcome(key, home, away):
    """True/False for a full-time market, from a final score."""
    return bool(FT_MARKETS[key][2](home, away))


def result_of(home, away):
    return "1" if home > away else ("2" if home < away else "X")


def poisson(rate, size=MAX_GOALS + 1):
    values = [math.exp(-rate)]
    for goals in range(1, size):
        values.append(values[-1] * rate / goals)
    return values


def score_matrix(home_rate, away_rate, rho=0.0, max_goals=MAX_GOALS):
    """matrix[h][a], normalized; rho is the Dixon-Coles low-score dependence."""
    home, away = poisson(home_rate, max_goals + 1), poisson(away_rate, max_goals + 1)
    matrix = [[hp * ap for ap in away] for hp in home]
    if rho:
        # Keep every correction factor non-negative.
        low = max(-1 / max(home_rate, 1e-9), -1 / max(away_rate, 1e-9))
        high = min(1 / max(home_rate * away_rate, 1e-9), 1)
        rho = min(high, max(low, rho))
        matrix[0][0] *= 1 - home_rate * away_rate * rho
        matrix[0][1] *= 1 + home_rate * rho
        matrix[1][0] *= 1 + away_rate * rho
        matrix[1][1] *= 1 - rho
    total = sum(map(sum, matrix))
    return [[p / total for p in row] for row in matrix]


def cells(matrix):
    return ((h, a, p) for h, row in enumerate(matrix) for a, p in enumerate(row))


def one_x_two(matrix):
    totals = {"1": 0.0, "X": 0.0, "2": 0.0}
    for h, a, p in cells(matrix):
        totals[result_of(h, a)] += p
    return totals


def reweight(matrix, target):
    """Scale each result region so the matrix reproduces a target 1X2, keeping its shape."""
    current = one_x_two(matrix)
    factor = {k: target[k] / current[k] if current[k] > 0 else 0 for k in RESULTS}
    output = [
        [p * factor[result_of(h, a)] for a, p in enumerate(row)] for h, row in enumerate(matrix)
    ]
    # A region with no mass cannot be scaled up; keep the matrix a probability distribution.
    total = sum(map(sum, output))
    if total <= 0:
        return matrix
    return [[p / total for p in row] for row in output] if abs(total - 1) > 1e-12 else output


def over_probability(matrix, line=2.5):
    """P(total goals > line) under the matrix."""
    return sum(p for h, a, p in cells(matrix) if h + a > line)


# --- goals calibration (docs/MODEL.md, "Calibrarea golurilor") --------------------------------
# The totals step moves ONE number, P(over 2.5), and then rescales both goal rates by the same
# factor so the whole matrix agrees with it; the 1X2 reweight is re-applied, so 1X2 never moves.
TOTAL_LINE = 2.5
# log(scale) is searched in [-SCALE_LIMIT, SCALE_LIMIT], i.e. rates x0.25 .. x4.
SCALE_LIMIT = math.log(4.0)
_P_EPS = 1e-9


def logit(p):
    p = min(1 - _P_EPS, max(_P_EPS, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    z = math.exp(x)
    return z / (1 + z)


def platt(p, intercept=0.0, slope=1.0):
    """Monotone (slope > 0) map of a probability, bounded in (0, 1); identity at (0, 1)."""
    if intercept == 0.0 and slope == 1.0:
        return p
    return sigmoid(intercept + slope * logit(p))


def pool_binary(model, market, weight):
    """Logarithmic opinion pool of two binary forecasts: weight 0 = model, 1 = market."""
    if market is None or weight <= 0:
        return model
    if weight >= 1:
        return market
    return sigmoid((1 - weight) * logit(model) + weight * logit(market))


def two_way_probability(odds, yes, no, low=0.95, high=1.4):
    """Margin-free P(yes) from a two-way price pair, or None when it is not a usable book.

    A best-of-bookmakers pair can be slightly under 1 (0.95 allowed); an absurd overround or a
    price of 1.0 or less (1/odds >= 1) is not a reference.
    """
    values = [odds.get(yes), odds.get(no)]
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 1
        for v in values
    ):
        return None
    inverse = [1 / v for v in values]
    total = sum(inverse)
    if not low <= total <= high:
        return None
    return inverse[0] / total


def _over_after_reweight(home_rate, away_rate, rho, target, line):
    """P(total > line) of reweight(score_matrix(home_rate, away_rate, rho), target), fast."""
    matrix = score_matrix(home_rate, away_rate, rho)
    mass = {"1": 0.0, "X": 0.0, "2": 0.0}
    over = {"1": 0.0, "X": 0.0, "2": 0.0}
    for h, a, p in cells(matrix):
        region = result_of(h, a)
        mass[region] += p
        if h + a > line:
            over[region] += p
    usable = [k for k in RESULTS if mass[k] > 0 and target[k] > 0]
    total = sum(target[k] for k in usable)
    if total <= 0:
        return sum(over.values())
    return sum(target[k] * over[k] / mass[k] for k in usable) / total


def fit_total(home_rate, away_rate, rho, target_1x2, target_over, line=TOTAL_LINE, tol=1e-10):
    """(scale, matrix): both rates x scale so that, after the 1X2 reweight to `target_1x2`,
    P(total > line) == target_over. The 1X2 of the returned matrix equals target_1x2.

    P(over) of the reweighted matrix rises with the scale (within every 1/X/2 region a higher
    rate shifts mass to higher totals), so a safeguarded secant/bisection search on log(scale)
    finds it in a few steps. An unreachable target returns the nearest end of [x0.25, x4].
    """

    def gap(x):
        scale = math.exp(x)
        return (
            _over_after_reweight(home_rate * scale, away_rate * scale, rho, target_1x2, line)
            - target_over
        )

    lo, hi = -SCALE_LIMIT, SCALE_LIMIT
    f_lo, f_hi = gap(lo), gap(hi)
    if f_lo >= 0:
        x = lo
    elif f_hi <= 0:
        x = hi
    else:
        # Illinois (modified regula falsi): always bracketed, superlinear in practice.
        x, side = 0.0, 0
        for _ in range(100):
            x = (lo * f_hi - hi * f_lo) / (f_hi - f_lo)
            f_x = gap(x)
            if abs(f_x) < tol or hi - lo < 1e-12:
                break
            if f_x < 0:
                lo, f_lo = x, f_x
                if side == -1:
                    f_hi /= 2
                side = -1
            else:
                hi, f_hi = x, f_x
                if side == 1:
                    f_lo /= 2
                side = 1
    scale = math.exp(x)
    matrix = reweight(score_matrix(home_rate * scale, away_rate * scale, rho), target_1x2)
    return scale, matrix


def full_time(matrix):
    probabilities = dict.fromkeys(FT_MARKETS, 0.0)
    for h, a, p in cells(matrix):
        for key, (_, _, won) in FT_MARKETS.items():
            if won(h, a):
                probabilities[key] += p
    return probabilities


def half_time(home_rate, away_rate, ft_target, share=FIRST_HALF_SHARE):
    """HT result, HT goals and HT/FT, consistent with the full-time 1X2 target."""
    first_home = poisson(home_rate * share, 9)
    first_away = poisson(away_rate * share, 9)
    second_home = poisson(home_rate * (1 - share), 11)
    second_away = poisson(away_rate * (1 - share), 11)
    difference = {}
    for h, hp in enumerate(second_home):
        for a, ap in enumerate(second_away):
            difference[h - a] = difference.get(h - a, 0.0) + hp * ap
    joint = {(ht, ft): 0.0 for ht in RESULTS for ft in RESULTS}
    for h, hp in enumerate(first_home):
        for a, ap in enumerate(first_away):
            ht = result_of(h, a)
            for diff, dp in difference.items():
                joint[ht, result_of(h - a + diff, 0)] += hp * ap * dp
    column = {ft: sum(joint[ht, ft] for ht in RESULTS) for ft in RESULTS}
    for ht, ft in joint:
        joint[ht, ft] *= ft_target[ft] / column[ft] if column[ft] else 0
    total_first = (home_rate + away_rate) * share
    markets = {
        "ht_1": sum(joint["1", ft] for ft in RESULTS),
        "ht_X": sum(joint["X", ft] for ft in RESULTS),
        "ht_2": sum(joint["2", ft] for ft in RESULTS),
        "ht_over05": 1 - math.exp(-total_first),
        "ht_over15": 1 - math.exp(-total_first) * (1 + total_first),
    }
    htft = {f"{ht}/{ft}": p for (ht, ft), p in joint.items()}
    return markets, htft


def correct_scores(matrix, limit=10):
    ranked = sorted(cells(matrix), key=lambda c: c[2], reverse=True)[:limit]
    return [{"score": f"{h}-{a}", "probability": p} for h, a, p in ranked]


def score_grid(matrix, size=6):
    """size x size grid of exact scores (0..size-1), for display."""
    return [[matrix[h][a] for a in range(size)] for h in range(size)]


def goal_distribution(matrix, size=6):
    totals = [0.0] * size
    for h, a, p in cells(matrix):
        totals[min(h + a, size - 1)] += p
    return totals


# --- extended football markets (generic keys, docs/CONTRACTS.md §4.1) ---------------------
# Bets with no legacy key: draw no bet, Asian handicaps (half and whole lines), totals and team
# totals on other lines, parity and correct score. Every one comes from the SAME score matrix,
# and settles exactly like footypreds.sports.settle (whole lines push, draw voids DNB).
EXTENDED_GROUPS = {
    "dnb": "Egal = pariu anulat",
    "handicap": "Handicap asiatic",
    "total": "Total goluri",
    "team_total": "Goluri pe echipă",
    "parity": "Par / impar",
    "exact": "Scor corect",
}
EXTENDED_ORDER = tuple(EXTENDED_GROUPS)
# Always shown, with or without a quoted price; any other generic key appears when quoted.
EXTENDED_DEFAULT = (
    "dnb_1",
    "dnb_2",
    "ah_1_-1.5",
    "ah_2_+1.5",
    "ah_1_+1.5",
    "ah_2_-1.5",
    "odd",
    "even",
)
HTFT_GROUP = "Pauză/Final"


def _sign(value):
    """True above zero, False below, None (push) exactly on it."""
    return True if value > 0 else False if value < 0 else None


def predicate(key):
    """(h, a) -> True | False | None for a generic football key, or None if unsupported.

    Mirrors footypreds.sports.settle.settle for a finished game, but parses the key once, so a
    whole score matrix can be summed quickly. Quarter lines and set scores are unsupported.
    """
    from footypreds.sports.keys import is_whole_or_half, parse

    spec = parse(key)
    if spec is None:
        return None
    family, groups = spec
    if family == "total":
        line = float(groups[1])
        sign = 1 if groups[0] == "over" else -1
        if not is_whole_or_half(line):
            return None
        return lambda h, a: _sign(sign * (h + a - line))
    if family == "team_total":
        line = float(groups[2])
        sign = 1 if groups[1] == "over" else -1
        if not is_whole_or_half(line):
            return None
        if groups[0] == "home":
            return lambda h, a: _sign(sign * (h - line))
        return lambda h, a: _sign(sign * (a - line))
    if family == "handicap":
        line = float(groups[1])
        if not is_whole_or_half(line):
            return None
        if groups[0] == "1":
            return lambda h, a: _sign(h + line - a)
        return lambda h, a: _sign(a + line - h)
    if family == "dnb":
        if groups[0] == "1":
            return lambda h, a: _sign(h - a)
        return lambda h, a: _sign(a - h)
    if family == "parity":
        rest = 1 if groups[0] == "odd" else 0
        return lambda h, a: (h + a) % 2 == rest
    if family == "exact" and groups[0] == "cs":
        score = (int(groups[1]), int(groups[2]))
        return lambda h, a: (h, a) == score
    return None


def win_push(matrix, key):
    """(P(win), P(push)) of a generic key under the score matrix, or None if unsupported."""
    decide = predicate(key)
    if decide is None:
        return None
    won = push = 0.0
    for h, a, p in cells(matrix):
        outcome_ = decide(h, a)
        if outcome_ is None:
            push += p
        elif outcome_:
            won += p
    return won, push


def extended_keys(odds):
    """Generic football keys to show: the defaults plus every quoted generic key."""
    from footypreds.sports.keys import FOOTBALL_ALIASES, parse

    keys = set(EXTENDED_DEFAULT)
    for key in odds:
        key = FOOTBALL_ALIASES.get(key, key)
        if key in FT_MARKETS or key in LABELS:
            continue
        spec = parse(key)
        if spec and spec[0] in EXTENDED_GROUPS and predicate(key) is not None:
            keys.add(key)

    def order(key):
        family, groups = parse(key)
        words, numbers = [], []
        for group in groups:
            try:
                numbers.append(float(group))
            except ValueError:
                words.append(group)
        return EXTENDED_ORDER.index(family), words, numbers, key

    return sorted(keys, key=order)


def extended(matrix, odds):
    """[(key, label, group, probability, push)] of the generic football markets.

    `probability` is the chance of winning when the bet is not refunded (P(win) / (1 -
    P(push))), so 1 / probability is the fair price of a bet with refunds and
    probability * odds - 1 keeps the sign of the true expected value.
    """
    from footypreds.sports.keys import label, parse

    output = []
    for key in extended_keys(odds):
        won, push = win_push(matrix, key)
        decided = 1 - push
        if decided <= 1e-12:
            continue
        family = parse(key)[0]
        output.append(
            (key, label("football", key), EXTENDED_GROUPS[family], min(1.0, won / decided), push)
        )
    return output
