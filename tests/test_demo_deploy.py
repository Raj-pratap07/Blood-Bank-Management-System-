"""Public demo deployment (Render Free): demo database builder, demo guard, banner and health check."""
from __future__ import annotations

import sqlite3

import bcrypt
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import BASE_DIR, settings
from app.database import make_engine
from app.models import BloodUnit, Facility, User, VerificationScope
from app.services import facilities as fsvc
from scripts import build_demo_db as demo
from scripts.seed import DEMO_APPLICATION_ID
from tests.conftest import csrf_from, login

PASSWORDS = {"BBMS_DEMO_PASSWORD_ADMIN": "DemoAdminPw2026", "BBMS_DEMO_PASSWORD_DOCTOR": "DemoDoctorPw2026",
             "BBMS_DEMO_PASSWORD_STAFF": "DemoStaffPw2026", "BBMS_DEMO_PASSWORD_PLATFORM": "DemoPlatformPw2026"}


@pytest.fixture()
def demo_env(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    for k, v in PASSWORDS.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def demo_engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'bbms-demo.db').as_posix()}")
    yield eng
    eng.dispose()


# ---------------------------------------------------------------- builder
def test_builds_synthetic_demo_with_real_facilities(demo_env, demo_engine):
    summary = demo.build(demo_engine)
    assert summary["users"] == ["demo.admin", "demo.doctor", "demo.platform", "demo.staff"]
    db = sessionmaker(bind=demo_engine)()
    try:
        for username, _, _, env in demo.ACCOUNTS:              # passwords come only from the environment
            u = db.scalar(select(User).where(User.username == username))
            assert bcrypt.checkpw(PASSWORDS[env].encode(), u.password_hash.encode()) and not u.must_change_password
        assert db.scalar(select(func.count()).select_from(User).where(
            User.username.in_(["admin", "staff", "dr.mehta", "platform", "kabir_admin"]))) == 0
        real = list(db.scalars(select(Facility).where(Facility.is_demo.is_(False)).order_by(Facility.slug)))
        assert [f.slug for f in real] == ["haldwani-city-blood-bank", "lt-sh-bal-kishan-devki-joshi-charitable-blood-bank",
                                          "ssj-base-haldwani", "sushila-tiwari-govt-hosp"]
        for f in real:
            assert fsvc.scope_status(f, VerificationScope.IDENTITY).status.value == "source_confirmed"
            assert not f.is_listed and not f.has_coordinates and not f.contacts
            assert db.scalar(select(func.count()).select_from(BloodUnit).where(BloodUnit.facility_id == f.id)) == 0
        assert db.scalar(select(func.count()).select_from(BloodUnit)) > 0          # synthetic stock: demo facility only
    finally:
        db.close()
    conn = sqlite3.connect(summary["path"])
    assert conn.execute("PRAGMA application_id").fetchone()[0] == DEMO_APPLICATION_ID
    conn.close()


def test_rebuild_is_repeatable(demo_env, demo_engine):
    first, second = demo.build(demo_engine), demo.build(demo_engine)
    assert first == second


@pytest.mark.parametrize("env,value,match", [
    ("BBMS_DEMO_PASSWORD_STAFF", "", "at least 12"),
    ("BBMS_DEMO_PASSWORD_ADMIN", "short1", "at least 12"),
    ("BBMS_DEMO_PASSWORD_DOCTOR", "onlyletterslong", "letters and digits"),
])
def test_refuses_weak_or_missing_passwords(demo_env, demo_engine, monkeypatch, env, value, match):
    monkeypatch.setenv(env, value)
    with pytest.raises(demo.Refused, match=match):
        demo.build(demo_engine)


def test_refuses_documented_local_password(demo_env, demo_engine, monkeypatch):
    monkeypatch.setattr(demo, "LOCAL_PASSWORDS", demo.LOCAL_PASSWORDS | {"DemoStaffPw2026"})
    with pytest.raises(demo.Refused, match="must not reuse"):
        demo.build(demo_engine)


