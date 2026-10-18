"""Durable, bounded, per-task event store backed by SQLite (stdlib only).

Every operation opens a short-lived connection inside a process-wide lock keyed
by database path and runs in an explicit SQLite transaction, so multiple
EventStore instances, threads, and processes can share one file safely. Async
callers should invoke methods through ``asyncio.to_thread`` (or an executor).
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_seq (
    task_id TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    timestamp REAL NOT NULL,
    truncated INTEGER NOT NULL,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;
"""

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


def _check_int(name, value, low, high=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < low or (high is not None and value > high):
        bound = f">= {low}" if high is None else f"in {low}..{high}"
        raise ValueError(f"{name} must be {bound}, got {value}")
    return value


def _check_str(name, value):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty str")
    return _clean(value)


def _clean(text):
    """Replace lone surrogates so the text is always encodable as UTF-8."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def _dumps(value):
    return _clean(json.dumps(value, ensure_ascii=False, default=str))


def _encode_payload(data, cap):
    """Return (json_text, truncated); oversized payloads become a JSON string preview."""
    try:
        text = _dumps(data)
    except (TypeError, ValueError, RecursionError):
        text = _dumps(repr(data))
    if len(text) <= cap:
        return text, False
    n = cap
    while True:
        preview = json.dumps(text[:n], ensure_ascii=False)
        if len(preview) <= cap:
            return preview, True
        n = max(0, n - (len(preview) - cap))


class EventStore:
    def __init__(self, path, capacity=1000, max_payload_chars=16000):
        path = os.fspath(path)
        if not path or path == ":memory:" or path.startswith("file:"):
            raise ValueError("path must be a filesystem path to a database file")
        self._path = os.path.normcase(os.path.abspath(path))
        self._capacity = _check_int("capacity", capacity, 1)
        self._max_payload = _check_int("max_payload_chars", max_payload_chars, 2)
        self._lock = _lock_for(self._path)
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _tx(self, write):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield conn
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    def append(self, task_id, kind, data):
        task_id = _check_str("task_id", task_id)
        kind = _check_str("kind", kind)
        payload, truncated = _encode_payload(data, self._max_payload)
        now = time.time()
        with self._tx(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO task_seq (task_id, last_seq) VALUES (?, 0)", (task_id,))
            conn.execute("UPDATE task_seq SET last_seq = last_seq + 1 WHERE task_id = ?", (task_id,))
            (seq,) = conn.execute("SELECT last_seq FROM task_seq WHERE task_id = ?", (task_id,)).fetchone()
            conn.execute(
                "INSERT INTO events (task_id, seq, kind, data, timestamp, truncated) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, seq, kind, payload, now, int(truncated)),
            )
            conn.execute(
                "DELETE FROM events WHERE task_id = ? AND seq <= ?",
                (task_id, seq - self._capacity),
            )
        return seq

    def events(self, task_id, after=0, limit=50):
        task_id = _check_str("task_id", task_id)
        after = _check_int("after", after, 0)
        limit = _check_int("limit", limit, 1, 100)
        with self._tx(write=False) as conn:
            (oldest,) = conn.execute("SELECT MIN(seq) FROM events WHERE task_id = ?", (task_id,)).fetchone()
            rows = conn.execute(
                "SELECT seq, kind, data, timestamp, truncated FROM events "
                "WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (task_id, after, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            {"seq": seq, "kind": kind, "data": json.loads(data), "timestamp": ts, "truncated": bool(trunc)}
            for seq, kind, data, ts, trunc in rows
        ]
        return {
            "events": events,
            "next_cursor": events[-1]["seq"] if events else after,
            "has_more": has_more,
            "gap": oldest is not None and after < oldest - 1,
        }

    def save_task(self, record):
        if not isinstance(record, dict):
            raise TypeError("record must be a dict")
        task_id = _check_str("task_id", record.get("task_id"))
        payload = _clean(json.dumps(record, ensure_ascii=False))
        now = time.time()
        with self._tx(write=True) as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, record, created, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET record = excluded.record, updated = excluded.updated",
                (task_id, payload, now, now),
            )

    def tasks(self):
        with self._tx(write=False) as conn:
            rows = conn.execute("SELECT record FROM tasks ORDER BY created, task_id").fetchall()
        return [json.loads(record) for (record,) in rows]
