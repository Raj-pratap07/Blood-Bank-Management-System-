"""Portable column types."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

from app.utils import to_utc


class UTCDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python.

    Writing: aware values are converted to UTC; naive values are treated as already-UTC (how all
    existing rows were written), so nothing is ever shifted twice.
    Storage: SQLite keeps exactly the previous format (naive UTC text in a DATETIME column), so the
    schema and existing data are unchanged. PostgreSQL (Phase 5B) uses TIMESTAMP WITH TIME ZONE.
    Reading: always returned as aware UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects a datetime, got {type(value).__name__}")
        value = to_utc(value)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return to_utc(value)
