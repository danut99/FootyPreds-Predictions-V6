import json
import sqlite3
import time
from contextlib import contextmanager

from app.domain import Match


class Store:
    def __init__(self, path):
        self.path = path
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
        with self.connect() as db:
            for match in matches:
                # Team-results responses may omit IDs/odds present in fixture responses.
                old = db.execute("SELECT payload FROM matches WHERE id=?", (match.id,)).fetchone()
                if old:
                    prior = Match.model_validate_json(old[0])
                    update = {
                        field: getattr(prior, field)
                        for field in ("home_id", "away_id", "odds", "country")
                        if not getattr(match, field)
                    }
                    if prior.status == "finished" and match.status != "finished":
                        continue
                    match = match.model_copy(update=update)
                db.execute(
                    "INSERT OR REPLACE INTO matches VALUES (?, ?, ?)",
                    (match.id, match.kickoff.timestamp(), match.model_dump_json()),
                )

    def matches(self):
        with self.connect() as db:
            return [
                Match.model_validate_json(r[0])
                for r in db.execute("SELECT payload FROM matches ORDER BY kickoff")
            ]

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
        from app.model import outcome

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
                    result = {
                        "won": outcome(pick["key"], match.home_goals, match.away_goals),
                        "score": f"{match.home_goals}-{match.away_goals}",
                    }
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
