"""Shared web-layer helpers: templates, flash messages, form parsing, auth dependencies."""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.database import get_db
from app.models import BloodGroup, Role, User
from app.services import compatibility, eligibility
from app.services.security import CSRF_FORM_FIELD, csrf_token_valid, get_csrf_token

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ------------------------------------------------------------------ exceptions
class NotAuthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFError(Exception):
    pass


# ------------------------------------------------------------------ flash
def flash(request: Request, message: str, category: str = "success") -> None:
    msgs = list(request.session.get("_flashes", []))
    msgs.append([category, message])
    request.session["_flashes"] = msgs


def pop_flashes(request: Request) -> list:
    return request.session.pop("_flashes", [])


# ------------------------------------------------------------------ jinja setup
def _label(value: Any) -> str:
    v = getattr(value, "value", value)
    return str(v).replace("_", " ").title() if v is not None else ""


def _fmt_date(value: Any, fmt: str = "%d %b %Y") -> str:
    return value.strftime(fmt) if value else "—"


templates.env.globals.update(
    settings=settings,
    BLOOD_GROUPS=[g.value for g in BloodGroup],
    csrf_token=lambda request: get_csrf_token(request.session),
    pop_flashes=pop_flashes,
    today=date.today,
    COMPAT_DISCLAIMER=compatibility.DISCLAIMER,
    ELIGIBILITY_DISCLAIMER=eligibility.DISCLAIMER,
    Role=Role,
)
templates.env.filters.update(label=_label, d=_fmt_date)


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"user": getattr(request.state, "user", None)}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ forms
async def form_dict(request: Request) -> dict[str, Any]:
    """Form data as a dict: empty strings -> None, repeated keys -> lists, checkboxes -> bool."""
    form = await request.form()
    data: dict[str, Any] = {}
    for key in form:
        if key == CSRF_FORM_FIELD:
            continue
        values = form.getlist(key)
        if key.endswith("[]"):
            data[key[:-2]] = [v for v in values if v != ""]
        else:
            v = values[-1]
            data[key] = None if isinstance(v, str) and v.strip() == "" else v
    return data


def validate(model: type[BaseModel], data: dict) -> tuple[Any, dict[str, str]]:
    """Return (model_instance, {}) or (None, {field: message})."""
    for name, field in model.model_fields.items():
        if field.annotation is bool and name not in data:
            data[name] = False  # unchecked checkbox
    try:
        return model.model_validate(data), {}
    except ValidationError as exc:
        errors: dict[str, str] = {}
        for err in exc.errors():
            loc = str(err["loc"][0]) if err["loc"] else "__all__"
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "missing":
                msg = "This field is required."
            errors.setdefault(loc, msg)
        return None, errors


# ------------------------------------------------------------------ pagination
class Page:
    def __init__(self, db: Session, stmt, page: int, size: int = settings.page_size):
        self.total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        self.pages = max(1, math.ceil(self.total / size))
        self.page = min(max(1, page), self.pages)
        self.items = list(db.scalars(stmt.offset((self.page - 1) * size).limit(size)))


# ------------------------------------------------------------------ date filters
class DateRange:
    """A parsed From/To query filter.

    HTML ``<input type="date">`` submits an *empty string* when left blank
    (``?start=&end=``). Declaring the route parameters as ``date | None`` makes
    FastAPI reject ``""`` with a 422, so routes accept raw strings and parse here:
    blank -> no bound, ISO ``YYYY-MM-DD`` -> date, anything else or From > To ->
    a user-facing ``error`` and the filter is not applied.
    """

    def __init__(self, start: date | None, end: date | None, error: str | None = None,
                 raw_start: str = "", raw_end: str = ""):
        self.start, self.end, self.error = start, end, error
        self.raw_start, self.raw_end = raw_start, raw_end


def parse_date_range(start: str | None, end: str | None) -> DateRange:
    raw_start, raw_end = (start or "").strip(), (end or "").strip()
    parsed: dict[str, date | None] = {}
    for label, raw in (("From", raw_start), ("To", raw_end)):
        if not raw:
            parsed[label] = None
            continue
        try:
            parsed[label] = date.fromisoformat(raw)
        except ValueError:
            return DateRange(None, None, f"{label} date '{raw}' is not a valid date (use YYYY-MM-DD).",
                             raw_start, raw_end)
    s, e = parsed["From"], parsed["To"]
    if s and e and s > e:
        return DateRange(None, None, f"From date ({s:%d %b %Y}) cannot be later than To date ({e:%d %b %Y}).",
                         raw_start, raw_end)
    return DateRange(s, e, None, raw_start, raw_end)


# ------------------------------------------------------------------ security deps
async def csrf_protect(request: Request) -> None:
    """Global dependency: every state-changing request must carry the session CSRF token."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        form = await request.form()
        token = form.get(CSRF_FORM_FIELD) or request.headers.get("X-CSRF-Token")
        if not csrf_token_valid(request.session, token if isinstance(token, str) else None):
            raise CSRFError()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        raise NotAuthenticated()
    request.state.user = user
    return user


def require_roles(*roles: Role):
    def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise Forbidden()
        return user
    return _dep


require_admin = require_roles(Role.ADMIN)
require_authorizer = require_roles(Role.ADMIN, Role.MEDICAL_OFFICER)


def get_or_404(db: Session, model, obj_id: int):
    from fastapi import HTTPException

    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj
