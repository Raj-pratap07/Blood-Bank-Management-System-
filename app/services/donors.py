from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import BloodGroup, Donor, User
from app.schemas.forms import DonorIn
from app.services.errors import ServiceError
from app.services.scope import ensure_owned, facility_id_of, scoped
from app.services.tx import transaction


def _check_phone_unique(db: Session, facility_id: int, phone: str, exclude_id: int | None = None) -> None:
    # Scoped to the facility: never reveals whether another blood bank has this number.
    q = select(Donor.id).where(Donor.facility_id == facility_id, Donor.phone == phone)
    if exclude_id:
        q = q.where(Donor.id != exclude_id)
    if db.scalar(q):
        raise ServiceError(f"A donor with phone {phone} is already registered.")


def create_donor(db: Session, data: DonorIn, actor: User) -> Donor:
    fid = facility_id_of(actor)
    _check_phone_unique(db, fid, data.phone)
    with transaction(db):
        donor = Donor(**data.model_dump(), facility_id=fid)   # facility comes from the account, never the form
        db.add(donor)
    return donor


def update_donor(db: Session, donor: Donor, data: DonorIn, actor: User) -> Donor:
    ensure_owned(donor, actor, "Donor")
    _check_phone_unique(db, donor.facility_id, data.phone, exclude_id=donor.id)
    if donor.donations and data.blood_group != donor.blood_group:
        raise ServiceError(
            "Blood group cannot be changed after donations are recorded; "
            "correct it through laboratory re-grouping."
        )
    with transaction(db):
        for k, v in data.model_dump().items():   # DonorIn has no facility_id: ownership cannot change
            setattr(donor, k, v)
    return donor


def set_donor_active(db: Session, donor: Donor, active: bool, actor: User) -> None:
    ensure_owned(donor, actor, "Donor")
    with transaction(db):
        donor.is_active = active


def search_donors(db: Session, facility_id: int, q: str = "", group: str = "", include_inactive: bool = False):
    stmt = scoped(select(Donor), Donor, facility_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Donor.full_name.ilike(like), Donor.phone.ilike(like),
                              Donor.city.ilike(like), Donor.email.ilike(like)))
    if group in BloodGroup:
        stmt = stmt.where(Donor.blood_group == BloodGroup(group))
    if not include_inactive:
        stmt = stmt.where(Donor.is_active.is_(True))
    return stmt.order_by(Donor.full_name)
