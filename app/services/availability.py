"""Facility-published blood availability (Phase 4).

Source of truth: the facility's own inventory. Only ELIGIBLE bags count:
    status == AVAILABLE and expires_on >= today and product tracked by inventory.
Quarantined (pending screening), reserved, issued, expired and discarded bags never count.

Publication: an authorised user (facility admin or medical officer) explicitly publishes an
aggregate snapshot (units per blood group and product). Nothing becomes public automatically.

Public value: min(published units, eligible units right now): a published figure can only ever
be shown LOWER than published (e.g. after issuing), never higher.

Freshness: valid for min(facility.stock_max_age_hours, BBMS_AVAILABILITY_MAX_HOURS). After that,
or after withdrawal, the public sees "not recently updated" / "not published", never old figures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    TRACKED_PRODUCTS,
    AvailabilityEntry,
    AvailabilityPublication,
    AvailabilityStatus,
    BloodGroup,
    BloodUnit,
    Facility,
    OperationalStatus,
    Product,
    StockVisibility,
    UnitStatus,
    User,
)
from app.services import facilities as fsvc
from app.services.errors import PermissionDenied, ServiceError
from app.services.scope import facility_id_of
from app.services.tx import transaction
from app.utils import utcnow, business_today


# ------------------------------------------------------------------ inventory side
def eligible_counts(db: Session, facility_id: int, on: date | None = None) -> dict[tuple[BloodGroup, Product], int]:
    """Aggregate count of bags that may be offered: available AND unexpired. Never individual bags."""
    on = on or business_today()
    rows = db.execute(
        select(BloodUnit.blood_group, BloodUnit.product, func.count())
        .where(BloodUnit.facility_id == facility_id, BloodUnit.status == UnitStatus.AVAILABLE,
               BloodUnit.expires_on >= on, BloodUnit.product.in_(TRACKED_PRODUCTS))
        .group_by(BloodUnit.blood_group, BloodUnit.product)
    ).all()
    counts = {(g, p): 0 for g in BloodGroup for p in TRACKED_PRODUCTS}
    counts.update({(g, p): n for g, p, n in rows})
    return counts


def validity_hours(facility: Facility) -> int:
    return max(1, min(facility.stock_max_age_hours, settings.availability_max_hours))


def latest_publication(db: Session, facility_id: int) -> AvailabilityPublication | None:
    return db.scalar(select(AvailabilityPublication).where(AvailabilityPublication.facility_id == facility_id)
                     .order_by(AvailabilityPublication.published_at.desc(), AvailabilityPublication.id.desc())
                     .limit(1))


def publication_state(pub: AvailabilityPublication | None, now: datetime | None = None) -> str:
    """'none' | 'withdrawn' | 'stale' | 'current' — only the LATEST publication counts."""
    now = now or utcnow()
    if pub is None:
        return "none"
    if pub.withdrawn_at is not None:
        return "withdrawn"
    return "current" if pub.valid_until > now else "stale"


def _own_facility(db: Session, actor: User) -> Facility:
    return db.get(Facility, facility_id_of(actor))      # platform admins are refused here


# ------------------------------------------------------------------ facility actions
def publish(db: Session, actor: User, now: datetime | None = None, on: date | None = None) -> AvailabilityPublication:
    if not actor.can_authorize:
        raise PermissionDenied("Only a facility administrator or medical officer can publish availability.")
    facility = _own_facility(db, actor)
    if facility.operational_status == OperationalStatus.CLOSED:
        raise ServiceError("A closed facility cannot publish availability.")
    if not facility.is_participating:
        raise ServiceError("This facility is not yet participating on the platform.")
    if facility.stock_visibility == StockVisibility.HIDDEN:
        raise ServiceError("Public availability is switched off for this facility. An administrator must choose "
                           "'banded' or 'exact' in the availability settings first.")
    now = now or utcnow()
    counts = eligible_counts(db, facility.id, on)
    with transaction(db):
        pub = AvailabilityPublication(facility_id=facility.id, published_by_id=actor.id, published_at=now,
                                      valid_until=now + timedelta(hours=validity_hours(facility)))
        db.add(pub)
        db.flush()
        for (group, product), units in counts.items():
            db.add(AvailabilityEntry(publication_id=pub.id, blood_group=group, product=product, units=units))
    db.refresh(pub)
    return pub


def withdraw(db: Session, actor: User, now: datetime | None = None) -> AvailabilityPublication:
    if not actor.can_authorize:
        raise PermissionDenied("Only a facility administrator or medical officer can withdraw availability.")
    facility = _own_facility(db, actor)
    pub = latest_publication(db, facility.id)
    if publication_state(pub, now) not in ("current", "stale"):
        raise ServiceError("There is no published availability to withdraw.")
    with transaction(db):
        pub.withdrawn_at = now or utcnow()
        pub.withdrawn_by_id = actor.id
    return pub


def update_settings(db: Session, actor: User, visibility: StockVisibility, max_age_hours: int) -> Facility:
    if not actor.is_admin:
        raise PermissionDenied("Only the facility administrator can change availability settings.")
    if not 1 <= max_age_hours <= settings.availability_max_hours:
        raise ServiceError(f"Validity must be between 1 and {settings.availability_max_hours} hours.")
    facility = _own_facility(db, actor)
    with transaction(db):
        facility.stock_visibility = visibility
        facility.stock_max_age_hours = max_age_hours
    return facility


def history(db: Session, actor: User, limit: int = 10) -> list[AvailabilityPublication]:
    fid = facility_id_of(actor)
    return list(db.scalars(select(AvailabilityPublication).where(AvailabilityPublication.facility_id == fid)
                           .order_by(AvailabilityPublication.published_at.desc(),
                                     AvailabilityPublication.id.desc()).limit(limit)))


# ------------------------------------------------------------------ public side
@dataclass
class PublicCell:
    blood_group: str
    product: str
    status: AvailabilityStatus | None      # None = product not tracked by the facility's inventory
    units: int | None = None               # only when the facility chose "exact"


@dataclass
class PublicAvailability:
    state: str                              # 'off' | 'none' | 'withdrawn' | 'stale' | 'current'
    published_at: datetime | None = None
    valid_until: datetime | None = None
    cells: list[PublicCell] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.state == "current"

    def cell(self, group: BloodGroup | str, product: Product | str = Product.WHOLE_BLOOD) -> PublicCell | None:
        return next((c for c in self.cells if c.blood_group == str(group) and c.product == str(product)), None)


def _band(units: int) -> AvailabilityStatus:
    if units <= 0:
        return AvailabilityStatus.NOT_AVAILABLE
    return AvailabilityStatus.LOW if units <= settings.availability_low_max else AvailabilityStatus.AVAILABLE


def public_view(db: Session, facility: Facility, now: datetime | None = None,
                on: date | None = None) -> PublicAvailability:
    """What an anonymous visitor may see for one facility.

    Defence in depth: even if a caller passes an unlisted facility (or one whose listing is blocked, e.g.
    unverified details), nothing is shown.
    """
    if (facility.is_demo or not facility.is_listed or fsvc.listing_blockers(facility, on)
            or not facility.is_participating
            or facility.operational_status == OperationalStatus.CLOSED
            or facility.stock_visibility == StockVisibility.HIDDEN):
        return PublicAvailability("off")
    return _snapshot_view(db, facility, now, on)


def demo_view(db: Session, facility: Facility, now: datetime | None = None,
              on: date | None = None) -> PublicAvailability:
    """The fictional demo facility's published SYNTHETIC availability, for the /demo pages only.

    Never used by /find or the public API (public_view still refuses demo facilities). Only in demo mode.
    """
    if (not settings.demo_mode or not facility.is_demo or not facility.is_participating
            or facility.stock_visibility == StockVisibility.HIDDEN):
        return PublicAvailability("off")
    return _snapshot_view(db, facility, now, on)


def _snapshot_view(db: Session, facility: Facility, now: datetime | None, on: date | None) -> PublicAvailability:
    pub = latest_publication(db, facility.id)
    state = publication_state(pub, now)
    if state != "current":
        return PublicAvailability(state, published_at=pub.published_at if pub else None,
                                  valid_until=pub.valid_until if pub else None)
    live = eligible_counts(db, facility.id, on)
    published = {(e.blood_group, e.product): e.units for e in pub.entries}
    exact = facility.stock_visibility == StockVisibility.EXACT
    cells = []
    for g in BloodGroup:
        for p in Product:
            if p not in TRACKED_PRODUCTS:
                cells.append(PublicCell(g.value, p.value, None))
                continue
            shown = min(published.get((g, p), 0), live.get((g, p), 0))   # never overstate
            cells.append(PublicCell(g.value, p.value, _band(shown), shown if exact else None))
    return PublicAvailability("current", pub.published_at, pub.valid_until, cells)


# ------------------------------------------------------------------ search results (Phase 6)
from app.models.enums import REQUESTABLE_PRODUCTS as SEARCH_PRODUCTS  # noqa: E402
UNIT_LABEL = "bags (units)"


@dataclass
class SearchRow:
    """One facility x blood group x component result. Missing data is NEVER turned into zero stock."""
    facility_name: str
    facility_slug: str | None
    blood_group: str
    product: str
    status: str                  # 'available' | 'unavailable' | 'not_reported' | 'stale'
    units: int | None            # only from a CURRENT report with exact figures
    low: bool
    updated_at: datetime | None  # time of the report the status is based on (None: no report)
    is_demo: bool
    simulated: bool = False      # /demo only: fixed synthetic component figure (app/services/demo_stock.py)

    @property
    def product_label(self) -> str:
        return Product(self.product).label

    @property
    def quantity_text(self) -> str:
        if self.status == "available":
            return f"{self.units} {UNIT_LABEL}" if self.units is not None else "Available (quantity not disclosed)"
        if self.status == "unavailable":
            return f"0 {UNIT_LABEL}"
        return "—"


def search_rows(view: PublicAvailability, name: str, slug: str | None, is_demo: bool,
                groups: list[BloodGroup], products: list[Product]) -> list[SearchRow]:
    rows = []
    for g in groups:
        for p in products:
            base = dict(facility_name=name, facility_slug=slug, blood_group=g.value, product=p.value, is_demo=is_demo)
            if view.state == "stale":                       # an expired report: show its time, never its figures
                rows.append(SearchRow(**base, status="stale", units=None, low=False, updated_at=view.published_at))
                continue
            cell = view.cell(g, p) if view.is_current else None
            if cell is None or cell.status is None:          # no current report, or component not tracked
                rows.append(SearchRow(**base, status="not_reported", units=None, low=False, updated_at=None))
            elif cell.status == AvailabilityStatus.NOT_AVAILABLE:
                rows.append(SearchRow(**base, status="unavailable", units=0 if cell.units is not None else None,
                                      low=False, updated_at=view.published_at))
            else:
                rows.append(SearchRow(**base, status="available", units=cell.units,
                                      low=cell.status == AvailabilityStatus.LOW, updated_at=view.published_at))
    return rows

STATUS_ORDER = {"available": 0, "unavailable": 1, "stale": 2, "not_reported": 3}
STATUS_LABEL = {"available": "Available", "unavailable": "Unavailable", "not_reported": "Not reported", "stale": "Stale"}
