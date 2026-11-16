from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Role, User
from app.routers import public
from app.services import reports
from app.services.inventory import expire_overdue_units, stock_summary
from app.services.scope import facility_id_of
from app.web import optional_user, redirect, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    if user is None:                      # anonymous visitors get the public landing page
        return public.landing(request, db)
    if user.must_change_password:
        return redirect("/account/password")
    if user.role == Role.PLATFORM_ADMIN:  # no operational data for platform administrators
        return redirect("/platform/facilities")
    expired_now = expire_overdue_units(db)
    return render(request, "dashboard.html", {
        "counts": reports.dashboard_counts(db, facility_id_of(user)),
        "stock": stock_summary(db, facility_id_of(user)),
        "recent": reports.recent_donations(db, facility_id_of(user)),
        "expired_now": expired_now,
    })
