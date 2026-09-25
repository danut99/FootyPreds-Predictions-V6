"""Exact competition identities, including stages of the same tournament."""

from collections import Counter
from functools import lru_cache

from footypreds.engine import canonical

POPULAR = [
    ("Europe", "Champions League"),
    ("Europe", "Europa League"),
    ("Europe", "Conference League"),
    ("Europe", "UEFA Nations League - League A"),
    ("Europe", "UEFA Nations League - League B"),
    ("Europe", "UEFA Nations League - League C"),
    ("Europe", "UEFA Nations League - League D"),
    ("World", "Friendly International"),
    ("England", "Premier League"),
    ("Spain", "LaLiga"),
    ("Germany", "Bundesliga"),
    ("Italy", "Serie A"),
    ("France", "Ligue 1"),
    ("Romania", "Superliga"),
    ("Portugal", "Liga Portugal"),
    ("Netherlands", "Eredivisie"),
    ("Belgium", "Jupiler Pro League"),
]
BASKETBALL_POPULAR = [
    ("USA", "NBA"),
    ("Europe", "Euroleague"),
    ("Europe", "Eurocup"),
    ("Europe", "Champions League"),
    ("Spain", "ACB"),
    ("Romania", "Liga Nationala"),
    ("Italy", "Lega A"),
    ("Turkey", "Super Lig"),
    ("Germany", "BBL"),
    ("France", "LNB"),
    ("Greece", "Basket League"),
    ("Australia", "NBL"),
    ("World", "World Cup"),
    ("Europe", "EuroBasket"),
]
# Tennis popularity is (category, tournament): FlashScore names a tournament
# "ATP - SINGLES: Chengdu (China), hard"; ids ignore the "(Country), surface" suffix.
SLAMS = ("Australian Open", "French Open", "Wimbledon", "US Open")
ATP_MASTERS = (
    "Indian Wells",
    "Miami",
    "Monte Carlo",
    "Madrid",
    "Rome",
    "Toronto",
    "Montreal",
    "Cincinnati",
    "Shanghai",
    "Paris",
)
ATP_500 = (
    "Rotterdam",
    "Dubai",
    "Acapulco",
    "Rio de Janeiro",
    "Barcelona",
    "Halle",
    "London",
    "Hamburg",
    "Washington",
    "Tokyo",
    "Beijing",
    "Basel",
    "Vienna",
)
WTA_1000 = (
    "Doha",
    "Dubai",
    "Indian Wells",
    "Miami",
    "Madrid",
    "Rome",
    "Toronto",
    "Montreal",
    "Cincinnati",
    "Beijing",
    "Wuhan",
)
TENNIS_POPULAR = (
    [("ATP - SINGLES", name) for name in SLAMS]
    + [("WTA - SINGLES", name) for name in SLAMS]
    + [("ATP - SINGLES", name) for name in ATP_MASTERS]
    + [("WTA - SINGLES", name) for name in WTA_1000]
    + [("ATP - SINGLES", name) for name in ATP_500 if name not in ATP_MASTERS]
    + [("ATP - SINGLES", "Finals"), ("WTA - SINGLES", "Finals")]
)
POPULAR_BY_SPORT = {
    "football": POPULAR,
    "basketball": BASKETBALL_POPULAR,
    "tennis": TENNIS_POPULAR,
}


def competition_name(league):
    # Keep women's/youth/lower divisions distinct; only remove the phase suffix.
    name = league.split(":", 1)[-1].strip()
    base, separator, phase = name.partition(" - ")
    if separator and canonical(phase) in {
        "league phase",
        "group stage",
        "play offs",
        "qualification",
        "final stage",
        "apertura",
        "clausura",
        "championship group",
        "relegation group",
        "promotion group",
    }:
        name = base
    aliases = {
        "la liga": "LaLiga",
        "uefa champions league": "Champions League",
        "uefa europa league": "Europa League",
        "europa conference league": "Conference League",
        "uefa conference league": "Conference League",
    }
    return aliases.get(canonical(name), name)


def tennis_tournament(league):
    """ "ATP - SINGLES: Chengdu (China), hard" -> "Chengdu"."""
    name = league.split(":", 1)[-1].strip()
    name = name.split(",", 1)[0].strip()
    return name.split(" (", 1)[0].strip() or name


