from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import reports
from app.services.inventory import expire_overdue_units, stock_summary
from app.web import current_user, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    expired_now = expire_overdue_units(db)
    return render(request, "dashboard.html", {
        "counts": reports.dashboard_counts(db),
        "stock": stock_summary(db),
        "recent": reports.recent_donations(db),
        "expired_now": expired_now,
    })
