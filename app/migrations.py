"""Minimal, idempotent schema migrations for existing SQLite databases.

`Base.metadata.create_all` creates missing *tables* but never alters existing
ones, so columns added after a database was created are applied here.
Progress is tracked with SQLite's `PRAGMA user_version`. Every step is additive
(ADD COLUMN + targeted UPDATE); no table or row is ever dropped.

Atomicity: Python's sqlite3 driver (legacy transaction mode) auto-commits DDL
such as ALTER TABLE, so the runner takes manual control of the connection and
wraps all pending steps *and* the version bump in one `BEGIN IMMEDIATE`
transaction. Either everything is applied or nothing is. IMMEDIATE also takes
the write lock up front, so two app processes starting together run the
migration one after the other; the second re-reads `user_version` under the lock
and finds nothing left to do.

Version history
---------------
1  blood_requests.rejected_by_id / rejected_at.
   Before v1, rejecting a request overwrote approved_by_id/approved_at with the
   rejecting officer. For legacy REJECTED rows those values therefore identify
   the *rejecting* officer, so they are moved to rejected_by_id/rejected_at.
   (Any genuine earlier approval on such rows was already overwritten and
   cannot be recovered.)
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from sqlalchemy.engine import Engine

log = logging.getLogger("bloodbank.migrations")

SCHEMA_VERSION = 1


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _v1_rejected_by(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "blood_requests")
    if "rejected_by_id" not in cols:
        conn.execute("ALTER TABLE blood_requests ADD COLUMN rejected_by_id INTEGER REFERENCES users(id)")
    if "rejected_at" not in cols:
        conn.execute("ALTER TABLE blood_requests ADD COLUMN rejected_at DATETIME")
    moved = conn.execute(
        "UPDATE blood_requests SET rejected_by_id = approved_by_id, rejected_at = approved_at, "
        "approved_by_id = NULL, approved_at = NULL "
        "WHERE status = 'rejected' AND rejected_by_id IS NULL AND approved_by_id IS NOT NULL"
    ).rowcount
    if moved:
        log.info("Migration v1: moved rejecting officer on %d legacy rejected request(s).", moved)


STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _v1_rejected_by}


def upgrade(engine: Engine) -> int:
    """Apply pending steps atomically; returns the resulting schema version."""
    if engine.dialect.name != "sqlite":  # other backends should use a real migration tool
        return SCHEMA_VERSION
    pooled = engine.raw_connection()
    conn: sqlite3.Connection = pooled.driver_connection
    saved_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control: no implicit commits around DDL
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0] or 0
            for target in range(version + 1, SCHEMA_VERSION + 1):
                STEPS[target](conn)
                conn.execute(f"PRAGMA user_version = {int(target)}")
                version = target
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return version
    finally:
        conn.isolation_level = saved_isolation
        pooled.close()