def test_refuses_without_demo_mode(demo_engine, monkeypatch):
    for k, v in PASSWORDS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(demo.Refused, match="DEMO_MODE"):
        demo.build(demo_engine)


def test_refuses_real_database_name_and_project_folder(demo_env, tmp_path):
    for url in (f"sqlite:///{(tmp_path / 'bloodbank.db').as_posix()}",
                f"sqlite:///{(BASE_DIR / 'demo-inside-project.db').as_posix()}"):
        eng = make_engine(url)
        with pytest.raises(demo.Refused):
            demo.build(eng)
        eng.dispose()
    assert not (BASE_DIR / "demo-inside-project.db").exists()


def test_never_replaces_a_non_demo_database(demo_env, tmp_path):
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE important (x)")
    conn.execute("INSERT INTO important VALUES (1)")
    conn.commit()
    conn.close()
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    with pytest.raises(demo.Refused, match="not a demo database"):
        demo.build(eng)
    eng.dispose()
    assert sqlite3.connect(path).execute("SELECT x FROM important").fetchone() == (1,)


def test_never_replaces_a_seeded_local_demo_database(demo_env, tmp_path):
    """A locally seeded database carries the same marker but has the local accounts: refuse."""
    path = tmp_path / "local-seeded.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA application_id = {DEMO_APPLICATION_ID}")
    conn.execute("CREATE TABLE users (username TEXT)")
    conn.execute("INSERT INTO users VALUES ('admin')")
    conn.commit()
    conn.close()
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    with pytest.raises(demo.Refused, match="not deployment demo accounts"):
        demo.build(eng)
    eng.dispose()
    assert path.exists()


# ---------------------------------------------------------------- guard, banner, health
@pytest.fixture()
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _post(client, url, data, page):
    return client.post(url, data={**data, "csrf_token": csrf_from(client.get(page).text)}, follow_redirects=False)


def test_guard_blocks_account_and_directory_changes(client, demo_on, users):
    login(client, "admin")
    r = _post(client, "/users/new", {"username": "intruder", "full_name": "In Truder", "role": "admin",
                                     "password": "Intruder2026xx"}, "/users/new")
    assert r.status_code == 403 and "Disabled in the public demo" in r.text
    r = _post(client, f"/users/{users['staff'].id}/toggle", {}, "/users")
    assert r.status_code == 403
    r = _post(client, "/account/password", {"current_password": "Password@123", "new_password": "Changed2026xx",
                                            "confirm_password": "Changed2026xx"}, "/account/password")
    assert r.status_code == 403
    client.cookies.clear()
    login(client, "platformadmin")
    r = _post(client, "/platform/facilities/new", {"display_name": "Fake Blood Bank"}, "/platform/facilities/new")
    assert r.status_code == 403
    assert client.get("/platform/facilities").status_code == 200          # viewing stays available


def test_guard_allows_operational_workflows(client, demo_on, db):
    login(client, "staff")
    r = _post(client, "/donors/new", {"full_name": "Demo Visitor Donor", "gender": "female",
                                      "date_of_birth": "1991-02-02", "blood_group": "B+", "phone": "9700088877",
                                      "weight_kg": "58", "city": "Haldwani"}, "/donors/new")
    assert r.status_code == 303


def test_guard_is_off_outside_demo_mode(client, users):
    login(client, "admin")
    r = _post(client, f"/users/{users['staff'].id}/toggle", {}, "/users")
    assert r.status_code == 303


def test_demo_banner_only_in_demo_mode(client, monkeypatch):
    assert "Public demonstration." not in client.get("/find").text
    monkeypatch.setattr(settings, "demo_mode", True)
    for url in ("/", "/find", "/login"):
        assert "Public demonstration." in client.get(url).text
    login(client, "staff")
    assert "erased whenever the free server restarts" in client.get("/donors").text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["Cache-Control"] == "no-store"
