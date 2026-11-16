"""Facility directory, provenance and verification rules (platform administration).

Every function that changes data requires a platform administrator. Trust is
never typed in: it is derived from the append-only verification log, and each
positive status requires evidence of the right kind.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ContactKind,
    Facility,
    FacilityContact,
    FacilitySource,
    FacilityVerification,
    OperationalStatus,
    Role,
    SourceType,
    User,
    VerificationMethod,
    VerificationScope,
    VerificationStatus,
)
from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, SourceIn, VerificationIn
from app.services.errors import PermissionDenied, ServiceError
from app.services.tx import transaction

S = VerificationStatus
CONFIRMED_IDENTITY = (S.SOURCE_CONFIRMED, S.FACILITY_CONFIRMED)
DIRECT_METHODS = (VerificationMethod.PHONE_CALL, VerificationMethod.EMAIL, VerificationMethod.LETTER,
                  VerificationMethod.SITE_VISIT)


def _reload_evidence(db: Session, facility: Facility) -> None:
    """Flush pending edits, then re-read contacts/verifications so listing rules see current rows."""
    db.flush()
    db.expire(facility, ["contacts", "verifications", "sources"])


def _require_platform_admin(actor: User) -> None:
    if actor.role != Role.PLATFORM_ADMIN or not actor.is_active:
        raise PermissionDenied("Only a platform administrator can manage the facility directory.")


# ---------------------------------------------------------------- slugs
def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:70] or "facility"


# Public URLs /find/<slug> must never collide with fixed public pages.
RESERVED_SLUGS = {"about-data", "map", "search", "api", "new"}


def _unique_slug(db: Session, base: str) -> str:
    slug, n = (base, 2) if base not in RESERVED_SLUGS else (f"{base}-2", 3)
    while db.scalar(select(func.count()).select_from(Facility).where(Facility.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------- facility CRUD
def create_facility(db: Session, data: FacilityIn, actor: User) -> Facility:
    _require_platform_admin(actor)
    with transaction(db):
        facility = Facility(**data.model_dump(), slug=_unique_slug(db, slugify(data.display_name)),
                            created_by=actor)
        db.add(facility)
    return facility


def update_facility(db: Session, facility: Facility, data: FacilityIn, actor: User) -> int:
    """Update directory details. Returns how many staff accounts were deactivated (closure only)."""
    _require_platform_admin(actor)
    if facility.is_demo and data.operational_status != facility.operational_status:
        raise ServiceError("The fictional demo facility's status cannot be changed.")
    closing = (data.operational_status == OperationalStatus.CLOSED
               and facility.operational_status != OperationalStatus.CLOSED)
    deactivated = 0
    with transaction(db):
        for k, v in data.model_dump().items():
            setattr(facility, k, v)          # slug deliberately unchanged (stable public URL)
        if closing:                          # Phase 4 hardening: a closed facility has no active accounts
            from app.services.users import deactivate_facility_accounts
            deactivated = deactivate_facility_accounts(db, facility.id)
        _reload_evidence(db, facility)
        if facility.is_listed and listing_blockers(facility):
            facility.is_listed = False       # an edit that breaks the listing rules unlists it
    return deactivated


def set_location(db: Session, facility: Facility, data: LocationIn, actor: User) -> None:
    _require_platform_admin(actor)
    if facility.is_demo and data.latitude is not None:
        raise ServiceError("The fictional demo facility must never have coordinates.")
    if data.location_source_id is not None:
        _own_source(db, facility, data.location_source_id)
    with transaction(db):
        facility.latitude, facility.longitude = data.latitude, data.longitude
        facility.location_source_id = data.location_source_id if data.latitude is not None else None


# ---------------------------------------------------------------- evidence
def _own_source(db: Session, facility: Facility, source_id: int) -> FacilitySource:
    source = db.get(FacilitySource, source_id)
    if source is None or source.facility_id != facility.id:
        raise ServiceError("The selected source does not belong to this facility.")
    return source


def _own_contact(db: Session, facility: Facility, contact_id: int) -> FacilityContact:
    contact = db.get(FacilityContact, contact_id)
    if contact is None or contact.facility_id != facility.id:
        raise ServiceError("The selected contact does not belong to this facility.")
    return contact


def add_source(db: Session, facility: Facility, data: SourceIn, actor: User) -> FacilitySource:
    _require_platform_admin(actor)
    with transaction(db):
        source = FacilitySource(facility_id=facility.id, created_by=actor, **data.model_dump())
        db.add(source)
    return source


def add_contact(db: Session, facility: Facility, data: ContactIn, actor: User) -> FacilityContact:
    _require_platform_admin(actor)
    if facility.is_demo:
        raise ServiceError("The fictional demo facility cannot have contact details.")
    _own_source(db, facility, data.source_id)
    duplicate = db.scalar(select(FacilityContact.id).where(
        FacilityContact.facility_id == facility.id, FacilityContact.kind == data.kind,
        FacilityContact.value == data.value, FacilityContact.is_active.is_(True)))
    if duplicate:
        raise ServiceError(f"This {data.kind.value} is already recorded for the facility.")
    with transaction(db):
        contact = FacilityContact(facility_id=facility.id, created_by_id=actor.id, **data.model_dump())
        db.add(contact)
    return contact


def retire_contact(db: Session, facility: Facility, contact_id: int, actor: User) -> None:
    """Contacts are retired, never deleted, so the verification history stays intact."""
    _require_platform_admin(actor)
    contact = _own_contact(db, facility, contact_id)
    with transaction(db):
        contact.is_active = False
        _reload_evidence(db, facility)
        if facility.is_listed and listing_blockers(facility):
            facility.is_listed = False


# ---------------------------------------------------------------- verification
def record_verification(db: Session, facility: Facility, data: VerificationIn, actor: User) -> FacilityVerification:
    _require_platform_admin(actor)
    source = _own_source(db, facility, data.source_id) if data.source_id else None
    if data.contact_id is not None:
        contact = _own_contact(db, facility, data.contact_id)
        if not contact.is_active:
            raise ServiceError("A retired contact cannot be verified.")
    if facility.is_demo and data.status.is_positive:
        raise ServiceError("The fictional demo facility can never be verified.")

    if data.status == S.SOURCE_CONFIRMED:
        if source is None or not source.source_type.is_official:
            raise ServiceError("'Source confirmed' requires an official source: a government portal, "
                               "government document or the facility's own website.")
    elif data.status == S.FACILITY_CONFIRMED:
        if data.method not in DIRECT_METHODS or source is None or \
                source.source_type != SourceType.FACILITY_CONFIRMATION:
            raise ServiceError("'Facility confirmed' requires direct contact with the facility (call, email, "
                               "letter or visit) and a 'facility confirmation' source recording it.")
    elif data.status == S.FIELD_VERIFIED:
        if data.scope != VerificationScope.LOCATION or data.method != VerificationMethod.SITE_VISIT:
            raise ServiceError("'Field verified' applies only to location, after a site visit.")
        if source is None or source.source_type != SourceType.SITE_VISIT:
            raise ServiceError("'Field verified' requires a 'site visit' source.")
    if data.scope == VerificationScope.LOCATION and data.status.is_positive and not facility.has_coordinates:
        raise ServiceError("Record the facility's coordinates (with their source) before verifying its location.")

    with transaction(db):
        entry = FacilityVerification(facility_id=facility.id, verified_by_id=actor.id, **data.model_dump())
        db.add(entry)
        _reload_evidence(db, facility)
        if facility.is_listed and listing_blockers(facility):
            facility.is_listed = False       # e.g. identity now disputed
    return entry


@dataclass
class EffectiveStatus:
    status: VerificationStatus
    entry: FacilityVerification | None

    @property
    def is_positive(self) -> bool:
        return self.status.is_positive


def _effective(entry: FacilityVerification | None, today: date | None = None) -> EffectiveStatus:
    if entry is None:
        return EffectiveStatus(S.UNVERIFIED, None)
    today = today or date.today()
    if entry.status.is_positive and (today - entry.verified_on) > timedelta(days=settings.verification_max_age_days):
        return EffectiveStatus(S.OUTDATED, entry)
    return EffectiveStatus(entry.status, entry)


def _latest(entries) -> FacilityVerification | None:
    return max(entries, key=lambda e: (e.verified_on, e.id), default=None)


def scope_status(facility: Facility, scope: VerificationScope, today: date | None = None) -> EffectiveStatus:
    if scope == VerificationScope.CONTACT:
        raise ValueError("Use contact_status() for contacts.")
    return _effective(_latest(v for v in facility.verifications if v.scope == scope), today)


def contact_status(contact: FacilityContact, today: date | None = None) -> EffectiveStatus:
    entries = (v for v in contact.facility.verifications
               if v.scope == VerificationScope.CONTACT and v.contact_id == contact.id)
    return _effective(_latest(entries), today)


def verification_summary(facility: Facility, today: date | None = None) -> dict[str, EffectiveStatus]:
    return {s.value: scope_status(facility, s, today) for s in VerificationScope if s != VerificationScope.CONTACT}


# ---------------------------------------------------------------- publication rules
def listing_blockers(facility: Facility, today: date | None = None) -> list[str]:
    """Reasons a facility may not be publicly listed (empty list = may be listed)."""
    if facility.is_demo:
        return ["The fictional demo facility can never be listed."]
    blockers = []
    if scope_status(facility, VerificationScope.IDENTITY, today).status not in CONFIRMED_IDENTITY:
        blockers.append("Identity must be source-confirmed or facility-confirmed (and not outdated or disputed).")
    if facility.operational_status == OperationalStatus.CLOSED:
        blockers.append("The facility is marked as closed.")
    if not (facility.address_line and facility.city):
        blockers.append("Address line and city are required.")
    if not any(c.kind == ContactKind.PHONE and c.is_active and c.is_public for c in facility.contacts):
        blockers.append("At least one active, public phone number (with its source) is required.")
    return blockers


def set_listed(db: Session, facility: Facility, listed: bool, actor: User) -> None:
    _require_platform_admin(actor)
    if listed:
        _reload_evidence(db, facility)
        blockers = listing_blockers(facility)
        if blockers:
            raise ServiceError("Cannot list this facility: " + " ".join(blockers), blockers)
    with transaction(db):
        facility.is_listed = listed


def map_eligible(facility: Facility, today: date | None = None) -> bool:
    """A map marker requires a listed facility with sourced coordinates and a current positive location check."""
    return (facility.is_listed and facility.has_coordinates and facility.location_source_id is not None
            and scope_status(facility, VerificationScope.LOCATION, today).is_positive)
