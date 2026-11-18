"""Public (anonymous) emergency blood requests and their append-only audit trail (Phase 6, migration v5).

A public request is NOT a staff BloodRequest: it carries no patient identity and has no staff creator.
Authorised staff of the receiving facility review it; accepting it creates a normal BloodRequest (patient
details collected by staff), which then follows the existing medical-approval, reservation and issue rules.
The requester can see only the status, using the reference AND the one-time tracking code (stored hashed).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, Product, PublicRequestStatus, db_enum
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.blood_request import BloodRequest
    from app.models.facility import Facility


class PublicRequest(Base):
    __tablename__ = "public_requests"
    __table_args__ = (CheckConstraint("units >= 1 AND units <= 10", name="ck_public_request_units"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup))
    product: Mapped[Product] = mapped_column(db_enum(Product))
    units: Mapped[int]
    required_by: Mapped[datetime]
    location: Mapped[str] = mapped_column(String(150))
    requester_name: Mapped[str] = mapped_column(String(120))
    contact_phone: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[PublicRequestStatus] = mapped_column(
        db_enum(PublicRequestStatus), default=PublicRequestStatus.SUBMITTED, index=True)
    public_message: Mapped[str | None] = mapped_column(String(500))
    blood_request_id: Mapped[int | None] = mapped_column(ForeignKey("blood_requests.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    facility: Mapped[Facility] = relationship()
    blood_request: Mapped[BloodRequest | None] = relationship()
    events: Mapped[list[PublicRequestEvent]] = relationship(
        back_populates="public_request", order_by="(PublicRequestEvent.created_at, PublicRequestEvent.id)")

    @property
    def masked_phone(self) -> str:
        digits = self.contact_phone
        return "•" * max(0, len(digits) - 3) + digits[-3:]


class PublicRequestEvent(Base):
    """Append-only audit trail (triggers forbid UPDATE and DELETE). actor_id NULL = the anonymous requester."""
    __tablename__ = "public_request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_request_id: Mapped[int] = mapped_column(ForeignKey("public_requests.id"), index=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    public_request: Mapped[PublicRequest] = relationship(back_populates="events")
