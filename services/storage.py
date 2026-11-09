import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def ident(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class Store:
    def __init__(self, path, kind):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript((ROOT / "migrations" / f"001_{kind}.sql").read_text(encoding="utf-8"))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def tx(self, write=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def emit(db, session, turn, kind, payload, event_id=None, at=None):
    event_id = event_id or ident("evt")
    db.execute("INSERT INTO events(id,session,turn,type,payload,at) VALUES(?,?,?,?,?,?)",
               (event_id, session, turn, kind, encode(payload), time.time() if at is None else at))
    return event_id


def events(db, session, after=0):
    return [{
        "schema_version": "1.0", "event_id": row["id"], "session_id": session,
        "turn_id": row["turn"], "authoritative_sequence": row["sequence"],
        "event_type": row["type"], "occurred_at": iso(row["at"]),
        "policy_version": row["policy"], "payload": json.loads(row["payload"]),
    } for row in db.execute(
        """SELECT events.*, sessions.policy FROM events JOIN sessions ON sessions.id=events.session
           WHERE session=? AND sequence>? ORDER BY sequence""", (session, after))]
