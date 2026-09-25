"""Exact competition identities, including stages of the same tournament."""

from collections import Counter

from app.model import canonical, predict

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


def competition_id(league, country=""):
    prefix = league.split(":", 1)[0] if ":" in league else ""
    return f"{canonical(country or prefix)}|{canonical(competition_name(league))}"


def match_competition(match):
    return competition_id(match.league, match.country)


def catalog(matches, demo=False):
    names = (
        {}
        if demo
        else {competition_id(name, country): (name, country) for country, name in POPULAR}
    )
    counts = Counter()
    for match in matches:
        key = match_competition(match)
        names[key] = (competition_name(match.league), match.country)
        counts[key] += 1
    popular = {competition_id(name, country): i for i, (country, name) in enumerate(POPULAR)}
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
    popular = {competition_id(name, country) for country, name in POPULAR}
    name = canonical(match.league)
    youth = any(token in name for token in ("u17", "u19", "u20", "u21", "u23", "youth"))
    return (match_competition(match) not in popular, youth, match.kickoff, match.id)


def predict_competition(match, history, threshold=0.85):
    """Group stages within a tournament, never pool unrelated domestic/cup results."""
    fixture = match.model_copy(update={"league": competition_name(match.league)})
    compatible = []
    for row in history:
        if match_competition(row) != match_competition(match):
            continue
        compatible.append(row.model_copy(update={"league": competition_name(row.league)}))
    return predict(fixture, compatible, threshold)
