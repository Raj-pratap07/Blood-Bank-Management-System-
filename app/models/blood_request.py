from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, Gender, RequestPriority, RequestStatus, UnitStatus, db_enum
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.blood_unit import BloodUnit
    from app.models.user import User


class BloodRequest(Base):
    __tablename__ = "blood_requests"
    __table_args__ = (CheckConstraint("units_required >= 1", name="ck_request_units_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owning facility (Phase 3). Required and immutable: enforced by database triggers (migration v3).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    patient_name: Mapped[str] = mapped_column(String(120))
    patient_age: Mapped[int]
    patient_gender: Mapped[Gender] = mapped_column(db_enum(Gender))
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup), index=True)
    units_required: Mapped[int]
    priority: Mapped[RequestPriority] = mapped_column(db_enum(RequestPriority), default=RequestPriority.ROUTINE)
    status: Mapped[RequestStatus] = mapped_column(db_enum(RequestStatus), default=RequestStatus.PENDING, index=True)
    hospital_name: Mapped[str] = mapped_column(String(150))
    hospital_address: Mapped[str | None] = mapped_column(Text)
    doctor_name: Mapped[str | None] = mapped_column(String(120))
    requester_name: Mapped[str] = mapped_column(String(120))
    requester_phone: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    required_by: Mapped[date | None]
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    # Recorded separately so a rejection never overwrites the original approval.
    rejected_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    rejected_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    rejected_by: Mapped[User | None] = relationship(foreign_keys=[rejected_by_id])
    reserved_units: Mapped[list[BloodUnit]] = relationship(
        back_populates="reserved_for", order_by="BloodUnit.expires_on"
    )
    issues: Mapped[list[Issue]] = relationship(back_populates="request", order_by="Issue.issued_at")

    @property
    def code(self) -> str:
        return f"RQ-{self.id:05d}"

    @property
    def units_issued(self) -> int:
        return len(self.issues)

    @property
    def units_reserved(self) -> int:
        return sum(1 for u in self.reserved_units if u.status == UnitStatus.RESERVED)

    @property
    def units_outstanding(self) -> int:
        return max(self.units_required - self.units_issued - self.units_reserved, 0)


class Issue(Base):
    """Issue (dispatch) of one blood unit against an approved request."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owning facility (Phase 3). Required and immutable: enforced by database triggers (migration v3).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("blood_requests.id"), index=True)
    # unique=True: a bag can never be issued twice, enforced by the database.
    blood_unit_id: Mapped[int] = mapped_column(ForeignKey("blood_units.id"), unique=True)
    issued_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    issued_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    received_by: Mapped[str] = mapped_column(String(120))
    crossmatch_confirmed: Mapped[bool]
    notes: Mapped[str | None] = mapped_column(Text)

    request: Mapped[BloodRequest] = relationship(back_populates="issues")
    blood_unit: Mapped[BloodUnit] = relationship(back_populates="issue")
    issued_by: Mapped[User] = relationship()
