from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest

# Use a throwaway DB/secret before the app modules read settings.
os.environ.setdefault("BBMS_SECRET_KEY", "test-secret")

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, make_engine  # noqa: E402
from app.models import BloodGroup, Gender, Role, User  # noqa: E402
from app.schemas.forms import DonationIn, DonorIn, ScreeningIn  # noqa: E402
from app.services import donations as donation_svc  # noqa: E402
from app.services import donors as donor_svc  # noqa: E402
from app.services.security import hash_password  # noqa: E402

PASSWORD = "Password@123"


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    import app.models  # noqa: F401
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def SessionTest(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(SessionTest):
    s = SessionTest()
    yield s
    s.close()


@pytest.fixture()
def users(db):
    hashed = hash_password(PASSWORD)
    out = {}
    for role in Role:
        u = User(username=role.value.replace("_", ""), full_name=f"Test {role.label}", role=role, password_hash=hashed)
        db.add(u)
        out[role.value] = u
    db.commit()
    return out


_phone = iter(range(9000000000, 9999999999))


@pytest.fixture()
def make_donor(db):
    def _make(group=BloodGroup.O_POS, gender=Gender.MALE, age=30, weight=65.0, **kw):
        dob = date.today().replace(year=date.today().year - age) - timedelta(days=10)
        data = DonorIn(full_name=kw.pop("name", "Test Donor"), gender=gender, date_of_birth=dob,
                       blood_group=group, phone=str(next(_phone)), weight_kg=weight, **kw)
        return donor_svc.create_donor(db, data)
    return _make


def vitals(**over) -> dict:
    base = dict(volume_ml=350, weight_kg=65, hemoglobin_g_dl=14.0, bp_systolic=120, bp_diastolic=80,
                pulse_bpm=72, temperature_c=36.8, questionnaire_ok=True)
    base.update(over)
    return base


@pytest.fixture()
def make_unit(db, users, make_donor):
    """Create a donor + donation and screen it as accepted. Returns the bag."""
    def _make(group=BloodGroup.O_POS, days_ago=1, accept=True):
        donor = make_donor(group=group)
        d = date.today() - timedelta(days=days_ago)
        donation = donation_svc.record_donation(
            db, donor, DonationIn(donor_id=donor.id, donation_date=d,
                                  cleared_by_id=users["medical_officer"].id, **vitals()),
            users["staff"])
        if accept:
            donation_svc.screen_donation(db, donation, screening("accept"), users["medical_officer"], on=d)
        return donation.units[0]
    return _make


def screening(decision="accept", **over) -> ScreeningIn:
    data = dict(hiv_result="non_reactive", hbv_result="non_reactive", hcv_result="non_reactive",
                syphilis_result="non_reactive", malaria_result="non_reactive", abo_rh_confirmed=True,
                decision=decision, screening_notes=None if decision == "accept" else "Reactive HBsAg")
    data.update(over)
    return ScreeningIn(**data)


# ---------------------------------------------------------------- web helpers
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    m = CSRF_RE.search(html)
    assert m, "CSRF token not found in page"
    return m.group(1)


@pytest.fixture()
def client(engine, SessionTest, users):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import create_app

    app = create_app(init=False)

    def _get_db():
        s = SessionTest()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def login(client, username: str, password: str = PASSWORD):
    page = client.get("/login")
    token = csrf_from(page.text)
    resp = client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                       follow_redirects=False)
    return resp
