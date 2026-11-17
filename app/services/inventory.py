"""Blood-bag inventory: expiry sweep, stock summaries, FEFO selection, discards."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BloodGroup, BloodUnit, UnitStatus, User
from app.services.compatibility import compatible_donor_groups
from app.services.errors import InventoryError, PermissionDenied
from app.services.scope import ensure_owned
from app.services.tx import transaction
from app.utils import business_today

EXPIRABLE = (UnitStatus.QUARANTINE, UnitStatus.AVAILABLE, UnitStatus.RESERVED)


def expire_overdue_units(db: Session, on: date | None = None) -> int:
    """Mark every non-final bag past its expiry date as EXPIRED (and drop any reservation).

    Deliberately platform-wide system maintenance: it applies one fixed rule, returns only a
    count, and exposes no data, so any request may trigger it.
    """
    on = on or business_today()
    with transaction(db):
        result = db.execute(
            update(BloodUnit)
            .where(BloodUnit.status.in_(EXPIRABLE), BloodUnit.expires_on < on)
            .values(status=UnitStatus.EXPIRED, reserved_for_request_id=None, status_note="Auto-expired")
            .execution_options(synchronize_session="fetch")
        )
    return result.rowcount or 0


def stock_summary(db: Session, facility_id: int, on: date | None = None) -> list[dict]:
    """Per blood group (one facility): counts by status plus near-expiry available units."""
    on = on or business_today()
    near = on + timedelta(days=settings.near_expiry_days)
    rows = db.execute(
        select(BloodUnit.blood_group, BloodUnit.status, func.count())
        .where(BloodUnit.facility_id == facility_id)
        .group_by(BloodUnit.blood_group, BloodUnit.status)
    ).all()
    near_rows = dict(db.execute(
        select(BloodUnit.blood_group, func.count())
        .where(BloodUnit.facility_id == facility_id, BloodUnit.status == UnitStatus.AVAILABLE,
               BloodUnit.expires_on <= near)
        .group_by(BloodUnit.blood_group)
    ).all())
    summary = {g: {s.value: 0 for s in UnitStatus} for g in BloodGroup}
    for group, status, n in rows:
        summary[group][status.value] = n
    return [{"group": g.value, **summary[g], "near_expiry": near_rows.get(g, 0)} for g in BloodGroup]


def fefo_candidates(db: Session, facility_id: int, recipient: BloodGroup, on: date | None = None,
                    limit: int | None = None):
    """Available, unexpired, ABO/Rh-compatible bags of ONE facility (never another blood bank's stock).

    Order: identical group first (conserves O-negative and other scarce groups),
    then First-Expiry-First-Out within each tier.
    """
    on = on or business_today()
    stmt = (
        select(BloodUnit)
        .where(
            BloodUnit.facility_id == facility_id,
            BloodUnit.status == UnitStatus.AVAILABLE,
            BloodUnit.expires_on >= on,
            BloodUnit.blood_group.in_(compatible_donor_groups(recipient)),
        )
        .order_by(case((BloodUnit.blood_group == recipient, 0), else_=1), BloodUnit.expires_on, BloodUnit.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def available_count_by_group(db: Session, facility_id: int, on: date | None = None) -> dict[str, int]:
    on = on or business_today()
    rows = db.execute(
        select(BloodUnit.blood_group, func.count())
        .where(BloodUnit.facility_id == facility_id, BloodUnit.status == UnitStatus.AVAILABLE,
               BloodUnit.expires_on >= on)
        .group_by(BloodUnit.blood_group)
    ).all()
    counts = {g.value: 0 for g in BloodGroup}
    counts.update({g.value: n for g, n in rows})
    return counts


# Quarantined bags are excluded: they must leave quarantine through screening
# (reject = discard), otherwise the donation would stay "awaiting screening" forever.
DISCARDABLE = (UnitStatus.AVAILABLE, UnitStatus.EXPIRED)


def discard_unit(db: Session, unit: BloodUnit, reason: str, actor: User) -> None:
    if not actor.can_authorize:
        raise PermissionDenied("Only a medical officer or administrator can discard bags.")
    ensure_owned(unit, actor, "Bag")
    if unit.status == UnitStatus.QUARANTINE:
        raise InventoryError(f"Bag {unit.unit_code} is in quarantine. Reject it through donation screening "
                             "instead; that discards the bag and closes the screening record.")
    if unit.status not in DISCARDABLE:
        raise InventoryError(f"Bag {unit.unit_code} is {unit.status.value} and cannot be discarded.")
    if not reason or len(reason.strip()) < 3:
        raise InventoryError("A discard reason is required.")
    with transaction(db):
        unit.status = UnitStatus.DISCARDED
        unit.status_note = f"Discarded by {actor.username}: {reason.strip()[:200]}"
