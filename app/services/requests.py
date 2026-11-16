"""Blood requests: registration, authorised approval + reservation, issue, cancel/reject.

Workflow:  PENDING --(MO/admin approves: reserves bags FEFO)--> APPROVED
           APPROVED --(issue reserved bags after crossmatch)--> PARTIALLY_ISSUED / FULFILLED
           PENDING/APPROVED/PARTIALLY_ISSUED --> REJECTED (MO/admin) or CANCELLED (any user)
Reservations are released back to AVAILABLE stock on reject/cancel.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import BloodRequest, BloodUnit, Issue, RequestStatus, UnitStatus, User
from app.models.enums import OPEN_REQUEST_STATUSES
from app.schemas.forms import BloodRequestIn, IssueIn
from app.services.compatibility import is_compatible
from app.services.errors import InventoryError, PermissionDenied, ServiceError
from app.services.inventory import expire_overdue_units, fefo_candidates
from app.services.scope import ensure_owned, facility_id_of
from app.services.tx import transaction
from app.utils import utcnow


def create_request(db: Session, data: BloodRequestIn, actor: User) -> BloodRequest:
    fid = facility_id_of(actor)                     # never taken from the submitted form
    with transaction(db):
        req = BloodRequest(**data.model_dump(), facility_id=fid, status=RequestStatus.PENDING, created_by=actor)
        db.add(req)
    return req


def _issued_count(db: Session, req: BloodRequest) -> int:
    return db.scalar(select(func.count()).select_from(Issue).where(Issue.request_id == req.id)) or 0


def _reserved_count(db: Session, req: BloodRequest) -> int:
    return db.scalar(select(func.count()).select_from(BloodUnit).where(
        BloodUnit.reserved_for_request_id == req.id, BloodUnit.status == UnitStatus.RESERVED)) or 0


def _claim_request(db: Session, req: BloodRequest, allowed: tuple[RequestStatus, ...], action: str,
                   actor: User) -> RequestStatus:
    """First statement of every state-changing transaction on a request.

    A no-op conditional UPDATE matches only if the request is *still* in an allowed
    status. It takes SQLite's write lock, so no other writer can change the request
    until this transaction ends; everything read afterwards is current. Without it,
    a status read before the transaction could be stale (e.g. approving a request
    another user had just cancelled). Returns the current status.
    """
    fid = facility_id_of(actor)
    res = db.execute(
        update(BloodRequest)
        .where(BloodRequest.id == req.id, BloodRequest.facility_id == fid, BloodRequest.status.in_(allowed))
        .values(status=BloodRequest.status)
        .execution_options(synchronize_session=False)
    )
    current = db.scalar(select(BloodRequest.status).where(BloodRequest.id == req.id))
    if res.rowcount != 1:
        raise ServiceError(f"{req.code} is now {current.value.replace('_', ' ')} (changed by another user), "
                           f"so it cannot be {action}. Reload the page.")
    return current


def approve_and_reserve(db: Session, req: BloodRequest, actor: User, note: str | None = None,
                        on: date | None = None) -> int:
    """Authorised approval: reserve compatible bags (identical group first, then FEFO).

    Can be repeated on an approved request to top up a partial reservation.
    Returns the number of bags reserved by this call.
    """
    if not actor.can_authorize:
        raise PermissionDenied("Only a medical officer or administrator can approve requests.")
    ensure_owned(req, actor, "Request")
    if req.status not in OPEN_REQUEST_STATUSES:
        raise ServiceError(f"Request is {req.status.value} and cannot be approved.")
    on = on or date.today()
    expire_overdue_units(db, on)

    with transaction(db):
        current = _claim_request(db, req, OPEN_REQUEST_STATUSES, "approved", actor)
        needed = req.units_required - _issued_count(db, req) - _reserved_count(db, req)
        if needed <= 0:
            raise InventoryError("Enough bags are already reserved or issued for this request.")
        candidates = fefo_candidates(db, req.facility_id, req.blood_group, on, limit=needed)  # own stock only
        if not candidates:
            raise InventoryError(f"No unexpired, compatible bags available for {req.blood_group.value}.")
        for unit in candidates:
            if not is_compatible(req.blood_group, unit.blood_group):  # defensive re-check
                raise InventoryError(f"Bag {unit.unit_code} is not ABO/Rh compatible.")
            # Conditional update: succeeds only if the bag is *still* available and unexpired,
            # so two concurrent approvals can never reserve the same bag.
            res = db.execute(
                update(BloodUnit)
                .where(BloodUnit.id == unit.id, BloodUnit.facility_id == req.facility_id,
                       BloodUnit.status == UnitStatus.AVAILABLE, BloodUnit.expires_on >= on)
                .values(status=UnitStatus.RESERVED, reserved_for_request_id=req.id,
                        status_note=f"Reserved for {req.code}")
                .execution_options(synchronize_session="fetch")
            )
            if res.rowcount != 1:
                raise InventoryError(f"Bag {unit.unit_code} is no longer available; please retry.")
        if current == RequestStatus.PENDING:
            req.status = RequestStatus.APPROVED
            req.approved_by = actor
            req.approved_at = utcnow()
            req.decision_note = note or None
    db.refresh(req)
    return len(candidates)


def _release_reservations(db: Session, req: BloodRequest) -> None:
    db.execute(
        update(BloodUnit)
        .where(BloodUnit.reserved_for_request_id == req.id, BloodUnit.status == UnitStatus.RESERVED)
        .values(status=UnitStatus.AVAILABLE, reserved_for_request_id=None, status_note=f"Released from {req.code}")
        .execution_options(synchronize_session="fetch")
    )


def reject_request(db: Session, req: BloodRequest, actor: User, reason: str) -> None:
    if not actor.can_authorize:
        raise PermissionDenied("Only a medical officer or administrator can reject requests.")
    ensure_owned(req, actor, "Request")
    if req.status not in OPEN_REQUEST_STATUSES:
        raise ServiceError(f"Request is {req.status.value} and cannot be rejected.")
    if not reason or len(reason.strip()) < 3:
        raise ServiceError("A rejection reason is required.")
    with transaction(db):
        _claim_request(db, req, OPEN_REQUEST_STATUSES, "rejected", actor)
        if _issued_count(db, req):  # checked under the write lock
            raise ServiceError("Bags have already been issued for this request, so it cannot be rejected. "
                               "Use Cancel to close the remaining units.")
        _release_reservations(db, req)
        req.status = RequestStatus.REJECTED
        req.decision_note = reason.strip()
        req.rejected_by = actor          # approved_by/approved_at (if any) are preserved
        req.rejected_at = utcnow()
    db.refresh(req)


def cancel_request(db: Session, req: BloodRequest, actor: User, reason: str | None = None) -> None:
    ensure_owned(req, actor, "Request")
    if req.status not in OPEN_REQUEST_STATUSES:
        raise ServiceError(f"Request is {req.status.value} and cannot be cancelled.")
    with transaction(db):
        _claim_request(db, req, OPEN_REQUEST_STATUSES, "cancelled", actor)
        _release_reservations(db, req)
        req.status = RequestStatus.CANCELLED
        req.decision_note = (reason or "").strip() or f"Cancelled by {actor.username}"
    db.refresh(req)


def issue_units(db: Session, req: BloodRequest, data: IssueIn, actor: User, on: date | None = None) -> list[Issue]:
    """Issue reserved bags. All-or-nothing: any failure rolls back every change."""
    ensure_owned(req, actor, "Request")
    if req.status not in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_ISSUED):
        raise ServiceError("Bags can only be issued for a request approved by a medical officer.")
    if not data.crossmatch_confirmed:
        raise ServiceError("Laboratory crossmatch must be confirmed before issue.")
    on = on or date.today()
    expire_overdue_units(db, on)

    issues: list[Issue] = []
    with transaction(db):
        _claim_request(db, req, (RequestStatus.APPROVED, RequestStatus.PARTIALLY_ISSUED), "issued", actor)
        issued_before = _issued_count(db, req)
        if issued_before + len(set(data.unit_ids)) > req.units_required:
            raise InventoryError("Issuing these bags would exceed the units required.")
        units = list(db.scalars(
            select(BloodUnit).where(BloodUnit.id.in_(data.unit_ids), BloodUnit.facility_id == req.facility_id)
            .order_by(BloodUnit.expires_on, BloodUnit.id)
            .execution_options(populate_existing=True)  # current values, not stale identity-map copies
        ))
        if len(units) != len(set(data.unit_ids)):
            raise InventoryError("One or more selected bags do not exist.")
        for unit in units:
            if unit.reserved_for_request_id != req.id or unit.status != UnitStatus.RESERVED:
                raise InventoryError(f"Bag {unit.unit_code} is not reserved for {req.code}.")
            if unit.is_expired(on):
                raise InventoryError(f"Bag {unit.unit_code} expired on {unit.expires_on:%d %b %Y}.")
            if not is_compatible(req.blood_group, unit.blood_group):
                raise InventoryError(f"Bag {unit.unit_code} ({unit.blood_group.value}) is not compatible "
                                     f"with {req.blood_group.value}.")
            res = db.execute(
                update(BloodUnit)
                .where(BloodUnit.id == unit.id, BloodUnit.status == UnitStatus.RESERVED,
                       BloodUnit.reserved_for_request_id == req.id)
                .values(status=UnitStatus.ISSUED, reserved_for_request_id=None, status_note=f"Issued to {req.code}")
                .execution_options(synchronize_session="fetch")
            )
            if res.rowcount != 1:
                raise InventoryError(f"Bag {unit.unit_code} changed state during issue; please retry.")
            issue = Issue(facility_id=req.facility_id, request_id=req.id, blood_unit_id=unit.id, issued_by=actor,
                          received_by=data.received_by, crossmatch_confirmed=True, notes=data.notes)
            db.add(issue)
            issues.append(issue)
        db.flush()
        total = issued_before + len(issues)
        req.status = RequestStatus.FULFILLED if total >= req.units_required else RequestStatus.PARTIALLY_ISSUED
    db.refresh(req)
    return issues
