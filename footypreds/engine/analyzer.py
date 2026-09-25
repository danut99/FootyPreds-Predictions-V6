"""Match analyzer: ratings from ALL competitions + recent form + H2H + market, one matrix.

Pipeline for one fixture (only results older than kickoff - 3h are visible):
1. Collect both teams' results in every competition (league, cups, Europe, friendlies).
2. Fit time-weighted attack/defence ratings on the teams, their opponents and the
   opponents' other results, so a 3-0 against a weak side counts less than against a
   strong one.
3. Adjust for short-term form (last N matches vs. what the ratings expected) and, lightly,
   for head-to-head history.
4. Build a Dixon-Coles score matrix; if 1X2 odds exist, blend the model with the
   bookmaker's margin-free probabilities.
5. Goals calibration: P(over 2.5) goes through a Platt map fitted on the validation season
   and, when an over/under 2.5 price exists, is pooled with its margin-free probability. Both
   goal rates are then rescaled by one factor so the whole matrix agrees with it, and the 1X2
   reweight is re-applied, so 1X2 is unchanged and every market still comes from one matrix.
6. Derive every market from the matrix, grade data quality instead of refusing, and pick
   at most one high-probability selection for the ledger.
"""

import math
from dataclasses import asdict, dataclass, replace
from datetime import timedelta

from footypreds.engine import markets as mk
from footypreds.engine.form import head_to_head, is_opponent, team_form
from footypreds.engine.history import HistoryIndex, canonical, is_friendly, is_youth
from footypreds.engine.ratings import decay, fit

VERSION = "8.1-calibrated-goals"
# Display only: mutual games are rare (national teams meet every few years), so the H2H table
# and insights look back 10 years. Ratings, form and the tuned h2h factors keep max_days.
H2H_DISPLAY_DAYS = 3650


@dataclass(frozen=True)
class Params:
    """Tuned on the 2024-25 validation season only (see footypreds/evaluation/tune.py)."""

    half_life: float = 540.0
    prior: float = 8.0
    max_days: int = 730
    rho: float = -0.12
    form_window: int = 6
    form_prior: float = 3.0
    # Validation optimum was 0.0; 0.15 costs ~0.001 log loss and keeps recent form visible.
    form_weight: float = 0.15
    h2h_prior: float = 6.0
    h2h_weight: float = 0.1
    # Validation improved up to 1.0 (pure market 1X2); 0.9 keeps the model's own view.
    market_weight: float = 0.9
    # Goals calibration (python -m footypreds.evaluation.tune --totals): fitted on the 2024-25
    # validation predictions of 16 leagues (5485 matches). logit P(over 2.5) -> intercept +
    # slope * logit; identity is (0, 1). A slope < 1 means the raw totals were too extreme.
    totals_intercept: float = 0.0556
    totals_slope: float = 0.7241
    # Weight of the margin-free over/under 2.5 price in the logarithmic pool (0 = model only).
    # Validation log loss fell monotonically up to 1.0: with a price, the market decides.
    totals_market_weight: float = 1.0
    friendly_weight: float = 0.5
    cutoff_hours: float = 3.0
    max_fit_matches: int = 6000


PARAMS = Params()


def with_params(**changes):
    return replace(PARAMS, **changes)


def weight_of(match, reference, params, youth=False):
    if is_youth(match.league) != youth:
        return 0.0
    age = (reference - match.kickoff).total_seconds() / 86400
    factor = params.friendly_weight if is_friendly(match.league) else 1.0
    return decay(age, params.half_life) * factor


def fit_rows(matches, reference, params, overrides=None, youth=False):
    overrides = overrides or {}
    rows = []
    for match in matches:
        w = weight_of(match, reference, params, youth)
        if w <= 0:
            continue
        home_key, away_key = overrides.get(match.id, (None, None))
        rows.append(
            (
                home_key or canonical(match.home),
                away_key or canonical(match.away),
                match.home_goals,
                match.away_goals,
                w,
            )
        )
    return rows


def fit_history(matches, reference, params=PARAMS, init=None, youth=False):
    """Ratings for a whole competition or store, e.g. in a walk-forward backtest."""
    rows = fit_rows(matches, reference, params, youth=youth)
    return fit(rows, prior=params.prior, iterations=10 if init else 40, init=init)


