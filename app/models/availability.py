"""Facility-published blood availability (Haldwani BloodLink Phase 4).

A publication is an explicit, authorised snapshot of a facility's ELIGIBLE inventory
(available, unexpired bags) aggregated per blood group and product. It never refers to
individual bags, donors, patients or requests. Rows are append-only (enforced by triggers);
the only later change allowed is withdrawing a publication.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import BloodGroup, Product, db_enum
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class AvailabilityPublication(Base):
    __tablename__ = "availability_publications"
    __table_args__ = (CheckConstraint("valid_until > published_at", name="ck_publication_validity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id"), index=True)
    published_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    valid_until: Mapped[datetime]
    withdrawn_at: Mapped[datetime | None]
    withdrawn_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    published_by: Mapped[User] = relationship(foreign_keys=[published_by_id])
    withdrawn_by: Mapped[User | None] = relationship(foreign_keys=[withdrawn_by_id])
    entries: Mapped[list[AvailabilityEntry]] = relationship(back_populates="publication",
                                                            order_by="AvailabilityEntry.id")


class AvailabilityEntry(Base):
    __tablename__ = "availability_entries"
    __table_args__ = (
        UniqueConstraint("publication_id", "blood_group", "product", name="uq_entry_group_product"),
        CheckConstraint("units >= 0", name="ck_entry_units_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("availability_publications.id"), index=True)
    blood_group: Mapped[BloodGroup] = mapped_column(db_enum(BloodGroup))
    product: Mapped[Product] = mapped_column(db_enum(Product))
    units: Mapped[int]

    publication: Mapped[AvailabilityPublication] = relationship(back_populates="entries")
