"""Application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, settings
from app.database import init_db
from app.routers import api, auth, dashboard, donations, donors, inventory, issues, reports, requests, users
from app.web import CSRFError, Forbidden, NotAuthenticated, csrf_protect, redirect, render


def create_app(init: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if init:
            init_db()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan, dependencies=[Depends(csrf_protect)],
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, session_cookie="bbms_session",
        max_age=settings.session_max_age_seconds, same_site="lax", https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

    for r in (auth, dashboard, donors, donations, inventory, requests, issues, reports, users, api):
        app.include_router(r.router)

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

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return render(request, "errors/error.html", {
            "code": 403, "title": "Access denied",
            "message": "Your role does not permit this action."}, status_code=403)

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
        logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return render(request, "errors/error.html", {
            "code": 500, "title": "Something went wrong",
            "message": "The operation was not completed and no changes were saved. Please try again."},
            status_code=500)

    return app


logger = logging.getLogger("bloodbank")
app = create_app()
