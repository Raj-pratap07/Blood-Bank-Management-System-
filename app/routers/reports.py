from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import BloodUnit, UnitStatus, User
from app.services import reports
from app.services.inventory import expire_overdue_units, stock_summary
from app.services.scope import facility_id_of
from app.web import current_user, parse_date_range, render, require_facility_user
from app.utils import business_today

router = APIRouter(prefix="/reports", dependencies=[Depends(require_facility_user)])


def _default_range(start: str | None, end: str | None) -> tuple[date, date, str | None]:
    """Parse raw query strings (blank inputs arrive as "") and fill missing bounds.

    Blank To -> today; blank From -> 90 days before To. An invalid or reversed
    range falls back to the default 90-day window and returns a message.
    """
    rng = parse_date_range(start, end)
    e = rng.end or business_today()
    s = rng.start or e - timedelta(days=90)
    return s, e, rng.error


@router.get("")
def index(request: Request, user: User = Depends(current_user)):
    return render(request, "reports/index.html")


@router.get("/donations")
def donation_report(request: Request, start: str | None = None, end: str | None = None,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end, range_error = _default_range(start, end)
    rows, totals = reports.donation_report(db, facility_id_of(user), start, end)
    return render(request, "reports/donations.html", {"rows": rows, "totals": totals, "start": start, "end": end,
                                                      "range_error": range_error})


@router.get("/inventory")
def inventory_report(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    expire_overdue_units(db)
    stock = stock_summary(db, facility_id_of(user))
    totals = {k: sum(r[k] for r in stock) for k in stock[0] if k != "group"}
    stmt = (select(BloodUnit).where(BloodUnit.facility_id == facility_id_of(user))
            .options(selectinload(BloodUnit.reserved_for))
            .where(BloodUnit.status.in_([UnitStatus.AVAILABLE, UnitStatus.RESERVED, UnitStatus.QUARANTINE]))
            .order_by(BloodUnit.expires_on))
    return render(request, "reports/inventory.html", {"stock": stock, "totals": totals,
                                                      "units": list(db.scalars(stmt))})


@router.get("/requests")
def request_report(request: Request, start: str | None = None, end: str | None = None,
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end, range_error = _default_range(start, end)
    rows, totals = reports.request_report(db, facility_id_of(user), start, end)
    return render(request, "reports/requests.html", {"rows": rows, "totals": totals, "start": start, "end": end,
                                                      "range_error": range_error})


@router.get("/issues")
def issues_report(request: Request, start: str | None = None, end: str | None = None,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end, range_error = _default_range(start, end)
    rows = list(db.scalars(reports.issue_report(db, facility_id_of(user), start, end)))
    return render(request, "reports/issues.html", {"rows": rows, "start": start, "end": end,
                                                   "range_error": range_error})
