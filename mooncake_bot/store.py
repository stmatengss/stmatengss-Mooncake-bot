import json
import sqlite3
import time
import uuid


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "tasks.sqlite3"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL,
                number INTEGER NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL,
                updated REAL NOT NULL, error TEXT, result TEXT, published_url TEXT
            )""")
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, task):
        task_id = uuid.uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO tasks(id,source,number,payload,created,updated) "
                "VALUES(?,?,?,?,?,?)",
                (task_id, task["source"], task["number"], json.dumps(task), now, now),
            )
            return dict(
                db.execute("SELECT * FROM tasks WHERE source=?", (task["source"],)).fetchone()
            )

    def claim(self, maximum):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT count(*) FROM tasks WHERE state IN ('running','publishing')"
            ).fetchone()[0]
            if count >= maximum:
                return None
            row = db.execute("""SELECT * FROM tasks q WHERE state='queued' AND NOT EXISTS (
                SELECT 1 FROM tasks a WHERE a.number=q.number
                AND a.state IN ('running','publishing')) ORDER BY created LIMIT 1""").fetchone()
            if row:
                db.execute(
                    "UPDATE tasks SET state='running',updated=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                return dict(row)

    def update(self, task_id, state, **fields):
        if set(fields) - {"error", "result", "published_url"}:
            raise ValueError("Unknown task field")
        values = {"state": state, "updated": time.time(), **fields}
        with self.db() as db:
            db.execute(
                "UPDATE tasks SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), task_id),
            )

    def recover(self):
        # Never replay an interrupted agent or a potentially successful remote write.
        with self.db() as db:
            db.execute(
                """UPDATE tasks SET state='interrupted',
                error='Service restarted; inspect local result and GitHub before retrying',updated=?
                WHERE state IN ('running','publishing')""",
                (time.time(),),
            )

    def list(self):
        with self.db() as db:
            return [
                dict(x) for x in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 100")
            ]
