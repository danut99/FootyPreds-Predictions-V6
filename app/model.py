"""Transparent baseline: time-weighted Poisson with Bayesian rate shrinkage.

No fitted calibration is claimed. All markets come from ONE normalized score matrix.
"""

import math
import unicodedata
from datetime import timedelta

VERSION = "7.0-poisson-shrinkage"
LABELS = {
    "1": "Victorie gazde",
    "X": "Egal",
    "2": "Victorie oaspeți",
    "1X": "Gazde sau egal",
    "X2": "Egal sau oaspeți",
    "12": "Fără egal",
    "over15": "Peste 1.5 goluri",
    "under15": "Sub 1.5 goluri",
    "over25": "Peste 2.5 goluri",
    "under25": "Sub 2.5 goluri",
    "over35": "Peste 3.5 goluri",
    "under35": "Sub 3.5 goluri",
    "btts": "Ambele marchează",
    "no_btts": "Nu marchează ambele",
}


def canonical(value):
    value = value.split(":", 1)[-1].strip().casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def outcome(key, home, away):
    return {
        "1": home > away,
        "X": home == away,
        "2": home < away,
        "1X": home >= away,
        "X2": home <= away,
        "12": home != away,
        "over15": home + away > 1.5,
        "under15": home + away < 1.5,
        "over25": home + away > 2.5,
        "under25": home + away < 2.5,
        "over35": home + away > 3.5,
        "under35": home + away < 3.5,
        "btts": home > 0 and away > 0,
        "no_btts": home == 0 or away == 0,
    }[key]


def score_matrix(home_rate, away_rate):
    def pmf(rate):
        p = [math.exp(-rate)]
        for i in range(1, 17):
            p.append(p[-1] * rate / i)
        return p

    home, away = pmf(home_rate), pmf(away_rate)
    cells = [(h, a, hp * ap) for h, hp in enumerate(home) for a, ap in enumerate(away)]
    total = sum(p for _, _, p in cells)
    return [(h, a, p / total) for h, a, p in cells]


def predict(fixture, history, threshold=0.85):
    # A three-hour completion buffer also excludes simultaneous matches.
    cutoff = fixture.kickoff - timedelta(hours=3)
    oldest = fixture.kickoff - timedelta(days=730)
    past = [
        m
        for m in history
        if m.status == "finished"
        and m.id != fixture.id
        and oldest <= m.kickoff < cutoff
        and canonical(m.league) == canonical(fixture.league)
        and (not m.country or not fixture.country or m.country == fixture.country)
    ]
    unique = {m.id: m for m in past}
    past = sorted(unique.values(), key=lambda m: (m.kickoff, m.id))
    weights = {
        m.id: math.exp(-math.log(2) * (fixture.kickoff - m.kickoff).days / 180) for m in past
    }
    total_weight = sum(weights.values())
    # 30 pseudo-matches stabilize a small or biased league sample.
    base_home = (45 + sum(m.home_goals * weights[m.id] for m in past)) / (30 + total_weight)
    base_away = (36 + sum(m.away_goals * weights[m.id] for m in past)) / (30 + total_weight)

    def strength(name, team_id):
        observations = []
        for m in past:
            is_home = (
                m.home_id == team_id
                if team_id and m.home_id
                else canonical(m.home) == canonical(name)
            )
            is_away = (
                m.away_id == team_id
                if team_id and m.away_id
                else canonical(m.away) == canonical(name)
            )
            if is_home or is_away:
                scored = m.home_goals if is_home else m.away_goals
                conceded = m.away_goals if is_home else m.home_goals
                observations.append(
                    (
                        m,
                        scored / (base_home if is_home else base_away),
                        conceded / (base_away if is_home else base_home),
                    )
                )
        observations = observations[-30:]
        w = sum(weights[m.id] for m, _, _ in observations)
        attack = (6 + sum(s * weights[m.id] for m, s, _ in observations)) / (6 + w)
        defense = (6 + sum(c * weights[m.id] for m, _, c in observations)) / (6 + w)
        recent = bool(observations and (fixture.kickoff - observations[-1][0].kickoff).days <= 90)
        return attack, defense, len(observations), recent

    ha, hd, hn, hr = strength(fixture.home, fixture.home_id)
    aa, ad, an, ar = strength(fixture.away, fixture.away_id)
    lh = min(4.0, max(0.25, base_home * ha * ad))
    la = min(4.0, max(0.25, base_away * aa * hd))
    matrix = score_matrix(lh, la)
    markets = []
    for key, label in LABELS.items():
        probability = sum(p for h, a, p in matrix if outcome(key, h, a))
        odd = fixture.odds.get(key)
        markets.append(
            {
                "key": key,
                "label": label,
                "probability": probability,
                "fair_odds": 1 / probability,
                "odds": odd,
                "ev": probability * odd - 1 if odd else None,
            }
        )
    ranked = sorted(markets, key=lambda m: m["probability"], reverse=True)
    sufficient = hn >= 8 and an >= 8 and hr and ar
    selection = ranked[0] if sufficient and ranked[0]["probability"] >= threshold else None
    if not sufficient:
        reason = "Istoric insuficient: minimum 8 meciuri/echipă și un rezultat în ultimele 90 zile."
    elif selection is None:
        reason = f"Nicio piață nu depășește pragul de {threshold:.0%}."
    else:
        reason = "Selecție statistică; probabilitatea nu este încă validată prin calibrare."
    return {
        "version": VERSION,
        "threshold": threshold,
        "calibrated": False,
        "expected_goals": {"home": lh, "away": la},
        "sample": {"home": hn, "away": an, "league": len(past)},
        "quality": "sufficient" if sufficient else "insufficient",
        "markets": markets,
        "selection": selection,
        "reason": reason,
        "scores": [
            {"score": f"{h}-{a}", "probability": p}
            for h, a, p in sorted(matrix, key=lambda c: c[2], reverse=True)[:6]
        ],
    }


