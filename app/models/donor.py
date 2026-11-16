from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, Gender, db_enum
from app.utils import age_on, utcnow

if TYPE_CHECKING:
    from app.models.donation import Donation


class Donor(Base):
    __tablename__ = "donors"
    # The same person may register at several blood banks: phone is unique per facility.
    __table_args__ = (Index("ux_donors_facility_phone", "facility_id", "phone", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owning facility (Phase 3). Required and immutable: enforced by database triggers (migration v3).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(120), index=True)
    gender: Mapped[Gender] = mapped_column(db_enum(Gender))
    date_of_birth: Mapped[date]
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup), index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(80))
    weight_kg: Mapped[float]
    # Last donation made at another centre (used for interval + first-time status).
    last_external_donation_date: Mapped[date | None]
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    donations: Mapped[list[Donation]] = relationship(
        back_populates="donor", order_by="desc(Donation.donation_date)"
    )

    def age(self, on: date | None = None) -> int:
        return age_on(self.date_of_birth, on or date.today())

    @property
    def last_donation_date(self) -> date | None:
        dates = [d.donation_date for d in self.donations]
        if self.last_external_donation_date:
            dates.append(self.last_external_donation_date)
        return max(dates) if dates else None
