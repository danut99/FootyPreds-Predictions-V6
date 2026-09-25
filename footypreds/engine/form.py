"""Descriptive form statistics: last 5/10 matches, home/away splits, streaks and H2H."""

from footypreds.engine.history import canonical


def perspective(match, side):
    scored = match.home_goals if side == "home" else match.away_goals
    conceded = match.away_goals if side == "home" else match.home_goals
    result = "W" if scored > conceded else ("L" if scored < conceded else "D")
    return scored, conceded, result


def window(rows):
    """rows: newest first, list of (match, side)."""
    if not rows:
        return None
    stats = [perspective(m, side) for m, side in rows]
    played = len(stats)
    wins = sum(r == "W" for _, _, r in stats)
    draws = sum(r == "D" for _, _, r in stats)
    scored = sum(s for s, _, _ in stats)
    conceded = sum(c for _, c, _ in stats)
    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": played - wins - draws,
        "points_per_game": (3 * wins + draws) / played,
        "scored": scored,
        "conceded": conceded,
        "scored_avg": scored / played,
        "conceded_avg": conceded / played,
        "over15": sum(s + c > 1 for s, c, _ in stats) / played,
        "over25": sum(s + c > 2 for s, c, _ in stats) / played,
        "btts": sum(s > 0 and c > 0 for s, c, _ in stats) / played,
        "clean_sheets": sum(c == 0 for _, c, _ in stats) / played,
        "failed_to_score": sum(s == 0 for s, _, _ in stats) / played,
    }


def streaks(rows):
    def run(predicate):
        count = 0
        for match, side in rows:
            if not predicate(*perspective(match, side)):
                break
            count += 1
        return count

    return {
        "wins": run(lambda s, c, r: r == "W"),
        "unbeaten": run(lambda s, c, r: r != "L"),
        "losses": run(lambda s, c, r: r == "L"),
        "winless": run(lambda s, c, r: r != "W"),
        "scoring": run(lambda s, c, r: s > 0),
        "clean_sheets": run(lambda s, c, r: c == 0),
    }


def team_form(rows, kickoff):
    """rows: (match, side) oldest first; returns a JSON-ready summary for display."""
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
    days = (kickoff - recent[0][0].kickoff).days if recent else None
    return {
        "sequence": "".join(item["result"] for item in last[:5]),
        "last": last,
        "last5": window(recent[:5]),
        "last10": window(recent[:10]),
        "home10": window([r for r in recent if r[1] == "home"][:10]),
        "away10": window([r for r in recent if r[1] == "away"][:10]),
        "streaks": streaks(recent),
        "days_since_last": days,
        "matches_last_30_days": sum((kickoff - m.kickoff).days <= 30 for m, _ in recent),
        "available": len(rows),
    }


def is_opponent(match, side, fixture):
    """True when the other side of `match` is the fixture's away team.

    A known, different team ID means a namesake (another country or division), exactly as in
    HistoryIndex.side_of; rows without IDs (e.g. FlashScore H2H) are matched by name.
    """
    opponent = match.away if side == "home" else match.home
    opponent_id = match.away_id if side == "home" else match.home_id
    if fixture.away_id and opponent_id:
        return opponent_id == fixture.away_id
    return canonical(opponent) == canonical(fixture.away)


def head_to_head(home_rows, fixture, window_days=None):
    """Mutual matches, from the perspective of the current home team (newest first)."""
    mutual = [
        (match, side) for match, side in reversed(home_rows) if is_opponent(match, side, fixture)
    ]
    if not mutual:
        return {"played": 0, "matches": [], "window_days": window_days}
    summary = window(mutual)
    return {
        "played": len(mutual),
        "home_wins": summary["wins"],
        "draws": summary["draws"],
        "away_wins": summary["losses"],
        "goals_avg": (summary["scored"] + summary["conceded"]) / len(mutual),
        "over25": summary["over25"],
        "btts": summary["btts"],
        "window_days": window_days,
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
