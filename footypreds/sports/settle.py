"""Universal settlement of a market key from a final score, for every sport.

settle() returns True (won), False (lost) or None: void, push, not final, or a market that a
final score cannot decide (half-time markets, tennis total games, quarter handicap lines).
"""

from footypreds.sports.keys import is_whole_or_half, parse

# A game that ended without a normal result: every bet on it is void.
VOID_STATUSES = {
    "retired",
    "walkover",
    "unavailable",
    "cancelled",
    "canceled",
    "postponed",
    "abandoned",
    "awarded",
}
# Final results. "aet"/"penalties" keep the stored final score, as before for football.
FINAL_STATUSES = {"finished", "aet", "penalties"}
SETTLEABLE_FAMILIES = {"result", "total", "team_total", "handicap", "dnb", "parity", "exact"}


def _compare(value, line):
    """Win above the line, lose below it, push (None) exactly on it."""
    if value > line:
        return True
    if value < line:
        return False
    return None


def settle(sport, key, home_score, away_score, status="finished"):
    status = (status or "").lower()
    if status in VOID_STATUSES or status not in FINAL_STATUSES:
        return None
    if home_score is None or away_score is None:
        return None
    h, a = int(home_score), int(away_score)
    if sport == "football":
        from footypreds.engine.markets import FT_MARKETS, outcome

        if key in FT_MARKETS:
            return outcome(key, h, a)
    spec = parse(key)
    if spec is None or spec[0] not in SETTLEABLE_FAMILIES:
        return None
    family, groups = spec
    if family == "result":
        return {"1": h > a, "X": h == a, "2": h < a}[groups[0]]
    if family == "total":
        line = float(groups[1])
        if not is_whole_or_half(line):
            return None
        won = _compare(h + a, line)
        return won if won is None or groups[0] == "over" else not won
    if family == "team_total":
        line = float(groups[2])
        if not is_whole_or_half(line):
            return None
        won = _compare(h if groups[0] == "home" else a, line)
        return won if won is None or groups[1] == "over" else not won
    if family == "handicap":
        line = float(groups[1])
        if not is_whole_or_half(line):
            return None
        own, other = (h, a) if groups[0] == "1" else (a, h)
        return _compare(own + line, other)
    if family == "dnb":
        if h == a:
            return None
        return (h > a) if groups[0] == "1" else (a > h)
    if family == "parity":
        return (h + a) % 2 == (1 if groups[0] == "odd" else 0)
    # exact: "cs_{h}-{a}" / "sets_{h}-{a}"
    return (h, a) == (int(groups[1]), int(groups[2]))


def is_settleable(sport, key):
    """True when a final score can decide `key` (possibly as a push)."""
    if sport == "football":
        from footypreds.engine.markets import FT_MARKETS

        if key in FT_MARKETS:
            return True
    spec = parse(key)
    if spec is None or spec[0] not in SETTLEABLE_FAMILIES:
        return False
    family, groups = spec
    line = {"total": 1, "team_total": 2, "handicap": 1}.get(family)
    return line is None or is_whole_or_half(groups[line])


def can_push(sport, key):
    """True when some final score refunds `key`: draw no bet and whole-number lines.

    Used to keep refundable bets off recommended tickets and simulated bets, for every sport
    (football marks them with a "push" probability, basketball whole lines do not).
    """
    if sport == "football":
        from footypreds.engine.markets import FT_MARKETS

        if key in FT_MARKETS:
            return False
    spec = parse(key)
    if spec is None:
        return False
    family, groups = spec
    if family == "dnb":
        return True
    line = {"total": 1, "team_total": 2, "handicap": 1}.get(family)
    return line is not None and float(groups[line]).is_integer()
