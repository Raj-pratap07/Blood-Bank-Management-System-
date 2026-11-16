from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FACILITY_ROLES, User
from app.schemas.forms import Password, UserIn
from app.services import users as user_svc
from app.services.errors import ServiceError
from app.services.scope import facility_id_of
from app.web import flash, form_dict, redirect, render, require_admin, validate

# Every route here is admin-only (enforced server-side by the router dependency).
router = APIRouter(prefix="/users", dependencies=[Depends(require_admin)])
_password_adapter = TypeAdapter(Password)
ROLE_OPTIONS = [(r.value, r.label) for r in FACILITY_ROLES]


@router.get("")
def list_users(request: Request, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    # Only this facility's accounts; platform administrators are never listed.
    users = user_svc.facility_users(db, facility_id_of(actor))
    return render(request, "users/list.html", {"users": users})


def _own_account(db: Session, user_id: int, actor: User) -> User:
    """An account of the admin's own facility; anything else is a 404 (no information leak)."""
    from fastapi import HTTPException

    target = db.get(User, user_id)
    if target is None or target.role.value == "platform_admin" or target.facility_id != facility_id_of(actor):
        raise HTTPException(status_code=404, detail="Not found")
    return target


@router.get("/new")
def new_user_form(request: Request):
    return render(request, "users/form.html", {"form": {"role": "staff"}, "errors": {},
                                               "role_options": ROLE_OPTIONS})


@router.post("/new")
async def create_user(request: Request, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    raw = await form_dict(request)
    data, errors = validate(UserIn, dict(raw))
    if not errors:
        try:
            u = user_svc.create_user(db, data, actor)
            flash(request, f"Account '{u.username}' created ({u.role.label}).")
            return redirect("/users")
        except ServiceError as e:
            errors["username"] = e.message
    raw.pop("password", None)
    return render(request, "users/form.html", {"form": raw, "errors": errors, "role_options": ROLE_OPTIONS}, status_code=400)


@router.post("/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _own_account(db, user_id, actor)
    try:
        user_svc.set_active(db, target, not target.is_active, actor)
        flash(request, f"Account '{target.username}' {'activated' if target.is_active else 'deactivated'}.")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect("/users")


@router.post("/{user_id}/password")
async def reset_password(request: Request, user_id: int, actor: User = Depends(require_admin),
                         db: Session = Depends(get_db)):
    target = _own_account(db, user_id, actor)
    raw = await form_dict(request)
    try:
        pw = _password_adapter.validate_python(raw.get("new_password") or "")
        user_svc.reset_password(db, target, pw, actor)
        flash(request, f"Password reset for '{target.username}'.")
    except ValidationError:
        flash(request, "Password must be 8-72 characters and at most 72 bytes "
                       "(fewer characters when using non-English letters).", "danger")
    except ServiceError as e:
        flash(request, e.message, "danger")
    return redirect("/users")
