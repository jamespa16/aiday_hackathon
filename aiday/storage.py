"""Transactional run storage. Secrets and private fixture data stay server-side."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch(mode=0o600)
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, data TEXT NOT NULL)"
        )
        self.connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS evaluation_entries (team TEXT NOT NULL, tier INTEGER NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(team, tier))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS benchmark_entries (team TEXT NOT NULL, tier INTEGER NOT NULL, trial INTEGER NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(team, tier, trial))")
        self.lock = threading.RLock()

    def setting(self, key: str, default):
        """Persist organizer configuration once, including an unrevealed suite seed."""
        with self.lock:
            self.connection.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, json.dumps(default)))
            return json.loads(self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])

    def create(self, run: dict) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if run.get("_meta", {}).get("mode") == "evaluation":
                    team = run["_meta"]["team_name"].strip().casefold()
                    if run["_meta"].get("round_type") == "throughput":
                        self.connection.execute("INSERT INTO benchmark_entries VALUES (?, ?, ?, ?)",
                                                (team, run["tier"], run["_meta"]["benchmark"]["trial"], run["run_id"]))
                    else:
                        self.connection.execute("INSERT INTO evaluation_entries VALUES (?, ?, ?)", (team, run["tier"], run["run_id"]))
                self.connection.execute("INSERT INTO runs VALUES (?, ?, ?)", (run["run_id"], token_digest(token), json.dumps(run)))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        return token

    def run_id_for_token(self, token: str) -> str | None:
        with self.lock:
            row = self.connection.execute("SELECT run_id FROM runs WHERE token_hash=?", (token_digest(token),)).fetchone()
        return row[0] if row else None

    @contextmanager
    def transaction(self, run_id: str):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                run = json.loads(row[0])
                yield run
                self.connection.execute("UPDATE runs SET data=? WHERE run_id=?", (json.dumps(run), run_id))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def read(self, run_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def all(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT data FROM runs ORDER BY run_id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self):
        with self.lock:
            self.connection.close()
