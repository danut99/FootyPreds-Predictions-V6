"""Helpers shared by the basketball and tennis analyzers (form, H2H, markets, grading)."""

import math
from datetime import timedelta
from statistics import NormalDist

from footypreds.engine.analyzer import grade_of, team_score
from footypreds.engine.form import is_opponent
from footypreds.engine.history import HistoryIndex
from footypreds.sports.keys import label

CUTOFF_HOURS = 3.0
NORMAL = NormalDist()


def as_index(history, sport):
    if isinstance(history, HistoryIndex):
        return history
    return HistoryIndex(m for m in history if m.sport == sport)


def team_rows(index, fixture, max_days):
    """Both sides' finished games strictly before kickoff - 3h (anti-leakage), oldest first."""
    cutoff = fixture.kickoff - timedelta(hours=CUTOFF_HOURS)
    oldest = fixture.kickoff - timedelta(days=max_days)
    home = index.team(fixture.home, fixture.home_id, cutoff, oldest, exclude=fixture.id)
    away = index.team(fixture.away, fixture.away_id, cutoff, oldest, exclude=fixture.id)
    return home, away


def perspective(match, side):
    scored = match.home_goals if side == "home" else match.away_goals
    conceded = match.away_goals if side == "home" else match.home_goals
    result = "W" if scored > conceded else ("L" if scored < conceded else "D")
    return scored, conceded, result


def window(rows):
    """rows newest first: (match, side)."""
    if not rows:
        return None
    stats = [perspective(m, side) for m, side in rows]
    played = len(stats)
    wins = sum(r == "W" for _, _, r in stats)
    draws = sum(r == "D" for _, _, r in stats)
    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": played - wins - draws,
        "win_rate": wins / played,
        "scored_avg": sum(s for s, _, _ in stats) / played,
        "conceded_avg": sum(c for _, c, _ in stats) / played,
    }


def team_form(rows, kickoff):
    """rows oldest first; JSON-ready form summary in the common analysis shape."""
    recent = list(reversed(rows))
    last = [
        {
            "id": match.id,
            "date": match.kickoff.date().isoformat(),
            "competition": match.league.split(":", 1)[-1].strip(),
            "venue": "A" if side == "home" else "D",
            "opponent": match.away if side == "home" else match.home,
            "score": f"{match.home_goals}-{match.away_goals}",
            "result": perspective(match, side)[2],
        }
        for match, side in recent[:10]
    ]
    return {
        "sequence": "".join(item["result"] for item in last[:5]),
        "last": last,
        "last5": window(recent[:5]),
        "last10": window(recent[:10]),
        "days_since_last": (kickoff - recent[0][0].kickoff).days if recent else None,
        "available": len(rows),
    }


def head_to_head(home_rows, fixture):
    mutual = [(m, s) for m, s in reversed(home_rows) if is_opponent(m, s, fixture)]
    stats = window(mutual)
    return {
        "played": len(mutual),
        "home_wins": stats["wins"] if stats else 0,
        "draws": stats["draws"] if stats else 0,
        "away_wins": stats["losses"] if stats else 0,
        "matches": [
            {
                "id": m.id,
                "date": m.kickoff.date().isoformat(),
                "competition": m.league.split(":", 1)[-1].strip(),
                "home": m.home,
                "away": m.away,
                "score": f"{m.home_goals}-{m.away_goals}",
            }
            for m, _ in mutual[:8]
        ],
    }


def two_way(odds, first, second):
    """Margin-free probabilities of a two-way price pair, or None."""
    a, b = odds.get(first), odds.get(second)
    if not (a and b and a > 1 and b > 1):
        return None
    total = 1 / a + 1 / b
    if not 0.98 <= total <= 1.4:
        return None
    return (1 / a) / total, (1 / b) / total


def market(sport, key, group, probability, odds, selectable=True):
    probability = min(1.0, max(0.0, probability))
    price = odds.get(key)
    return {
        "key": key,
        "label": label(sport, key),
        "group": group,
        "probability": probability,
        "fair_odds": 1 / probability if probability > 0 else None,
        "odds": price,
        "ev": probability * price - 1 if price else None,
        "selectable": selectable,
    }


def confidence_of(home_rows, away_rows, kickoff, has_market, half_life=365.0):
    score_home = team_score(home_rows, kickoff, half_life)
    score_away = team_score(away_rows, kickoff, half_life)
    base = 0.6 * min(score_home, score_away) + 0.4 * (score_home + score_away) / 2
    confidence = round(100 * (0.8 * base + 0.2 * has_market))
    return confidence, grade_of(confidence)


def choose(markets, threshold, sufficient):
    """(selection, reason) with the same rules as football."""
    candidates = sorted(
        (m for m in markets if m["selectable"]), key=lambda m: m["probability"], reverse=True
    )
    best = candidates[0] if candidates else None
    selection = best if sufficient and best and best["probability"] >= threshold else None
    if not sufficient:
        reason = "Date puține sau vechi: predicția există, dar nu intră în selecții."
    elif selection is None:
        reason = f"Nicio piață nu depășește pragul de {threshold:.0%}."
    else:
        reason = "Selecție statistică; probabilitățile nu sunt garanții."
    return selection, reason


def pick(category, item):
    return {
        "category": category,
        "key": item["key"],
        "label": item["label"],
        "probability": item["probability"],
    }


def value_tip(markets):
    value = [
        m for m in markets if m["ev"] is not None and m["ev"] > 0.02 and m["probability"] >= 0.25
    ]
    if not value:
        return None
    top = max(value, key=lambda m: m["ev"])
    return {**pick("Valoare", top), "odds": top["odds"], "ev": top["ev"]}


def logit(p):
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def logistic(x):
    return 1 / (1 + math.exp(-x))
