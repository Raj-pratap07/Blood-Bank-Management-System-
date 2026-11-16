from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.reports import issue_report
from app.services.scope import facility_id_of
from app.web import Page, current_user, parse_date_range, render, require_facility_user

router = APIRouter(prefix="/issues", dependencies=[Depends(require_facility_user)])


@router.get("")
def issue_history(request: Request, start: str | None = None, end: str | None = None, page: int = 1,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Raw strings: blank date inputs arrive as "" and must mean "no bound", not a 422.
    rng = parse_date_range(start, end)
    return render(request, "issues/list.html", {
        "page": Page(db, issue_report(db, facility_id_of(user), rng.start, rng.end), page),
        "start": rng.raw_start, "end": rng.raw_end, "range_error": rng.error,
    }, status_code=400 if rng.error else 200)
