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
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage(day TEXT,kind TEXT,calls INTEGER,"
                "PRIMARY KEY(day,kind))"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS core_progress(
                path TEXT PRIMARY KEY,blob TEXT,next_window INTEGER,total_windows INTEGER,
                last_attempt REAL,revisit_after REAL,attempts INTEGER,reviewed INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS core_problems(
                key TEXT PRIMARY KEY,run_id TEXT,path TEXT,state TEXT,url TEXT)""")
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def day(self):
        return datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%d")

    def reserve(self, maximum, bucket=None, bucket_maximum=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO budget VALUES(?,0)", (self.day(),))
            used = db.execute("SELECT calls FROM budget WHERE day=?", (self.day(),)).fetchone()[0]
            if used >= maximum:
                raise BudgetExhausted("Daily model-call budget exhausted")
            if bucket is not None:
                if type(bucket_maximum) is not int or bucket_maximum < 1:
                    raise ValueError("An explicit bucket limit is required")
                db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,0)", (self.day(), bucket))
                count = db.execute(
                    "SELECT calls FROM usage WHERE day=? AND kind=?", (self.day(), bucket)
                ).fetchone()[0]
                if count >= bucket_maximum:
                    raise BudgetExhausted("Daily proactive-scan budget exhausted")
                db.execute(
                    "UPDATE usage SET calls=calls+1 WHERE day=? AND kind=?", (self.day(), bucket)
                )
            db.execute("UPDATE budget SET calls=calls+1 WHERE day=?", (self.day(),))

    def bucket_used(self, kind):
        with self.db() as db:
            row = db.execute(
                "SELECT calls FROM usage WHERE day=? AND kind=?", (self.day(), kind)
            ).fetchone()
            return row[0] if row else 0

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
            db.execute("UPDATE core_problems SET state='interrupted' WHERE state='evaluating'")
            db.execute("UPDATE core_problems SET state='ambiguous' WHERE state='publishing'")

    def recent(self, kind=None):
        with self.db() as db:
            query = "SELECT * FROM runs" + (" WHERE kind=?" if kind else "")
            return [
                dict(row)
                for row in db.execute(
                    query + " ORDER BY created DESC,rowid DESC LIMIT 50", (kind,) if kind else ()
                )
            ]

    def core_progress(self):
        with self.db() as db:
            return {r["path"]: dict(r) for r in db.execute("SELECT * FROM core_progress")}

    def core_attempt(self, path, blob, index, total):
        with self.db() as db:
            db.execute(
                """INSERT INTO core_progress VALUES(?,?,?,?,?,?,1,0)
                ON CONFLICT(path) DO UPDATE SET blob=excluded.blob,next_window=excluded.next_window,
                total_windows=excluded.total_windows,last_attempt=excluded.last_attempt,
                revisit_after=0,attempts=attempts+1,
                reviewed=CASE WHEN blob=excluded.blob THEN reviewed ELSE 0 END""",
                (path, blob, (index + 1) % total, total, self.now(), 0),
            )

    def core_reviewed(self, path, wrapped, cooldown):
        with self.db() as db:
            db.execute(
                "UPDATE core_progress SET reviewed=reviewed+1,revisit_after=? WHERE path=?",
                (self.now() + cooldown if wrapped else 0, path),
            )

    def core_defer(self, path, blob, cooldown):
        self.core_attempt(path, blob, 0, 1)
        with self.db() as db:
            db.execute(
                "UPDATE core_progress SET revisit_after=? WHERE path=?",
                (self.now() + cooldown, path),
            )

    def core_retry(self, path, index, cooldown):
        with self.db() as db:
            db.execute(
                "UPDATE core_progress SET next_window=?,revisit_after=? WHERE path=?",
                (index, self.now() + cooldown, path),
            )

    def claim_problem(self, key, run_id, path):
        with self.db() as db:
            if (
                db.execute(
                    "INSERT OR IGNORE INTO core_problems VALUES(?,?,?,'evaluating',NULL)",
                    (key, run_id, path),
                ).rowcount
                == 1
            ):
                return True
            return (
                db.execute(
                    "UPDATE core_problems SET run_id=?,state='evaluating' WHERE key=? "
                    "AND state IN ('budget_wait','stale_input','analysis_failed')",
                    (run_id, key),
                ).rowcount
                == 1
            )

    def problem(self, key, state, url=None):
        with self.db() as db:
            db.execute("UPDATE core_problems SET state=?,url=? WHERE key=?", (state, url, key))

    def begin_core_publication(self, key):
        with self.db() as db:
            return (
                db.execute(
                    "UPDATE core_problems SET state='publishing' WHERE key=? AND state='pending'",
                    (key,),
                ).rowcount
                == 1
            )

    def core_problems(self, states):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM core_problems WHERE state IN ("
                    + ",".join("?" for _ in states)
                    + ")",
                    tuple(states),
                )
            ]
