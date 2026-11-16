from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.schemas.forms import PasswordChangeIn
from app.services import users as user_svc
from app.services.errors import ServiceError
from app.services.security import get_csrf_token
from app.web import current_user, flash, form_dict, redirect, render, validate

router = APIRouter()


def _safe_next(nxt: str | None) -> str:
    # Only allow local paths (prevents open redirects like //evil.com).
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/"


@router.get("/login")
def login_page(request: Request, next: str | None = None):
    if request.session.get("user_id"):
        return redirect("/")
    return render(request, "login.html", {"next": next or ""})


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    nxt = str(form.get("next") or "")
    user = user_svc.authenticate(db, username, password) if username and password else None
    if user is None:
        return render(request, "login.html", {"error": "Invalid username or password.", "next": nxt,
                                              "username": username}, status_code=401)
    request.session.clear()  # prevent session fixation
    request.session["user_id"] = user.id
    get_csrf_token(request.session)
    if user.must_change_password:
        flash(request, "Welcome. Your account was set up by an administrator: please choose your own password.",
              "warning")
        return redirect("/account/password")
    flash(request, f"Welcome, {user.full_name}.")
    return redirect(_safe_next(nxt))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/account/password")
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account/password.html", {"errors": {}})


# ------------------------------------------------------------------ one-time setup links
def _setup_page(request: Request, token: str, valid: bool, errors: dict, status_code: int = 200):
    resp = render(request, "account/setup.html", {"token": token, "valid": valid, "errors": errors},
                  status_code=status_code)
    # The URL of every setup response carries the token: no caching, no Referer (GET and POST alike).
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/account/setup/{token}")
def setup_page(request: Request, token: str, db: Session = Depends(get_db)):
    if request.session.get("user_id"):
        request.session.clear()            # never complete someone's setup inside another session
    valid = user_svc.user_for_setup_token(db, token) is not None
    return _setup_page(request, token, valid, {}, 200 if valid else 404)


@router.post("/account/setup/{token}")
async def setup_submit(request: Request, token: str, db: Session = Depends(get_db)):
    from app.schemas.forms import SetupIn

    data, errors = validate(SetupIn, await form_dict(request))
    if not errors:
        try:
            user = user_svc.complete_setup(db, token, data.new_password)
            flash(request, f"Password set for {user.username}. You can now sign in.")
            return redirect("/login")
        except ServiceError as e:
            errors = {"__all__": e.message}
    valid = user_svc.user_for_setup_token(db, token) is not None
    return _setup_page(request, token, valid, errors, 400)


@router.post("/account/password")
async def change_password(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data, errors = validate(PasswordChangeIn, await form_dict(request))
    if not errors:
        try:
            user_svc.change_own_password(db, user, data.current_password, data.new_password)
            flash(request, "Password changed.")
            return redirect("/")
        except ServiceError as e:
            errors = {"current_password": e.message}
    return render(request, "account/password.html", {"errors": errors}, status_code=400)
