"""Small read-only JSON API used by dashboard charts and live donor search."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import reports
from app.services.donors import search_donors
from app.services.eligibility import check_donor
from app.services.inventory import expire_overdue_units, stock_summary
from app.web import current_user

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


@router.get("/stats/inventory")
def inventory_stats(db: Session = Depends(get_db)):
    # Same expiry sweep as the dashboard and inventory pages, so a direct API call never
    # reports bags past their expiry date as available.
    expire_overdue_units(db)
    stock = stock_summary(db)
    return {
        "labels": [r["group"] for r in stock],
        "available": [r["available"] for r in stock],
        "reserved": [r["reserved"] for r in stock],
        "quarantine": [r["quarantine"] for r in stock],
        "near_expiry": [r["near_expiry"] for r in stock],
    }


@router.get("/stats/donations-trend")
def donations_trend(months: int = 6, db: Session = Depends(get_db)):
    return reports.donations_by_month(db, months=min(max(months, 1), 24))


@router.get("/stats/requests-by-status")
def requests_by_status(db: Session = Depends(get_db)):
    return reports.requests_by_status(db)


@router.get("/donors/search")
def donor_search(q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    if len(q.strip()) < 2:
        return []
    donors = list(db.scalars(search_donors(db, q).limit(10)))
    return [{"id": d.id, "name": d.full_name, "phone": d.phone, "blood_group": d.blood_group.value,
             "eligible": check_donor(d).eligible} for d in donors]