def local_ratings(fixture, index, home_rows, away_rows, cutoff, oldest, params):
    """Fit on the two teams, their opponents and the opponents' other results (2 hops)."""
    home_key, away_key = canonical(fixture.home), canonical(fixture.away)
    overrides, selected = {}, {}
    for rows, key in ((home_rows, home_key), (away_rows, away_key)):
        for match, side in rows:
            selected[match.id] = match
            keys = overrides.get(match.id, (None, None))
            overrides[match.id] = (key, keys[1]) if side == "home" else (keys[0], key)
    opponents = {
        canonical(match.away if side == "home" else match.home)
        for match, side in home_rows + away_rows
    } - {home_key, away_key}
    for opponent in opponents:
        for match, _ in index.team(opponent, "", cutoff, oldest):
            selected.setdefault(match.id, match)
    matches = sorted(selected.values(), key=lambda m: m.kickoff)[-params.max_fit_matches :]
    youth = is_youth(fixture.league)
    rows = fit_rows(matches, fixture.kickoff, params, overrides, youth)
    return fit(rows, prior=params.prior)


def ratio(actual, expected, prior):
    return (actual + prior) / (expected + prior)


def form_factors(rows, key, ratings, params):
    """How the last N matches compare with what the ratings expected (1 = as expected)."""
    # rows[-0:] would be the whole history, not an empty window.
    recent = rows[-params.form_window :] if params.form_window > 0 else []
    scored = conceded = expected_for = expected_against = 0.0
    for match, side in recent:
        home = key if side == "home" else canonical(match.home)
        away = key if side == "away" else canonical(match.away)
        e_home, e_away = ratings.expected(home, away)
        if side == "home":
            scored, conceded = scored + match.home_goals, conceded + match.away_goals
            expected_for, expected_against = expected_for + e_home, expected_against + e_away
        else:
            scored, conceded = scored + match.away_goals, conceded + match.home_goals
            expected_for, expected_against = expected_for + e_away, expected_against + e_home
    return (
        ratio(scored, expected_for, params.form_prior),
        ratio(conceded, expected_against, params.form_prior),
    )


def h2h_factors(fixture, home_rows, ratings, params):
    away = canonical(fixture.away)
    home_key = canonical(fixture.home)
    scored = conceded = expected_for = expected_against = 0.0
    count = 0
    for match, side in reversed(home_rows):
        if not is_opponent(match, side, fixture):
            continue
        home = home_key if side == "home" else away
        guest = away if side == "home" else home_key
        e_home, e_away = ratings.expected(home, guest)
        goals_for = match.home_goals if side == "home" else match.away_goals
        goals_against = match.away_goals if side == "home" else match.home_goals
        scored += goals_for
        conceded += goals_against
        expected_for += e_home if side == "home" else e_away
        expected_against += e_away if side == "home" else e_home
        count += 1
        if count == 6:
            break
    if not count:
        return 1.0, 1.0, 0
    return (
        ratio(scored, expected_for, params.h2h_prior),
        ratio(conceded, expected_against, params.h2h_prior),
        count,
    )


def market_probabilities(odds):
    values = [odds.get(k) for k in mk.RESULTS]
    # Odds of 1.0 or less (or NaN/inf) are not prices: 1/odds would be >= 1 or undefined.
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 1 for v in values):
        return None
    inverse = {k: 1 / odds[k] for k in mk.RESULTS}
    total = sum(inverse.values())
    # A book with a negative margin or absurd overround is not a usable reference.
    if not 0.98 <= total <= 1.4:
        return None
    return {k: v / total for k, v in inverse.items()}


def blend(model, market, weight):
    pooled = {k: model[k] ** (1 - weight) * market[k] ** weight for k in mk.RESULTS}
    total = sum(pooled.values())
    return {k: v / total for k, v in pooled.items()}


def goals_calibration(odds, matrix, params):
    """Target P(over 2.5) for the matrix: Platt map, then the pool with the market price.

    Returns the components dict. With the identity map and no usable over/under price the
    target equals the matrix and `analyze` leaves the matrix untouched (scale 1).
    """
    raw = mk.over_probability(matrix, mk.TOTAL_LINE)
    calibrated = mk.platt(raw, params.totals_intercept, params.totals_slope)
    market = mk.two_way_probability(odds, "over25", "under25")
    weight = params.totals_market_weight if market is not None else 0.0
    target = mk.pool_binary(calibrated, market, weight)
    return {
        "model_over25": raw,
        "calibrated_over25": calibrated,
        "market_over25": market,
        "market_weight": weight,
        "over25": target,
        "scale": 1.0,
    }


