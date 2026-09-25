"""Market keys shared by every sport: formatting, parsing and Romanian labels.

Generic families (see docs/CONTRACTS.md):
    "1" / "X" / "2"                   result; basketball and tennis "1"/"2" = winner incl. OT
    "over_{line}" / "under_{line}"    total goals / points / sets
    "home_over_{line}" ...            team totals ("home"|"away", "over"|"under")
    "ah_1_{signed}" / "ah_2_{signed}" handicap added to that side ("ah_1_-1.5", "ah_2_+4.5")
    "dnb_1" / "dnb_2"                 draw no bet
    "odd" / "even"                    parity of the total
    "cs_{h}-{a}"                      exact final score (football)
    "sets_{h}-{a}"                    exact set score (tennis)
    "games_over_{line}" ...           tennis total games: priced, never settleable from sets
Football keeps its legacy keys (engine.markets.FT_MARKETS); FOOTBALL_ALIASES maps the
generic spelling of the same bet onto them.
"""

import re

NUMBER = r"\d+(?:\.\d+)?"
PATTERNS = (
    ("result", re.compile(r"^(1|X|2)$")),
    ("total", re.compile(rf"^(over|under)_({NUMBER})$")),
    ("team_total", re.compile(rf"^(home|away)_(over|under)_({NUMBER})$")),
    ("handicap", re.compile(rf"^ah_(1|2)_([+-]?{NUMBER})$")),
    ("dnb", re.compile(r"^dnb_(1|2)$")),
    ("parity", re.compile(r"^(odd|even)$")),
    ("exact", re.compile(r"^(cs|sets)_(\d{1,3})-(\d{1,3})$")),
    ("games_total", re.compile(rf"^games_(over|under)_({NUMBER})$")),
)


def fmt_line(value):
    """2.5 -> "2.5", 180.0 -> "180", 0 -> "0" (no sign)."""
    value = float(value)
    text = f"{abs(value):.2f}".rstrip("0").rstrip(".")
    return text if value >= 0 or text == "0" else "-" + text


def fmt_signed(value):
    """Handicap line with an explicit sign: +1.5, -4.5; zero is "0"."""
    text = fmt_line(value)
    return text if text == "0" or text.startswith("-") else "+" + text


def over(line):
    return f"over_{fmt_line(line)}"


def under(line):
    return f"under_{fmt_line(line)}"


def handicap(side, line):
    """side "1" (home) or "2" (away); `line` is added to that side's score."""
    return f"ah_{side}_{fmt_signed(line)}"


def is_half(line):
    """x.5 lines never push."""
    return (float(line) * 2) % 2 == 1


def is_whole_or_half(line):
    """Quarter lines (x.25 / x.75) are split bets: not supported."""
    return float(line) * 2 == int(float(line) * 2)


def parse(key):
    """(family, groups) of a generic key, or None."""
    for family, pattern in PATTERNS:
        found = pattern.match(key)
        if found:
            return family, found.groups()
    return None


# Generic spelling -> legacy football key (same bet, same settlement).
FOOTBALL_ALIASES = {}
for _tenths in (5, 15, 25, 35, 45):
    _line = fmt_line(_tenths / 10)
    FOOTBALL_ALIASES[f"over_{_line}"] = f"over{_tenths:02d}"
    FOOTBALL_ALIASES[f"under_{_line}"] = f"under{_tenths:02d}"
for _side in ("home", "away"):
    for _tenths in (5, 15):
        FOOTBALL_ALIASES[f"{_side}_over_{fmt_line(_tenths / 10)}"] = f"{_side}_over{_tenths:02d}"

UNIT = {
    "football": ("gol", "goluri"),
    "basketball": ("punct", "puncte"),
    "tennis": ("set", "seturi"),
}
SIDE = {
    "football": ("gazde", "oaspeți"),
    "basketball": ("gazde", "oaspeți"),
    "tennis": ("jucătorul 1", "jucătorul 2"),
}


def label(sport, key):
    """Romanian label of a market key for `sport` (the key itself when unknown)."""
    if sport == "football":
        from footypreds.engine.markets import LABELS

        if key in LABELS:
            return LABELS[key]
    spec = parse(key)
    if spec is None:
        return key
    family, groups = spec
    units = UNIT.get(sport, UNIT["football"])[1]
    home, away = SIDE.get(sport, SIDE["football"])
    if family == "result":
        return {"1": f"Victorie {home}", "X": "Egal", "2": f"Victorie {away}"}[groups[0]]
    if family == "total":
        word = "Peste" if groups[0] == "over" else "Sub"
        return f"{word} {groups[1]} {units}"
    if family == "team_total":
        who = home if groups[0] == "home" else away
        word = "peste" if groups[1] == "over" else "sub"
        return f"{who.capitalize()} {word} {groups[2]} {units}"
    if family == "handicap":
        who = home if groups[0] == "1" else away
        return f"Handicap {who} {groups[1]}"
    if family == "dnb":
        who = home if groups[0] == "1" else away
        return f"{who.capitalize()} (egal = anulat)"
    if family == "parity":
        return "Total impar" if groups[0] == "odd" else "Total par"
    if family == "exact":
        prefix = "Scor la seturi" if groups[0] == "sets" else "Scor corect"
        return f"{prefix} {groups[1]}-{groups[2]}"
    word = "Peste" if groups[0] == "over" else "Sub"
    return f"{word} {groups[1]} game-uri"


# --- bookmaker margin of one market ---------------------------------------------------------

# Overrounds outside this band are not a usable reference (broken or one-sided books).
MARGIN_RANGE = (1.0, 1.3)
_LEGACY = {legacy: generic for generic, legacy in FOOTBALL_ALIASES.items()}


def complements(sport, key):
    """All outcomes of the market `key` belongs to (they add up to one bet), or None."""
    key = _LEGACY.get(key, key)
    if key in ("btts", "no_btts"):
        return ("btts", "no_btts")
    parsed = parse(key)
    if parsed is None:
        return None
    family, groups = parsed
    if family == "result":
        return ("1", "X", "2") if sport == "football" else ("1", "2")
    if family == "total":
        return (over(groups[1]), under(groups[1]))
    if family == "team_total":
        side, _, line = groups
        return (f"{side}_over_{fmt_line(line)}", f"{side}_under_{fmt_line(line)}")
    if family == "handicap":
        side, line = groups
        other = "2" if side == "1" else "1"
        return (handicap(side, float(line)), handicap(other, -float(line)))
    if family == "parity":
        return ("odd", "even")
    if family == "dnb":
        return ("dnb_1", "dnb_2")
    return None


def market_margin(sport, key, prices):
    """Overround (sum of 1/price) of the fully priced market of `key`, else None.

    `prices` maps market keys to decimal odds (Match.odds); football legacy spellings such
    as over25 are accepted for their generic keys and vice versa.
    """
    group = complements(sport, key)
    if not group:
        return None
    values = []
    for name in group:
        price = prices.get(name) or prices.get(FOOTBALL_ALIASES.get(name, ""))
        if not price or price <= 1:
            return None
        values.append(price)
    margin = sum(1 / price for price in values)
    return margin if MARGIN_RANGE[0] <= margin <= MARGIN_RANGE[1] else None