def competition_id(league, country="", sport="football"):
    """Football ids are unchanged ("england|premier league"); other sports are prefixed:
    "basketball:usa|nba", "tennis:atp - singles|chengdu"."""
    prefix = league.split(":", 1)[0] if ":" in league else ""
    if sport == "tennis":
        return f"tennis:{canonical(prefix or country)}|{canonical(tennis_tournament(league))}"
    key = f"{canonical(country or prefix)}|{canonical(competition_name(league))}"
    return key if sport == "football" else f"{sport}:{key}"


def match_competition(match):
    return competition_id(match.league, match.country, match.sport)


@lru_cache(maxsize=8)
def popular_ids(sport="football"):
    """{competition id: rank} of the sport's popular competitions (read-only)."""
    entries = POPULAR_BY_SPORT.get(sport, [])
    if sport == "tennis":
        ids = [competition_id(f"{category}: {name}", "", "tennis") for category, name in entries]
    else:
        ids = [competition_id(name, country, sport) for country, name in entries]
    ranks = {}
    for rank, key in enumerate(ids):
        ranks.setdefault(key, rank)
    return ranks


def catalog(matches, demo=False, sport="football"):
    names = {}
    if not demo:
        for country, name in POPULAR_BY_SPORT.get(sport, []):
            if sport == "tennis":
                names[competition_id(f"{country}: {name}", "", sport)] = (name, country)
            else:
                names[competition_id(name, country, sport)] = (name, country)
    counts = Counter()
    for match in matches:
        key = match_competition(match)
        if match.sport == "tennis":
            category = match.league.split(":", 1)[0] if ":" in match.league else match.country
            names[key] = (tennis_tournament(match.league), category)
        else:
            names[key] = (competition_name(match.league), match.country)
        counts[key] += 1
    popular = popular_ids(sport)
    return [
        {
            "id": key,
            "name": name,
            "country": country,
            "count": counts[key],
            "popular": key in popular,
        }
        for key, (name, country) in sorted(
            names.items(), key=lambda item: (popular.get(item[0], 100), item[1][1], item[1][0])
        )
    ]


def priority(match):
    """Popular competitions first, then senior men's games with odds from major countries."""
    if match.sport == "tennis":
        return tennis_priority(match)
    if match.sport == "basketball":
        return basketball_priority(match)
    popular = {competition_id(name, country) for country, name in POPULAR}
    names = " ".join(canonical(value) for value in (match.league, match.home, match.away))
    youth = any(token in names for token in YOUTH)
    women = any(canonical(team).endswith(" w") for team in (match.home, match.away))
    lower = any(token in canonical(match.league) for token in LOWER_TIERS)
    return (
        match_competition(match) not in popular,
        youth,
        women,
        not match.odds,
        canonical(match.country) not in MAJOR_COUNTRIES,
        lower,
        match.kickoff,
        match.id,
    )


YOUTH = ("u16", "u17", "u18", "u19", "u20", "u21", "u23", "youth", "primavera")
MAJOR_COUNTRIES = {
    "england",
    "spain",
    "germany",
    "italy",
    "france",
    "netherlands",
    "portugal",
    "belgium",
    "turkey",
    "scotland",
    "romania",
    "greece",
    "austria",
    "switzerland",
    "denmark",
    "brazil",
    "argentina",
    "usa",
    "mexico",
    "europe",
    "world",
}
LOWER_TIERS = (
    "regionalliga",
    "oberliga",
    "tercera",
    "segunda rfef",
    "serie d",
    "liga 3",
    "national league north",
    "national league south",
    "derde",
    "division 3",
    "amateur",
    "reserve",
)


def basketball_priority(match):
    names = " ".join(canonical(value) for value in (match.league, match.home, match.away))
    women = "women" in names or any(
        canonical(team).endswith(" w") for team in (match.home, match.away)
    )
    return (
        match_competition(match) not in popular_ids("basketball"),
        any(token in names for token in YOUTH),
        women,
        not match.odds,
        canonical(match.country) not in MAJOR_COUNTRIES,
        match.kickoff,
        match.id,
    )


# Lower is more important: main tours, then Challenger / WTA 125, then ITF and the rest.
TENNIS_TIERS = (("atp", 0), ("wta", 0), ("challenger", 1), ("itf", 2))


def tennis_priority(match):
    category = canonical(match.league.split(":", 1)[0] if ":" in match.league else match.league)
    tier = next((rank for token, rank in TENNIS_TIERS if token in category), 3)
    if "challenger" in category or "125" in category:
        tier = max(tier, 1)
    return (
        match_competition(match) not in popular_ids("tennis"),
        "singles" not in category,
        tier,
        not match.odds,
        match.kickoff,
        match.id,
    )