def team_score(rows, kickoff, half_life):
    if not rows:
        return 0.0
    effective = sum(decay((kickoff - m.kickoff).days, half_life) for m, _ in rows)
    age = (kickoff - rows[-1][0].kickoff).days
    recency = 1.0 if age <= 45 else 0.75 if age <= 120 else 0.45 if age <= 365 else 0.15
    return min(1.0, effective / 8) * recency


def grade_of(confidence):
    return (
        "A" if confidence >= 75 else "B" if confidence >= 55 else "C" if confidence >= 35 else "D"
    )


def analyze(fixture, history, threshold=0.85, *, params=PARAMS, ratings=None):
    """Full pre-match analysis. `history` is a list of matches or a HistoryIndex."""
    index = history if isinstance(history, HistoryIndex) else HistoryIndex(history)
    cutoff = fixture.kickoff - timedelta(hours=params.cutoff_hours)
    oldest = fixture.kickoff - timedelta(days=params.max_days)
    h2h_oldest = fixture.kickoff - timedelta(days=max(params.max_days, H2H_DISPLAY_DAYS))
    # Senior and youth football are different populations: never mix them.
    youth = is_youth(fixture.league)
    home_long = [
        (m, s)
        for m, s in index.team(
            fixture.home, fixture.home_id, cutoff, h2h_oldest, exclude=fixture.id
        )
        if is_youth(m.league) == youth
    ]
    home_rows = [(m, s) for m, s in home_long if m.kickoff >= oldest]
    away_rows = index.team(fixture.away, fixture.away_id, cutoff, oldest, exclude=fixture.id)
    away_rows = [(m, s) for m, s in away_rows if is_youth(m.league) == youth]
    if ratings is None:
        ratings = local_ratings(fixture, index, home_rows, away_rows, cutoff, oldest, params)
    home_key, away_key = canonical(fixture.home), canonical(fixture.away)
    base_home, base_away = ratings.expected(home_key, away_key)

    home_attack, home_defence = form_factors(home_rows, home_key, ratings, params)
    away_attack, away_defence = form_factors(away_rows, away_key, ratings, params)
    h2h_for, h2h_against, h2h_count = h2h_factors(fixture, home_rows, ratings, params)
    phi, psi = params.form_weight, params.h2h_weight
    home_rate = base_home * (home_attack * away_defence) ** phi * (h2h_for**psi)
    away_rate = base_away * (away_attack * home_defence) ** phi * (h2h_against**psi)
    home_rate = min(5.0, max(0.15, home_rate))
    away_rate = min(5.0, max(0.15, away_rate))

    matrix = mk.score_matrix(home_rate, away_rate, params.rho)
    model_1x2 = mk.one_x_two(matrix)
    market = market_probabilities(fixture.odds)
    market_weight = params.market_weight if market else 0.0
    final_1x2 = blend(model_1x2, market, market_weight) if market_weight else model_1x2
    if market_weight:
        matrix = mk.reweight(matrix, final_1x2)
    totals = goals_calibration(fixture.odds, matrix, params)
    if abs(totals["over25"] - totals["model_over25"]) > 1e-12:
        # One factor on both rates: the matrix agrees with the calibrated P(over 2.5) and the
        # 1X2 reweight keeps final_1x2 exactly (docs/MODEL.md).
        totals["scale"], matrix = mk.fit_total(
            home_rate, away_rate, params.rho, final_1x2, totals["over25"]
        )
        home_rate, away_rate = home_rate * totals["scale"], away_rate * totals["scale"]
    ft = mk.full_time(matrix)
    ht, htft = mk.half_time(home_rate, away_rate, final_1x2)

    score_home = team_score(home_rows, fixture.kickoff, params.half_life)
    score_away = team_score(away_rows, fixture.kickoff, params.half_life)
    base = 0.6 * min(score_home, score_away) + 0.4 * (score_home + score_away) / 2
    confidence = round(100 * (0.8 * base + 0.2 * (market is not None)))
    grade = grade_of(confidence)
    sufficient = grade != "D"

    markets = football_markets(fixture.odds, matrix, ft, ht, htft)
    by_key = {m["key"]: m for m in markets}
    # The prospective ledger only ever picks from the original, comparable market set; the
    # extended markets are bettable legs (tickets, recommendations) but never the selection.
    candidates = sorted(
        (m for m in markets if m["selectable"] and m["key"] in mk.SELECTABLE),
        key=lambda m: m["probability"],
        reverse=True,
    )
    selection = candidates[0] if sufficient and candidates[0]["probability"] >= threshold else None
    if not sufficient:
        reason = (
            "Date puține sau vechi: predicția există, dar nu intră în selecții. "
            "Rulează analiza completă FlashScore pentru formă și H2H."
        )
    elif selection is None:
        reason = f"Nicio piață nu depășește pragul de {threshold:.0%}."
    else:
        reason = "Selecție statistică; probabilitățile nu sunt garanții."

    home_form = team_form(home_rows, fixture.kickoff)
    away_form = team_form(away_rows, fixture.kickoff)
    # Still strictly before kickoff - cutoff_hours; only the display window is longer.
    h2h = head_to_head(home_long, fixture, max(params.max_days, H2H_DISPLAY_DAYS))
    top_scores = mk.correct_scores(matrix, 10)
    analysis = {
        "version": VERSION,
        "threshold": threshold,
        "calibrated": False,
        "sport": "football",
        "expected_goals": {"home": home_rate, "away": away_rate},
        # Common multi-sport shape (footypreds.sports.validate_analysis).
        "expected": {"home": home_rate, "away": away_rate},
        "components": {
            "ratings": {"home": base_home, "away": base_away},
            "form": {
                "home_attack": home_attack,
                "home_defence": home_defence,
                "away_attack": away_attack,
                "away_defence": away_defence,
                "weight": phi,
            },
            "h2h": {"for": h2h_for, "against": h2h_against, "matches": h2h_count, "weight": psi},
            "model_1x2": model_1x2,
            "market_1x2": market,
            "market_weight": market_weight,
            "totals": totals,
            "fit_matches": ratings.matches,
        },
        "sample": {
            "home": len(home_rows),
            "away": len(away_rows),
            "league": ratings.matches,
            "h2h": h2h["played"],
        },
        "quality": "sufficient" if sufficient else "insufficient",
        "grade": grade,
        "confidence": confidence,
        "markets": markets,
        "selection": selection,
        "reason": reason,
        "scores": top_scores,
        "score_grid": mk.score_grid(matrix),
        "goal_distribution": mk.goal_distribution(matrix),
        "htft": sorted(
            ({"key": k, "label": mk.LABELS[k], "probability": p} for k, p in htft.items()),
            key=lambda item: item["probability"],
            reverse=True,
        ),
        "form": {"home": home_form, "away": away_form},
        "h2h": h2h,
    }
    analysis["tips"] = tips(by_key, top_scores, analysis["htft"])
    analysis["insights"] = insights(fixture, home_form, away_form, h2h)
    analysis["summary"] = summary(fixture, by_key, top_scores, grade)
    return analysis


