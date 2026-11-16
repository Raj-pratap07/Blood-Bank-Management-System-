from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Role, User
from app.schemas.forms import UserIn
from app.services.errors import PermissionDenied, ServiceError
from app.services.security import DUMMY_HASH, hash_password, verify_password
from app.services.tx import transaction


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(password, DUMMY_HASH)  # equalise timing
        return None
    if not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


def create_user(db: Session, data: UserIn, actor: User) -> User:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can manage accounts.")
    if db.scalar(select(func.count()).where(User.username == data.username)):
        raise ServiceError(f"Username '{data.username}' is already taken.")
    with transaction(db):
        user = User(
            username=data.username, full_name=data.full_name, role=data.role,
            password_hash=hash_password(data.password),
        )
        db.add(user)
    return user


def set_active(db: Session, user: User, active: bool, actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can manage accounts.")
    if user.id == actor.id and not active:
        raise ServiceError("You cannot deactivate your own account.")
    if not active and user.role == Role.ADMIN:
        admins = db.scalar(select(func.count()).where(User.role == Role.ADMIN, User.is_active.is_(True)))
        if admins <= 1:
            raise ServiceError("At least one active administrator is required.")
    with transaction(db):
        user.is_active = active


def reset_password(db: Session, user: User, new_password: str, actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Only administrators can reset passwords.")
    with transaction(db):
        user.password_hash = hash_password(new_password)


def change_own_password(db: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise ServiceError("Current password is incorrect.")
    with transaction(db):
        user.password_hash = hash_password(new)


def authorizers(db: Session) -> list[User]:
    return list(db.scalars(
        select(User).where(User.is_active.is_(True), User.role.in_([Role.ADMIN, Role.MEDICAL_OFFICER]))
        .order_by(User.full_name)
    ))
