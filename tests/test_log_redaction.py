"""Regression: one-time setup tokens must never appear in any log output.

Covers the real Uvicorn access log (separate server process), the application's error path
(logged traceback), and the redaction helper. Also re-checks single use, expiry and
re-issue invalidation of setup links over HTTP.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import time
from datetime import timedelta

import httpx
import pytest

from app import log_redaction
from app.config import BASE_DIR
from app.schemas.facilities import FacilityIn
from app.schemas.forms import OnboardIn
from app.services import facilities as fsvc
from app.services import users as user_svc
from app.utils import utcnow
from tests.conftest import csrf_from, login

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def _onboard(db, users, name="Token Test Facility (fictional)", username="tok.admin"):
    p = users["platform_admin"]
    f = fsvc.create_facility(db, FacilityIn(display_name=name), p)
    admin, token = user_svc.onboard_facility_admin(db, f, OnboardIn(username=username, full_name="Token Admin"), p)
    return f, admin, token


# ------------------------------------------------------------------ helper
class TestRedactHelper:
    @pytest.mark.parametrize("raw,expected", [
        ("/account/setup/abcDEF_123-xyz", "/account/setup/[REDACTED]"),
        ("/account/setup/abc?next=1", "/account/setup/[REDACTED]?next=1"),
        ('GET /account/setup/abc HTTP/1.1" 200', 'GET /account/setup/[REDACTED] HTTP/1.1" 200'),
        ("https://x.example/account/setup/abc#frag", "https://x.example/account/setup/[REDACTED]#frag"),
        ("/account/password", "/account/password"),
        ("/account/setup/", "/account/setup/"),
        # M2: case variants, repeated/back slashes and percent-encoded separators
        ("/Account/Setup/abc", "/Account/Setup/[REDACTED]"),
        ("//account//setup//abc", "//account//setup//[REDACTED]"),
        ("/account%2Fsetup%2Fabc", "/account%2Fsetup%2F[REDACTED]"),
        ("%2Faccount%2fsetup%2Fabc?x=1", "%2Faccount%2fsetup%2F[REDACTED]?x=1"),
        (r"\account\setup\abc", r"\account\setup\[REDACTED]"),
        ("b'/account/setup/abc'", "b'/account/setup/[REDACTED]'"),
    ])
    def test_redact(self, raw, expected):
        assert log_redaction.redact(raw) == expected

    def test_filter_rewrites_uvicorn_style_record(self, caplog):
        log_redaction.install()
        token = "SECRETtoken_0123456789abcdef"
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET",
                                                     f"/account/setup/{token}", "1.1", 200)
        assert token not in caplog.text and "/account/setup/[REDACTED]" in caplog.text


# --------------------------------------------- application error path (traceback)
def test_unhandled_error_on_setup_route_logs_no_token(engine, SessionTest, db, users, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import create_app

    _, _, token = _onboard(db, users)

    def boom(_db, tok):
        raise RuntimeError(f"simulated failure while checking /account/setup/{tok}")

    monkeypatch.setattr(user_svc, "user_for_setup_token", boom)
    app = create_app(init=False)

    def _get_db():
        s = SessionTest()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with caplog.at_level(logging.ERROR), TestClient(app, raise_server_exceptions=False) as c:
        assert c.get(f"/account/setup/{token}").status_code == 500
    logged = caplog.text + "".join((r.exc_text or "") for r in caplog.records)
    assert "Unhandled error" in logged and "simulated failure" in logged
    assert token not in logged
    assert "/account/setup/[REDACTED]" in logged


# ------------------------------------------------- real Uvicorn access log
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_uvicorn_access_log_never_contains_setup_token(engine, db, users, tmp_path):
    f, admin, token = _onboard(db, users)
    db.close()
    port, log_path = _free_port(), tmp_path / "uvicorn.log"
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    env["BBMS_DATABASE_URL"] = str(engine.url)
    with open(log_path, "w") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "debug",
             "--access-log"], cwd=BASE_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    httpx.get(base + "/login", timeout=1)
                    break
                except Exception:
                    time.sleep(0.1)
            with httpx.Client(base_url=base) as c:
                first = c.get(f"/account/setup/{token}")
                assert first.status_code == 200
                assert first.headers["referrer-policy"] == "no-referrer"
                assert c.get(f"/account/setup/{token}?utm_source=email").status_code == 200
                csrf = CSRF.search(first.text).group(1)
                done = c.post(f"/account/setup/{token}", data={"csrf_token": csrf, "new_password": "TokAdmin@123",
                                                                "confirm_password": "TokAdmin@123"})
                assert done.status_code == 303
                assert c.get(f"/account/setup/{token}").status_code == 404           # single use
        finally:
            server.terminate()
            server.wait(timeout=20)
    output = log_path.read_text()
    assert token not in output, "raw setup token found in server output"
    assert output.count("/account/setup/[REDACTED]") >= 4                      # every request was logged, redacted


# ------------------------------------- single use, expiry, re-issue (over HTTP)
def test_reissued_link_invalidates_previous_over_http(client, db, users):
    p = users["platform_admin"]
    f = fsvc.create_facility(db, FacilityIn(display_name="Reissue Test Facility (fictional)"), p)
    login(client, "platformadmin")
    page = f"/platform/facilities/{f.id}"
    r = client.post(f"{page}/onboard", data={"username": "re.admin", "full_name": "Reissue Admin",
                                             "csrf_token": csrf_from(client.get(page).text)})
    first = re.search(r"/account/setup/([A-Za-z0-9_\-]+)", r.text).group(1)
    admin = user_svc.pending_setup_accounts(db, f.id)[0]
    r = client.post(f"{page}/onboard/{admin.id}/reissue", data={"csrf_token": csrf_from(client.get(page).text)})
    second = re.search(r"/account/setup/([A-Za-z0-9_\-]+)", r.text).group(1)
    assert first != second
    client.cookies.clear()
    assert client.get(f"/account/setup/{first}").status_code == 404           # previous link dead
    assert client.get(f"/account/setup/{second}").status_code == 200


def test_expired_link_rejected_over_http(client, db, users):
    _, admin, token = _onboard(db, users, "Expiry Test Facility (fictional)", "exp.admin")
    admin.setup_token_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    page = client.get(f"/account/setup/{token}")
    assert page.status_code == 404 and "expired" in page.text and 'name="new_password"' not in page.text
    r = client.post(f"/account/setup/{token}", data={"csrf_token": csrf_from(client.get("/login").text),
                                                     "new_password": "Late@12345", "confirm_password": "Late@12345"})
    assert r.status_code == 400
    assert login(client, "exp.admin", "Late@12345").status_code == 401



# ======================================================== H1 / M1: handler-level protection
TOKEN = "Sx9_handler-level-token_0123456789"


@pytest.fixture()
def protected_stream():
    """A parent logger with one protected handler; children propagate to it (like uvicorn.asgi)."""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    parent = logging.getLogger("bbms_redaction_test")
    parent.handlers[:] = [handler]
    parent.setLevel(1)
    parent.propagate = False
    log_redaction.protect_handler(handler)
    yield parent, stream
    parent.handlers[:] = []


def test_child_logger_dict_and_bytes_args_are_redacted(protected_stream):
    parent, stream = protected_stream
    scope = {"type": "http", "path": f"/account/setup/{TOKEN}", "raw_path": f"/account/setup/{TOKEN}".encode(),
             "headers": [(b"referer", f"https://x.example/account/setup/{TOKEN}".encode())]}
    logging.getLogger("bbms_redaction_test.asgi").log(5, "%s [%d] Started scope=%s", "ASGI", 1, scope)
    out = stream.getvalue()
    assert TOKEN not in out and out.count("[REDACTED]") >= 3


def test_parameterised_message_formats_correctly_and_is_redacted(protected_stream, capsys):
    parent, stream = protected_stream
    parent.info("setup link opened: /account/setup/%s (attempt %d)", TOKEN, 2)
    out = stream.getvalue()
    assert "setup link opened: /account/setup/[REDACTED] (attempt 2)" in out
    assert TOKEN not in out and "Logging error" not in capsys.readouterr().err


def test_unformattable_record_never_dumps_raw_arguments(protected_stream, capsys):
    parent, stream = protected_stream
    parent.info("broken format %s %s", TOKEN)                     # too few arguments
    parent.info("bad type %d", f"/account/setup/{TOKEN}")          # wrong type
    out, err = stream.getvalue(), capsys.readouterr().err
    assert TOKEN not in out and TOKEN not in err
    assert out.count("arguments withheld") == 2


def test_handler_error_path_never_prints_record_contents(protected_stream, capsys):
    parent, _ = protected_stream

    class Broken:                                                  # stream whose write fails -> handleError
        def write(self, _):
            raise OSError("disk full")

        def flush(self):
            pass

    parent.handlers[0].stream = Broken()
    parent.info("path /account/setup/%s", TOKEN)
    err = capsys.readouterr().err
    assert "record contents withheld" in err and TOKEN not in err


def test_traceback_and_exception_message_are_redacted(protected_stream):
    parent, stream = protected_stream
    try:
        raise ValueError(f"failed on /account/setup/{TOKEN}")
    except ValueError:
        parent.exception("request failed")
    out = stream.getvalue()
    assert "Traceback" in out and "ValueError" in out and TOKEN not in out


def test_install_is_idempotent_and_covers_last_resort():
    log_redaction.install()
    log_redaction.install()
    h = logging.lastResort
    assert sum(isinstance(f, log_redaction.RedactArgsFilter) for f in h.filters) == 1
    assert isinstance(h.formatter, log_redaction.RedactingFormatter)


# ===================================================== H1: real Uvicorn at trace level
def test_uvicorn_trace_level_output_never_contains_setup_token(engine, db, users, tmp_path):
    """--log-level trace logs whole ASGI scopes via the child logger uvicorn.asgi (the original H1 leak)."""
    f, admin, token = _onboard(db, users, "Trace Test Facility (fictional)", "trace.admin")
    db.close()
    port, log_path = _free_port(), tmp_path / "uvicorn-trace.log"
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    env["BBMS_DATABASE_URL"] = str(engine.url)
    with open(log_path, "w") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                                   "--log-level", "trace", "--access-log"],
                                  cwd=BASE_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(150):
                try:
                    httpx.get(base + "/login", timeout=1)
                    break
                except Exception:
                    time.sleep(0.1)
            with httpx.Client() as c:
                first = c.get(f"{base}/account/setup/{token}")
                assert first.status_code == 200
                for variant in (f"{base}/account/setup/{token}?utm=x", f"{base}/Account/Setup/{token}",
                                f"{base}//account//setup//{token}", f"{base}/account%2Fsetup%2F{token}"):
                    c.get(variant)                                  # 200 or 404: either way it is logged
                csrf = CSRF.search(first.text).group(1)
                done = c.post(f"{base}/account/setup/{token}", data={
                    "csrf_token": csrf, "new_password": "Trace@12345", "confirm_password": "Trace@12345"})
                assert done.status_code == 303
                assert c.get(f"{base}/account/setup/{token}").status_code == 404          # single use
        finally:
            server.terminate()
            server.wait(timeout=20)
    output = log_path.read_text()
    assert "ASGI [" in output, "trace-level ASGI logging was not active"
    assert token not in output, "raw setup token found in trace-level server output"
    assert output.count("[REDACTED]") >= 10


# ============================================ M3: pages showing a raw setup link are never cached
def test_responses_carrying_setup_links_are_not_cacheable(client, db, users):
    p = users["platform_admin"]
    f = fsvc.create_facility(db, FacilityIn(display_name="Cache Test Facility (fictional)"), p)
    login(client, "platformadmin")
    page = f"/platform/facilities/{f.id}"
    r = client.post(f"{page}/onboard", data={"username": "cache.admin", "full_name": "Cache Admin",
                                             "csrf_token": csrf_from(client.get(page).text)})
    assert "/account/setup/" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    admin = user_svc.pending_setup_accounts(db, f.id)[0]
    r = client.post(f"{page}/onboard/{admin.id}/reissue", data={"csrf_token": csrf_from(client.get(page).text)})
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    token = re.search(r"/account/setup/([A-Za-z0-9_\-]+)", r.text).group(1)
    client.cookies.clear()
    form = client.get(f"/account/setup/{token}")
    bad = client.post(f"/account/setup/{token}", data={"csrf_token": csrf_from(form.text),
                                                       "new_password": "short", "confirm_password": "short"})
    assert bad.status_code == 400
    assert bad.headers["cache-control"] == "no-store" and bad.headers["referrer-policy"] == "no-referrer"
    assert "no-store" not in client.get(page).headers.get("cache-control", "")   # ordinary page unaffected
