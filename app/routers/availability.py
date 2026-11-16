"""Facility availability publishing (Phase 4). Facility roles only; platform admins are refused.

Internal stock (eligible bags) is shown to the facility; nothing is public until an
administrator or medical officer explicitly publishes, and only while it is current.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import TRACKED_PRODUCTS, BloodGroup, Facility, StockVisibility, User
from app.services import availability as svc
from app.services.errors import ServiceError
from app.services.scope import facility_id_of
from app.web import (
    current_user,
    flash,
    form_dict,
    redirect,
    render,
    require_admin,
    require_authorizer,
    require_facility_user,
)

router = APIRouter(prefix="/availability", dependencies=[Depends(require_facility_user)])


@router.get("")
def availability_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    fid = facility_id_of(user)
    facility = db.get(Facility, fid)
    counts = svc.eligible_counts(db, fid)
    latest = svc.latest_publication(db, fid)
    return render(request, "availability/index.html", {
        "facility": facility, "counts": counts, "groups": list(BloodGroup), "tracked": TRACKED_PRODUCTS,
        "latest": latest, "state": svc.publication_state(latest), "public": svc.public_view(db, facility),
        "history": svc.history(db, user), "validity_hours": svc.validity_hours(facility),
        "max_hours": settings.availability_max_hours, "low_max": settings.availability_low_max,
        "visibilities": list(StockVisibility),
    })


@router.post("/publish")
def publish(request: Request, user: User = Depends(require_authorizer), db: Session = Depends(get_db)):
    try:
        pub = svc.publish(db, user)
        flash(request, f"Availability published. It stays public until the validity window ends "
                       f"({svc.validity_hours(db.get(Facility, pub.facility_id))} h) or you withdraw it.")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect("/availability")


@router.post("/withdraw")
def withdraw(request: Request, user: User = Depends(require_authorizer), db: Session = Depends(get_db)):
    try:
        svc.withdraw(db, user)
        flash(request, "Availability withdrawn. The public now sees 'not published'.", "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect("/availability")


@router.post("/settings")
async def update_settings(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    raw = await form_dict(request)
    try:
        visibility = StockVisibility(str(raw.get("stock_visibility") or ""))
        hours = int(str(raw.get("stock_max_age_hours") or "0"))
    except ValueError:
        flash(request, "Choose a visibility option and a whole number of hours.", "danger")
        return redirect("/availability")
    try:
        svc.update_settings(db, user, visibility, hours)
        flash(request, "Availability settings saved.")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect("/availability")
