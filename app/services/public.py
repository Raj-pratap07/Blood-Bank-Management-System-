"""Read-only view of the facility directory for the public site (Haldwani BloodLink Phase 2).

This module is the single gatekeeper for what anonymous visitors can see:
- only facilities that are listed AND still meet every listing rule today
  (a confirmation that has become outdated or disputed hides the facility);
- coordinates only when the location is sourced and verified (map_eligible);
- only active, public contacts, each labelled with its own verification status;
- availability only from a current, facility-published snapshot (app.services.availability).
It never exposes donors, donations, individual bags, requests or patients.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ContactKind,
    Facility,
    FacilityContact,
    SourceType,
    VerificationScope,
    OperationalStatus,
    VerificationStatus,
)
from app.services import availability as avail
from app.services import facilities as fsvc

ERAKTKOSH_URL = "https://eraktkosh.mohfw.gov.in/"


# ------------------------------------------------------------------ view models
@dataclass
class PublicContact:
    kind: str
    value: str
    label: str | None
    confirmed: bool
    status_text: str
    href: str
    badge: str = "Not yet confirmed"        # short public label for the contact's verification status


@dataclass
class PublicFacility:
    facility: Facility
    slug: str
    name: str
    official_name: str | None
    aliases: list[str]
    ownership: str
    parent_hospital: str | None
    address: str
    city: str | None
    identity_text: str
    identity_checked_on: date | None
    location_verified: bool
    location_text: str
    latitude: float | None
    longitude: float | None
    services_text: str
    contacts: list[PublicContact]
    directions: dict[str, str]
    address_search_url: str | None
    eraktkosh_code: str | None
    evidence: list[dict] = field(default_factory=list)
    distance_km: float | None = None

    @property
    def phones(self) -> list[PublicContact]:
        return [c for c in self.contacts if c.kind == ContactKind.PHONE.value]

    def to_json(self) -> dict:
        return {
            "slug": self.slug, "name": self.name, "official_name": self.official_name,
            "ownership": self.ownership, "address": self.address, "city": self.city,
            "identity": self.identity_text, "location_verified": self.location_verified,
            "lat": self.latitude, "lon": self.longitude,
            "phones": [{"value": c.value, "label": c.label, "confirmed": c.confirmed, "badge": c.badge,
                        "status": c.status_text, "href": c.href} for c in self.phones],
            "directions": self.directions, "url": f"/find/{self.slug}",
        }


@dataclass
class CandidateFacility:
    """A real facility named in an official source but not yet verified for public listing.

    Deliberately minimal: name, district and provenance only. No contacts, no address, no
    coordinates and no availability, whatever is stored, because none of it is verified yet.
    """
    slug: str
    name: str
    official_name: str | None
    aliases: list[str]
    area: str
    identity_text: str
    missing: list[str]
    evidence: list[dict]


# ------------------------------------------------------------------ helpers
def _status_text(eff: fsvc.EffectiveStatus, scope_label: str) -> str:
    when = f" ({eff.entry.verified_on:%d %b %Y})" if eff.entry else ""
    return {
        VerificationStatus.SOURCE_CONFIRMED: f"{scope_label} checked against an official source{when}",
        VerificationStatus.FACILITY_CONFIRMED: f"{scope_label} confirmed by the facility{when}",
        VerificationStatus.FIELD_VERIFIED: f"{scope_label} verified on site{when}",
        VerificationStatus.OUTDATED: f"{scope_label} last checked{when} — re-check due",
        VerificationStatus.DISPUTED: f"{scope_label} disputed",
        VerificationStatus.UNVERIFIED: f"{scope_label} not yet verified",
    }[eff.status]


def directions_links(lat: float, lon: float) -> dict[str, str]:
    """Hand-off links only; no routing is done by this application."""
    coords = f"{lat:.6f},{lon:.6f}"
    return {
        "openstreetmap": "https://www.openstreetmap.org/directions?" + urlencode({"route": f";{coords}"}),
        "google": "https://www.google.com/maps/dir/?" + urlencode({"api": "1", "destination": coords}),
        "geo": f"geo:{coords}",
    }


# Contact badges say HOW a detail was checked: "Source confirmed" means it matched an official source (such as
# e-RaktKosh) on the date shown, NOT that anyone contacted the facility.
CONTACT_BADGES = {
    VerificationStatus.SOURCE_CONFIRMED: "Source confirmed",
    VerificationStatus.FACILITY_CONFIRMED: "Confirmed by facility",
    VerificationStatus.FIELD_VERIFIED: "Verified on site",
    VerificationStatus.OUTDATED: "Re-check due",
    VerificationStatus.DISPUTED: "Disputed",
    VerificationStatus.UNVERIFIED: "Not yet confirmed",
}


def _contact(c: FacilityContact) -> PublicContact:
    eff = fsvc.contact_status(c)
    href = {"phone": f"tel:{c.value}", "email": f"mailto:{c.value}", "website": c.value}[c.kind.value]
    when = f" on {eff.entry.verified_on:%d %b %Y}" if eff.entry else ""
    text = {
        VerificationStatus.SOURCE_CONFIRMED: f"Matched an official source{when}; not tested by contacting the facility",
        VerificationStatus.FACILITY_CONFIRMED: f"Confirmed directly with the facility{when}",
        VerificationStatus.FIELD_VERIFIED: f"Verified on site{when}",
        VerificationStatus.OUTDATED: f"Last checked{when}; re-check due",
        VerificationStatus.DISPUTED: "Disputed: treat with care",
        VerificationStatus.UNVERIFIED: "Not yet confirmed: taken from the source listed below",
    }[eff.status]
    return PublicContact(c.kind.value, c.value, c.label, eff.is_positive, text, href, CONTACT_BADGES[eff.status])


def _evidence(f: Facility) -> list[dict]:
    """Public provenance. Direct communications are summarised, never quoted (they may name officials)."""
    out = []
    for s in f.sources:
        private = s.source_type in (SourceType.FACILITY_CONFIRMATION, SourceType.SITE_VISIT)
        out.append({
            "type": s.source_type.value.replace("_", " ").capitalize(),
            "title": {SourceType.FACILITY_CONFIRMATION: "Confirmed directly with the facility",
                      SourceType.SITE_VISIT: "Site visit"}.get(s.source_type, s.title),
            "url": s.reference if (not private and s.reference.startswith(("http://", "https://"))) else None,
            "date": s.retrieved_on,
        })
    return out


def to_public(f: Facility) -> PublicFacility:
    identity = fsvc.scope_status(f, VerificationScope.IDENTITY)
    location = fsvc.scope_status(f, VerificationScope.LOCATION)
    services = fsvc.scope_status(f, VerificationScope.SERVICES)
    on_map = fsvc.map_eligible(f)
    address = ", ".join(x for x in (f.address_line, f.locality, f.city, f.district, f.state, f.pincode) if x)
    contacts = [_contact(c) for c in f.contacts if c.is_active and c.is_public]
    return PublicFacility(
        facility=f, slug=f.slug, name=f.display_name, official_name=f.official_name, aliases=f.alias_list,
        ownership=f.ownership.value, parent_hospital=f.parent_hospital, address=address, city=f.city,
        identity_text=_status_text(identity, "Identity"),
        identity_checked_on=identity.entry.verified_on if identity.entry else None,
        location_verified=on_map,
        location_text=_status_text(location, "Location") if on_map else "Location not yet verified — no map pin",
        latitude=f.latitude if on_map else None, longitude=f.longitude if on_map else None,
        services_text=_status_text(services, "Services"),
        contacts=contacts,
        directions=directions_links(f.latitude, f.longitude) if on_map else {},
        address_search_url=("https://www.openstreetmap.org/search?query=" + quote(address)) if address else None,
        eraktkosh_code=f.eraktkosh_code, evidence=_evidence(f),
    )


# ------------------------------------------------------------------ queries
def _base_query():
    return (select(Facility)
            .where(Facility.is_listed.is_(True), Facility.is_demo.is_(False))
            .options(selectinload(Facility.contacts).selectinload(FacilityContact.facility),
                     selectinload(Facility.verifications), selectinload(Facility.sources))
            .order_by(Facility.display_name))


def public_facilities(db: Session) -> list[PublicFacility]:
    """Listed facilities that still satisfy every listing rule today."""
    return [to_public(f) for f in db.scalars(_base_query()) if not fsvc.listing_blockers(f)]


def public_facility(db: Session, slug: str) -> PublicFacility | None:
    f = db.scalar(_base_query().where(Facility.slug == slug))
    if f is None or fsvc.listing_blockers(f):
        return None
    return to_public(f)


def availability(db: Session, f: PublicFacility) -> avail.PublicAvailability:
    """Current facility-published availability for a PUBLIC facility (else an 'off'/stale state)."""
    return avail.public_view(db, f.facility)


# ------------------------------------------------------------------ under verification
def _missing(f: Facility) -> list[str]:
    out = []
    if not (f.address_line and f.city):
        out.append("verified address")
    if not any(c.kind == ContactKind.PHONE and c.is_active and c.is_public for c in f.contacts):
        out.append("sourced public phone number")
    if not fsvc.map_eligible(f):
        out.append("verified map location")
    return out


def _to_candidate(f: Facility, identity: fsvc.EffectiveStatus) -> CandidateFacility:
    return CandidateFacility(
        slug=f.slug, name=f.display_name, official_name=f.official_name, aliases=f.alias_list,
        area=", ".join(x for x in (f.district, f.state) if x),
        identity_text=_status_text(identity, "Identity"), missing=_missing(f), evidence=_evidence(f))


def candidate_facilities(db: Session) -> list[CandidateFacility]:
    """Real (non-demo), not-closed facilities with a current confirmed identity that are not publicly listed."""
    stmt = (select(Facility)
            .where(Facility.is_demo.is_(False), Facility.operational_status != OperationalStatus.CLOSED)
            .options(selectinload(Facility.contacts), selectinload(Facility.verifications),
                     selectinload(Facility.sources))
            .order_by(Facility.display_name))
    out = []
    for f in db.scalars(stmt):
        if f.is_listed and not fsvc.listing_blockers(f):
            continue                                            # fully public listing instead
        identity = fsvc.scope_status(f, VerificationScope.IDENTITY)
        if identity.status in fsvc.CONFIRMED_IDENTITY:
            out.append(_to_candidate(f, identity))
    return out


def candidate_facility(db: Session, slug: str) -> CandidateFacility | None:
    return next((c for c in candidate_facilities(db) if c.slug == slug), None)
