"""Regression tests: inventory API expiry, configuration validation, production session cookie."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.config import (
    ConfigurationError,
    Settings,
    _env,
    session_cookie_secure,
    settings,
    validate_settings,
)
from app.database import get_db
from app.models import BloodGroup as G
from app.models import BloodUnit, UnitStatus
from tests.conftest import PASSWORD, csrf_from, login

STRONG_KEY = "p" * 48  # test-only value


# =========================================================== 1. inventory API
class TestInventoryApiExpiry:
    def _a_pos(self, data, field):
        return data[field][data["labels"].index("A+")]

    def test_direct_api_call_does_not_report_expired_bags(self, client, db, make_unit):
        expired = make_unit(G.A_POS, days_ago=40)   # accepted while fresh; now 5 days past expiry, not yet swept
        make_unit(G.A_POS, days_ago=2)              # genuinely available
        assert expired.status == UnitStatus.AVAILABLE
        login(client, "staff")                      # does not follow the redirect to the dashboard
        data = client.get("/api/stats/inventory").json()
        assert self._a_pos(data, "available") == 1
        assert self._a_pos(data, "near_expiry") == 0
        db.expire_all()
        assert db.get(BloodUnit, expired.id).status == UnitStatus.EXPIRED

    def test_api_matches_dashboard_card(self, client, make_unit):
        make_unit(G.O_NEG, days_ago=50), make_unit(G.O_NEG, days_ago=3)
        login(client, "staff")
        api_total = sum(client.get("/api/stats/inventory").json()["available"])
        html = client.get("/").text
        assert f'stat-value text-success">{api_total}<' in html


# ===================================================== 2. configuration validation
class TestEnvParsing:
    @pytest.mark.parametrize("raw,cast,expected", [
        ("abc", int, "expected a whole number"), ("1.5", int, "expected a whole number"),
        ("ten", float, "expected a number"), ("nan", float, "finite"), ("inf", float, "finite"),
        ("maybe", None, "true or false"),
    ])
    def test_malformed_values_name_the_variable(self, monkeypatch, raw, cast, expected):
        from app.config import _bool

        monkeypatch.setenv("BBMS_PROBE", raw)
        with pytest.raises(ConfigurationError, match=f"BBMS_PROBE=.*{expected}"):
            _env("PROBE", 0, cast or _bool)

    @pytest.mark.parametrize("raw,expected", [("true", True), ("ON", True), ("1", True), ("no", False), ("0", False)])
    def test_boolean_values(self, monkeypatch, raw, expected):
        from app.config import _bool

        monkeypatch.setenv("BBMS_PROBE", raw)
        assert _env("PROBE", None, _bool) is expected

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("BBMS_PROBE", raising=False)
        assert _env("PROBE", 35, int) == 35


class TestValidateSettings:
    def test_defaults_are_valid(self):
        validate_settings(Settings())

    @pytest.mark.parametrize("field,value,env", [
        ("shelf_life_days", 0, "BBMS_SHELF_LIFE_DAYS"),
        ("shelf_life_days", -5, "BBMS_SHELF_LIFE_DAYS"),
        ("near_expiry_days", -1, "BBMS_NEAR_EXPIRY_DAYS"),
        ("near_expiry_days", 35, "BBMS_NEAR_EXPIRY_DAYS"),        # not less than shelf life
        ("session_max_age_seconds", 0, "BBMS_SESSION_MAX_AGE"),
        ("min_age", 0, "BBMS_MIN_AGE"),
        ("min_age", 70, "BBMS_MIN_AGE"),                          # above max age
        ("max_age_first_time", 70, "BBMS_MAX_AGE_FIRST_TIME"),    # above max age
        ("min_weight_kg", 0, "BBMS_MIN_WEIGHT_KG"),
        ("min_weight_450ml_kg", 40, "BBMS_MIN_WEIGHT_450ML_KG"),  # below the 350 ml minimum
        ("interval_male_days", 0, "BBMS_INTERVAL_MALE_DAYS"),
        ("interval_female_days", -1, "BBMS_INTERVAL_FEMALE_DAYS"),
        ("min_hemoglobin_g_dl", 0, "BBMS_MIN_HB"),
        ("max_temperature_c", 50, "BBMS_MAX_TEMP_C"),
    ])
    def test_out_of_range_rejected(self, field, value, env):
        s = Settings()
        setattr(s, field, value)
        with pytest.raises(ConfigurationError, match=env):
            validate_settings(s)

    def test_all_problems_reported_together(self):
        s = Settings()
        s.shelf_life_days, s.interval_male_days = 0, 0
        with pytest.raises(ConfigurationError) as exc:
            validate_settings(s)
        assert "BBMS_SHELF_LIFE_DAYS" in str(exc.value) and "BBMS_INTERVAL_MALE_DAYS" in str(exc.value)

    def test_create_app_refuses_invalid_settings(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setattr(settings, "shelf_life_days", 0)
        with pytest.raises(ConfigurationError, match="BBMS_SHELF_LIFE_DAYS"):
            create_app(init=False)


def _start(tmp_path, **env_extra) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    env["BBMS_DATABASE_URL"] = f"sqlite:///{(tmp_path / 'cfg.db').as_posix()}"
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", "import app.main"], cwd=os.getcwd(), env=env,
                          capture_output=True, text=True, timeout=120)


class TestStartupProcess:
    @pytest.mark.parametrize("var,value", [("BBMS_SHELF_LIFE_DAYS", "abc"), ("BBMS_SHELF_LIFE_DAYS", "0"),
                                           ("BBMS_MIN_HB", "nan"), ("BBMS_SESSION_HTTPS_ONLY", "maybe")])
    def test_invalid_value_stops_startup_with_clear_error(self, tmp_path, var, value):
        r = _start(tmp_path, **{var: value})
        assert r.returncode != 0
        assert "ConfigurationError" in r.stderr and var in r.stderr

    def test_valid_override_starts(self, tmp_path):
        r = _start(tmp_path, BBMS_SHELF_LIFE_DAYS="42", BBMS_NEAR_EXPIRY_DAYS="5")
        assert r.returncode == 0, r.stderr


# =================================================== 3. production session cookie
def _app(monkeypatch, SessionTest, app_env: str, https_only: bool | None = None):
    from app.main import create_app

    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(settings, "secret_key", STRONG_KEY)
    monkeypatch.setattr(settings, "session_https_only", https_only)
    app = create_app(init=False)

    def _get_db():
        s = SessionTest()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return app


def _cookie_flags(client) -> str:
    return client.get("/login").headers.get("set-cookie", "").lower()


class TestSessionCookieSecurity:
    @pytest.mark.parametrize("app_env,override,secure", [
        ("development", None, False),     # local HTTP development keeps working
        ("production", None, True),       # secure by default in production
        ("production", False, False),     # explicit opt-out (warned)
        ("development", True, True),      # opt-in, e.g. HTTPS staging
    ])
    def test_secure_flag(self, monkeypatch, SessionTest, app_env, override, secure):
        app = _app(monkeypatch, SessionTest, app_env, override)
        assert session_cookie_secure(settings) is secure
        with TestClient(app, base_url="https://testserver") as c:
            flags = _cookie_flags(c)
        assert ("; secure" in flags) is secure
        assert "httponly" in flags and "samesite=lax" in flags

    def test_production_login_works_over_https(self, monkeypatch, SessionTest, users):
        app = _app(monkeypatch, SessionTest, "production")
        with TestClient(app, base_url="https://testserver") as c:
            assert login(c, "staff").status_code == 303
            assert c.get("/", follow_redirects=False).status_code == 200

    def test_production_cookie_not_sent_over_plain_http(self, monkeypatch, SessionTest, users):
        """Documents the HTTPS requirement: over http:// the Secure cookie never comes back."""
        app = _app(monkeypatch, SessionTest, "production")
        with TestClient(app, base_url="http://testserver") as c:
            token = csrf_from(c.get("/login").text)
            r = c.post("/login", data={"username": "staff", "password": PASSWORD, "csrf_token": token},
                       follow_redirects=False)
            assert r.status_code == 403            # session (and its CSRF token) not returned over HTTP

    def test_production_opt_out_logs_warning(self, monkeypatch, SessionTest, caplog):
        with caplog.at_level(logging.WARNING, logger="bloodbank"):
            _app(monkeypatch, SessionTest, "production", https_only=False)
        assert "BBMS_SESSION_HTTPS_ONLY=false" in caplog.text
