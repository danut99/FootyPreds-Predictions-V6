from datetime import datetime, timedelta, timezone

from footypreds.domain import Match

KICKOFF = datetime(2026, 6, 1, 18, tzinfo=timezone.utc)


def fixture(**kwargs):
    data = dict(id="fixture", kickoff=KICKOFF, league="Test", home="Strong", away="Weak")
    return Match(**(data | kwargs))


def result(match_id, days_before, home, away, home_goals, away_goals, league="Test", **extra):
    return Match(
        id=match_id,
        kickoff=KICKOFF - timedelta(days=days_before),
        league=league,
        home=home,
        away=away,
        status="finished",
        home_goals=home_goals,
        away_goals=away_goals,
        **extra,
    )


def strong_history():
    """Strong beats Weak 4-0 every week in the same league."""
    return [result(f"past-{i}", 7 * (i + 1), "Strong", "Weak", 4, 0) for i in range(25)]


def league_history(days=300, teams=("Strong", "Mid", "Weak", "Other"), rates=None):
    """Deterministic round robin: Strong scores most, Weak concedes most."""
    rates = rates or {"Strong": (3, 0), "Mid": (1, 1), "Weak": (0, 3), "Other": (1, 1)}
    rows, n = [], 0
    for week in range(days // 7):
        for i, home in enumerate(teams):
            away = teams[(i + week + 1) % len(teams)]
            if home == away:
                continue
            n += 1
            home_goals = max(0, rates[home][0] - rates[away][1] // 3 + (week % 2))
            away_goals = max(0, rates[away][0] - 1 + rates[home][1] // 3)
            rows.append(result(f"lg-{n}", 7 * week + 1, home, away, home_goals, away_goals))
    return rows


def fixtures_payload(kickoff=None, odds=None):
    kickoff = kickoff or datetime.now(timezone.utc) + timedelta(days=1)
    return [
        {
            "name": "ENGLAND: Test",
            "country_name": "England",
            "matches": [
                {
                    "match_id": "fixture",
                    "timestamp": kickoff.timestamp(),
                    "home_team": {"name": "Strong", "team_id": "h"},
                    "away_team": {"name": "Weak", "team_id": "a"},
                    "match_status": {"is_started": False, "is_finished": False},
                    "scores": {"home": None, "away": None},
                    "odds": odds if odds is not None else {"1": 1.5, "2": "-", "X": None},
                }
            ],
        }
    ]


def h2h_payload(kickoff, count=12):
    """Current FlashScore h2h schema: flat rows, string scores, no team IDs."""
    rows = []
    for i in range(count):
        when = kickoff - timedelta(days=5 * (i + 1))
        home, away = ("Strong", f"Opp{i}") if i % 2 == 0 else (f"Opp{i}", "Weak")
        rows.append(
            {
                "match_id": f"h2h-{i}",
                "timestamp": when.timestamp(),
                "status": "FINISHED",
                "winner": "home",
                "tournament_name": "Cup" if i % 3 else "Test",
                "home_team": {"name": home},
                "away_team": {"name": away},
                "scores": {"home": "2", "away": "0"},
            }
        )
    rows.append(
        {
            "match_id": "mutual",
            "timestamp": (kickoff - timedelta(days=200)).timestamp(),
            "status": "FINISHED",
            "tournament_name": "Test",
            "home_team": {"name": "Strong"},
            "away_team": {"name": "Weak"},
            "scores": {"home": "3", "away": "1"},
        }
    )
    # A future row must never become history.
    rows.append(
        {**rows[0], "match_id": "future", "timestamp": (kickoff + timedelta(days=3)).timestamp()}
    )
    return rows
