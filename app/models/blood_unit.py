from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, Product, UnitStatus, db_enum
from app.utils import utcnow, business_today

if TYPE_CHECKING:
    from app.models.blood_request import BloodRequest, Issue
    from app.models.donation import Donation


class BloodUnit(Base):
    """One physical blood bag."""

    __tablename__ = "blood_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owning facility (Phase 3). Required and immutable: enforced by database triggers (migration v3).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    unit_code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    donation_id: Mapped[int] = mapped_column(ForeignKey("donations.id"), index=True)
    product: Mapped[Product] = mapped_column(db_enum(Product), default=Product.WHOLE_BLOOD)
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup), index=True)
    volume_ml: Mapped[int]
    collected_on: Mapped[date]
    expires_on: Mapped[date] = mapped_column(index=True)
    status: Mapped[UnitStatus] = mapped_column(db_enum(UnitStatus), default=UnitStatus.QUARANTINE, index=True)
    reserved_for_request_id: Mapped[int | None] = mapped_column(ForeignKey("blood_requests.id"), index=True)
    status_note: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    donation: Mapped[Donation] = relationship(back_populates="units")
    reserved_for: Mapped[BloodRequest | None] = relationship(back_populates="reserved_units")
    issue: Mapped[Issue | None] = relationship(back_populates="blood_unit", uselist=False)

    def days_to_expiry(self, on: date | None = None) -> int:
        return (self.expires_on - (on or business_today())).days

    def is_expired(self, on: date | None = None) -> bool:
        """A unit is usable up to and including its expiry date."""
        return self.expires_on < (on or business_today())
