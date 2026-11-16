"""Production start-up must refuse the development (or a weak) session secret key."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app.config import DEV_SECRET_KEY, InsecureConfigurationError, check_secret_key, settings

STRONG_KEY = "k" * 16 + "Zq3-vT9_pL2xW8nR4mC7yB1eH6jK0sD5"  # 48 chars, test-only value


# ------------------------------------------------------------- unit level
class TestCheckSecretKey:
    @pytest.mark.parametrize("env", [None, "", "development", "dev", "test", "local"])
    def test_development_allows_default_key(self, env):
        check_secret_key(env, DEV_SECRET_KEY)  # no exception

    @pytest.mark.parametrize("env", ["production", "PRODUCTION", " Production ", "prod"])
    def test_production_rejects_default_key(self, env):
        with pytest.raises(InsecureConfigurationError, match="built-in development value") as exc:
            check_secret_key(env, DEV_SECRET_KEY)
        msg = str(exc.value)
        assert "BBMS_SECRET_KEY" in msg and "secrets.token_urlsafe" in msg  # tells the operator how to fix it

    @pytest.mark.parametrize("key,reason", [("", "is missing"), ("   ", "is missing"), ("short-key", "shorter than")])
    def test_production_rejects_blank_or_short_key(self, key, reason):
        with pytest.raises(InsecureConfigurationError, match=reason):
            check_secret_key("production", key)

    def test_production_accepts_custom_key(self):
        check_secret_key("production", STRONG_KEY)  # no exception


# ------------------------------------------------------- application factory
class TestCreateApp:
    def test_development_default_key_starts(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setattr(settings, "app_env", "development")
        monkeypatch.setattr(settings, "secret_key", DEV_SECRET_KEY)
        assert create_app(init=False) is not None

    def test_production_default_key_refuses_to_start(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "secret_key", DEV_SECRET_KEY)
        with pytest.raises(InsecureConfigurationError):
            create_app(init=False)

    def test_production_custom_key_starts_and_serves(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from app.main import create_app

        monkeypatch.setattr(settings, "app_env", "production")
        monkeypatch.setattr(settings, "secret_key", STRONG_KEY)
        with TestClient(create_app(init=False)) as c:
            assert c.get("/login").status_code == 200


# ----------------------------------------- real process start (env variables)
def _import_app(env_overrides: dict[str, str | None], tmp_path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "BBMS_SECRET_KEY")}
    env["BBMS_DATABASE_URL"] = f"sqlite:///{(tmp_path / 'proc.db').as_posix()}"  # never touch bloodbank.db
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run([sys.executable, "-c", "import app.main"], cwd=os.getcwd(), env=env,
                          capture_output=True, text=True, timeout=120)


class TestProcessStartup:
    def test_dev_without_secret_starts(self, tmp_path):
        r = _import_app({}, tmp_path)
        assert r.returncode == 0, r.stderr

    def test_production_without_secret_fails_with_clear_message(self, tmp_path):
        r = _import_app({"APP_ENV": "production"}, tmp_path)
        assert r.returncode != 0
        assert "InsecureConfigurationError" in r.stderr and "BBMS_SECRET_KEY" in r.stderr

    def test_production_with_custom_secret_starts(self, tmp_path):
        r = _import_app({"APP_ENV": "production", "BBMS_SECRET_KEY": STRONG_KEY}, tmp_path)
        assert r.returncode == 0, r.stderr
