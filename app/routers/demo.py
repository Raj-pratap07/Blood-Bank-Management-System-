"""Public demonstration: synthetic availability search, emergency request submission and secure tracking.

Only in demo mode (404 otherwise). Everything here concerns the FICTIONAL demo facility and is labelled DEMO DATA.
The real directory (/find, /api/public/*) is untouched: it still never shows the demo facility.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import BloodGroup, Product
from app.models.enums import REQUESTABLE_PRODUCTS
from app.routers.public import _render
from app.schemas.forms import PublicRequestIn
from app.services import availability as avail
from app.services import public_requests as prsvc
from app.services.errors import ServiceError
from app.services.ratelimit import SUBMISSIONS, TRACKING, client_key
from app.web import form_dict, validate

router = APIRouter(prefix="/demo")
PRIVATE = "no-store"                      # pages showing a tracking code or a request's status are never cached


def _require_demo() -> None:
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not found")


def _ctx(db: Session) -> dict:
    return {"demo_facilities": prsvc.eligible_facilities(db), "groups": list(BloodGroup),
            "components": list(REQUESTABLE_PRODUCTS)}


@router.get("")
def hub(request: Request, group: str = "", component: str = "", db: Session = Depends(get_db)):
    _require_demo()
    sel_group = BloodGroup(group) if group in BloodGroup else None
    sel_product = Product(component) if component in REQUESTABLE_PRODUCTS else None
    rows = []
    if sel_group or sel_product:
        groups = [sel_group] if sel_group else list(BloodGroup)
        products = [sel_product] if sel_product else list(REQUESTABLE_PRODUCTS)
        for f in prsvc.eligible_facilities(db):
            rows += avail.search_rows(avail.demo_view(db, f), f.display_name, None, True, groups, products)
        rows.sort(key=lambda r: avail.STATUS_ORDER[r.status])
    return _render(request, "public/demo/hub.html", {**_ctx(db), "rows": rows, "group": sel_group,
                                                      "component": sel_product, "searched": bool(sel_group or sel_product)})


@router.get("/request")
def request_form(request: Request, db: Session = Depends(get_db)):
    _require_demo()
    return _render(request, "public/demo/request.html", {**_ctx(db), "form": {}, "errors": {}})


@router.post("/request")
async def request_submit(request: Request, db: Session = Depends(get_db)):
    _require_demo()
    if not SUBMISSIONS.allow(client_key(request)):
        return _render(request, "public/demo/message.html", {"title": "Too many requests",
                       "message": "Too many requests from this connection. Please wait a few minutes. "
                                  "In an emergency, call the hospital or blood bank directly."}, status_code=429)
    raw = await form_dict(request)
    data, errors = validate(PublicRequestIn, dict(raw))
    if not errors:
        try:
            req, token = prsvc.submit(db, data)
        except ServiceError as e:
            errors = {"__all__": e.message}
        else:
            return _render(request, "public/demo/request_done.html", {"req": req, "token": token}, cache=PRIVATE)
    return _render(request, "public/demo/request.html", {**_ctx(db), "form": raw, "errors": errors}, status_code=400)


@router.get("/track")
def track_form(request: Request):
    _require_demo()
    return _render(request, "public/demo/track.html", {"error": None, "reference": ""}, cache=PRIVATE)


@router.post("/track")
async def track_submit(request: Request, db: Session = Depends(get_db)):
    _require_demo()
    if not TRACKING.allow(client_key(request)):
        return _render(request, "public/demo/message.html", {"title": "Too many attempts",
                       "message": "Too many tracking attempts from this connection. Please wait a few minutes."},
                       status_code=429)
    raw = await form_dict(request)
    reference, code = str(raw.get("reference") or ""), str(raw.get("code") or "")
    req = prsvc.track(db, reference, code)
    if req is None:      # one generic answer: never reveal whether the reference or the code was wrong
        return _render(request, "public/demo/track.html", {"reference": reference[:20],
                       "error": "No request matches that reference and tracking code."}, status_code=404, cache=PRIVATE)
    return _render(request, "public/demo/track_result.html", {
        "req": req, "status": prsvc.public_status(req), "labels": prsvc.TIMELINE_LABELS}, cache=PRIVATE)
