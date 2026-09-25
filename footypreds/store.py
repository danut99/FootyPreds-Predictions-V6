import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from footypreds.domain import Match


class Store:
    def __init__(self, path):
        self.path = path
        self.version = 0
        self._matches = None
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY, kickoff REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS match_time ON matches(kickoff);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    match_id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL,
                    settled TEXT
                );
                CREATE TABLE IF NOT EXISTS assessments (
                    match_id TEXT PRIMARY KEY, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS synced_days (
                    day TEXT PRIMARY KEY, fetched REAL NOT NULL, matches INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_matches(self, matches):
        """Upsert matches; returns how many rows changed.

        The in-memory cache and `version` change only AFTER the commit, and only when a row
        really changed: re-loading an unchanged day must not invalidate every cached analysis,
        and a concurrent reader must never cache the pre-write rows under the new version.
        """
        matches = list(matches)
        if not matches:
            return 0
        changed = 0
        try:
            with self.connect() as db:
                for match in matches:
                    # Team-results responses may omit IDs/odds/logos present in fixture responses.
                    old = db.execute(
                        "SELECT payload FROM matches WHERE id=?", (match.id,)
                    ).fetchone()
                    if old:
                        prior = Match.model_validate_json(old[0])
                        update = {
                            field: getattr(prior, field)
                            for field in (
                                "home_id",
                                "away_id",
                                "country",
                                "home_participant_id",
                                "away_participant_id",
                                # H2H/results rows may lack the crests of the day list.
                                "home_logo",
                                "away_logo",
                                "league_logo",
                            )
                            if not getattr(match, field)
                        }
                        # Union of prices: matches/odds markets survive a list-by-date
                        # reload, which only carries 1/X/2 (new prices win) - but only while the
                        # new row is scheduled. A live/finished/called-off row cannot tell when
                        # its prices were quoted, so the stored (pre-match) prices are kept and
                        # it only fills keys that were missing.
                        if match.status != "scheduled":
                            update["odds"] = {**match.odds, **prior.odds}
                        else:
                            update["odds"] = {**prior.odds, **match.odds}
                        if prior.status == "finished" and match.status != "finished":
                            continue
                        match = match.model_copy(update=update)
                    payload = match.model_dump_json()
                    if old and old[0] == payload:
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO matches VALUES (?, ?, ?)",
                        (match.id, match.kickoff.timestamp(), payload),
                    )
                    changed += 1
        finally:
            if changed:
                with self._lock:
                    self._matches = None
                    self.version += 1
        return changed

    def matches(self, sport=None):
        """All stored matches (or one sport's), parsed once and cached until the next write."""
        if sport is not None:
            return [m for m in self.matches() if m.sport == sport]
        with self._lock:
            if self._matches is not None:
                return list(self._matches)
            version = self.version
        with self.connect() as db:
            rows = [
                Match.model_validate_json(r[0])
                for r in db.execute("SELECT payload FROM matches ORDER BY kickoff")
            ]
        with self._lock:
            if version == self.version:
                self._matches = rows
        return list(rows)

    def matches_on(self, day, sport=None):
        start = datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()
        with self.connect() as db:
            rows = [
                Match.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT payload FROM matches WHERE kickoff>=? AND kickoff<? ORDER BY kickoff",
                    (start, start + 86400),
                )
            ]
        return rows if sport is None else [m for m in rows if m.sport == sport]

    def synced_days(self, sport="football"):
        """ISO days already synced for `sport`. Football keys stay plain "YYYY-MM-DD";
        other sports are stored as "sport:YYYY-MM-DD"."""
        with self.connect() as db:
            keys = [r[0] for r in db.execute("SELECT day FROM synced_days")]
        if sport == "football":
            return {key for key in keys if ":" not in key}
        prefix = f"{sport}:"
        return {key[len(prefix) :] for key in keys if key.startswith(prefix)}

    def mark_synced(self, day, count, sport="football"):
        key = day.isoformat() if sport == "football" else f"{sport}:{day.isoformat()}"
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO synced_days VALUES (?, ?, ?)",
                (key, time.time(), count),
            )

    def match(self, match_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM matches WHERE id=?", (match_id,)).fetchone()
            return Match.model_validate_json(row[0]) if row else None

    def get_cache(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
            return json.loads(row[0]) if row else None

    def put_cache(self, key, payload, ttl):
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, time.time() + ttl, json.dumps(payload)),
            )

    def snapshot(self, match, prediction, now):
        if match.status != "scheduled" or match.kickoff <= now or match.source != "flashscore":
            return False
        payload = {"match": match.model_dump(mode="json"), "prediction": prediction}
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO assessments VALUES (?, ?)", (match.id, now.timestamp())
            )
            if prediction["selection"] is None:
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO predictions VALUES (?, ?, ?, NULL)",
                (match.id, now.timestamp(), json.dumps(payload)),
            )
            return cursor.rowcount == 1

    def assessment_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]

    def settle(self, matches):
        from footypreds.sports.settle import settle

        settled = 0
        with self.connect() as db:
            for match in matches:
                if match.status != "finished" or match.source != "flashscore":
                    continue
                row = db.execute(
                    "SELECT payload, created FROM predictions WHERE match_id=? AND settled IS NULL",
                    (match.id,),
                ).fetchone()
                if row and row["created"] < match.kickoff.timestamp():
                    pick = json.loads(row["payload"])["prediction"]["selection"]
                    won = settle(
                        match.sport,
                        pick["key"],
                        match.home_goals,
                        match.away_goals,
                        match.finish_type or match.status,
                    )
                    result = {"won": won, "score": f"{match.home_goals}-{match.away_goals}"}
                    if won is None:
                        # Void (retirement, walkover, push): settled, but no win or loss.
                        result["void"] = True
                    db.execute(
                        "UPDATE predictions SET settled=? WHERE match_id=?",
                        (json.dumps(result), match.id),
                    )
                    settled += 1
        return settled

    def predictions(self):
        with self.connect() as db:
            return [
                {
                    **json.loads(r["payload"]),
                    "created": r["created"],
                    "result": json.loads(r["settled"]) if r["settled"] else None,
                }
                for r in db.execute("SELECT * FROM predictions ORDER BY created DESC")
            ]

    def save_plan(self, plan):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO plans VALUES (?, ?, ?)",
                (plan["id"], plan["created"], json.dumps(plan)),
            )

    def plans(self):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM plans ORDER BY created DESC LIMIT 30")
            ]

    def plan(self, plan_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
            return json.loads(row[0]) if row else None
