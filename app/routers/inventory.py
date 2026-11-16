from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models import BloodGroup, BloodUnit, UnitStatus, User
from app.services.errors import ServiceError
from app.services.inventory import expire_overdue_units, stock_summary
from app.services.scope import facility_id_of
from app.web import Page, current_user, flash, form_dict, get_owned_or_404, redirect, render, require_authorizer, require_facility_user

router = APIRouter(prefix="/inventory", dependencies=[Depends(require_facility_user)])


@router.get("")
def inventory_summary(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    expire_overdue_units(db)
    stock = stock_summary(db, facility_id_of(user))
    totals = {k: sum(r[k] for r in stock) for k in stock[0] if k != "group"}
    return render(request, "inventory/summary.html", {"stock": stock, "totals": totals})


@router.get("/units")
def list_units(request: Request, group: str = "", status: str = "", near_expiry: bool = False, q: str = "",
               page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    expire_overdue_units(db)
    stmt = (select(BloodUnit).where(BloodUnit.facility_id == facility_id_of(user))
            .options(selectinload(BloodUnit.donation), selectinload(BloodUnit.reserved_for)))
    if group in BloodGroup:
        stmt = stmt.where(BloodUnit.blood_group == BloodGroup(group))
    if status in UnitStatus:
        stmt = stmt.where(BloodUnit.status == UnitStatus(status))
    if near_expiry:
        today = date.today()
        stmt = stmt.where(BloodUnit.status == UnitStatus.AVAILABLE, BloodUnit.expires_on >= today,
                          BloodUnit.expires_on <= today + timedelta(days=settings.near_expiry_days))
    if q:
        stmt = stmt.where(BloodUnit.unit_code.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(BloodUnit.expires_on, BloodUnit.id)  # FEFO order
    return render(request, "inventory/units.html", {
        "page": Page(db, stmt, page), "group": group, "status": status, "near_expiry": near_expiry, "q": q,
        "statuses": [s.value for s in UnitStatus]})


@router.get("/units/{unit_id}")
def unit_detail(request: Request, unit_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "inventory/unit_detail.html", {"unit": get_owned_or_404(db, BloodUnit, unit_id, user)})


@router.post("/units/{unit_id}/discard")
async def discard(request: Request, unit_id: int, user: User = Depends(require_authorizer),
                  db: Session = Depends(get_db)):
    from app.services.inventory import discard_unit

    unit = get_owned_or_404(db, BloodUnit, unit_id, user)
    raw = await form_dict(request)
    try:
        discard_unit(db, unit, str(raw.get("reason") or ""), user)
        flash(request, f"Bag {unit.unit_code} discarded.", "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/inventory/units/{unit.id}")
