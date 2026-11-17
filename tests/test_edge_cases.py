"""Edge-case and failure-mode tests (audit section B gaps + regressions for defects C1-C5)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.config import BASE_DIR, settings
from app.database import make_engine
from app.migrations import SCHEMA_VERSION, STEPS, upgrade
from app.models import BloodGroup as G
from app.models import (
    BloodRequest,
    BloodUnit,
    Gender,
    Issue,
    RequestPriority,
    RequestStatus,
    UnitStatus,
    User,
)
from app.schemas.forms import BloodRequestIn, DonationIn, DonorIn, IssueIn, UserIn
from app.services import donations as donation_svc
from app.services import donors as donor_svc
from app.services import requests as req_svc
from app.services.compatibility import is_compatible
from app.services.eligibility import check_donor
from app.services.errors import InventoryError, ServiceError
from app.utils import business_today
from tests.conftest import csrf_from, login, screening, vitals
from tests.test_rejection_history import _legacy_engine


def _request(db, users, group=G.A_POS, units=1):
    return req_svc.create_request(db, BloodRequestIn(
        patient_name="Edge Patient", patient_age=50, patient_gender=Gender.MALE, blood_group=group,
        units_required=units, priority=RequestPriority.URGENT, hospital_name="Edge Hospital",
        requester_name="Dr Edge", requester_phone="9876500009"), users["staff"])


def _issue(unit_ids):
    return IssueIn(unit_ids=list(unit_ids), received_by="Ward Nurse", crossmatch_confirmed=True)


def _stale_copy(SessionTest, req, user):
    """The same request/user as loaded by a second, concurrent HTTP request (own session)."""
    s = SessionTest()
    return s, s.get(BloodRequest, req.id), s.get(User, user.id)


# =============================================================== C1: stale-state conflicts
class TestConflictingRequestActions:
    """Two users act on the same request; the second acts on what it read before the first committed."""

    def test_approve_after_concurrent_cancel_is_refused(self, db, users, make_unit, SessionTest):
        unit = make_unit(G.A_POS)
        req = _request(db, users)
        s2, stale, mo = _stale_copy(SessionTest, req, users["medical_officer"])   # loaded while PENDING
        req_svc.cancel_request(db, req, users["staff"])
        with pytest.raises(ServiceError, match="now cancelled"):
            req_svc.approve_and_reserve(s2, stale, mo)
        s2.close()
        db.expire_all()
        assert db.get(BloodRequest, req.id).status == RequestStatus.CANCELLED
        assert db.get(BloodUnit, unit.id).status == UnitStatus.AVAILABLE           # no bag stuck in reservation

    def test_cancel_after_concurrent_fulfilment_is_refused(self, db, users, make_unit, SessionTest):
        make_unit(G.A_POS)
        req = _request(db, users)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        s2, stale, staff = _stale_copy(SessionTest, req, users["staff"])          # loaded while APPROVED
        req_svc.issue_units(db, req, _issue(u.id for u in req.reserved_units), users["staff"])
        with pytest.raises(ServiceError, match="now fulfilled"):
            req_svc.cancel_request(s2, stale, staff)
        s2.close()
        db.expire_all()
        assert db.get(BloodRequest, req.id).status == RequestStatus.FULFILLED

    def test_reject_after_concurrent_issue_is_refused(self, db, users, make_unit, SessionTest):
        make_unit(G.A_POS), make_unit(G.A_POS)
        req = _request(db, users, units=2)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        s2, stale, mo = _stale_copy(SessionTest, req, users["medical_officer"])
        req_svc.issue_units(db, req, _issue([req.reserved_units[0].id]), users["staff"])  # -> PARTIALLY_ISSUED
        with pytest.raises(ServiceError, match="already been issued"):
            req_svc.reject_request(s2, stale, mo, "Late rejection")
        s2.close()
        db.expire_all()
        assert db.get(BloodRequest, req.id).status == RequestStatus.PARTIALLY_ISSUED

    def test_issue_after_concurrent_cancel_is_refused(self, db, users, make_unit, SessionTest):
        unit = make_unit(G.A_POS)
        req = _request(db, users)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        s2, stale, staff = _stale_copy(SessionTest, req, users["staff"])
        req_svc.cancel_request(db, req, users["staff"])
        with pytest.raises(ServiceError, match="now cancelled"):
            req_svc.issue_units(s2, stale, _issue([unit.id]), staff)
        s2.close()
        assert db.scalar(select(func.count()).select_from(Issue)) == 0

    def test_repeat_approval_never_over_reserves(self, db, users, make_unit):
        for _ in range(3):
            make_unit(G.B_POS)
        req = _request(db, users, G.B_POS, units=2)
        assert req_svc.approve_and_reserve(db, req, users["medical_officer"]) == 2
        with pytest.raises(InventoryError, match="Enough bags"):                    # double-clicked Approve
            req_svc.approve_and_reserve(db, req, users["medical_officer"])
        assert req.units_reserved == 2


# =========================================== real threads: SQLite locking + double booking
def _run_parallel(*fns):
    barrier = threading.Barrier(len(fns))
    results: list = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - report every failure type
            results[i] = (type(exc).__name__, str(exc))

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


class TestConcurrency:
    @pytest.mark.parametrize("attempt", range(3))
    def test_two_approvals_racing_for_last_bag(self, db, users, make_unit, SessionTest, attempt):
        unit = make_unit(G.AB_NEG)
        r1, r2 = _request(db, users, G.AB_NEG), _request(db, users, G.AB_NEG)

        def approve(rid):
            def fn():
                s = SessionTest()
                try:
                    return req_svc.approve_and_reserve(s, s.get(BloodRequest, rid),
                                                       s.get(User, users["medical_officer"].id))
                finally:
                    s.close()
            return fn

        results = _run_parallel(approve(r1.id), approve(r2.id))
        assert sorted(k for k, _ in results) == ["InventoryError", "ok"], results   # never a lock error
        db.expire_all()
        assert db.get(BloodUnit, unit.id).status == UnitStatus.RESERVED
        assert db.get(BloodUnit, unit.id).reserved_for_request_id in (r1.id, r2.id)

    @pytest.mark.parametrize("attempt", range(3))
    def test_double_issue_of_same_bag(self, db, users, make_unit, SessionTest, attempt):
        unit = make_unit(G.O_POS)
        req = _request(db, users, G.O_POS)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])

        def issue():
            s = SessionTest()
            try:
                return len(req_svc.issue_units(s, s.get(BloodRequest, req.id), _issue([unit.id]),
                                               s.get(User, users["staff"].id)))
            finally:
                s.close()

        results = _run_parallel(issue, issue)
        assert [k for k, _ in results].count("ok") == 1, results
        assert all(k in ("ok", "ServiceError", "InventoryError") for k, _ in results), results
        assert db.scalar(select(func.count()).select_from(Issue).where(Issue.blood_unit_id == unit.id)) == 1


# ============================================================ expiry of reserved blood
class TestReservedExpiry:
    def test_expired_reservation_reopens_request_for_fresh_stock(self, db, users, make_unit):
        old = make_unit(G.A_POS, days_ago=30)                     # expires in 5 days
        req = _request(db, users)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        later = business_today() + timedelta(days=6)                  # reserved bag is now past expiry
        fresh = make_unit(G.A_POS, days_ago=1)
        assert req_svc.approve_and_reserve(db, req, users["medical_officer"], on=later) == 1
        db.expire_all()
        assert db.get(BloodUnit, old.id).status == UnitStatus.EXPIRED
        assert db.get(BloodUnit, old.id).reserved_for_request_id is None
        assert db.get(BloodUnit, fresh.id).reserved_for_request_id == req.id


# ======================================================================== screening
class TestScreeningEdges:
    def test_screening_cannot_be_submitted_twice(self, db, users, make_unit):
        unit = make_unit(accept=False)
        donation_svc.screen_donation(db, unit.donation, screening("accept"), users["medical_officer"])
        with pytest.raises(ServiceError, match="already been completed"):
            donation_svc.screen_donation(db, unit.donation, screening("reject", screening_notes="flip"),
                                         users["medical_officer"])
        assert unit.status == UnitStatus.AVAILABLE

    def test_accepting_a_bag_that_expired_in_quarantine_marks_it_expired(self, db, users, make_unit):
        unit = make_unit(days_ago=40, accept=False)
        donation_svc.screen_donation(db, unit.donation, screening("accept"), users["medical_officer"])
        assert unit.status == UnitStatus.EXPIRED                  # never released as available


# ====================================================== eligibility boundaries (both directions)
class TestEligibilityBoundaries:
    @pytest.mark.parametrize("gender,interval", [(Gender.MALE, 90), (Gender.FEMALE, 120)])
    def test_interval_boundary_after_previous(self, make_donor, gender, interval):
        last = business_today() - timedelta(days=interval)
        donor = make_donor(gender=gender, last_external_donation_date=last)
        assert check_donor(donor).eligible                                         # exactly the interval
        assert not check_donor(donor, on=business_today() - timedelta(days=1)).eligible  # one day short

    def test_interval_boundary_before_later_donation(self, db, users, make_donor):
        donor = make_donor(gender=Gender.MALE)
        later = business_today() - timedelta(days=1)
        donation_svc.record_donation(db, donor, DonationIn(
            donor_id=donor.id, donation_date=later, cleared_by_id=users["medical_officer"].id, **vitals()),
            users["staff"])
        assert check_donor(donor, on=later - timedelta(days=90)).eligible
        assert not check_donor(donor, on=later - timedelta(days=89)).eligible

    def test_age_boundaries(self, make_donor):
        def donor_aged(years, days_offset=0, **kw):
            d = make_donor(**kw)
            t = business_today()
            d.date_of_birth = t.replace(year=t.year - years) + timedelta(days=days_offset)
            return d
        assert check_donor(donor_aged(18)).eligible                       # 18th birthday today
        assert not check_donor(donor_aged(18, days_offset=1)).eligible    # turns 18 tomorrow
        assert check_donor(donor_aged(60, days_offset=1)).eligible        # first-time, still 59
        assert not check_donor(donor_aged(61)).eligible                   # first-time, 61


# =================================================== ABO/Rh: full matrix vs antigen rule
def test_full_abo_rh_matrix_matches_antigen_rule():
    """Red cells are compatible iff every donor antigen (A, B, D) is also present in the recipient."""
    def antigens(g: G) -> set[str]:
        abo, rh = g.value[:-1], g.value[-1]
        return set(abo.replace("O", "")) | ({"D"} if rh == "+" else set())
    for recipient in G:
        for donor in G:
            assert is_compatible(recipient, donor) is (antigens(donor) <= antigens(recipient)), (recipient, donor)


# ============================================== C2: password length in bytes (bcrypt limit)
class TestPasswordBytes:
    HINDI = "पासवर्ड" * 4          # 28 characters, 84 UTF-8 bytes

    def test_schema_rejects_over_72_bytes(self):
        with pytest.raises(ValidationError, match="72 bytes"):
            UserIn(username="hindi.user", full_name="Hindi User", role="staff", password=self.HINDI)

    def test_multibyte_password_within_limit_works(self, client):
        pw = "पासवर्ड" * 3          # 63 bytes
        UserIn(username="hindi.ok", full_name="Hindi Ok", role="staff", password=pw)
        login(client, "admin")
        r = client.post("/users/new", data={"username": "hindi.ok", "full_name": "Hindi Ok", "role": "staff",
                                            "password": pw, "csrf_token": csrf_from(client.get("/users/new").text)},
                        follow_redirects=False)
        assert r.status_code == 303
        client.cookies.clear()
        assert login(client, "hindi.ok", pw).status_code == 303

    def test_http_create_user_returns_400_not_500(self, client):
        login(client, "admin")
        r = client.post("/users/new", data={"username": "hindi.user", "full_name": "Hindi User", "role": "staff",
                                            "password": self.HINDI,
                                            "csrf_token": csrf_from(client.get("/users/new").text)})
        assert r.status_code == 400 and "72 bytes" in r.text


# ======================================================== C4: phone normalisation
class TestPhoneNormalisation:
    @pytest.mark.parametrize("variant", ["98765 12345", "98765-12345", " 9876512345 "])
    def test_formatted_duplicate_is_detected(self, db, users, variant):
        base = dict(full_name="Phone Person", gender="female", date_of_birth="1992-02-02",
                    blood_group="B+", weight_kg=55)
        donor_svc.create_donor(db, DonorIn(phone="9876512345", **base), users["staff"])
        with pytest.raises(ServiceError, match="already registered"):
            donor_svc.create_donor(db, DonorIn(phone=variant, **base), users["staff"])

    def test_international_prefix_kept(self):
        d = DonorIn(full_name="Intl Person", gender="male", date_of_birth="1990-01-01", blood_group="O+",
                    phone="+91 98765-43210", weight_kg=70)
        assert d.phone == "+919876543210"


# ============================================================ C3: migration safety
class TestMigrationSafety:
    def test_failing_step_rolls_back_everything(self, tmp_path, monkeypatch):
        eng = _legacy_engine(tmp_path)

        def failing(conn):
            conn.execute("ALTER TABLE blood_requests ADD COLUMN rejected_by_id INTEGER")
            raise RuntimeError("simulated failure")

        monkeypatch.setitem(STEPS, 1, failing)
        with pytest.raises(RuntimeError):
            upgrade(eng)
        with eng.connect() as c:
            cols = {r[1] for r in c.execute(text("PRAGMA table_info(blood_requests)"))}
            assert "rejected_by_id" not in cols                                  # ALTER rolled back
            assert c.execute(text("PRAGMA user_version")).scalar() == 0
        monkeypatch.undo()
        assert upgrade(eng) == SCHEMA_VERSION                                    # clean retry succeeds
        eng.dispose()

    def test_two_processes_starting_together(self, tmp_path):
        eng = _legacy_engine(tmp_path)
        url = str(eng.url)
        eng.dispose()
        engines = [make_engine(url), make_engine(url)]                           # like two app workers
        results = _run_parallel(*(lambda e=e: upgrade(e) for e in engines))
        assert results == [("ok", SCHEMA_VERSION), ("ok", SCHEMA_VERSION)], results
        with engines[0].connect() as c:
            assert c.execute(text("SELECT count(*) FROM blood_requests")).scalar() == 4
            assert c.execute(text("SELECT rejected_by_id FROM blood_requests WHERE id = 1")).scalar() == 1
        for e in engines:
            e.dispose()


# ========================================================= repeated seed execution
def test_seed_runs_once_and_refuses_to_reseed(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    db_path = (tmp_path / "seed.db").as_posix()
    env["BBMS_DATABASE_URL"] = f"sqlite:///{db_path}"

    def run(*args):
        return subprocess.run([sys.executable, "-m", "scripts.seed", *args], cwd=BASE_DIR, env=env,
                              capture_output=True, text=True, timeout=300)

    def counts():
        e = make_engine(f"sqlite:///{db_path}")
        with e.connect() as c:
            out = tuple(c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                        for t in ("users", "donors", "donations", "blood_requests", "issues"))
        e.dispose()
        return out

    first = run()
    assert first.returncode == 0, first.stderr
    before = counts()
    second = run()                                                                # no --reset
    assert second.returncode == 1 and "already contains data" in second.stdout
    assert counts() == before                                                     # nothing duplicated or wiped


# ============================================================ C5: test isolation guard
def test_suite_never_points_at_real_database():
    real = (BASE_DIR / "bloodbank.db").as_posix()
    assert real not in settings.database_url
    assert settings.app_env == "test"



def test_seed_reset_rebuilds_a_fully_migrated_database(tmp_path):
    """Regression (found in Phase 4 browser testing): --reset on an existing, migrated database with
    availability publications failed on foreign keys, and did not reset the schema version."""
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    db_path = (tmp_path / "reset.db").as_posix()
    env["BBMS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["BBMS_BACKUP_DIR"] = str(tmp_path / "backups")
    run = lambda *a: subprocess.run([sys.executable, "-m", "scripts.seed", *a], cwd=BASE_DIR, env=env,  # noqa: E731
                                    capture_output=True, text=True, timeout=300)
    assert run().returncode == 0
    e = make_engine(f"sqlite:///{db_path}")
    with e.begin() as c:   # simulate a published snapshot (dependent rows in a child table)
        uid = c.execute(text("SELECT id FROM users WHERE role = 'medical_officer'")).scalar()
        c.execute(text("INSERT INTO availability_publications (facility_id, published_by_id, published_at, valid_until) "
                       "VALUES (1, :u, '2026-01-01 00:00:00', '2026-01-02 00:00:00')"), {"u": uid})
        c.execute(text("INSERT INTO availability_entries (publication_id, blood_group, product, units) "
                       "VALUES (1, 'A+', 'whole_blood', 1)"))
    e.dispose()
    r = run("--reset", "--confirm-reset", "reset.db")
    assert r.returncode == 0, r.stderr[-500:]
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1                                   # verified backup taken before the drop
    e = make_engine(f"sqlite:///{db_path}")
    with e.connect() as c:
        from app.migrations import SCHEMA_VERSION
        assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
        assert c.execute(text("SELECT count(*) FROM facilities WHERE is_demo = 1")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM availability_publications")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'")).scalar() >= 35
    e.dispose()
