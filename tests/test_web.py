"""HTTP-level tests: authentication, role-based authorisation, CSRF and the end-to-end workflow."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models import BloodRequest, BloodUnit, Donation, Donor, RequestStatus, UnitStatus
from tests.conftest import csrf_from, login


def post(client, url: str, data: dict, page: str | None = None):
    """POST a form with a valid CSRF token taken from `page` (or the dashboard)."""
    html = client.get(page or "/").text
    return client.post(url, data={**data, "csrf_token": csrf_from(html)}, follow_redirects=False)


# ------------------------------------------------------------- authentication
class TestAuthentication:
    @pytest.mark.parametrize("path", ["/donors", "/donations/new", "/inventory", "/requests", "/issues",
                                      "/reports/inventory", "/users", "/platform/facilities"])
    def test_pages_require_login(self, client, path):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")

    def test_root_is_public_landing_for_anonymous_and_dashboard_for_staff(self, client):
        # Phase 2 (approved): anonymous "/" shows the public landing page, never operational data.
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200 and "Find a blood bank in Haldwani" in r.text and "Recent donations" not in r.text
        login(client, "staff")
        assert "Recent donations" in client.get("/").text

    def test_api_requires_login(self, client):
        assert client.get("/api/stats/inventory").status_code == 401

    def test_login_success_and_logout(self, client):
        assert login(client, "staff").status_code == 303
        assert client.get("/").status_code == 200
        post(client, "/logout", {})
        # Logged out: "/" is now the public landing page (Phase 2), and staff pages require login again.
        assert "Recent donations" not in client.get("/").text
        assert client.get("/donors", follow_redirects=False).status_code == 303

    def test_wrong_password(self, client):
        r = login(client, "staff", "wrong-password")
        assert r.status_code == 401 and "Invalid username or password" in r.text

    def test_open_redirect_blocked(self, client):
        token = csrf_from(client.get("/login").text)
        r = client.post("/login", data={"username": "staff", "password": "Password@123", "next": "//evil.example",
                                        "csrf_token": token}, follow_redirects=False)
        assert r.headers["location"] == "/"

    def test_inactive_user_cannot_login(self, client, db, users):
        users["staff"].is_active = False
        db.commit()
        assert login(client, "staff").status_code == 401


# ------------------------------------------------------------------- CSRF
class TestCSRF:
    def test_post_without_token_rejected(self, client):
        login(client, "admin")
        r = client.post("/donors/new", data={"full_name": "No Token"}, follow_redirects=False)
        assert r.status_code == 403 and "security token" in r.text

    def test_post_with_wrong_token_rejected(self, client):
        login(client, "admin")
        r = client.post("/donors/new", data={"full_name": "X", "csrf_token": "forged"}, follow_redirects=False)
        assert r.status_code == 403

    def test_login_requires_token(self, client):
        r = client.post("/login", data={"username": "admin", "password": "Password@123"})
        assert r.status_code == 403


# ------------------------------------------------------- role-based access
class TestAuthorization:
    def test_staff_cannot_manage_users(self, client):
        login(client, "staff")
        assert client.get("/users").status_code == 403
        r = post(client, "/users/new", {"username": "hacker", "full_name": "Hack Er", "role": "admin",
                                        "password": "Password@123"})
        assert r.status_code == 403

    def test_medical_officer_cannot_manage_users(self, client):
        login(client, "medicalofficer")
        assert client.get("/users").status_code == 403

    def test_admin_creates_staff_account(self, client):
        login(client, "admin")
        r = post(client, "/users/new", {"username": "nurse.ria", "full_name": "Ria Thapa", "role": "staff",
                                        "password": "Nurse@12345"}, page="/users/new")
        assert r.status_code == 303
        client.cookies.clear()
        assert login(client, "nurse.ria", "Nurse@12345").status_code == 303

    def test_staff_cannot_screen_or_approve_or_discard(self, client, make_unit, users, db):
        unit = make_unit(accept=False)
        login(client, "staff")
        r = post(client, f"/donations/{unit.donation_id}/screening",
                 {"hiv_result": "non_reactive", "hbv_result": "non_reactive", "hcv_result": "non_reactive",
                  "syphilis_result": "non_reactive", "malaria_result": "non_reactive",
                  "abo_rh_confirmed": "true", "decision": "accept"})
        assert r.status_code == 403
        r = post(client, f"/inventory/units/{unit.id}/discard", {"reason": "test"})
        assert r.status_code == 403
        db.expire_all()
        assert db.get(BloodUnit, unit.id).status == UnitStatus.QUARANTINE


# ------------------------------------------------------------- end to end
class TestWorkflow:
    def test_full_workflow_over_http(self, client, db, users):
        # 1. Staff registers a donor.
        login(client, "staff")
        r = post(client, "/donors/new", {
            "full_name": "Sunil Rawat", "gender": "male", "date_of_birth": "1990-05-04", "blood_group": "A+",
            "phone": "9811122233", "weight_kg": "70", "city": "Dehradun"}, page="/donors/new")
        assert r.status_code == 303, r.text
        donor = db.scalar(select(Donor).where(Donor.phone == "9811122233"))
        assert donor is not None

        # 2. Staff records a donation, cleared by the medical officer -> quarantine.
        r = post(client, "/donations/new", {
            "donor_id": donor.id, "donation_date": date.today().isoformat(), "volume_ml": "450",
            "weight_kg": "70", "hemoglobin_g_dl": "14.2", "bp_systolic": "122", "bp_diastolic": "78",
            "pulse_bpm": "74", "temperature_c": "36.7", "questionnaire_ok": "true",
            "cleared_by_id": users["medical_officer"].id}, page=f"/donations/new?donor_id={donor.id}")
        assert r.status_code == 303, r.text
        donation = db.scalar(select(Donation).where(Donation.donor_id == donor.id))
        assert donation.units[0].status == UnitStatus.QUARANTINE

        # 3. Immediate repeat donation is blocked by the interval rule.
        r = post(client, "/donations/new", {
            "donor_id": donor.id, "donation_date": date.today().isoformat(), "volume_ml": "350",
            "weight_kg": "70", "hemoglobin_g_dl": "14.2", "bp_systolic": "122", "bp_diastolic": "78",
            "pulse_bpm": "74", "temperature_c": "36.7", "questionnaire_ok": "true",
            "cleared_by_id": users["medical_officer"].id})
        assert r.status_code == 400 and "minimum interval" in r.text

        # 4. Medical officer accepts screening -> bag available.
        client.cookies.clear()
        login(client, "medicalofficer")
        r = post(client, f"/donations/{donation.id}/screening", {
            "hiv_result": "non_reactive", "hbv_result": "non_reactive", "hcv_result": "non_reactive",
            "syphilis_result": "non_reactive", "malaria_result": "non_reactive", "abo_rh_confirmed": "true",
            "decision": "accept"}, page=f"/donations/{donation.id}")
        assert r.status_code == 303, r.text
        db.expire_all()
        unit = db.get(Donation, donation.id).units[0]
        assert unit.status == UnitStatus.AVAILABLE

        # 5. Staff registers an AB+ request (A+ is compatible) but cannot issue before approval.
        client.cookies.clear()
        login(client, "staff")
        r = post(client, "/requests/new", {
            "patient_name": "Meera Joshi", "patient_age": "45", "patient_gender": "female", "blood_group": "AB+",
            "units_required": "1", "priority": "urgent", "hospital_name": "Doon Hospital",
            "requester_name": "Dr Pant", "requester_phone": "9876501234"}, page="/requests/new")
        assert r.status_code == 303, r.text
        req = db.scalar(select(BloodRequest).where(BloodRequest.patient_name == "Meera Joshi"))
        r = post(client, f"/requests/{req.id}/approve", {})
        assert r.status_code == 403

        # 6. Medical officer approves -> bag reserved.
        client.cookies.clear()
        login(client, "medicalofficer")
        post(client, f"/requests/{req.id}/approve", {"note": "OK"})
        db.expire_all()
        assert db.get(BloodUnit, unit.id).status == UnitStatus.RESERVED

        # 7. Staff issues; crossmatch confirmation is mandatory.
        client.cookies.clear()
        login(client, "staff")
        page = f"/requests/{req.id}"
        r = post(client, f"/requests/{req.id}/issue", {"unit_ids[]": unit.id, "received_by": "Nurse Kiran"}, page)
        assert r.status_code == 400 and "crossmatch" in r.text
        r = post(client, f"/requests/{req.id}/issue", {"unit_ids[]": unit.id, "received_by": "Nurse Kiran",
                                                         "crossmatch_confirmed": "true"}, page)
        assert r.status_code == 303, r.text
        db.expire_all()
        assert db.get(BloodUnit, unit.id).status == UnitStatus.ISSUED
        assert db.get(BloodRequest, req.id).status == RequestStatus.FULFILLED

        # 8. Every page renders for the result.
        for path in ["/", "/donors", f"/donors/{donor.id}", "/donations", f"/donations/{donation.id}",
                     "/inventory", "/inventory/units", f"/inventory/units/{unit.id}", "/requests",
                     f"/requests/{req.id}", "/issues", "/reports", "/reports/donations", "/reports/inventory",
                     "/reports/requests", "/reports/issues", "/account/password", "/api/stats/inventory",
                     "/api/stats/donations-trend", "/api/stats/requests-by-status", "/api/donors/search?q=Sunil"]:
            assert client.get(path).status_code == 200, path

    def test_invalid_donor_form_shows_errors(self, client):
        login(client, "staff")
        r = post(client, "/donors/new", {"full_name": "A", "gender": "male", "date_of_birth": "2999-01-01",
                                         "blood_group": "Q+", "phone": "abc", "weight_kg": "5"}, page="/donors/new")
        assert r.status_code == 400
        assert r.text.count("invalid-feedback") >= 4

    def test_404_page(self, client):
        login(client, "staff")
        assert client.get("/donors/99999").status_code == 404
