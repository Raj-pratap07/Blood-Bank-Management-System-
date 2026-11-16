"""Minimal, idempotent schema migrations for existing SQLite databases.

`Base.metadata.create_all` creates missing *tables* but never alters existing
ones, so columns added after a database was created are applied here.
Progress is tracked with SQLite's `PRAGMA user_version`. Every step is additive
(ADD COLUMN + targeted UPDATE); no table or row is ever dropped.

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

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("bloodbank.migrations")

SCHEMA_VERSION = 1


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _v1_rejected_by(conn) -> None:
    cols = _columns(conn, "blood_requests")
    if "rejected_by_id" not in cols:
        conn.execute(text("ALTER TABLE blood_requests ADD COLUMN rejected_by_id INTEGER REFERENCES users(id)"))
    if "rejected_at" not in cols:
        conn.execute(text("ALTER TABLE blood_requests ADD COLUMN rejected_at DATETIME"))
    moved = conn.execute(text(
        "UPDATE blood_requests SET rejected_by_id = approved_by_id, rejected_at = approved_at, "
        "approved_by_id = NULL, approved_at = NULL "
        "WHERE status = 'rejected' AND rejected_by_id IS NULL AND approved_by_id IS NOT NULL"
    )).rowcount
    if moved:
        log.info("Migration v1: moved rejecting officer on %d legacy rejected request(s).", moved)


STEPS = {1: _v1_rejected_by}


def upgrade(engine: Engine) -> int:
    """Apply pending steps; returns the resulting schema version."""
    if engine.dialect.name != "sqlite":  # other backends should use a real migration tool
        return SCHEMA_VERSION
    with engine.begin() as conn:  # one transaction: all pending steps or none
        version = conn.execute(text("PRAGMA user_version")).scalar() or 0
        for target in range(version + 1, SCHEMA_VERSION + 1):
            STEPS[target](conn)
            conn.execute(text(f"PRAGMA user_version = {target}"))
            version = target
    return version
