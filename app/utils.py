from __future__ import annotations

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone support)."""
    return datetime.now(UTC).replace(tzinfo=None)


def today() -> date:
    return date.today()


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
