"""Public demo deployment (Render Free): demo database builder, demo guard, banner and health check."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

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
            assert f.is_listed and not fsvc.listing_blockers(f)
            assert not f.has_coordinates and not fsvc.map_eligible(f)                 # no pin without verified location
            assert not f.is_participating and f.stock_visibility.value == "hidden"
            assert {"government_document", "government_portal"} <= {s.source_type.value for s in f.sources}
            phones = [c for c in f.contacts if c.kind.value == "phone"]
            assert len(phones) == 1 and phones[0].is_public and fsvc.contact_status(phones[0]).is_positive
            assert db.scalar(select(func.count()).select_from(BloodUnit).where(BloodUnit.facility_id == f.id)) == 0
        bk = next(f for f in real if f.slug.startswith("lt-sh-bal-kishan"))
        assert [c for c in bk.contacts if c.kind.value == "email"] == []            # deliberately not retained
        assert "does not retain it" in " ".join(s.notes or "" for s in bk.sources)
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


# ---------------------------------------------------------------- listed real facilities on the demo site
@pytest.fixture()
def demo_site(demo_env, demo_engine):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import create_app

    demo.build(demo_engine)
    app = create_app(init=False)
    S = sessionmaker(bind=demo_engine, autoflush=False, expire_on_commit=False)

    def _db():
        s = S()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c, S


REAL = sorted(demo.EXPECTED_FACILITIES)


def test_demo_site_lists_four_facilities_without_pins(demo_site):
    c, _ = demo_site
    html = c.get("/find").text
    assert all(f'data-slug="{s}"' in html for s in REAL)
    assert "pub-card-candidate" not in html                                    # nothing left "under verification"
    data = json.loads(html.split('<script id="facility-data" type="application/json">')[1].split("</script>")[0])
    assert sorted(d["slug"] for d in data) == REAL and all(d["lat"] is None and d["lon"] is None for d in data)
    features = c.get("/api/public/facilities").json()["features"]
    assert len(features) == 4 and all(f["geometry"] is None for f in features)


def test_demo_site_never_shows_stock_for_real_facilities(demo_site):
    c, _ = demo_site
    body = c.get("/api/public/availability").json()
    assert body["results"] == [] and body["facilities_reporting"] == 0
    for slug in REAL:
        assert "Not published." in c.get(f"/find/{slug}").text
    page = " ".join(c.get("/find?group=O%2B").text.split())
    assert "No participating blood bank has published current stock" in page


def test_withheld_email_is_not_stored_and_others_are_public(demo_site):
    c, S = demo_site
    with S() as db:
        emails = {f.slug: [x.value for x in f.contacts if x.kind.value == "email"]
                  for f in db.scalars(select(Facility).where(Facility.is_demo.is_(False)))}
    assert emails["lt-sh-bal-kishan-devki-joshi-charitable-blood-bank"] == []
    assert sum(len(v) for v in emails.values()) == 3
    for slug, values in emails.items():
        for v in values:
            assert v in c.get(f"/find/{slug}").text


def test_public_contacts_say_source_confirmed(demo_site):
    c, _ = demo_site
    for slug in REAL:
        page = c.get(f"/find/{slug}").text
        assert "Source confirmed" in page and "not tested by contacting the facility" in page


def test_demo_facility_still_private_and_workflows_work(demo_site):
    c, _ = demo_site
    assert c.get("/find/demo-blood-centre").status_code == 404
    login(c, "demo.staff", PASSWORDS["BBMS_DEMO_PASSWORD_STAFF"])
    r = _post(c, "/donors/new", {"full_name": "Listed Demo Donor", "gender": "male", "date_of_birth": "1990-01-01",
                                 "blood_group": "A+", "phone": "9700066655", "weight_kg": "70", "city": "Haldwani"},
              "/donors/new")
    assert r.status_code == 303


# ---------------------------------------------------------------- reviewed-data contract and expiry
def _data_variant(tmp_path, monkeypatch, change):
    entries = json.loads(demo.FACILITIES_FILE.read_text(encoding="utf-8"))
    change(entries)
    path = tmp_path / "facilities-variant.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(demo, "FACILITIES_FILE", path)


def _age_all_checks(entries, days):
    old = (date.today() - timedelta(days=days)).isoformat()
    for e in entries:
        for v in e["verifications"]:
            v["verified_on"] = old


def _unverified_public_phone(entries):
    entries[0]["contacts"].append({"key": "extra", "kind": "phone", "value": "9800000111", "source": "nhm",
                                   "is_public": True})


def _add_coordinates(entries):
    entries[0]["location"] = {"latitude": 29.2, "longitude": 79.5, "source": "eraktkosh"}


def _unlist_one(entries):
    entries[1]["list"] = False


def _extra_facility(entries):
    extra = json.loads(json.dumps(entries[0]))
    extra["display_name"] = extra["official_name"] = "Fictional Extra Centre"
    extra["aliases"] = []
    extra["contacts"] = [dict(extra["contacts"][0], value="9800000112")]
    extra["verifications"] = [v for v in extra["verifications"] if v.get("contact") in (None, "phone")]
    entries.append(extra)


@pytest.mark.parametrize("change,match", [
    (_add_coordinates, "has coordinates"),
    (_unverified_public_phone, "never confirmed against a source"),
    (_unlist_one, "not listed"),
    (_extra_facility, "expected exactly the reviewed facilities"),
])
def test_builder_refuses_unreviewed_data(demo_env, demo_engine, tmp_path, monkeypatch, change, match):
    _data_variant(tmp_path, monkeypatch, change)
    with pytest.raises(demo.Refused, match=match):
        demo.build(demo_engine)


def test_expired_verification_keeps_demo_up_but_unlists(demo_env, demo_engine, tmp_path, monkeypatch):
    _data_variant(tmp_path, monkeypatch, lambda e: _age_all_checks(e, settings.verification_max_age_days + 10))
    summary = demo.build(demo_engine)                                   # starts: no outage on expiry
    assert summary["listed"] == 0 and len(summary["warnings"]) == 4
    assert all("NOT publicly listed until re-verified" in w for w in summary["warnings"])
    db = sessionmaker(bind=demo_engine)()
    try:
        real = list(db.scalars(select(Facility).where(Facility.is_demo.is_(False))))
        assert len(real) == 4 and not any(f.is_listed for f in real)      # automatically withheld
        for f in real:                                                   # verification dates were NOT extended
            assert fsvc.scope_status(f, VerificationScope.IDENTITY).status.value == "outdated"
    finally:
        db.close()


def test_mixed_expiry_lists_only_current_facilities(demo_env, demo_engine, tmp_path, monkeypatch):
    def change(entries):
        old = (date.today() - timedelta(days=settings.verification_max_age_days + 10)).isoformat()
        for v in entries[0]["verifications"]:
            v["verified_on"] = old
    _data_variant(tmp_path, monkeypatch, change)
    summary = demo.build(demo_engine)
    assert summary["listed"] == 3 and len(summary["warnings"]) == 1


def test_expired_record_with_a_data_error_still_refuses(demo_env, demo_engine, tmp_path, monkeypatch):
    def change(entries):
        _age_all_checks(entries, settings.verification_max_age_days + 10)
        entries[0]["contacts"] = []                                       # structural error, not just expiry
        entries[0]["verifications"] = [v for v in entries[0]["verifications"] if v["scope"] != "contact"]
    _data_variant(tmp_path, monkeypatch, change)
    with pytest.raises(demo.Refused, match="loading the Haldwani facility records failed"):
        demo.build(demo_engine)


def test_warns_before_verification_expires(demo_env, demo_engine, tmp_path, monkeypatch):
    _data_variant(tmp_path, monkeypatch,
                  lambda e: _age_all_checks(e, settings.verification_max_age_days - demo.EXPIRY_WARNING_DAYS + 5))
    summary = demo.build(demo_engine)
    assert summary["listed"] == 4 and all("re-verify on e-RaktKosh" in w for w in summary["warnings"])
