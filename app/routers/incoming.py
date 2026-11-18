"""Facility staff: review public (anonymous) requests for THEIR OWN facility and decide them.

Accepting creates a normal PENDING staff request; medical approval, reservation and issue then happen in the
existing /requests workflow with all its rules. Requests of other facilities are invisible (404).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Gender, RequestPriority, User
from app.schemas.forms import PublicAcceptIn, PublicDecisionIn
from app.services import public_requests as prsvc
from app.services.errors import RecordNotFound, ServiceError
from app.web import current_user, flash, form_dict, redirect, render, require_facility_user, validate

router = APIRouter(prefix="/incoming-requests", dependencies=[Depends(require_facility_user)])


def _get(db: Session, req_id: int, user: User):
    try:
        return prsvc.get_for_staff(db, req_id, user)
    except RecordNotFound:
        raise HTTPException(status_code=404, detail="Not found") from None


def _detail(request: Request, req, form: dict | None = None, errors: dict | None = None, status_code: int = 200):
    return render(request, "incoming/detail.html", {
        "req": req, "status": prsvc.public_status(req), "form": form or {}, "errors": errors or {},
        "genders": [(g.value, g.value.title()) for g in Gender],
        "priorities": [(p.value, p.value.title()) for p in RequestPriority],
        "labels": prsvc.TIMELINE_LABELS}, status_code=status_code)


@router.get("")
def list_incoming(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reqs = prsvc.incoming(db, user)
    return render(request, "incoming/list.html", {"reqs": reqs, "status_of": prsvc.public_status})


@router.get("/{req_id}")
def detail(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _detail(request, _get(db, req_id, user))


@router.post("/{req_id}/accept")
async def accept(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = _get(db, req_id, user)
    raw = await form_dict(request)
    data, errors = validate(PublicAcceptIn, dict(raw))
    if errors:
        return _detail(request, req, raw, errors, 400)
    try:
        br = prsvc.accept(db, req, data, user)
    except ServiceError as e:
        return _detail(request, req, raw, {"__all__": e.message}, 400)
    flash(request, f"{req.reference} accepted as staff request {br.code}. A medical officer must now approve it.")
    return redirect(f"/requests/{br.id}")


@router.post("/{req_id}/{action}")
async def decide(request: Request, req_id: int, action: str, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    if action not in ("reject", "clarify"):
        raise HTTPException(status_code=404, detail="Not found")
    req = _get(db, req_id, user)
    raw = await form_dict(request)
    data, errors = validate(PublicDecisionIn, dict(raw))
    if errors:
        return _detail(request, req, raw, {f"{action}_message": errors.get("message", "Invalid message.")}, 400)
    try:
        prsvc.decide(db, req, action, data, user)
    except ServiceError as e:
        return _detail(request, req, raw, {"__all__": e.message}, 400)
    flash(request, f"{req.reference}: " + ("rejected." if action == "reject" else "clarification requested."))
    return redirect(f"/incoming-requests/{req.id}")
