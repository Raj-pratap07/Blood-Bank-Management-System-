from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FACILITY_ROLES, Facility, OperationalStatus, Role, User
from app.config import settings
from app.schemas.forms import OnboardIn, UserIn
from app.services.errors import PermissionDenied, RecordNotFound, ServiceError
from app.services.scope import facility_id_of
from app.services.security import DUMMY_HASH, hash_password, verify_password
from app.services.tx import transaction
from app.utils import utcnow


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(password, DUMMY_HASH)  # equalise timing
        return None
    if not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


def _guard_target(target: User, actor: User) -> None:
    """A facility administrator may only manage accounts of their own facility (never platform accounts)."""
    if target.role == Role.PLATFORM_ADMIN:
        raise PermissionDenied("Platform administrator accounts can only be managed by the platform.")
    if target.facility_id != facility_id_of(actor):
        raise RecordNotFound("Account not found.")   # indistinguishable from a non-existent account


def _username_free(db: Session, username: str) -> None:
    if db.scalar(select(func.count()).where(User.username == username)):
        raise ServiceError(f"Username '{username}' is already taken.")


def create_user(db: Session, data: UserIn, actor: User) -> User:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can manage accounts.")
    if data.role not in FACILITY_ROLES:
        raise PermissionDenied("Administrators can only create staff, medical officer or administrator accounts.")
    fid = facility_id_of(actor)                         # new accounts always join the admin's own facility
    _username_free(db, data.username)
    with transaction(db):
        user = User(username=data.username, full_name=data.full_name, role=data.role, facility_id=fid,
                    password_hash=hash_password(data.password), must_change_password=True)
        db.add(user)
    return user


def set_active(db: Session, user: User, active: bool, actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can manage accounts.")
    _guard_target(user, actor)
    if user.id == actor.id and not active:
        raise ServiceError("You cannot deactivate your own account.")
    if not active and user.role == Role.ADMIN:
        admins = db.scalar(select(func.count()).where(
            User.role == Role.ADMIN, User.is_active.is_(True), User.facility_id == user.facility_id))
        if admins <= 1:
            raise ServiceError("At least one active administrator is required for this blood bank.")
    with transaction(db):
        user.is_active = active


def reset_password(db: Session, user: User, new_password: str, actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can reset passwords.")
    _guard_target(user, actor)
    with transaction(db):
        user.password_hash = hash_password(new_password)
        user.must_change_password = True            # the admin knows it; the user must replace it


def change_own_password(db: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise ServiceError("Current password is incorrect.")
    if verify_password(new, user.password_hash):
        raise ServiceError("Choose a password different from the current one.")
    with transaction(db):
        user.password_hash = hash_password(new)
        user.must_change_password = False
        user.password_set_at = utcnow()


def facility_users(db: Session, facility_id: int) -> list[User]:
    return list(db.scalars(select(User).where(User.facility_id == facility_id, User.role.in_(FACILITY_ROLES))
                           .order_by(User.role, User.username)))


def authorizers(db: Session, facility_id: int) -> list[User]:
    """Active administrators / medical officers of ONE facility (eligible to clear donors)."""
    return list(db.scalars(
        select(User).where(User.is_active.is_(True), User.facility_id == facility_id,
                           User.role.in_([Role.ADMIN, Role.MEDICAL_OFFICER]))
        .order_by(User.full_name)
    ))


def create_platform_admin(db: Session, username: str, full_name: str, password: str) -> User:
    """Bootstrap path used only by `scripts/create_platform_admin.py` (no web route can create one)."""
    data = UserIn(username=username, full_name=full_name, role=Role.PLATFORM_ADMIN, password=password)
    _username_free(db, data.username)
    with transaction(db):
        user = User(username=data.username, full_name=data.full_name, role=Role.PLATFORM_ADMIN, facility_id=None,
                    password_hash=hash_password(data.password), password_set_at=utcnow())
        db.add(user)
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_setup_token(user: User) -> str:
    """Create a single-use setup link token. Only its hash is stored; the token is shown once."""
    token = secrets.token_urlsafe(32)
    user.setup_token_hash = _token_hash(token)
    user.setup_token_expires_at = utcnow() + timedelta(hours=settings.setup_link_valid_hours)
    user.password_hash = hash_password(secrets.token_urlsafe(32))   # unusable until set up
    user.password_set_at = None
    return token


def onboard_facility_admin(db: Session, facility: Facility, data: OnboardIn, actor: User) -> tuple[User, str]:
    """Platform onboarding: create the facility's administrator WITHOUT a password and mark the
    facility as participating. Returns (user, one-time setup token). The platform administrator never
    knows the password and gains no access to the facility's data.
    """
    if actor.role != Role.PLATFORM_ADMIN or not actor.is_active:
        raise PermissionDenied("Only a platform administrator can onboard a facility.")
    if facility.operational_status == OperationalStatus.CLOSED:
        raise ServiceError("A facility marked as closed cannot be onboarded.")
    _username_free(db, data.username)
    with transaction(db):
        user = User(username=data.username, full_name=data.full_name, role=Role.ADMIN, facility_id=facility.id,
                    password_hash="!")
        token = _issue_setup_token(user)
        db.add(user)
        facility.is_participating = True
    return user, token


def reissue_setup_link(db: Session, facility: Facility, user_id: int, actor: User) -> tuple[User, str]:
    """New link for an onboarded admin who has not completed setup yet (the old link stops working)."""
    if actor.role != Role.PLATFORM_ADMIN or not actor.is_active:
        raise PermissionDenied("Only a platform administrator can issue setup links.")
    user = db.get(User, user_id)
    if user is None or user.facility_id != facility.id or user.role != Role.ADMIN:
        raise RecordNotFound("Account not found.")
    if user.password_set_at is not None or not user.is_active:
        raise ServiceError("This account is already set up (or inactive); its owner manages their own password.")
    with transaction(db):
        token = _issue_setup_token(user)
    return user, token


def pending_setup_accounts(db: Session, facility_id: int) -> list[User]:
    return list(db.scalars(select(User).where(User.facility_id == facility_id, User.setup_token_hash.is_not(None),
                                              User.password_set_at.is_(None)).order_by(User.username)))


def user_for_setup_token(db: Session, token: str) -> User | None:
    if not token or len(token) > 200:
        return None
    user = db.scalar(select(User).where(User.setup_token_hash == _token_hash(token)))
    if user is None or not user.is_active or user.setup_token_expires_at is None             or user.setup_token_expires_at <= utcnow():
        return None
    return user


def complete_setup(db: Session, token: str, new_password: str) -> User:
    user = user_for_setup_token(db, token)
    if user is None:
        raise ServiceError("This setup link is invalid, expired or has already been used.")
    with transaction(db):
        user.password_hash = hash_password(new_password)
        user.setup_token_hash = None                 # single use
        user.setup_token_expires_at = None
        user.password_set_at = utcnow()
        user.must_change_password = False
    return user


def deactivate_facility_accounts(db: Session, facility_id: int) -> int:
    """Facility closure: every account of the facility is deactivated (records are kept). No commit here."""
    users = list(db.scalars(select(User).where(User.facility_id == facility_id, User.is_active.is_(True))))
    for u in users:
        u.is_active = False
        u.setup_token_hash = None
        u.setup_token_expires_at = None
    return len(users)


def account_count(db: Session, facility_id: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.facility_id == facility_id)) or 0
