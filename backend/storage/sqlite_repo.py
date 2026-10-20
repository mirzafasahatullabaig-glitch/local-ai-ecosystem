"""Persistence for devices and tasks. SQLite, thread-safe, no ORM.

Rows are stored as plain columns plus a JSON blob for the parts that vary
(capabilities, task payloads). Callers hold schema objects; this layer only
reads and writes dicts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional, Protocol


class Repository(Protocol):
    def upsert_device(self, device_id: str, data: dict) -> None: ...
    def get_device(self, device_id: str) -> Optional[dict]: ...
    def list_devices(self) -> list[dict]: ...
    def set_device_last_seen(self, device_id: str, ts_ms: int) -> None: ...
    def add_task(self, task_id: str, data: dict) -> None: ...
    def list_tasks(self) -> list[dict]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    data        TEXT NOT NULL,      -- JSON manifest/state
    last_seen   INTEGER NOT NULL    -- ms since epoch
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,      -- JSON
    created_at  INTEGER NOT NULL
);
"""


class SQLiteRepository:
    """Thread-safe SQLite repository. Pass ``:memory:`` for tests."""

    def __init__(self, path: str = "eco.db") -> None:
        # check_same_thread=False lets us share one connection; the lock below
        # serializes access so that is safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def upsert_device(self, device_id: str, data: dict) -> None:
        last_seen = int(data.get("ts", 0))
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices(device_id, data, last_seen) VALUES(?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET data=excluded.data, "
                "last_seen=excluded.last_seen",
                (device_id, json.dumps(data), last_seen),
            )
            self._conn.commit()

    def get_device(self, device_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, last_seen FROM devices WHERE device_id=?",
                (device_id,),
            ).fetchone()
        return self._row_to_device(row) if row else None

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, last_seen FROM devices"
            ).fetchall()
        return [self._row_to_device(r) for r in rows]

    def set_device_last_seen(self, device_id: str, ts_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_seen=? WHERE device_id=?",
                (ts_ms, device_id),
            )
            self._conn.commit()

    def add_task(self, task_id: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks(task_id, data, created_at) "
                "VALUES(?,?,?)",
                (task_id, json.dumps(data), int(data.get("created_at", 0))),
            )
            self._conn.commit()

    def list_tasks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM tasks ORDER BY created_at"
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> dict:
        d = json.loads(row["data"])
        d["last_seen"] = row["last_seen"]
        return d
