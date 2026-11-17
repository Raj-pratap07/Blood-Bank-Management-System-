"""Platform administration: facility directory, sources, contacts and verification log.

Every route is restricted to the platform_admin role (router dependency). The
platform administrator never sees facility operational data.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import (
    ContactKind,
    Facility,
    OperationalStatus,
    Ownership,
    SourceType,
    User,
    VerificationMethod,
    VerificationScope,
    VerificationStatus,
)
from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, SourceIn, VerificationIn
from app.schemas.forms import OnboardIn
from app.services import users as user_svc
from app.services import facilities as svc
from app.services.errors import ServiceError
from app.web import flash, form_dict, get_or_404, redirect, render, require_platform_admin, validate
from app.utils import business_today

router = APIRouter(prefix="/platform", dependencies=[Depends(require_platform_admin)])

TRISTATE = [("", "Unknown"), ("true", "Yes"), ("false", "No")]
OPTIONS = {
    "ownership": [(o.value, o.value.title()) for o in Ownership],
    "operational_status": [(o.value, o.value.title()) for o in OperationalStatus],
    "source_type": [(s.value, s.value.replace("_", " ").capitalize()) for s in SourceType],
    "contact_kind": [(k.value, k.value.title()) for k in ContactKind],
    "scope": [(s.value, s.value.title()) for s in VerificationScope],
    "status": [(s.value, s.value.replace("_", " ").capitalize()) for s in VerificationStatus
               if s != VerificationStatus.OUTDATED],
    "method": [(m.value, m.value.replace("_", " ").capitalize()) for m in VerificationMethod],
    "tristate": TRISTATE,
}


def _load(db: Session, facility_id: int) -> Facility:
    facility = get_or_404(db, Facility, facility_id)
    db.refresh(facility)
    return facility


@router.get("")
def platform_home():
    return redirect("/platform/facilities")


@router.get("/facilities")
def list_facilities(request: Request, db: Session = Depends(get_db)):
    facilities = list(db.scalars(select(Facility).options(
        selectinload(Facility.contacts), selectinload(Facility.verifications))
        .order_by(Facility.is_demo, Facility.display_name)))
    rows = [(f, svc.verification_summary(f), svc.listing_blockers(f)) for f in facilities]
    return render(request, "platform/facility_list.html", {"rows": rows})


def _facility_form(request: Request, facility: Facility | None, form: dict, errors: dict, status_code: int = 200):
    return render(request, "platform/facility_form.html",
                  {"facility": facility, "form": form, "errors": errors, "options": OPTIONS},
                  status_code=status_code)


@router.get("/facilities/new")
def new_facility(request: Request):
    return _facility_form(request, None, {"ownership": "unknown", "operational_status": "unknown"}, {})


@router.post("/facilities/new")
async def create_facility(request: Request, user: User = Depends(require_platform_admin),
                          db: Session = Depends(get_db)):
    raw = await form_dict(request)
    data, errors = validate(FacilityIn, dict(raw))
    if not errors:
        try:
            facility = svc.create_facility(db, data, user)
            flash(request, f"Facility '{facility.display_name}' created as an unverified, unlisted entry. "
                           "Add its sources next.")
            return redirect(f"/platform/facilities/{facility.id}")
        except ServiceError as e:
            errors["__all__"] = e.message
    return _facility_form(request, None, raw, errors, 400)


def _bool_form(v: bool | None) -> str:
    return "" if v is None else ("true" if v else "false")


@router.get("/facilities/{facility_id}/edit")
def edit_facility(request: Request, facility_id: int, db: Session = Depends(get_db)):
    facility = _load(db, facility_id)
    form = {k: getattr(facility, k) for k in FacilityIn.model_fields}
    form = {k: (v.value if hasattr(v, "value") else v) for k, v in form.items()}
    form["has_component_separation"] = _bool_form(facility.has_component_separation)
    form["has_apheresis"] = _bool_form(facility.has_apheresis)
    return _facility_form(request, facility, form, {})


@router.post("/facilities/{facility_id}/edit")
async def update_facility(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                          db: Session = Depends(get_db)):
    facility = _load(db, facility_id)
    raw = await form_dict(request)
    data, errors = validate(FacilityIn, dict(raw))
    if not errors:
        was_listed = facility.is_listed
        try:
            deactivated = svc.update_facility(db, facility, data, user)
            flash(request, "Facility details updated.")
            if deactivated:
                flash(request, f"Facility marked closed: {deactivated} staff account(s) were deactivated. "
                               "All records are kept.", "warning")
            if was_listed and not facility.is_listed:
                flash(request, "The facility no longer meets the listing rules and was unlisted.", "warning")
            return redirect(f"/platform/facilities/{facility.id}")
        except ServiceError as e:
            errors["__all__"] = e.message
    return _facility_form(request, facility, raw, errors, 400)


def _detail(request: Request, db: Session, facility: Facility, section: str | None = None,
            form: dict | None = None, errors: dict | None = None, status_code: int = 200,
            setup_link: dict | None = None):
    contacts = [(c, svc.contact_status(c)) for c in facility.contacts]
    history = sorted(facility.verifications, key=lambda v: (v.verified_on, v.id), reverse=True)
    resp = render(request, "platform/facility_detail.html", {
        "f": facility, "summary": svc.verification_summary(facility), "contacts": contacts,
        "history": history, "blockers": svc.listing_blockers(facility), "map_eligible": svc.map_eligible(facility),
        "options": OPTIONS, "section": section, "form": form or {}, "errors": errors or {},
        "account_count": user_svc.account_count(db, facility.id),
        "pending_setup": user_svc.pending_setup_accounts(db, facility.id), "setup_link": setup_link,
        "today": business_today().isoformat(),
    }, status_code=status_code)
    if setup_link:
        # The page contains a raw one-time credential: never cache it, never leak it via Referer.
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/facilities/{facility_id}")
def facility_detail(request: Request, facility_id: int, db: Session = Depends(get_db)):
    return _detail(request, db, _load(db, facility_id))


async def _section_post(request: Request, db: Session, facility_id: int, section: str, schema, action, ok_msg: str):
    return await _section_post_raw(request, db, facility_id, section, schema, await form_dict(request), action, ok_msg)


async def _section_post_raw(request: Request, db: Session, facility_id: int, section: str, schema, raw: dict,
                            action, ok_msg: str):
    facility = _load(db, facility_id)
    data, errors = validate(schema, dict(raw))
    if not errors:
        try:
            action(facility, data)
            flash(request, ok_msg)
            return redirect(f"/platform/facilities/{facility.id}#{section}")
        except ServiceError as e:
            errors["__all__"] = e.message
    db.rollback()
    return _detail(request, db, _load(db, facility_id), section, raw, errors, 400)


@router.post("/facilities/{facility_id}/sources")
async def add_source(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                     db: Session = Depends(get_db)):
    return await _section_post(request, db, facility_id, "sources", SourceIn,
                               lambda f, d: svc.add_source(db, f, d, user), "Source recorded.")


@router.post("/facilities/{facility_id}/contacts")
async def add_contact(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                      db: Session = Depends(get_db)):
    return await _section_post(request, db, facility_id, "contacts", ContactIn,
                               lambda f, d: svc.add_contact(db, f, d, user),
                               "Contact recorded as unverified. Record a verification to confirm it.")


@router.post("/facilities/{facility_id}/location")
async def set_location(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                       db: Session = Depends(get_db)):
    return await _section_post(request, db, facility_id, "location", LocationIn,
                               lambda f, d: svc.set_location(db, f, d, user),
                               "Location saved. It stays off the map until a location verification is recorded.")


@router.post("/facilities/{facility_id}/verifications")
async def add_verification(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                           db: Session = Depends(get_db)):
    return await _section_post(request, db, facility_id, "verification", VerificationIn,
                               lambda f, d: svc.record_verification(db, f, d, user), "Verification recorded.")


def _setup_link(request: Request, user, token: str) -> dict:
    return {"username": user.username, "url": str(request.base_url).rstrip("/") + f"/account/setup/{token}"}


@router.post("/facilities/{facility_id}/onboard")
async def onboard(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                  db: Session = Depends(get_db)):
    """Create the facility's administrator and show a ONE-TIME setup link (no password is ever chosen here).

    The link is rendered directly in this response (never put in the session/flash). Note: web-server access
    logs record request paths, so the setup URL appears in them when the link is opened.
    """
    facility = _load(db, facility_id)
    raw = await form_dict(request)
    data, errors = validate(OnboardIn, dict(raw))
    if not errors:
        try:
            admin, token = user_svc.onboard_facility_admin(db, facility, data, user)
            return _detail(request, db, _load(db, facility_id), setup_link=_setup_link(request, admin, token))
        except ServiceError as e:
            errors["__all__"] = e.message
    db.rollback()
    return _detail(request, db, _load(db, facility_id), "onboarding", raw, errors, 400)


@router.post("/facilities/{facility_id}/onboard/{user_id}/reissue")
def reissue(request: Request, facility_id: int, user_id: int, user: User = Depends(require_platform_admin),
            db: Session = Depends(get_db)):
    facility = _load(db, facility_id)
    try:
        admin, token = user_svc.reissue_setup_link(db, facility, user_id, user)
        return _detail(request, db, _load(db, facility_id), setup_link=_setup_link(request, admin, token))
    except ServiceError as e:
        flash(request, e.message, "danger")
        return redirect(f"/platform/facilities/{facility_id}#onboarding")


@router.post("/facilities/{facility_id}/contacts/{contact_id}/retire")
def retire_contact(request: Request, facility_id: int, contact_id: int, user: User = Depends(require_platform_admin),
                   db: Session = Depends(get_db)):
    facility = _load(db, facility_id)
    try:
        svc.retire_contact(db, facility, contact_id, user)
        flash(request, "Contact retired. Its verification history is kept.", "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/platform/facilities/{facility.id}#contacts")


@router.post("/facilities/{facility_id}/listing")
async def set_listing(request: Request, facility_id: int, user: User = Depends(require_platform_admin),
                      db: Session = Depends(get_db)):
    facility = _load(db, facility_id)
    listed = (await form_dict(request)).get("listed") == "true"
    try:
        svc.set_listed(db, facility, listed, user)
        flash(request, "Facility is now listed for public display (from Phase 2)." if listed
              else "Facility unlisted.", "success" if listed else "warning")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect(f"/platform/facilities/{facility.id}")
