from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import BloodGroup, BloodRequest, RequestPriority, RequestStatus, User
from app.models.enums import OPEN_REQUEST_STATUSES
from app.schemas.forms import BloodRequestIn, IssueIn
from app.services import requests as req_svc
from app.services.compatibility import compatible_donor_groups
from app.services.errors import ServiceError
from app.services.inventory import available_count_by_group, expire_overdue_units
from app.services.scope import facility_id_of
from app.web import Page, current_user, flash, form_dict, get_owned_or_404, redirect, render, require_authorizer, require_facility_user, validate

router = APIRouter(prefix="/requests", dependencies=[Depends(require_facility_user)])

PRIORITY_ORDER = case(
    (BloodRequest.priority == RequestPriority.EMERGENCY, 0),
    (BloodRequest.priority == RequestPriority.URGENT, 1), else_=2)


@router.get("")
def list_requests(request: Request, status: str = "", priority: str = "", group: str = "", q: str = "",
                  page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = (select(BloodRequest).where(BloodRequest.facility_id == facility_id_of(user))
            .options(selectinload(BloodRequest.issues), selectinload(BloodRequest.reserved_units)))
    if status == "open":
        stmt = stmt.where(BloodRequest.status.in_(OPEN_REQUEST_STATUSES))
    elif status in RequestStatus:
        stmt = stmt.where(BloodRequest.status == RequestStatus(status))
    if priority in RequestPriority:
        stmt = stmt.where(BloodRequest.priority == RequestPriority(priority))
    if group in BloodGroup:
        stmt = stmt.where(BloodRequest.blood_group == BloodGroup(group))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(BloodRequest.patient_name.ilike(like) | BloodRequest.hospital_name.ilike(like))
    stmt = stmt.order_by(PRIORITY_ORDER, BloodRequest.created_at.desc())
    return render(request, "requests/list.html", {
        "page": Page(db, stmt, page), "status": status, "priority": priority, "group": group, "q": q,
        "statuses": [s.value for s in RequestStatus], "priorities": [p.value for p in RequestPriority]})


@router.get("/new")
def new_request_form(request: Request, user: User = Depends(current_user)):
    return render(request, "requests/form.html", {"form": {"priority": "routine", "units_required": 1}, "errors": {}})


@router.post("/new")
async def create_request(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw = await form_dict(request)
    data, errors = validate(BloodRequestIn, dict(raw))
    if not errors:
        req = req_svc.create_request(db, data, user)
        flash(request, f"Request {req.code} registered and awaiting medical approval.")
        return redirect(f"/requests/{req.id}")
    return render(request, "requests/form.html", {"form": raw, "errors": errors}, status_code=400)


def _detail_ctx(db: Session, req: BloodRequest, errors: dict | None = None, form: dict | None = None) -> dict:
    counts = available_count_by_group(db, req.facility_id)
    groups = compatible_donor_groups(req.blood_group)
    return {"req": req, "errors": errors or {}, "form": form or {},
            "compatible": [(g.value, counts[g.value], g == req.blood_group) for g in groups],
            "compatible_total": sum(counts[g.value] for g in groups)}


@router.get("/{req_id}")
def request_detail(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    expire_overdue_units(db)
    req = get_owned_or_404(db, BloodRequest, req_id, user)
    db.refresh(req)
    return render(request, "requests/detail.html", _detail_ctx(db, req))


@router.post("/{req_id}/approve")
async def approve(request: Request, req_id: int, user: User = Depends(require_authorizer),
                  db: Session = Depends(get_db)):
    req = get_owned_or_404(db, BloodRequest, req_id, user)
    raw = await form_dict(request)
    try:
        n = req_svc.approve_and_reserve(db, req, user, note=raw.get("note"))
        msg = f"{n} bag(s) reserved for {req.code}."
        if req.units_outstanding:
            msg += f" {req.units_outstanding} unit(s) still outstanding — reserve more when stock arrives."
        flash(request, msg, "success" if not req.units_outstanding else "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/requests/{req.id}")


@router.post("/{req_id}/reject")
async def reject(request: Request, req_id: int, user: User = Depends(require_authorizer),
                 db: Session = Depends(get_db)):
    req = get_owned_or_404(db, BloodRequest, req_id, user)
    raw = await form_dict(request)
    try:
        req_svc.reject_request(db, req, user, str(raw.get("reason") or ""))
        flash(request, f"Request {req.code} rejected; reservations released.", "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/requests/{req.id}")


@router.post("/{req_id}/cancel")
async def cancel(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = get_owned_or_404(db, BloodRequest, req_id, user)
    raw = await form_dict(request)
    try:
        req_svc.cancel_request(db, req, user, raw.get("reason"))
        flash(request, f"Request {req.code} cancelled; reservations released.", "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/requests/{req.id}")


@router.post("/{req_id}/issue")
async def issue(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = get_owned_or_404(db, BloodRequest, req_id, user)
    raw = await form_dict(request)
    raw.setdefault("unit_ids", [])
    data, errors = validate(IssueIn, dict(raw))
    if not errors:
        try:
            issued = req_svc.issue_units(db, req, data, user)
            flash(request, f"{len(issued)} bag(s) issued for {req.code}. Status: {req.status.value.replace('_', ' ')}.")
            return redirect(f"/requests/{req.id}")
        except ServiceError as e:
            errors["__all__"] = e.message
    db.refresh(req)
    return render(request, "requests/detail.html", _detail_ctx(db, req, errors, raw), status_code=400)
