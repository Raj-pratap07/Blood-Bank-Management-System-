"""Facility directory with provenance (Haldwani BloodLink, Phase 1).

Nothing here is pre-filled with real-world data: every fact about a real
facility must be entered by a platform administrator together with the source
it came from, and its trust level comes only from the verification log.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    ContactKind,
    OperationalStatus,
    Ownership,
    SourceType,
    StockVisibility,
    VerificationMethod,
    VerificationScope,
    VerificationStatus,
    db_enum,
)
from app.models.user import User
from app.utils import utcnow


class Facility(Base):
    __tablename__ = "facilities"
    __table_args__ = (
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_facility_coords_pair"),
        CheckConstraint("latitude IS NULL OR location_source_id IS NOT NULL", name="ck_facility_coords_sourced"),
        CheckConstraint("NOT (is_demo AND is_listed)", name="ck_facility_demo_never_listed"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(150))
    official_name: Mapped[str | None] = mapped_column(String(200))
    aliases: Mapped[str | None] = mapped_column(Text)              # one per line
    ownership: Mapped[Ownership] = mapped_column(db_enum(Ownership), default=Ownership.UNKNOWN)
    parent_hospital: Mapped[str | None] = mapped_column(String(200))
    address_line: Mapped[str | None] = mapped_column(String(300))
    locality: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    district: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(80))
    pincode: Mapped[str | None] = mapped_column(String(6))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    # Coordinates may only be stored together with the source they came from.
    location_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("facility_sources.id", use_alter=True, name="fk_facility_location_source"))
    eraktkosh_code: Mapped[str | None] = mapped_column(String(20))
    cdsco_license_no: Mapped[str | None] = mapped_column(String(60))
    has_component_separation: Mapped[bool | None]                 # None = unknown
    has_apheresis: Mapped[bool | None]
    operational_status: Mapped[OperationalStatus] = mapped_column(
        db_enum(OperationalStatus), default=OperationalStatus.UNKNOWN)
    is_listed: Mapped[bool] = mapped_column(default=False)         # shown publicly (Phase 2)
    is_participating: Mapped[bool] = mapped_column(default=False)  # manages inventory here (Phase 3)
    stock_visibility: Mapped[StockVisibility] = mapped_column(
        db_enum(StockVisibility), default=StockVisibility.HIDDEN)
    stock_max_age_hours: Mapped[int] = mapped_column(default=24)
    is_demo: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    sources: Mapped[list[FacilitySource]] = relationship(
        back_populates="facility", foreign_keys="FacilitySource.facility_id", order_by="FacilitySource.id")
    contacts: Mapped[list[FacilityContact]] = relationship(back_populates="facility", order_by="FacilityContact.id")
    verifications: Mapped[list[FacilityVerification]] = relationship(
        back_populates="facility", order_by="FacilityVerification.id")
    location_source: Mapped[FacilitySource | None] = relationship(foreign_keys=[location_source_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def alias_list(self) -> list[str]:
        return [a.strip() for a in (self.aliases or "").splitlines() if a.strip()]


class FacilitySource(Base):
    """A document, portal entry, communication or visit that evidence was taken from."""

    __tablename__ = "facility_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    source_type: Mapped[SourceType] = mapped_column(db_enum(SourceType))
    title: Mapped[str] = mapped_column(String(200))
    reference: Mapped[str] = mapped_column(String(500))            # URL or document/communication reference
    retrieved_on: Mapped[date]
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    facility: Mapped[Facility] = relationship(back_populates="sources", foreign_keys=[facility_id])
    created_by: Mapped[User] = relationship()


class FacilityContact(Base):
    __tablename__ = "facility_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    kind: Mapped[ContactKind] = mapped_column(db_enum(ContactKind))
    value: Mapped[str] = mapped_column(String(300))
    label: Mapped[str | None] = mapped_column(String(120))
    source_id: Mapped[int] = mapped_column(ForeignKey("facility_sources.id"))  # no unsourced contacts
    is_public: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)          # retired, never deleted (audit trail)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    facility: Mapped[Facility] = relationship(back_populates="contacts")
    source: Mapped[FacilitySource] = relationship()


class FacilityVerification(Base):
    """Append-only verification log. The latest entry per scope (and per contact) is authoritative."""

    __tablename__ = "facility_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    scope: Mapped[VerificationScope] = mapped_column(db_enum(VerificationScope))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("facility_contacts.id"))
    status: Mapped[VerificationStatus] = mapped_column(db_enum(VerificationStatus))
    method: Mapped[VerificationMethod] = mapped_column(db_enum(VerificationMethod))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("facility_sources.id"))
    evidence_note: Mapped[str] = mapped_column(Text)
    verified_on: Mapped[date]                                      # when the check was actually done
    verified_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(default=utcnow)

    facility: Mapped[Facility] = relationship(back_populates="verifications")
    contact: Mapped[FacilityContact | None] = relationship()
    source: Mapped[FacilitySource | None] = relationship()
    verified_by: Mapped[User] = relationship()
