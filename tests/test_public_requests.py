"""Phase 6: availability search statuses, public emergency requests, secure tracking and staff processing.

All data here is fictional. The demo facility is the conftest's fictional participating facility.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import (
    AvailabilityPublication,
    BloodRequest,
    BloodUnit,
    Facility,
    PublicRequest,
    PublicRequestEvent,
    RequestStatus,
    Role,
    StockVisibility,
    UnitStatus,
    User,
)
from app.schemas.facilities import FacilityIn
from app.schemas.forms import IssueIn, PublicAcceptIn, PublicDecisionIn, PublicRequestIn
from app.services import availability as avail
from app.services import facilities as fsvc
from app.services import public_requests as prsvc
from app.services import requests as req_svc
from app.services.errors import ServiceError
from app.services.ratelimit import SUBMISSIONS, TRACKING
from app.services.security import hash_password
from app.utils import business_tz, utcnow
from tests.conftest import PASSWORD, csrf_from, login


@pytest.fixture(autouse=True)
def _demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    SUBMISSIONS.reset()
    TRACKING.reset()
    yield
    SUBMISSIONS.reset()
    TRACKING.reset()


def _required_by(hours=6) -> str:
    return (datetime.now(business_tz()) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def _form(fid, **over):
    data = dict(facility_id=str(fid), blood_group="B+", product="whole_blood", units="2", required_by=_required_by(),
                location="Fictional General Hospital, Ward 3", requester_name="Test Requester",
                contact_phone="9811100099", note="Fictional test request")
    data.update(over)
    return data


def _submit_web(client, fid, **over):
    page = client.get("/demo/request")
    return client.post("/demo/request", data={**_form(fid, **over), "csrf_token": csrf_from(page.text)})


def _ref_and_code(html):
    ref = re.search(r'id="reference">([^<]+)<', html).group(1)
    code = re.search(r'id="tracking-code">([^<]+)<', html).group(1)
    return ref, code


def _track_web(client, ref, code):
    page = client.get("/demo/track")
    return client.post("/demo/track", data={"reference": ref, "code": code, "csrf_token": csrf_from(page.text)})


def _submit(db, fid, **over):
    data = PublicRequestIn.model_validate(_form(fid, **over))
    return prsvc.submit(db, data)


@pytest.fixture()
def other(db, users):
    """A second, non-demo facility with its own staff account (fictional)."""
    fac = fsvc.create_facility(db, FacilityIn(display_name="Facility Other (fictional test)"), users["platform_admin"])
    staff = User(username="other.staff", full_name="Other Staff", role=Role.STAFF, facility_id=fac.id,
                 password_hash=hash_password(PASSWORD))
    db.add(staff)
    db.commit()
    return fac, staff


# ======================================================================= migration v5
class TestMigration:
    def test_v5_tables_and_triggers_exist(self, engine):
        with engine.connect() as c:
            assert c.execute(text("PRAGMA user_version")).scalar() == 5
            names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"))}
        assert {"public_requests", "public_request_events", "trg_public_requests_participating",
                "trg_public_requests_immutable", "trg_public_requests_link_ins", "trg_public_requests_link_upd",
                "trg_public_requests_no_delete", "trg_public_request_events_same_facility",
                "trg_public_request_events_no_update", "trg_public_request_events_no_delete"} <= names

    def test_v4_database_upgrades_additively(self, tmp_path):
        from app.database import init_db, make_engine
        path = tmp_path / "v4.db"
        eng = make_engine(f"sqlite:///{path.as_posix()}")
        init_db(eng)
        with eng.begin() as c:                      # simulate a v4 database: drop the v5 objects, reset version
            for t in ("public_request_events", "public_requests"):
                c.exec_driver_sql(f"DROP TABLE {t}")
            c.exec_driver_sql("PRAGMA user_version = 4")
            before = c.exec_driver_sql("SELECT count(*) FROM facilities").scalar()
        init_db(eng)
        init_db(eng)                                 # idempotent
        with eng.connect() as c:
            assert c.exec_driver_sql("PRAGMA user_version").scalar() == 5
            assert c.exec_driver_sql("SELECT count(*) FROM facilities").scalar() == before
            assert c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        eng.dispose()

    def test_db_refuses_requests_for_non_participating_facility(self, db, other):
        fac, _ = other
        db.add(PublicRequest(reference="PR-TEST01", token_hash="x" * 64, facility_id=fac.id, blood_group="A+",
                             product="whole_blood", units=1, required_by=utcnow(), location="X Hospital",
                             requester_name="X Y", contact_phone="9800000000", status="submitted"))
        with pytest.raises(IntegrityError, match="participating facility"):
            db.commit()
        db.rollback()

    def test_db_audit_events_are_append_only_and_codes_immutable(self, db, fid):
        req, _ = _submit(db, fid)
        ev = db.scalar(select(PublicRequestEvent).where(PublicRequestEvent.public_request_id == req.id))
        for stmt, match in [(f"UPDATE public_request_events SET note='x' WHERE id={ev.id}", "append-only"),
                            (f"DELETE FROM public_request_events WHERE id={ev.id}", "append-only"),
                            (f"UPDATE public_requests SET token_hash='{'y' * 64}' WHERE id={req.id}", "cannot be changed"),
                            (f"DELETE FROM public_requests WHERE id={req.id}", "audit trail")]:
            with pytest.raises(IntegrityError, match=match):
                db.execute(text(stmt))
            db.rollback()

    def test_db_refuses_cross_facility_link(self, db, users, fid, other):
        _, ostaff = other
        req, _ = _submit(db, fid)
        foreign = BloodRequest(facility_id=ostaff.facility_id, patient_name="Other Patient", patient_age=30,
                               patient_gender="male", blood_group="A+", units_required=1, priority="routine",
                               hospital_name="Other Hospital", requester_name="Dr Other", requester_phone="9800000001",
                               created_by_id=ostaff.id)
        db.add(foreign)
        db.commit()
        with pytest.raises(IntegrityError, match="cross-facility"):
            db.execute(text(f"UPDATE public_requests SET blood_request_id={foreign.id} WHERE id={req.id}"))
        db.rollback()


# ======================================================================= availability search
class TestAvailabilityStatuses:
    def _view(self, db, users, make_unit, now=None):
        make_unit(group="B+")
        make_unit(group="B+")
        db.get(Facility, users["admin"].facility_id).stock_visibility = StockVisibility.EXACT
        db.commit()
        avail.publish(db, users["medical_officer"])
        fac = db.get(Facility, users["admin"].facility_id)
        return avail.demo_view(db, fac, now=now)

    def test_available_unavailable_and_untracked(self, db, users, make_unit):
        view = self._view(db, users, make_unit)
        rows = {(r.blood_group, r.product): r for r in avail.search_rows(
            view, "Demo", None, True, list(avail.BloodGroup), list(avail.SEARCH_PRODUCTS))}
        assert rows[("B+", "whole_blood")].status == "available" and rows[("B+", "whole_blood")].units == 2
        assert rows[("B+", "whole_blood")].quantity_text == "2 bags (units)"
        assert rows[("O-", "whole_blood")].status == "unavailable" and rows[("O-", "whole_blood")].units == 0
        for p in ("packed_red_cells", "platelets", "fresh_frozen_plasma"):          # never tracked -> not reported
            assert rows[("B+", p)].status == "not_reported" and rows[("B+", p)].units is None
        assert all(r.is_demo for r in rows.values())

    def test_stale_report_shows_time_not_figures(self, db, users, make_unit):
        later = utcnow() + timedelta(hours=settings.availability_max_hours + 1)
        view = self._view(db, users, make_unit, now=later)
        (row,) = avail.search_rows(view, "Demo", None, True, [avail.BloodGroup.B_POS], [avail.Product.WHOLE_BLOOD])
        assert row.status == "stale" and row.units is None and row.updated_at is not None

    def test_no_report_is_not_reported_never_zero(self, db, users):
        fac = db.get(Facility, users["admin"].facility_id)
        (row,) = avail.search_rows(avail.demo_view(db, fac), "Demo", None, True,
                                   [avail.BloodGroup.A_POS], [avail.Product.WHOLE_BLOOD])
        assert row.status == "not_reported" and row.units is None and row.quantity_text == "—"

    def test_demo_view_only_in_demo_mode_and_public_view_still_hides_demo(self, db, users, make_unit, monkeypatch):
        self._view(db, users, make_unit)
        fac = db.get(Facility, users["admin"].facility_id)
        assert avail.public_view(db, fac).state == "off"                 # the real directory rule is unchanged
        monkeypatch.setattr(settings, "demo_mode", False)
        assert avail.demo_view(db, fac).state == "off"

    def test_demo_hub_search_labels_demo_data(self, client, db, users, make_unit):
        self._view(db, users, make_unit)
        html = client.get("/demo?group=B%2B&component=whole_blood").text
        assert 'data-status="available"' in html and "2 bags (units)" in html and "DEMO DATA" in html
        assert "does not reserve or guarantee blood" in html and "112" in html


# ======================================================================= public submission
class TestSubmission:
    def test_submit_shows_reference_and_code_once_and_stores_only_hash(self, client, db, fid):
        r = _submit_web(client, fid)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        ref, code = _ref_and_code(r.text)
        assert re.fullmatch(r"PR-[A-Z2-9]{6}", ref) and re.fullmatch(r"([A-Z2-9]{4}-){3}[A-Z2-9]{4}", code)
        req = db.scalar(select(PublicRequest).where(PublicRequest.reference == ref))
        assert req.token_hash == prsvc.token_hash(code) and code not in req.token_hash
        assert code.replace("-", "") not in str(db.execute(text("SELECT * FROM public_requests")).fetchall())

    @pytest.mark.parametrize("field,value", [("units", "0"), ("units", "11"), ("contact_phone", "abc"),
                                             ("blood_group", "Q+"), ("product", "cryoprecipitate"),
                                             ("location", ""), ("requester_name", ""), ("note", "x" * 501)])
    def test_validation(self, client, db, fid, field, value):
        r = _submit_web(client, fid, **{field: value})
        assert r.status_code == 400 and f'data-error="{field}"' in r.text
        assert db.scalar(select(func.count()).select_from(PublicRequest)) == 0

    @pytest.mark.parametrize("hours", [-2, 24 * 31])
    def test_required_by_window(self, client, db, fid, hours):
        r = _submit_web(client, fid, required_by=_required_by(hours))
        assert r.status_code == 400 and 'data-error="required_by"' in r.text

    def test_csrf_required(self, client, fid):
        client.get("/demo/request")
        r = client.post("/demo/request", data=_form(fid))
        assert r.status_code == 403
        assert client.post("/demo/request", data={**_form(fid), "csrf_token": "forged"}).status_code == 403

    def test_only_demo_facility_can_receive(self, client, db, fid, other):
        fac, _ = other
        r = _submit_web(client, fid, facility_id=str(fac.id))
        assert r.status_code == 400 and "not accepted by this blood bank" in r.text
        with pytest.raises(ServiceError):
            _submit(db, fac.id)

    def test_real_facilities_never_eligible(self, db, users):
        real = fsvc.create_facility(db, FacilityIn(display_name="Real-like Facility (fictional)"), users["platform_admin"])
        assert real.id not in {f.id for f in prsvc.eligible_facilities(db)}
        assert all(f.is_demo for f in prsvc.eligible_facilities(db))

    def test_demo_pages_404_outside_demo_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "demo_mode", False)
        for url in ("/demo", "/demo/request", "/demo/track"):
            assert client.get(url).status_code == 404

    def test_rate_limit(self, client, fid):
        statuses = [_submit_web(client, fid).status_code for _ in range(6)]
        assert statuses[:5] == [200] * 5 and statuses[5] == 429

    def test_no_session_cookie_on_plain_public_pages(self, client):
        client.cookies.clear()
        for url in ("/", "/find", "/demo"):
            assert "set-cookie" not in client.get(url).headers, url


# ======================================================================= tracking
class TestTracking:
    def test_track_with_reference_and_code(self, client, db, fid):
        ref, code = _ref_and_code(_submit_web(client, fid).text)
        r = _track_web(client, ref, code)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert 'data-status="submitted"' in r.text
        assert "Test Requester" not in r.text and "9811100099" not in r.text and "099" in r.text   # masked phone
        assert code not in r.text                                              # the code is never echoed back

    def test_wrong_code_or_reference_gives_one_generic_answer(self, client, db, fid):
        ref, code = _ref_and_code(_submit_web(client, fid).text)
        bad_code = _track_web(client, ref, "AAAA-BBBB-CCCC-DDDD")
        bad_ref = _track_web(client, "PR-ZZZZZZ", code)
        for r in (bad_code, bad_ref):
            assert r.status_code == 404 and "No request matches that reference and tracking code." in r.text
            assert "Test Requester" not in r.text

    def test_no_listing_or_enumeration(self, client, db, fid):
        _submit_web(client, fid)
        assert client.get("/demo/track?reference=PR-AAAAAA").status_code == 200   # GET never shows a request
        assert "Fictional General Hospital" not in client.get("/demo/track").text
        assert client.get("/api/public/requests").status_code == 404

    def test_tracking_code_never_logged(self, client, db, fid, caplog):
        caplog.set_level(logging.DEBUG)
        ref, code = _ref_and_code(_submit_web(client, fid).text)
        _track_web(client, ref, code)
        assert code not in caplog.text and code.replace("-", "") not in caplog.text

    def test_tracking_rate_limit(self, client, fid):
        statuses = [_track_web(client, "PR-AAAAAA", "AAAA-AAAA-AAAA-AAAA").status_code for _ in range(31)]
        assert statuses[-1] == 429


# ======================================================================= staff workflow
class TestStaffWorkflow:
    def test_anonymous_and_platform_admin_cannot_view(self, client, db, fid):
        req, _ = _submit(db, fid)
        for url in ("/incoming-requests", f"/incoming-requests/{req.id}"):
            r = client.get(url, follow_redirects=False)
            assert r.status_code in (302, 303) and "/login" in r.headers["location"]
        login(client, "platformadmin")
        assert client.get("/incoming-requests").status_code == 403

    def test_other_facility_staff_see_nothing(self, client, db, fid, other):
        req, _ = _submit(db, fid)
        login(client, "other.staff")
        assert "Test Requester" not in client.get("/incoming-requests").text
        assert client.get(f"/incoming-requests/{req.id}").status_code == 404
        page = client.get("/incoming-requests").text
        r = client.post(f"/incoming-requests/{req.id}/reject", data={"message": "Not ours at all", "csrf_token": csrf_from(page)})
        assert r.status_code == 404
        db.refresh(req)
        assert req.status.value == "submitted"

    def test_staff_sees_own_facility_requests_with_contact(self, client, db, fid):
        req, _ = _submit(db, fid)
        login(client, "staff")
        assert req.reference in client.get("/incoming-requests").text
        detail = client.get(f"/incoming-requests/{req.id}").text
        assert "Test Requester" in detail and "9811100099" in detail and "DEMO DATA" in detail

    def test_accept_creates_pending_staff_request_and_audit(self, client, db, users, fid):
        req, code = _submit(db, fid)
        login(client, "staff")
        page = client.get(f"/incoming-requests/{req.id}").text
        r = client.post(f"/incoming-requests/{req.id}/accept", data={
            "patient_name": "Fictional Patient", "patient_age": "40", "patient_gender": "female", "priority": "urgent",
            "csrf_token": csrf_from(page)}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/requests/")
        db.expire_all()
        req = db.get(PublicRequest, req.id)
        br = req.blood_request
        assert req.status.value == "accepted" and br.status == RequestStatus.PENDING
        assert br.facility_id == req.facility_id and br.created_by_id == users["staff"].id
        assert (br.blood_group, br.units_required) == (req.blood_group, 2)
        assert [e.action for e in req.events] == ["submitted", "accepted"]
        assert req.events[1].actor_id == users["staff"].id
        assert prsvc.public_status(req).key == "accepted"
        assert db.scalar(select(func.count()).select_from(BloodUnit).where(
            BloodUnit.reserved_for_request_id == br.id)) == 0                    # acceptance never reserves

    def test_staff_cannot_approve_medical_officer_must(self, db, users, fid, make_unit):
        make_unit(group="B+")
        req, _ = _submit(db, fid)
        br = prsvc.accept(db, req, PublicAcceptIn(patient_name="Fictional Patient", patient_age=40,
                                                  patient_gender="male", priority="urgent"), users["staff"])
        with pytest.raises(ServiceError):
            req_svc.approve_and_reserve(db, br, users["staff"])

    def test_double_accept_is_refused(self, db, users, fid):
        req, _ = _submit(db, fid)
        data = PublicAcceptIn(patient_name="Fictional Patient", patient_age=40, patient_gender="male", priority="routine")
        prsvc.accept(db, req, data, users["staff"])
        stale = db.get(PublicRequest, req.id)
        stale.status = prsvc.S.SUBMITTED              # simulate a second user's stale page
        with pytest.raises(ServiceError, match="already been decided|no longer open"):
            prsvc.accept(db, stale, data, users["admin"])
        db.rollback()
        assert db.scalar(select(func.count()).select_from(BloodRequest).where(
            BloodRequest.reason.like(f"%{req.reference}%"))) == 1

    def test_non_whole_blood_cannot_be_accepted(self, db, users, fid):
        req, _ = _submit(db, fid, product="platelets")
        with pytest.raises(ServiceError, match="Only whole blood"):
            prsvc.accept(db, req, PublicAcceptIn(patient_name="Fictional Patient", patient_age=40,
                                                 patient_gender="male", priority="routine"), users["staff"])
        assert db.get(PublicRequest, req.id).status.value == "submitted"

    @pytest.mark.parametrize("action,key", [("reject", "rejected"), ("clarify", "clarification")])
    def test_reject_and_clarify_show_message_to_requester(self, client, db, users, fid, action, key):
        ref, code = _ref_and_code(_submit_web(client, fid).text)
        req = db.scalar(select(PublicRequest).where(PublicRequest.reference == ref))
        prsvc.decide(db, req, action, PublicDecisionIn(message="Please call the blood bank on its main number."),
                     users["staff"])
        r = _track_web(client, ref, code)
        assert f'data-status="{key}"' in r.text and "Please call the blood bank on its main number." in r.text

    def test_full_journey_updates_status_and_inventory(self, client, db, users, fid, make_unit):
        units = [make_unit(group="B+"), make_unit(group="B+")]
        db.get(Facility, fid).stock_visibility = StockVisibility.EXACT
        db.commit()
        avail.publish(db, users["medical_officer"])
        search = client.get("/demo?group=B%2B&component=whole_blood").text
        assert "2 bags (units)" in search
        ref, code = _ref_and_code(_submit_web(client, fid).text)
        req = db.scalar(select(PublicRequest).where(PublicRequest.reference == ref))
        br = prsvc.accept(db, req, PublicAcceptIn(patient_name="Fictional Patient", patient_age=40,
                                                  patient_gender="male", priority="urgent"), users["staff"])
        assert req_svc.approve_and_reserve(db, br, users["medical_officer"]) == 2
        req_svc.issue_units(db, br, IssueIn(unit_ids=[u.id for u in units], received_by="Fictional Nurse",
                                            crossmatch_confirmed=True), users["staff"])
        db.expire_all()
        assert db.get(BloodRequest, br.id).status == RequestStatus.FULFILLED
        assert all(db.get(BloodUnit, u.id).status == UnitStatus.ISSUED for u in units)
        r = _track_web(client, ref, code)
        assert 'data-status="fulfilled"' in r.text and "Bag issued" in r.text
        after = client.get("/demo?group=B%2B&component=whole_blood").text      # min(published, live) -> 0
        assert 'data-status="unavailable"' in after and "0 bags (units)" in after


# ======================================================================= real vs synthetic separation
class TestSeparation:
    def test_find_and_public_api_never_show_demo_or_requests(self, client, db, fid):
        _submit(db, fid)
        assert "demo-blood-centre" not in client.get("/find").text
        assert client.get("/find/demo-blood-centre").status_code == 404
        assert client.get("/api/public/availability?facility=demo-blood-centre").status_code == 422

    def test_demo_publication_never_touches_real_facilities(self, db, users, make_unit):
        real = fsvc.create_facility(db, FacilityIn(display_name="Another Real-like Facility"), users["platform_admin"])
        make_unit(group="A+")
        db.get(Facility, users["admin"].facility_id).stock_visibility = StockVisibility.EXACT
        db.commit()
        avail.publish(db, users["medical_officer"])
        assert db.scalar(select(func.count()).select_from(AvailabilityPublication).where(
            AvailabilityPublication.facility_id == real.id)) == 0
        assert db.scalar(select(func.count()).select_from(BloodUnit).where(BloodUnit.facility_id == real.id)) == 0