def wilson(wins, count):
    if not count:
        return None
    z, rate = 1.96, wins / count
    denominator = 1 + z * z / count
    middle = (rate + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count**2)) / denominator
    return [max(0, middle - margin), min(1, middle + margin)]


def summarize(rows, total):
    """One selection per fixture. Only settled rows enter accuracy and Brier."""
    settled = [r for r in rows if r["result"] is not None]
    count = len(settled)
    wins = sum(r["result"]["won"] for r in settled)
    interval = wilson(wins, count)
    bins = []
    for lower, upper in ((0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.000001)):
        group = [r for r in settled if lower <= r["prediction"]["selection"]["probability"] < upper]
        if group:
            bins.append(
                {
                    "range": f"{lower:.0%}–{min(upper, 1):.0%}",
                    "count": len(group),
                    "predicted": sum(r["prediction"]["selection"]["probability"] for r in group)
                    / len(group),
                    "actual": sum(r["result"]["won"] for r in group) / len(group),
                }
            )
    return {
        "total_matches": total,
        "selected": len(rows),
        "settled": count,
        "wins": wins,
        "pending": len(rows) - count,
        "coverage": len(rows) / total if total else 0,
        "accuracy": wins / count if count else None,
        "interval95": interval,
        "brier": sum(
            (r["prediction"]["selection"]["probability"] - r["result"]["won"]) ** 2 for r in settled
        )
        / count
        if count
        else None,
        "target_supported": bool(count >= 100 and interval and interval[0] >= 0.85),
        "calibration": bins,
    }


def backtest(matches, threshold=0.85):
    matches = sorted(
        {m.id: m for m in matches if m.status == "finished"}.values(),
        key=lambda m: (m.kickoff, m.id),
    )
    history, rows, sufficient = [], [], 0
    for match in matches:
        hidden = match.model_copy(
            update={"home_goals": None, "away_goals": None, "status": "scheduled", "odds": {}}
        )
        prediction = predict(hidden, history, threshold)
        sufficient += prediction["quality"] == "sufficient"
        pick = prediction["selection"]
        if pick:
            rows.append(
                {
                    "match": match.model_dump(mode="json"),
                    "prediction": prediction,
                    "result": {
                        "won": outcome(pick["key"], match.home_goals, match.away_goals),
                        "score": f"{match.home_goals}-{match.away_goals}",
                    },
                }
            )
        history.append(match)
    return {
        "metrics": {**summarize(rows, len(matches)), "sufficient_history": sufficient},
        "rows": rows[-100:],
        "threshold": threshold,
        "version": VERSION,
        "method": "walk-forward; minimum 3 ore între istoricul folosit și start",
        "warning": "Evaluare retrospectivă, nu dovadă prospectivă. Nu optimiza pragul pe test.",
    }
