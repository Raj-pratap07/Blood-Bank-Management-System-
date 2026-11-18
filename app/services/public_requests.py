"""Public (anonymous) emergency blood requests: submission, secure tracking and facility-scoped staff review.

Rules (Phase 6):
- Only the fictional, participating DEMO facility can receive requests, and only in demo mode (the database also
  refuses requests for non-participating facilities). Real Haldwani facilities never receive them.
- A request never reserves blood. Accepting it creates a normal PENDING staff BloodRequest (patient details
  entered by staff); medical approval, FEFO reservation, crossmatch-confirmed issue and all concurrency and
  facility-isolation rules then apply unchanged.
- Tracking needs the reference AND the one-time tracking code. Only a SHA-256 hash of the code is stored; it is
  compared in constant time and never logged or put in a URL.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    BloodRequest,
    Facility,
    OperationalStatus,
    Product,
    PublicRequest,
    PublicRequestEvent,
    PublicRequestStatus,
    RequestStatus,
    User,
)
from app.schemas.forms import BloodRequestIn, PublicAcceptIn, PublicDecisionIn, PublicRequestIn
from app.services import requests as req_svc
from app.services.errors import RecordNotFound, ServiceError
from app.services.scope import ensure_owned, facility_id_of
from app.services.tx import transaction
from app.utils import to_business, utcnow

S = PublicRequestStatus
OPEN = (S.SUBMITTED, S.CLARIFICATION_REQUESTED)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O or 1/I: easy to read out over the phone


# ------------------------------------------------------------------ eligibility
def eligible_facilities(db: Session) -> list[Facility]:
    """Facilities that may receive public requests: only the fictional demo facility, only in demo mode."""
    if not settings.demo_mode:
        return []
    return list(db.scalars(select(Facility).where(
        Facility.is_demo.is_(True), Facility.is_participating.is_(True),
        Facility.operational_status != OperationalStatus.CLOSED)))


# ------------------------------------------------------------------ codes
def _code(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def token_hash(token: str) -> str:
    return hashlib.sha256(normalise_token(token).encode("utf-8")).hexdigest()


def normalise_token(token: str) -> str:
    return "".join(ch for ch in (token or "").upper() if ch.isalnum())


def normalise_reference(ref: str) -> str:
    ref = "".join(ch for ch in (ref or "").upper() if ch.isalnum())
    return f"PR-{ref[2:]}" if ref.startswith("PR") else ref


def _new_reference(db: Session) -> str:
    for _ in range(20):
        ref = f"PR-{_code(6)}"
        if db.scalar(select(PublicRequest.id).where(PublicRequest.reference == ref)) is None:
            return ref
    raise ServiceError("Could not allocate a request reference; please try again.")


# ------------------------------------------------------------------ public side
def submit(db: Session, data: PublicRequestIn) -> tuple[PublicRequest, str]:
    """Create a public request. Returns (request, tracking_code). The code is shown ONCE and only its hash is kept."""
    facility = next((f for f in eligible_facilities(db) if f.id == data.facility_id), None)
    if facility is None:
        raise ServiceError("Online requests are not accepted by this blood bank. Please contact it directly.")
    token = "-".join(_code(4) for _ in range(4))       # 16 symbols from 32 = 80 bits
    with transaction(db):
        req = PublicRequest(reference=_new_reference(db), token_hash=token_hash(token), facility_id=facility.id,
                            blood_group=data.blood_group, product=data.product, units=data.units,
                            required_by=data.required_by, location=data.location,
                            requester_name=data.requester_name, contact_phone=data.contact_phone, note=data.note,
                            status=S.SUBMITTED)
        db.add(req)
        db.flush()
        db.add(PublicRequestEvent(public_request_id=req.id, facility_id=facility.id, actor_id=None,
                                  action="submitted", to_status=S.SUBMITTED.value))
    return req, token


def track(db: Session, reference: str, token: str) -> PublicRequest | None:
    """The request only if BOTH the reference and the tracking code match; otherwise None (no hint which failed)."""
    req = db.scalar(select(PublicRequest).where(PublicRequest.reference == normalise_reference(reference)))
    expected = req.token_hash if req is not None else "0" * 64
    ok = hmac.compare_digest(token_hash(token), expected)      # constant time, also when the reference is unknown
    return req if (req is not None and ok) else None


@dataclass
class PublicStatus:
    key: str
    label: str
    detail: str


def public_status(req: PublicRequest) -> PublicStatus:
    """Status shown to the requester. After acceptance it follows the linked staff request."""
    if req.status == S.SUBMITTED:
        return PublicStatus("submitted", "Received", "Waiting for review by the blood bank.")
    if req.status == S.CLARIFICATION_REQUESTED:
        return PublicStatus("clarification", "More information needed",
                            "The blood bank needs more information. Please call the blood bank directly.")
    if req.status == S.REJECTED:
        return PublicStatus("rejected", "Not accepted", "The blood bank could not accept this request online.")
    br = req.blood_request
    st = br.status if br is not None else RequestStatus.PENDING
    return {
        RequestStatus.PENDING: PublicStatus("accepted", "Accepted", "Accepted by the blood bank; awaiting medical approval."),
        RequestStatus.APPROVED: PublicStatus("approved", "Approved", "Medically approved; compatible bags are being prepared."),
        RequestStatus.PARTIALLY_ISSUED: PublicStatus("partially_issued", "Partially issued",
                                                     "Some of the requested units have been issued."),
        RequestStatus.FULFILLED: PublicStatus("fulfilled", "Fulfilled", "All requested units have been issued."),
        RequestStatus.REJECTED: PublicStatus("not_approved", "Not approved", "The medical officer did not approve it."),
        RequestStatus.CANCELLED: PublicStatus("cancelled", "Cancelled", "The request was cancelled by the blood bank."),
    }[st]


TIMELINE_LABELS = {"submitted": "Request received", "accepted": "Accepted by the blood bank",
                   "rejected": "Not accepted", "clarification_requested": "More information requested"}


# ------------------------------------------------------------------ staff side (own facility only)
def incoming(db: Session, actor: User) -> list[PublicRequest]:
    return list(db.scalars(select(PublicRequest).where(PublicRequest.facility_id == facility_id_of(actor))
                           .order_by(PublicRequest.created_at.desc())))


def get_for_staff(db: Session, req_id: int, actor: User) -> PublicRequest:
    req = db.get(PublicRequest, req_id)
    if req is None or req.facility_id != facility_id_of(actor):
        raise RecordNotFound("Request not found.")        # same answer for other facilities' requests
    return req


def _claim(db: Session, req: PublicRequest, new_status: PublicRequestStatus, message: str | None) -> None:
    """Conditional UPDATE: succeeds only if the request is still open, so two staff cannot both decide it."""
    res = db.execute(update(PublicRequest)
                     .where(PublicRequest.id == req.id, PublicRequest.facility_id == req.facility_id,
                            PublicRequest.status.in_(OPEN))
                     .values(status=new_status, public_message=message, updated_at=utcnow())
                     .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise ServiceError(f"{req.reference} has already been decided by another user. Reload the page.")


def accept(db: Session, req: PublicRequest, data: PublicAcceptIn, actor: User) -> BloodRequest:
    """Turn the public request into a PENDING staff request (same transaction). Whole blood only."""
    ensure_owned(req, actor, "Request")
    if req.product != Product.WHOLE_BLOOD:
        raise ServiceError("Only whole blood is held in inventory here. Reject this request or ask for "
                           "clarification, and direct the requester to a blood bank that stocks this component.")
    if req.status not in OPEN:
        raise ServiceError(f"{req.reference} is no longer open.")
    staff_form = BloodRequestIn(
        patient_name=data.patient_name, patient_age=data.patient_age, patient_gender=data.patient_gender,
        blood_group=req.blood_group, units_required=req.units, priority=data.priority,
        hospital_name=req.location[:120], requester_name=req.requester_name, requester_phone=req.contact_phone,
        reason=f"Public request {req.reference}" + (f": {req.note}" if req.note else ""),
        required_by=to_business(req.required_by).date())
    with transaction(db):
        _claim(db, req, S.ACCEPTED, None)
        br = req_svc.new_request(db, staff_form, actor)
        db.flush()
        db.execute(update(PublicRequest).where(PublicRequest.id == req.id).values(blood_request_id=br.id)
                   .execution_options(synchronize_session=False))
        db.add(PublicRequestEvent(public_request_id=req.id, facility_id=req.facility_id, actor_id=actor.id,
                                  action="accepted", to_status=S.ACCEPTED.value, note=f"Staff request {br.code}"))
    db.refresh(req)
    return br


def decide(db: Session, req: PublicRequest, action: str, data: PublicDecisionIn, actor: User) -> None:
    """Reject, or ask the requester for clarification (they are told to call the blood bank)."""
    ensure_owned(req, actor, "Request")
    new_status = {"reject": S.REJECTED, "clarify": S.CLARIFICATION_REQUESTED}[action]
    with transaction(db):
        _claim(db, req, new_status, data.message)
        db.add(PublicRequestEvent(public_request_id=req.id, facility_id=req.facility_id, actor_id=actor.id,
                                  action=new_status.value, to_status=new_status.value, note=data.message))
    db.refresh(req)