def market_row(key, label, group, probability, odds, selectable):
    price = odds.get(key)
    return {
        "key": key,
        "label": label,
        "group": group,
        "probability": probability,
        "fair_odds": 1 / probability if probability > 0 else None,
        "odds": price,
        "ev": probability * price - 1 if price else None,
        "selectable": selectable,
    }


def football_markets(odds, matrix, ft, ht, htft):
    """Every football market from one score matrix, with the quoted price when there is one.

    - FT_MARKETS: the ledger set (mk.SELECTABLE) is always selectable; the other legacy keys
      (over 0.5, team goals, combos) are selectable only with a real price.
    - Half-time and HT/FT: display only (a final score cannot settle them).
    - Extended generic keys (DNB, Asian handicap, other totals, team totals, parity, correct
      score): selectable only with a real price; whole lines carry their push probability.
    """
    from footypreds.sports.settle import is_settleable

    markets = []
    for key, (label, group, _) in mk.FT_MARKETS.items():
        priced = bool(odds.get(key))
        selectable = key in mk.SELECTABLE or priced
        markets.append(market_row(key, label, group, ft[key], odds, selectable))
    for key, (label, group) in mk.HT_MARKETS.items():
        markets.append(market_row(key, label, group, ht[key], odds, False))
    for key, probability in htft.items():
        markets.append(market_row(key, mk.LABELS[key], mk.HTFT_GROUP, probability, odds, False))
    for key, label, group, probability, push in mk.extended(matrix, odds):
        selectable = bool(odds.get(key)) and is_settleable("football", key)
        row = market_row(key, label, group, probability, odds, selectable)
        if push > 1e-12:
            row["push"] = push
        markets.append(row)
    return markets


