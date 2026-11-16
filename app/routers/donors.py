from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Donor, User
from app.schemas.forms import DonorIn
from app.services import donors as donor_svc
from app.services.eligibility import check_donor
from app.services.errors import ServiceError
from app.services.scope import facility_id_of
from app.web import Page, current_user, flash, form_dict, get_owned_or_404, redirect, render, require_authorizer, require_facility_user, validate

router = APIRouter(prefix="/donors", dependencies=[Depends(require_facility_user)])


@router.get("")
def list_donors(request: Request, q: str = "", group: str = "", eligible: str = "", inactive: bool = False,
                page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = donor_svc.search_donors(db, facility_id_of(user), q, group, include_inactive=inactive)
    if eligible in ("yes", "no"):
        # Eligibility is computed in Python (depends on dates + history), then paginated.
        all_donors = list(db.scalars(stmt))
        want = eligible == "yes"
        matched = [d for d in all_donors if check_donor(d).eligible is want]
        size = 20
        pages = max(1, -(-len(matched) // size))
        page = min(max(page, 1), pages)
        pg = type("P", (), {"items": matched[(page - 1) * size: page * size], "page": page,
                            "pages": pages, "total": len(matched)})()
    else:
        pg = Page(db, stmt, page)
    rows = [(d, check_donor(d)) for d in pg.items]
    return render(request, "donors/list.html", {"page": pg, "rows": rows, "q": q, "group": group,
                                                "eligible": eligible, "inactive": inactive})


@router.get("/new")
def new_donor_form(request: Request, user: User = Depends(current_user)):
    return render(request, "donors/form.html", {"form": {}, "errors": {}, "donor": None})


@router.post("/new")
async def create_donor(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw = await form_dict(request)
    data, errors = validate(DonorIn, dict(raw))
    if not errors:
        try:
            donor = donor_svc.create_donor(db, data, user)
            flash(request, f"Donor {donor.full_name} registered.")
            return redirect(f"/donors/{donor.id}")
        except ServiceError as e:
            errors = {"phone": e.message}
    return render(request, "donors/form.html", {"form": raw, "errors": errors, "donor": None}, status_code=400)


@router.get("/{donor_id}")
def donor_detail(request: Request, donor_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    donor = get_owned_or_404(db, Donor, donor_id, user)
    return render(request, "donors/detail.html", {"donor": donor, "elig": check_donor(donor)})


@router.get("/{donor_id}/edit")
def edit_donor_form(request: Request, donor_id: int, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    donor = get_owned_or_404(db, Donor, donor_id, user)
    form = {c: getattr(donor, c) for c in DonorIn.model_fields}
    form = {k: (v.value if hasattr(v, "value") else v) for k, v in form.items()}
    return render(request, "donors/form.html", {"form": form, "errors": {}, "donor": donor})


@router.post("/{donor_id}/edit")
async def update_donor(request: Request, donor_id: int, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    donor = get_owned_or_404(db, Donor, donor_id, user)
    raw = await form_dict(request)
    data, errors = validate(DonorIn, dict(raw))
    if not errors:
        try:
            donor_svc.update_donor(db, donor, data, user)
            flash(request, "Donor details updated.")
            return redirect(f"/donors/{donor.id}")
        except ServiceError as e:
            errors = {"__all__": e.message}
    return render(request, "donors/form.html", {"form": raw, "errors": errors, "donor": donor}, status_code=400)


@router.post("/{donor_id}/toggle")
def toggle_donor(request: Request, donor_id: int, user: User = Depends(require_authorizer),
                 db: Session = Depends(get_db)):
    donor = get_owned_or_404(db, Donor, donor_id, user)
    donor_svc.set_donor_active(db, donor, not donor.is_active, user)
    flash(request, f"Donor {'reactivated' if donor.is_active else 'deactivated'}.")
    return redirect(f"/donors/{donor.id}")
