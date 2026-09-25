"""FlashScore matches/odds payload -> {market_key: {"best", "avg", "books"}} for every sport.

Payload: a list of bookmakers {name, odds: [{bettingType, bettingScope, odds: [row]}]}, each
row {eventParticipantId, value, active, handicap: {value}|null, selection: OVER|UNDER|ODD|
EVEN|null, winner: "1/1"|null, score: "3:1"|null, bothTeamsToScore: bool|null}.

Only prices whose side and meaning are unambiguous are kept:
- a participant row needs the match's home/away eventParticipantId (or an unambiguous
  inference from the payload: exactly two participants, home listed first in the winner group);
- handicap rows are checked per bookmaker: prices must fall as the handicap grows and the two
  sides of one line must form a plausible two-way book, otherwise that bookmaker's handicap
  prices are dropped;
- tennis FULL_TIME ASIAN_HANDICAP mixes set and game handicaps with the same lines; only the
  +/-1.5 set handicap of a best-of-3 match is kept, and only when both readings are quoted
  (a set handicap is provably the lower +1.5 / higher -1.5 price there);
- a key quoted twice by the same bookmaker is ambiguous and dropped for that bookmaker.
"""

from collections import defaultdict

from footypreds.sports.keys import (
    FOOTBALL_ALIASES,
    fmt_line,
    handicap,
    is_whole_or_half,
    over,
    under,
)

WINNER_TYPES = ("HOME_DRAW_AWAY", "HOME_AWAY")
FULL_SCOPE = {"football": "FULL_TIME", "basketball": "FULL_TIME_OVER_TIME", "tennis": "FULL_TIME"}


def price_of(row):
    value = raw_value(row) if row.get("active") is not False else None
    return value if value is not None and 1 < value < 1001 else None


def raw_value(row):
    try:
        return float(row.get("value"))
    except (TypeError, ValueError):
        return None


def line_of(row):
    spec = row.get("handicap")
    if not isinstance(spec, dict):
        return None
    try:
        line = float(spec.get("value"))
    except (TypeError, ValueError):
        return None
    return line if is_whole_or_half(line) else None


def groups_of(payload):
    """(book name, bettingType, bettingScope, rows) for every well-formed group."""
    for number, book in enumerate(payload if isinstance(payload, list) else []):
        if not isinstance(book, dict) or not isinstance(book.get("odds"), list):
            continue
        name = str(book.get("name") or f"book{number}")
        for group in book["odds"]:
            if isinstance(group, dict) and isinstance(group.get("odds"), list):
                rows = [row for row in group["odds"] if isinstance(row, dict)]
                yield name, group.get("bettingType"), group.get("bettingScope"), rows


def participants(payload, home_pid="", away_pid=""):
    """{participant id: "1"|"2"} or {} when the sides cannot be told apart."""
    seen = []
    first = None
    for _, kind, _, rows in groups_of(payload):
        for row in rows:
            pid = row.get("eventParticipantId")
            if pid and pid not in seen:
                seen.append(pid)
            if pid and first is None and kind in WINNER_TYPES:
                first = pid
    if home_pid and away_pid and home_pid != away_pid:
        return {home_pid: "1", away_pid: "2"} if home_pid in seen or away_pid in seen else {}
    if len(seen) != 2 or first is None:
        return {}
    other = seen[1] if seen[0] == first else seen[0]
    return {first: "1", other: "2"}


def football_key(key):
    return FOOTBALL_ALIASES.get(key, key)


