from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import SCREENING_MARKERS, BloodGroup, Donation, DonationStatus, Donor, User
from app.schemas.forms import DonationIn, ScreeningIn
from app.services import donations as donation_svc
from app.services import users as user_svc
from app.services.eligibility import check_donor
from app.services.errors import EligibilityError, ServiceError
from app.web import Page, current_user, flash, form_dict, get_or_404, redirect, render, require_authorizer, validate

router = APIRouter(prefix="/donations")


@router.get("")
def list_donations(request: Request, status: str = "", group: str = "", page: int = 1,
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Donation).options(selectinload(Donation.donor), selectinload(Donation.units))
    if status in DonationStatus:
        stmt = stmt.where(Donation.status == DonationStatus(status))
    if group in BloodGroup:
        stmt = stmt.where(Donation.blood_group == BloodGroup(group))
    stmt = stmt.order_by(Donation.donation_date.desc(), Donation.id.desc())
    return render(request, "donations/list.html", {"page": Page(db, stmt, page), "status": status, "group": group})


def _form_ctx(db: Session, donor: Donor | None, form: dict, errors: dict, reasons: list | None = None):
    return {"donor": donor, "form": form, "errors": errors, "reasons": reasons or [],
            "authorizer_options": [(u.id, f"{u.full_name} ({u.role.label})") for u in user_svc.authorizers(db)],
            "elig": check_donor(donor) if donor else None}


@router.get("/new")
def new_donation_form(request: Request, donor_id: int | None = None, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    donor = db.get(Donor, donor_id) if donor_id else None
    form = {"donation_date": date.today().isoformat(), "volume_ml": "350",
            "weight_kg": donor.weight_kg if donor else "",
            "cleared_by_id": user.id if user.can_authorize else ""}
    return render(request, "donations/form.html", _form_ctx(db, donor, form, {}))


@router.post("/new")
async def create_donation(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw = await form_dict(request)
    donor = db.get(Donor, int(raw["donor_id"])) if str(raw.get("donor_id") or "").isdigit() else None
    data, errors = validate(DonationIn, dict(raw))
    if donor is None:
        errors["donor_id"] = "Select a registered donor."
    reasons: list[str] = []
    if not errors:
        try:
            donation = donation_svc.record_donation(db, donor, data, user)
            flash(request, f"Donation {donation.code} recorded. Bag {donation.units[0].unit_code} is in quarantine "
                           "pending screening.")
            return redirect(f"/donations/{donation.id}")
        except EligibilityError as e:
            errors["__all__"] = e.message
            reasons = e.details
        except ServiceError as e:
            errors["__all__"] = e.message
    return render(request, "donations/form.html", _form_ctx(db, donor, raw, errors, reasons), status_code=400)


@router.get("/{donation_id}")
def donation_detail(request: Request, donation_id: int, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    donation = get_or_404(db, Donation, donation_id)
    return render(request, "donations/detail.html", {"donation": donation, "markers": SCREENING_MARKERS,
                                                     "form": {}, "errors": {}})


@router.post("/{donation_id}/screening")
async def screen_donation(request: Request, donation_id: int, user: User = Depends(require_authorizer),
                          db: Session = Depends(get_db)):
    donation = get_or_404(db, Donation, donation_id)
    raw = await form_dict(request)
    data, errors = validate(ScreeningIn, dict(raw))
    if "decision" in errors:
        # The decision comes from the Accept/Reject button, which has no field to show an error next to.
        errors.setdefault("__all__", "Choose Accept or Reject to complete screening.")
    if not errors:
        try:
            donation_svc.screen_donation(db, donation, data, user)
            if donation.status == DonationStatus.ACCEPTED:
                flash(request, f"Screening accepted. Bag {donation.units[0].unit_code} released to inventory.")
            else:
                flash(request, "Screening rejected. Bag discarded.", "warning")
            return redirect(f"/donations/{donation.id}")
        except ServiceError as e:
            errors["__all__"] = e.message
    return render(request, "donations/detail.html", {"donation": donation, "markers": SCREENING_MARKERS,
                                                     "form": raw, "errors": errors}, status_code=400)
