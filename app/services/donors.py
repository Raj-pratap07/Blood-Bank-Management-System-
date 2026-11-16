from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import BloodGroup, Donor
from app.schemas.forms import DonorIn
from app.services.errors import ServiceError
from app.services.tx import transaction


def _check_phone_unique(db: Session, phone: str, exclude_id: int | None = None) -> None:
    q = select(Donor.id).where(Donor.phone == phone)
    if exclude_id:
        q = q.where(Donor.id != exclude_id)
    if db.scalar(q):
        raise ServiceError(f"A donor with phone {phone} is already registered.")


def create_donor(db: Session, data: DonorIn) -> Donor:
    _check_phone_unique(db, data.phone)
    with transaction(db):
        donor = Donor(**data.model_dump())
        db.add(donor)
    return donor


def update_donor(db: Session, donor: Donor, data: DonorIn) -> Donor:
    _check_phone_unique(db, data.phone, exclude_id=donor.id)
    if donor.donations and data.blood_group != donor.blood_group:
        raise ServiceError(
            "Blood group cannot be changed after donations are recorded; "
            "correct it through laboratory re-grouping."
        )
    with transaction(db):
        for k, v in data.model_dump().items():
            setattr(donor, k, v)
    return donor


def set_donor_active(db: Session, donor: Donor, active: bool) -> None:
    with transaction(db):
        donor.is_active = active


def search_donors(db: Session, q: str = "", group: str = "", include_inactive: bool = False):
    stmt = select(Donor)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Donor.full_name.ilike(like), Donor.phone.ilike(like),
                              Donor.city.ilike(like), Donor.email.ilike(like)))
    if group in BloodGroup:
        stmt = stmt.where(Donor.blood_group == BloodGroup(group))
    if not include_inactive:
        stmt = stmt.where(Donor.is_active.is_(True))
    return stmt.order_by(Donor.full_name)
