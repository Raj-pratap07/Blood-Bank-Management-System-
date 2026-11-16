"""Public site: landing page, facility directory with map, facility pages, read-only JSON.

No authentication, no forms, no session cookie is created. Visibility rules live in
app.services.public (which facilities) and app.services.availability (which figures).
Nothing here returns donors, patients, requests, individual bags or internal notes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import TRACKED_PRODUCTS, AvailabilityStatus, BloodGroup, Product
from app.services import public as pub
from app.web import templates

router = APIRouter()

NOTICE = ("Availability is reported by the blood bank itself and can change at any time. It is not a reservation "
          "and does not guarantee that blood can be issued. Always call the blood bank to confirm before travelling.")


# Pages/API carrying listing or availability data must never be served from a browser cache:
# a withdrawn, expired or closed facility's figures would otherwise linger after the change.
NO_CACHE = "no-cache, max-age=0, must-revalidate"


def _render(request: Request, name: str, ctx: dict, status_code: int = 200, cache: str = NO_CACHE):
    base = {"public_nav": True, "notice": NOTICE, "map": {
        "tile_url": settings.map_tile_url, "attribution": settings.map_attribution,
        "center": list(settings.map_default_center), "zoom": settings.map_default_zoom}}
    base.update(ctx)
    resp = templates.TemplateResponse(request, name, base, status_code=status_code)
    resp.headers["Cache-Control"] = cache
    return resp


def landing(request: Request, db: Session):
    facilities = pub.public_facilities(db)
    published = sum(1 for f in facilities if pub.availability(db, f).is_current)
    return _render(request, "public/landing.html", {
        "count": len(facilities), "mapped": sum(1 for f in facilities if f.location_verified),
        "published": published})


def _filters(group: str, component: str, facility: str, facilities):
    sel_group = BloodGroup(group) if group in BloodGroup else None
    sel_product = Product(component) if component in Product else None
    slugs = {f.slug for f in facilities}
    sel_facility = facility if facility in slugs else None
    invalid = bool((group and not sel_group) or (component and not sel_product) or (facility and not sel_facility))
    return sel_group, sel_product, sel_facility, invalid


@router.get("/find")
def directory(request: Request, group: str = "", component: str = "", facility: str = "",
              db: Session = Depends(get_db)):
    facilities = pub.public_facilities(db)
    sel_group, sel_product, sel_facility, invalid = _filters(group, component, facility, facilities)
    shown = [f for f in facilities if not sel_facility or f.slug == sel_facility]
    views = {f.slug: pub.availability(db, f) for f in shown}
    filtering = bool(sel_group or sel_product)
    results = []                                         # (facility, cell) for current publications only
    if filtering:
        product = sel_product or Product.WHOLE_BLOOD
        for f in shown:
            v = views[f.slug]
            if not v.is_current:
                continue
            for g in ([sel_group] if sel_group else list(BloodGroup)):
                c = v.cell(g, product)
                if c and c.status in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.LOW):
                    results.append((f, c))
    return _render(request, "public/directory.html", {
        "facilities": shown, "all_facilities": facilities, "views": views, "filtering": filtering,
        "results": results, "group": sel_group, "component": sel_product, "facility_filter": sel_facility,
        "tracked": TRACKED_PRODUCTS, "groups": [g.value for g in BloodGroup], "components": list(Product),
        "invalid_filter": invalid, "map_data": [f.to_json() for f in shown],
        "eraktkosh_url": pub.ERAKTKOSH_URL,
    })


@router.get("/find/about-data")
def about_data(request: Request):
    return _render(request, "public/about.html", {"eraktkosh_url": pub.ERAKTKOSH_URL},
                   cache="public, max-age=300")       # static explanatory content only


@router.get("/find/{slug}")
def facility_page(request: Request, slug: str, db: Session = Depends(get_db)):
    f = pub.public_facility(db, slug)
    if f is None:
        raise HTTPException(status_code=404, detail="This blood bank is not listed in the public directory.")
    return _render(request, "public/facility.html", {
        "f": f, "availability": pub.availability(db, f), "eraktkosh_url": pub.ERAKTKOSH_URL,
        "groups": [g.value for g in BloodGroup], "tracked": TRACKED_PRODUCTS, "products": list(Product),
        "map_data": [f.to_json()] if f.location_verified else [],
    })


@router.get("/api/public/facilities")
def facilities_geojson(response: Response, db: Session = Depends(get_db)):
    """GeoJSON FeatureCollection. Facilities without a verified location have geometry = null."""
    features = []
    for f in pub.public_facilities(db):
        props = f.to_json()
        lat, lon = props.pop("lat"), props.pop("lon")
        v = pub.availability(db, f)
        props["availability_published_at"] = v.published_at.isoformat() + "Z" if v.is_current else None
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]} if f.location_verified else None,
            "properties": props,
        })
    response.headers["Cache-Control"] = NO_CACHE
    return {"type": "FeatureCollection", "features": features,
            "attribution": "Directory data: Haldwani BloodLink (sources listed per facility)."}


@router.get("/api/public/availability")
def availability_api(response: Response, group: str = "", component: str = "", facility: str = "",
                     db: Session = Depends(get_db)):
    """Current facility-published availability. Minimal fields only; no operational identifiers."""
    facilities = pub.public_facilities(db)
    sel_group, sel_product, sel_facility, invalid = _filters(group, component, facility, facilities)
    if invalid:
        return JSONResponse({"detail": "Unknown blood group, component or facility.",
                             "groups": [g.value for g in BloodGroup], "components": [p.value for p in Product]},
                            status_code=422)
    out = []
    for f in facilities:
        if sel_facility and f.slug != sel_facility:
            continue
        v = pub.availability(db, f)
        if not v.is_current:
            continue
        for c in v.cells:
            if c.status is None:
                continue
            if (sel_group and c.blood_group != sel_group.value) or (sel_product and c.product != sel_product.value):
                continue
            item = {"facility": f.slug, "facility_name": f.name, "blood_group": c.blood_group,
                    "component": c.product, "status": c.status.value,
                    "published_at": v.published_at.isoformat() + "Z", "valid_until": v.valid_until.isoformat() + "Z"}
            if c.units is not None:
                item["units"] = c.units
            out.append(item)
    response.headers["Cache-Control"] = NO_CACHE
    return {"results": out, "notice": NOTICE}
