"""Sports registry, analyzer dispatch and the common analysis contract (docs/CONTRACTS.md)."""

from footypreds.domain import SCORE_LIMITS

SPORTS = {
    "football": {"id": 1, "label": "Fotbal"},
    "basketball": {"id": 3, "label": "Baschet"},
    "tennis": {"id": 2, "label": "Tenis"},
}
SPORT_PATTERN = "^(" + "|".join(SPORTS) + ")$"
GRADES = ("A", "B", "C", "D")
ANALYSIS_KEYS = (
    "version",
    "sport",
    "threshold",
    "calibrated",
    "grade",
    "confidence",
    "quality",
    "expected",
    "markets",
    "selection",
    "reason",
    "tips",
    "summary",
    "insights",
    "form",
    "h2h",
    "sample",
    "components",
)
MARKET_KEYS = ("key", "label", "group", "probability", "fair_odds", "odds", "ev", "selectable")
FORM_ROW_KEYS = ("date", "competition", "venue", "opponent", "score", "result")

__all__ = [
    "SCORE_LIMITS",
    "SPORTS",
    "SPORT_PATTERN",
    "analyze_match",
    "main_markets",
    "sport_list",
    "validate_analysis",
]


def sport_list():
    """[{key, id, label}] in display order."""
    return [{"key": key, **value} for key, value in SPORTS.items()]


def analyze_match(fixture, history, threshold=0.85, **kw):
    """Analysis of `fixture` by its sport's analyzer. `history` is a list or HistoryIndex.

    A plain list is filtered to the fixture's sport: team names repeat across sports
    (Real Madrid plays football and basketball), so histories must never mix.
    """
    sport = fixture.sport
    if sport not in SPORTS:
        raise ValueError(f"Sport necunoscut: {sport}")
    if isinstance(history, (list, tuple)):
        history = [m for m in history if m.sport == sport]
    if sport == "football":
        from footypreds.engine import analyze

        return analyze(fixture, history, threshold, **kw)
    if sport == "basketball":
        from footypreds.sports.basketball import analyze
    else:
        from footypreds.sports.tennis import analyze
    return analyze(fixture, history, threshold, **kw)


def _closest_to_even(market):
    """Sort key: distance from 50%, ties within 1e-9 go to the over / home-handicap side.

    Over and under of one line are complements, so they tie mathematically; floating point
    can put either a hair closer to 0.5, hence the tolerance and the explicit preference.
    """
    distance = round(abs(market["probability"] - 0.5), 9)
    return distance, not market["key"].startswith(("over_", "ah_1_")), market["key"]


TIP_MIN_ODDS = 1.08


def headline_tip(analysis):
    """The one tip a board card, export row or Excel row shows for an analysis.

    - football: the likeliest market of the ledger set (engine.markets.SELECTABLE), exactly
      as before extended markets existed, so a priced "Handicap gazde +2.5" at 99% and odds
      1.02 never becomes the headline;
    - basketball / tennis: the likeliest selectable market with a real price of at least
      TIP_MIN_ODDS that cannot be refunded; without such a price, the likeliest selectable
      headline market (main_markets), so a far-away unpriced line is never the tip.
    Falls back to any selectable market, then to any market.
    """
    from footypreds.sports.settle import can_push

    markets = analysis["markets"]
    sport = analysis.get("sport", "football")
    selectable = [m for m in markets if m["selectable"]]
    if sport == "football":
        from footypreds.engine.markets import SELECTABLE

        pool = [m for m in selectable if m["key"] in SELECTABLE]
    else:
        pool = [
            m
            for m in selectable
            if (m.get("odds") or 0) >= TIP_MIN_ODDS and not can_push(sport, m["key"])
        ]
        if not pool:
            main = {m["key"] for m in main_markets(analysis)}
            pool = [m for m in selectable if m["key"] in main]
    return max(pool or selectable or markets, key=lambda m: m["probability"])


def main_markets(analysis, count=4):
    """The 3-4 headline markets of an analysis, for board cards."""
    by_key = {m["key"]: m for m in analysis["markets"]}
    sport = analysis.get("sport", "football")
    if sport == "football":
        keys = ["1", "X", "2", "over25"]
    else:
        keys = ["1", "2"]
        for group in ("Handicap", "Total puncte") if sport == "basketball" else ("Total seturi",):
            options = [m for m in analysis["markets"] if m["group"] == group]
            if options:
                keys.append(min(options, key=_closest_to_even)["key"])
        if sport == "tennis":
            exact = [m for m in analysis["markets"] if m["key"].startswith("sets_")]
            if exact:
                keys.append(max(exact, key=lambda m: m["probability"])["key"])
    return [
        {k: by_key[key][k] for k in ("key", "label", "probability", "fair_odds", "odds", "ev")}
        for key in keys[:count]
        if key in by_key
    ]


def validate_analysis(a):
    """Assert the common analysis shape shared by every sport; returns `a`."""
    missing = [k for k in ANALYSIS_KEYS if k not in a]
    assert not missing, f"missing keys: {missing}"
    assert a["sport"] in SPORTS
    assert a["calibrated"] is False
    assert a["grade"] in GRADES
    assert 0 <= a["confidence"] <= 100
    assert a["quality"] == ("insufficient" if a["grade"] == "D" else "sufficient")
    assert isinstance(a["expected"], dict) and a["expected"]
    assert a["markets"], "no markets"
    keys = set()
    for m in a["markets"]:
        assert all(k in m for k in MARKET_KEYS), m
        assert m["key"] not in keys, f"duplicate market {m['key']}"
        keys.add(m["key"])
        assert 0 <= m["probability"] <= 1, m
        assert isinstance(m["selectable"], bool)
        if m["odds"] is not None:
            assert m["ev"] is not None
    if a["selection"] is not None:
        assert a["selection"]["key"] in keys and a["selection"]["selectable"]
        assert a["selection"]["probability"] >= a["threshold"]
        assert a["grade"] != "D"
    assert isinstance(a["reason"], str) and a["reason"]
    assert isinstance(a["summary"], str) and a["summary"]
    assert all(isinstance(note, str) for note in a["insights"])
    for tip in a["tips"]:
        assert all(k in tip for k in ("category", "key", "label", "probability")), tip
    for side in ("home", "away"):
        form = a["form"][side]
        assert isinstance(form["sequence"], str)
        for row in form["last"]:
            assert all(k in row for k in FORM_ROW_KEYS), row
    assert "played" in a["h2h"] and "matches" in a["h2h"]
    if a["h2h"]["played"]:
        assert all(k in a["h2h"] for k in ("home_wins", "draws", "away_wins"))
    assert all(k in a["sample"] for k in ("home", "away", "h2h"))
    assert isinstance(a["components"], dict)
    return a
