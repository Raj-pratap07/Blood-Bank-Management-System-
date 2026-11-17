"""Application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import log_redaction
from app.config import (
    BASE_DIR,
    check_secret_key,
    is_production,
    session_cookie_secure,
    settings,
    validate_settings,
)
from app.database import get_db, init_db
from app.routers import (
    api,
    auth,
    availability,
    dashboard,
    donations,
    donors,
    inventory,
    issues,
    platform,
    public,
    reports,
    requests,
    users,
)
from app.web import (
    CSRFError,
    DemoReadOnly,
    Forbidden,
    NotAuthenticated,
    PasswordChangeRequired,
    csrf_protect,
    demo_guard,
    flash,
    redirect,
    render,
)


def create_app(init: bool = True) -> FastAPI:
    # Setup tokens must never reach a log line (access log, error log or traceback).
    log_redaction.install()

    # Fail fast (before serving anything) on invalid settings or a dev/weak production secret key.
    validate_settings(settings)
    check_secret_key(settings.app_env, settings.secret_key)
    secure_cookie = session_cookie_secure(settings)
    if is_production(settings.app_env) and not secure_cookie:
        logger.warning("APP_ENV=production with BBMS_SESSION_HTTPS_ONLY=false: session cookies will be "
                       "sent over plain HTTP. Serve the app over HTTPS and remove this override.")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log_redaction.install()            # again at start-up: covers handlers configured after import
        if init:
            init_db()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan, dependencies=[Depends(demo_guard), Depends(csrf_protect)],
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, session_cookie="bbms_session",
        max_age=settings.session_max_age_seconds, same_site="lax", https_only=secure_cookie,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

    for r in (auth, dashboard, public, donors, donations, inventory, requests, issues, reports, users, api, platform, availability):
        app.include_router(r.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz(db: Session = Depends(get_db)):
        """Liveness + database check for the hosting platform. Reveals no data."""
        try:
            db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any database failure means unhealthy
            return JSONResponse({"status": "error"}, status_code=503, headers={"Cache-Control": "no-store"})
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(NotAuthenticated)
    async def _not_auth(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return redirect(f"/login?next={quote(nxt)}")

    @app.exception_handler(PasswordChangeRequired)
    async def _must_change(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Password change required"}, status_code=403)
        flash(request, "Please choose your own password before continuing.", "warning")
        return redirect("/account/password")

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return render(request, "errors/error.html", {
            "code": 403, "title": "Access denied",
            "message": "Your role does not permit this action."}, status_code=403)

    @app.exception_handler(DemoReadOnly)
    async def _demo_read_only(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Disabled in the public demo"}, status_code=403)
        return render(request, "errors/error.html", {
            "code": 403, "title": "Disabled in the public demo",
            "message": "Account, password and facility-directory changes are disabled in this public demonstration, "
                       "because all visitors share the demo accounts and the directory lists real blood banks. "
                       "Donor, donation, inventory, request and issue workflows remain available."}, status_code=403)

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, _exc):
        return render(request, "errors/error.html", {
            "code": 403, "title": "Form expired",
            "message": "The security token was missing or invalid. Go back, reload the page and try again."},
            status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        title = {404: "Not found", 405: "Method not allowed"}.get(exc.status_code, "Error")
        return render(request, "errors/error.html",
                      {"code": exc.status_code, "title": title, "message": str(exc.detail)},
                      status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return render(request, "errors/error.html", {
            "code": 422, "title": "Invalid request",
            "message": "Some parameters were invalid. Please check the link or form and try again."},
            status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, log_redaction.redact(request.url.path),
                         exc_info=exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return render(request, "errors/error.html", {
            "code": 500, "title": "Something went wrong",
            "message": "The operation was not completed and no changes were saved. Please try again."},
            status_code=500)

    return app


logger = logging.getLogger("bloodbank")
app = create_app()
