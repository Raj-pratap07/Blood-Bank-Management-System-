from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, DonationStatus, Product, TestResult, db_enum
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.blood_unit import BloodUnit
    from app.models.donor import Donor
    from app.models.user import User

SCREENING_MARKERS = (
    ("hiv_result", "HIV 1 & 2"),
    ("hbv_result", "Hepatitis B (HBsAg)"),
    ("hcv_result", "Hepatitis C"),
    ("syphilis_result", "Syphilis"),
    ("malaria_result", "Malaria"),
)


class Donation(Base):
    __tablename__ = "donations"
    __table_args__ = (
        CheckConstraint("units_collected >= 1", name="ck_donation_units_positive"),
        CheckConstraint("volume_ml IN (350, 450)", name="ck_donation_volume"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owning facility (Phase 3). Required and immutable: enforced by database triggers (migration v3).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    donor_id: Mapped[int] = mapped_column(ForeignKey("donors.id"), index=True)
    donation_date: Mapped[date] = mapped_column(index=True)
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup))
    product: Mapped[Product] = mapped_column(db_enum(Product), default=Product.WHOLE_BLOOD)
    volume_ml: Mapped[int] = mapped_column(default=350)
    units_collected: Mapped[int] = mapped_column(default=1)
    status: Mapped[DonationStatus] = mapped_column(
        db_enum(DonationStatus), default=DonationStatus.QUARANTINE, index=True
    )

    # Pre-donation screening (recorded at the time of collection)
    weight_kg: Mapped[float]
    hemoglobin_g_dl: Mapped[float]
    bp_systolic: Mapped[int]
    bp_diastolic: Mapped[int]
    pulse_bpm: Mapped[int]
    temperature_c: Mapped[float]
    questionnaire_ok: Mapped[bool]
    pre_donation_notes: Mapped[str | None] = mapped_column(Text)
    cleared_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    # Laboratory screening (post-collection, while in quarantine)
    hiv_result: Mapped[TestResult | None] = mapped_column(db_enum(TestResult))
    hbv_result: Mapped[TestResult | None] = mapped_column(db_enum(TestResult))
    hcv_result: Mapped[TestResult | None] = mapped_column(db_enum(TestResult))
    syphilis_result: Mapped[TestResult | None] = mapped_column(db_enum(TestResult))
    malaria_result: Mapped[TestResult | None] = mapped_column(db_enum(TestResult))
    abo_rh_confirmed: Mapped[bool | None]
    screening_notes: Mapped[str | None] = mapped_column(Text)
    screened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    screened_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    donor: Mapped[Donor] = relationship(back_populates="donations")
    units: Mapped[list[BloodUnit]] = relationship(back_populates="donation", order_by="BloodUnit.id")
    cleared_by: Mapped[User] = relationship(foreign_keys=[cleared_by_id])
    recorded_by: Mapped[User] = relationship(foreign_keys=[recorded_by_id])
    screened_by: Mapped[User | None] = relationship(foreign_keys=[screened_by_id])

    @property
    def code(self) -> str:
        return f"DN-{self.id:05d}"

    @property
    def screening_summary(self) -> str:
        if self.status == DonationStatus.QUARANTINE:
            return "Pending"
        results = [getattr(self, f) for f, _ in SCREENING_MARKERS]
        if any(r == TestResult.REACTIVE for r in results):
            return "Reactive"
        return "Non-reactive" if all(r == TestResult.NON_REACTIVE for r in results) else "Incomplete"