def simple_rows(sport, kind, scope, rows, side):
    """(key, price) of every row outside the handicap families."""
    full = scope == FULL_SCOPE[sport]
    for row in rows:
        price = price_of(row)
        if price is None:
            continue
        pid = row.get("eventParticipantId")
        who = side.get(pid) if pid else None
        key = None
        if kind in WINNER_TYPES and full:
            if sport == "football" and kind == "HOME_DRAW_AWAY":
                key = who if pid else "X"
            elif sport != "football" and kind == "HOME_AWAY":
                key = who
        elif sport == "football" and kind == "HOME_DRAW_AWAY" and scope == "FIRST_HALF":
            key = ("ht_" + who if who else None) if pid else "ht_X"
        elif kind == "OVER_UNDER" and row.get("selection") in ("OVER", "UNDER"):
            line = line_of(row)
            if line is None:
                continue
            is_over = row["selection"] == "OVER"
            if full and sport == "tennis":
                key = f"games_{'over' if is_over else 'under'}_{fmt_line(line)}"
            elif full:
                key = over(line) if is_over else under(line)
            elif sport == "football" and scope == "FIRST_HALF" and is_over and line in (0.5, 1.5):
                key = f"ht_over{int(line * 10):02d}"
        elif not full:
            continue
        elif kind == "DOUBLE_CHANCE" and sport == "football":
            key = {"1": "1X", "2": "X2"}.get(who) if pid else "12"
        elif kind == "DRAW_NO_BET" and who:
            key = f"dnb_{who}"
        elif kind == "BOTH_TEAMS_TO_SCORE" and sport == "football":
            flag = row.get("bothTeamsToScore")
            key = "btts" if flag is True else "no_btts" if flag is False else None
        elif kind == "ODD_OR_EVEN" and sport != "tennis":
            key = {"ODD": "odd", "EVEN": "even"}.get(row.get("selection"))
        elif kind == "CORRECT_SCORE" and sport in ("football", "tennis"):
            score = str(row.get("score") or "")
            h, _, a = score.partition(":")
            if h.isdigit() and a.isdigit():
                key = f"{'sets' if sport == 'tennis' else 'cs'}_{int(h)}-{int(a)}"
        elif kind == "HALF_FULL_TIME" and sport == "football":
            winner = str(row.get("winner") or "")
            if len(winner) == 3 and winner[1] == "/" and {winner[0], winner[2]} <= set("1X2"):
                key = winner
        if key:
            yield (football_key(key) if sport == "football" else key), price


def consistent_handicaps(entries):
    """entries: {(side, line): price}; False when the quotes contradict the side reading."""
    for side in ("1", "2"):
        ladder = sorted((line, price) for (s, line), price in entries.items() if s == side)
        # A bigger handicap is easier to win: its price may not be higher.
        if any(b[1] > a[1] + 1e-9 for a, b in zip(ladder, ladder[1:])):
            return False
    for (side, line), price in entries.items():
        other = entries.get(("2" if side == "1" else "1", -line))
        if other and not 0.98 <= 1 / price + 1 / other <= 1.25:
            return False
    return True


def handicap_rows(sport, rows, side, sets=3):
    """(key, price) of one bookmaker's full-time ASIAN_HANDICAP group."""
    if sport == "tennis":
        if sets != 3:
            return []
        quotes = defaultdict(list)
        for row in rows:
            who = side.get(row.get("eventParticipantId"))
            line = line_of(row)
            if who and line in (1.5, -1.5):
                quotes[who, line].append(row)
        entries = {}
        for (who, line), found in quotes.items():
            if len(found) != 2:
                continue
            values = [(raw_value(r), r) for r in found]
            if any(value is None for value, _ in values):
                continue
            # Set handicap: the likelier (+1.5) or less likely (-1.5) of the two readings.
            pick = min if line > 0 else max
            price = price_of(pick(values, key=lambda v: v[0])[1])
            if price:
                entries[who, line] = price
    else:
        entries, seen = {}, set()
        for row in rows:
            who = side.get(row.get("eventParticipantId"))
            line, price = line_of(row), price_of(row)
            if not who or line is None or price is None:
                continue
            if (who, line) in seen:
                entries.pop((who, line), None)
                continue
            seen.add((who, line))
            entries[who, line] = price
    if not consistent_handicaps(entries):
        return []
    return [(handicap(who, line), price) for (who, line), price in entries.items()]


def parse_odds(payload, sport, home_pid="", away_pid="", sets=3):
    side = participants(payload, home_pid, away_pid)
    books = defaultdict(lambda: defaultdict(list))
    for name, kind, scope, rows in groups_of(payload):
        if kind == "ASIAN_HANDICAP":
            if scope == FULL_SCOPE[sport] and side:
                pairs = handicap_rows(sport, rows, side, sets)
            else:
                pairs = []
        else:
            pairs = simple_rows(sport, kind, scope, rows, side)
        for key, price in pairs:
            books[name][key].append(price)
    prices = defaultdict(list)
    for quoted in books.values():
        for key, values in quoted.items():
            if len(values) == 1:
                prices[key].append(values[0])
    return {
        key: {"best": max(values), "avg": sum(values) / len(values), "books": len(values)}
        for key, values in sorted(prices.items())
    }


def best_prices(parsed):
    """{key: best price} for Match.odds."""
    return {key: value["best"] for key, value in parsed.items()}


def merge_odds(current, parsed):
    """Match.odds after an odds fetch: new prices win, but list-by-date 1/X/2 stay as they are."""
    fresh = best_prices(parsed)
    kept = {k: v for k, v in current.items() if k in ("1", "X", "2")}
    return {**current, **fresh, **kept}
