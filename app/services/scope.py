"""Facility scoping: the single source of truth for "which facility may this user touch?".

Rules (Phase 3):
- Every operational record (donor, donation, bag, request, issue) belongs to exactly one facility.
- A facility user (admin / medical officer / staff) may only read or change records of their own facility.
- A platform administrator has no facility and therefore no operational access at all.
- A record of another facility is reported as "not found", exactly like a record that does not exist,
  so IDs cannot be used to probe other facilities.

Database triggers (migration v3) independently reject cross-facility links and facility changes.
"""
from __future__ import annotations

from typing import TypeVar

from sqlalchemy import Select

from app.models import Role, User
from app.services.errors import PermissionDenied, RecordNotFound

T = TypeVar("T")


def facility_id_of(user: User) -> int:
    """The facility a user acts for. Platform administrators (and broken accounts) have none."""
    if user is None or user.role == Role.PLATFORM_ADMIN or user.facility_id is None:
        raise PermissionDenied("This account is not attached to a blood bank, so it has no access to operational data.")
    return user.facility_id


def scoped(stmt: Select, model, facility_id: int) -> Select:
    """Restrict a SELECT on an operational model to one facility."""
    return stmt.where(model.facility_id == facility_id)


def is_owned(record, user: User) -> bool:
    return record is not None and record.facility_id is not None and record.facility_id == facility_id_of(user)


def ensure_owned(record: T | None, user: User, what: str = "Record") -> T:
    """Return the record if it belongs to the user's facility; otherwise behave as if it did not exist."""
    if not is_owned(record, user):
        raise RecordNotFound(f"{what} not found.")
    return record
