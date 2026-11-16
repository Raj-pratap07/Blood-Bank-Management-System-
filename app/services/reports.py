"""Read-only aggregates for the dashboard, JSON API and reports.

Every function takes the caller's facility_id and only ever reads that facility's rows.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    BloodRequest,
    BloodUnit,
    Donation,
    DonationStatus,
    Donor,
    Issue,
    RequestStatus,
    UnitStatus,
)
from app.models.enums import OPEN_REQUEST_STATUSES


def dashboard_counts(db: Session, facility_id: int, on: date | None = None) -> dict:
    from app.config import settings

    on = on or date.today()
    near = on + timedelta(days=settings.near_expiry_days)

    def count(model, *conds):
        return db.scalar(select(func.count()).select_from(model).where(model.facility_id == facility_id, *conds)) or 0

    return {
        "donors": count(Donor),  # total registered (incl. inactive)
        "available": count(BloodUnit, BloodUnit.status == UnitStatus.AVAILABLE, BloodUnit.expires_on >= on),
        "reserved": count(BloodUnit, BloodUnit.status == UnitStatus.RESERVED),
        "quarantine": count(BloodUnit, BloodUnit.status == UnitStatus.QUARANTINE),
        "near_expiry": count(BloodUnit, BloodUnit.status == UnitStatus.AVAILABLE, BloodUnit.expires_on >= on,
                             BloodUnit.expires_on <= near),
        "pending_requests": count(BloodRequest, BloodRequest.status == RequestStatus.PENDING),
        "open_requests": count(BloodRequest, BloodRequest.status.in_(OPEN_REQUEST_STATUSES)),
        "pending_screening": count(Donation, Donation.status == DonationStatus.QUARANTINE),
    }


def recent_donations(db: Session, facility_id: int, limit: int = 8) -> list[Donation]:
    return list(db.scalars(select(Donation).where(Donation.facility_id == facility_id)
                           .options(selectinload(Donation.donor))
                           .order_by(Donation.donation_date.desc(), Donation.id.desc()).limit(limit)))


def donations_by_month(db: Session, facility_id: int, months: int = 6, on: date | None = None) -> dict:
    on = on or date.today()
    # first day of the month `months-1` months ago
    y, m = on.year, on.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    rows = dict(db.execute(
        select(func.strftime("%Y-%m", Donation.donation_date), func.count())
        .where(Donation.facility_id == facility_id, Donation.donation_date >= start)
        .group_by(func.strftime("%Y-%m", Donation.donation_date))
    ).all())
    labels, values = [], []
    for i in range(months):
        mm, yy = (m + i - 1) % 12 + 1, y + (m + i - 1) // 12
        key = f"{yy:04d}-{mm:02d}"
        labels.append(date(yy, mm, 1).strftime("%b %Y"))
        values.append(rows.get(key, 0))
    return {"labels": labels, "values": values}


def requests_by_status(db: Session, facility_id: int) -> dict:
    rows = dict(db.execute(select(BloodRequest.status, func.count())
                           .where(BloodRequest.facility_id == facility_id).group_by(BloodRequest.status)).all())
    return {"labels": [s.value.replace("_", " ").title() for s in RequestStatus],
            "values": [rows.get(s, 0) for s in RequestStatus]}


def _range(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    return (datetime.combine(start, time.min) if start else None,
            datetime.combine(end, time.max) if end else None)


def donation_report(db: Session, facility_id: int, start: date | None, end: date | None):
    stmt = (select(Donation).where(Donation.facility_id == facility_id)
            .options(selectinload(Donation.donor), selectinload(Donation.units)))
    if start:
        stmt = stmt.where(Donation.donation_date >= start)
    if end:
        stmt = stmt.where(Donation.donation_date <= end)
    rows = list(db.scalars(stmt.order_by(Donation.donation_date.desc(), Donation.id.desc())))
    totals = {s.value: sum(1 for r in rows if r.status == s) for s in DonationStatus}
    totals["volume_ml"] = sum(r.volume_ml for r in rows)
    return rows, totals


def request_report(db: Session, facility_id: int, start: date | None, end: date | None):
    s, e = _range(start, end)
    stmt = (select(BloodRequest).where(BloodRequest.facility_id == facility_id)
            .options(selectinload(BloodRequest.issues), selectinload(BloodRequest.reserved_units)))
    if s:
        stmt = stmt.where(BloodRequest.created_at >= s)
    if e:
        stmt = stmt.where(BloodRequest.created_at <= e)
    rows = list(db.scalars(stmt.order_by(BloodRequest.created_at.desc())))
    totals = {st.value: sum(1 for r in rows if r.status == st) for st in RequestStatus}
    totals["units_required"] = sum(r.units_required for r in rows)
    totals["units_issued"] = sum(r.units_issued for r in rows)
    return rows, totals


def issue_report(db: Session, facility_id: int, start: date | None, end: date | None):
    s, e = _range(start, end)
    stmt = select(Issue).where(Issue.facility_id == facility_id).options(
        selectinload(Issue.blood_unit), selectinload(Issue.request),
                                 selectinload(Issue.issued_by))
    if s:
        stmt = stmt.where(Issue.issued_at >= s)
    if e:
        stmt = stmt.where(Issue.issued_at <= e)
    return stmt.order_by(Issue.issued_at.desc())