def tips(by_key, top_scores, htft):
    def best(*keys):
        return max((by_key[k] for k in keys), key=lambda m: m["probability"])

    output = [
        {"category": "Rezultat final", **pick(best("1", "X", "2"))},
        {"category": "Șansă dublă", **pick(best("1X", "X2", "12"))},
        {"category": "Goluri", **pick(best("over25", "under25"))},
        {"category": "Ambele marchează", **pick(best("btts", "no_btts"))},
        {
            "category": "Scor corect",
            "key": "cs",
            "label": top_scores[0]["score"],
            "probability": top_scores[0]["probability"],
        },
        {
            "category": "Pauză/Final",
            "key": htft[0]["key"],
            "label": htft[0]["label"],
            "probability": htft[0]["probability"],
        },
    ]
    value = [
        m
        for m in by_key.values()
        if m["ev"] is not None and m["ev"] > 0.02 and m["probability"] >= 0.25
    ]
    if value:
        top = max(value, key=lambda m: m["ev"])
        output.append({"category": "Valoare", **pick(top), "odds": top["odds"], "ev": top["ev"]})
    return output


def pick(market):
    return {"key": market["key"], "label": market["label"], "probability": market["probability"]}


def insights(fixture, home_form, away_form, h2h):
    notes = []
    for name, form in ((fixture.home, home_form), (fixture.away, away_form)):
        last10, streak = form["last10"], form["streaks"]
        if not last10:
            notes.append(f"{name}: niciun rezultat recent disponibil.")
            continue
        n = last10["played"]
        if streak["unbeaten"] >= 5:
            notes.append(f"{name} este neînvinsă în ultimele {streak['unbeaten']} meciuri.")
        if streak["winless"] >= 5:
            notes.append(f"{name} nu a mai câștigat de {streak['winless']} meciuri.")
        if streak["wins"] >= 3:
            notes.append(f"{name} are {streak['wins']} victorii consecutive.")
        if last10["over25"] >= 0.7:
            notes.append(
                f"Peste 2.5 goluri în {round(last10['over25'] * n)}/{n} meciuri ale echipei {name}."
            )
        if last10["over25"] <= 0.3:
            under = round((1 - last10["over25"]) * n)
            notes.append(f"Sub 2.5 goluri în {under}/{n} meciuri ale echipei {name}.")
        if last10["btts"] >= 0.7:
            notes.append(
                f"Ambele au marcat în {round(last10['btts'] * n)}/{n} meciuri ale echipei {name}."
            )
        if last10["failed_to_score"] >= 0.4:
            notes.append(
                f"{name} nu a marcat în {round(last10['failed_to_score'] * n)}/{n} meciuri."
            )
        if last10["clean_sheets"] >= 0.5:
            notes.append(
                f"{name} nu a primit gol în {round(last10['clean_sheets'] * n)}/{n} meciuri."
            )
        days = form["days_since_last"]
        if days is not None and days > 60:
            notes.append(f"{name}: ultimul rezultat cunoscut este de acum {days} zile.")
        if form["matches_last_30_days"] >= 7:
            notes.append(
                f"{name} a jucat {form['matches_last_30_days']} meciuri în ultimele 30 de zile."
            )
    if h2h["played"]:
        notes.append(
            f"Meciuri directe: {h2h['home_wins']}-{h2h['draws']}-{h2h['away_wins']} "
            f"(gazde-egal-oaspeți), {h2h['goals_avg']:.1f} goluri/meci."
        )
    return notes


def summary(fixture, by_key, top_scores, grade):
    results = {"1": fixture.home, "X": "egalul", "2": fixture.away}
    favourite = max(("1", "X", "2"), key=lambda k: by_key[k]["probability"])
    p = by_key[favourite]["probability"]
    lead = (
        f"Modelul favorizează {results[favourite]} ({p:.0%})"
        if favourite != "X"
        else f"Modelul vede egalul drept cel mai probabil rezultat ({p:.0%})"
    )
    text = (
        f"{lead}. Cel mai probabil scor: {top_scores[0]['score']}. "
        f"Peste 2.5 goluri: {by_key['over25']['probability']:.0%}; "
        f"ambele marchează: {by_key['btts']['probability']:.0%}."
    )
    if p < 0.5:
        text += " Niciun rezultat nu depășește 50%: meci deschis."
    if grade == "D":
        text += " Atenție: date insuficiente sau vechi, încrederea este scăzută."
    return text


def params_dict(params=PARAMS):
    return asdict(params)
