"""Competition identities, catalog counts and board priority on random fixture lists."""

import random
from datetime import timedelta

import pytest

from footypreds.competitions import POPULAR, catalog, competition_id, match_competition, priority
from footypreds.tests.helpers import KICKOFF, fixture

LEAGUES = [
    ("ENGLAND: Premier League", "England"),
    ("EUROPE: Champions League - League phase", "Europe"),
    ("UEFA Champions League - Play Offs", "Europe"),
    ("SPAIN: LaLiga", "Spain"),
    ("SPAIN: La Liga", ""),
    ("ITALY: Primavera U19", "Italy"),
    ("GERMANY: Regionalliga West", "Germany"),
    ("BHUTAN: Premier League", "Bhutan"),
    ("ROMÂNIA: Superliga", "Romania"),
    ("WORLD: Friendly International", "World"),
    ("Serie C - Group A", "Italy"),
]


def random_fixtures(seed, count=60):
    rng = random.Random(seed)
    rows = []
    for n in range(count):
        league, country = rng.choice(LEAGUES)
        rows.append(
            fixture(
                id=f"f{n}",
                league=league,
                country=country,
                home=f"Home{n}" + rng.choice(["", " W", " U21"]),
                away=f"Away{n}",
                kickoff=KICKOFF + timedelta(minutes=rng.randint(0, 600)),
                odds={"1": 2.0} if rng.random() < 0.6 else {},
            )
        )
    return rows


@pytest.mark.parametrize("seed", range(10))
def test_catalog_counts_every_fixture_once(seed):
    rows = random_fixtures(seed)
    for demo in (False, True):
        entries = catalog(rows, demo=demo)
        ids = [entry["id"] for entry in entries]
        assert len(ids) == len(set(ids))
        assert sum(entry["count"] for entry in entries) == len(rows)
        for entry in entries:
            assert entry["count"] == sum(match_competition(m) == entry["id"] for m in rows)
    popular = {competition_id(name, country) for country, name in POPULAR}
    flags = [entry["popular"] for entry in catalog(rows)]
    assert flags == sorted(flags, reverse=True), "popular competitions come first"
    assert {e["id"] for e in catalog([])} == popular


@pytest.mark.parametrize("seed", range(10))
def test_priority_is_a_deterministic_total_order(seed):
    rows = random_fixtures(seed)
    shuffled = rows[:]
    random.Random(seed).shuffle(shuffled)
    assert sorted(rows, key=priority) == sorted(shuffled, key=priority)
    ordered = sorted(rows, key=priority)
    popular = {competition_id(name, country) for country, name in POPULAR}
    seen_other = False
    for match in ordered:
        if match_competition(match) not in popular:
            seen_other = True
        else:
            assert not seen_other, "a popular competition was ranked after another one"


def test_stage_suffixes_and_aliases_share_one_identity():
    assert competition_id("EUROPE: Champions League - League phase", "Europe") == competition_id(
        "UEFA Champions League - Play Offs", "Europe"
    )
    assert competition_id("SPAIN: La Liga") == competition_id("LaLiga", "Spain")
    assert competition_id("ROMÂNIA: Superliga") == competition_id("Superliga", "Romania")
