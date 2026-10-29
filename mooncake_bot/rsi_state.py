"""Durable attempt history and model-call limits for the bounded RSI worker."""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone


class BudgetExhausted(ValueError):
    pass


class State:
    def __init__(self, directory, now=time.time):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory, self.now = directory, now
        self.path = directory / "state.sqlite3"
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY,calls INTEGER)")
            db.execute("""CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY,kind TEXT,fingerprint TEXT,created REAL,state TEXT,result TEXT,
                UNIQUE(kind,fingerprint))""")
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def day(self):
        return datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%d")

    def reserve(self, maximum):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO budget VALUES(?,0)", (self.day(),))
            used = db.execute("SELECT calls FROM budget WHERE day=?", (self.day(),)).fetchone()[0]
            if used >= maximum:
                raise BudgetExhausted("Daily RSI model-call budget exhausted")
            db.execute("UPDATE budget SET calls=calls+1 WHERE day=?", (self.day(),))

    def used(self):
        with self.db() as db:
            row = db.execute("SELECT calls FROM budget WHERE day=?", (self.day(),)).fetchone()
            return row[0] if row else 0

    def due(self, kind, interval):
        with self.db() as db:
            row = db.execute("SELECT MAX(created) FROM runs WHERE kind=?", (kind,)).fetchone()
            return row[0] is None or self.now() - row[0] >= interval

    def seen(self, kind, fingerprint):
        with self.db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM runs WHERE kind=? AND fingerprint=?", (kind, fingerprint)
                ).fetchone()
                is not None
            )

    def begin(self, kind, fingerprint):
        run_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (run_id, kind, fingerprint, self.now(), "running", "{}"),
            )
        return run_id

    def finish(self, run_id, state, result):
        with self.db() as db:
            db.execute(
                "UPDATE runs SET state=?,result=? WHERE id=?", (state, json.dumps(result), run_id)
            )
        path = self.directory / run_id
        path.mkdir(exist_ok=True, mode=0o700)
        (path / "outcome.json").write_text(json.dumps(result | {"state": state}, indent=2) + "\n")

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE runs SET state='interrupted' WHERE state='running'")

    def recent(self):
        with self.db() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 50")
            ]
