"""Time helpers: UTC for stored events, the business time zone (Asia/Kolkata) for calendar days.

- Stored timestamps (created_at, issued_at, published_at, ...) are **timezone-aware UTC**.
  Older rows were written as naive UTC; they are read as UTC (never shifted twice).
- Business dates (expiry, eligibility intervals, form defaults, report days) are calendar days
  in ``settings.timezone`` (default Asia/Kolkata), never the server's local date.
- Calendar-day queries use an inclusive lower bound and an exclusive upper bound.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def business_tz() -> ZoneInfo:
    from app.config import settings          # local import: config must not import utils
    return ZoneInfo(settings.timezone)


def to_utc(value: datetime) -> datetime:
    """Aware -> converted to UTC. Naive -> assumed to already be UTC (legacy rows); no shift."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_business(value: datetime) -> datetime:
    return to_utc(value).astimezone(business_tz())


def business_today(now: datetime | None = None) -> date:
    """Today's calendar date in the business time zone (e.g. IST), whatever the server's zone."""
    return to_business(now or utcnow()).date()


# Backwards-compatible name used across the code base.
today = business_today


def business_day_bounds_utc(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """UTC instants for business calendar days [start 00:00, (end + 1 day) 00:00): inclusive / exclusive."""
    tz = business_tz()
    lower = datetime.combine(start, time.min, tzinfo=tz).astimezone(UTC) if start else None
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC) if end else None
    return lower, upper


def iso_utc(value: datetime) -> str:
    """RFC 3339 / ISO 8601 UTC string ending in 'Z' (single offset, no double suffix)."""
    return to_utc(value).isoformat().replace("+00:00", "Z")


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
