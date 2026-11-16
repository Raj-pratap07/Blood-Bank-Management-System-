"""Regression tests: rejecting a request must not overwrite who approved it."""
from __future__ import annotations

from sqlalchemy import text

from app.database import Base, make_engine
from app.migrations import SCHEMA_VERSION, upgrade
from app.models import BloodGroup as G
from app.models import Gender, RequestPriority, RequestStatus
from app.schemas.forms import BloodRequestIn
from app.services import requests as req_svc
from tests.conftest import login


def _request(db, users, group=G.A_POS, units=1):
    return req_svc.create_request(db, BloodRequestIn(
        patient_name="History Patient", patient_age=33, patient_gender=Gender.MALE, blood_group=group,
        units_required=units, priority=RequestPriority.ROUTINE, hospital_name="History Hospital",
        requester_name="Dr History", requester_phone="9876500003"), users["staff"])


class TestRejectionHistory:
    def test_reject_after_approval_preserves_approver(self, db, users, make_unit):
        make_unit(G.A_POS)
        req = _request(db, users)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        approved_at = req.approved_at
        req_svc.reject_request(db, req, users["admin"], "Patient transferred")
        assert req.status == RequestStatus.REJECTED
        assert req.approved_by_id == users["medical_officer"].id and req.approved_at == approved_at
        assert req.rejected_by_id == users["admin"].id and req.rejected_at is not None

    def test_reject_pending_records_only_rejecter(self, db, users):
        req = _request(db, users)
        req_svc.reject_request(db, req, users["medical_officer"], "Sample not received")
        assert req.approved_by_id is None and req.approved_at is None
        assert req.rejected_by_id == users["medical_officer"].id

    def test_detail_page_shows_both(self, client, db, users, make_unit):
        make_unit(G.A_POS)
        req = _request(db, users)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        req_svc.reject_request(db, req, users["admin"], "Patient transferred")
        login(client, "staff")
        html = client.get(f"/requests/{req.id}").text
        assert "Approved by</dt>" in html and "Test Medical Officer" in html
        assert "Rejected by</dt>" in html and "Test Administrator" in html


# Schema as it existed before migration v1 (no rejected_by_id / rejected_at).
LEGACY_BLOOD_REQUESTS = """
CREATE TABLE blood_requests (
    id INTEGER PRIMARY KEY, patient_name VARCHAR(120) NOT NULL, patient_age INTEGER NOT NULL,
    patient_gender VARCHAR(32) NOT NULL, blood_group VARCHAR(32) NOT NULL, units_required INTEGER NOT NULL,
    priority VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL, hospital_name VARCHAR(150) NOT NULL,
    hospital_address TEXT, doctor_name VARCHAR(120), requester_name VARCHAR(120) NOT NULL,
    requester_phone VARCHAR(20) NOT NULL, reason TEXT, required_by DATE, created_by_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL, approved_by_id INTEGER, approved_at DATETIME, decision_note TEXT,
    FOREIGN KEY(created_by_id) REFERENCES users (id), FOREIGN KEY(approved_by_id) REFERENCES users (id)
)"""


def _legacy_engine(tmp_path):
    import app.models  # noqa: F401

    eng = make_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with eng.begin() as c:
        c.execute(text(LEGACY_BLOOD_REQUESTS))
    Base.metadata.create_all(eng)  # creates the other tables; leaves legacy blood_requests untouched
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (id, username, full_name, password_hash, role, is_active, created_at) "
                       "VALUES (1, 'mo', 'Dr MO', 'x', 'medical_officer', 1, '2026-01-01')"))
        cols = ("patient_name, patient_age, patient_gender, blood_group, units_required, priority, status, "
                "hospital_name, requester_name, requester_phone, created_by_id, created_at, approved_by_id, approved_at")
        for rid, status, appr in [(1, "rejected", 1), (2, "approved", 1), (3, "pending", None), (4, "rejected", None)]:
            c.execute(text(f"INSERT INTO blood_requests (id, {cols}) VALUES (:id, 'P', 30, 'male', 'A+', 1, 'routine', "
                           f":s, 'H', 'R', '9876500000', 1, '2026-01-01', :a, :at)"),
                      {"id": rid, "s": status, "a": appr, "at": "2026-01-02 10:00:00" if appr else None})
    return eng


class TestMigrationV1:
    def test_upgrade_adds_columns_and_moves_legacy_rejecter(self, tmp_path):
        eng = _legacy_engine(tmp_path)
        assert upgrade(eng) == SCHEMA_VERSION
        with eng.connect() as c:
            rows = {r.id: r for r in c.execute(text(
                "SELECT id, status, approved_by_id, approved_at, rejected_by_id, rejected_at FROM blood_requests"))}
            assert c.execute(text("SELECT count(*) FROM blood_requests")).scalar() == 4   # nothing lost
            assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
        assert rows[1].rejected_by_id == 1 and rows[1].approved_by_id is None and rows[1].rejected_at is not None
        assert rows[2].approved_by_id == 1 and rows[2].rejected_by_id is None   # approved row untouched
        assert rows[3].approved_by_id is None and rows[3].rejected_by_id is None
        assert rows[4].rejected_by_id is None                                    # nothing to move
        eng.dispose()

    def test_upgrade_is_idempotent(self, tmp_path):
        eng = _legacy_engine(tmp_path)
        upgrade(eng)
        with eng.begin() as c:   # a post-migration rejection that keeps its approver
            c.execute(text("UPDATE blood_requests SET status='rejected', rejected_by_id=1 WHERE id=2"))
        assert upgrade(eng) == SCHEMA_VERSION
        with eng.connect() as c:
            r = c.execute(text("SELECT approved_by_id, rejected_by_id FROM blood_requests WHERE id=2")).one()
        assert tuple(r) == (1, 1)
        eng.dispose()

    def test_fresh_database_gets_latest_schema(self, tmp_path):
        from app.database import init_db

        eng = make_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
        init_db(eng)
        with eng.connect() as c:
            cols = {r[1] for r in c.execute(text("PRAGMA table_info(blood_requests)"))}
            assert {"rejected_by_id", "rejected_at"} <= cols
            assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
        eng.dispose()
