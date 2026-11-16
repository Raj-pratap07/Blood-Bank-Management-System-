"""Phase 3 security tests: multi-facility data isolation.

Two fictional facilities share one temporary database:
  A = the fictional demo facility (standard test users: admin / medicalofficer / staff)
  B = "Facility B (fictional test)", onboarded by the platform admin, with its own users and data.
B's records use distinctive "Bravo Secret ..." names so any leak into A's pages is detectable.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    BloodRequest,
    BloodUnit,
    Donation,
    Donor,
    Facility,
    Issue,
    RequestStatus,
    Role,
    UnitStatus,
    User,
)
from app.schemas.facilities import FacilityIn
from app.schemas.forms import BloodRequestIn, DonationIn, DonorIn, IssueIn, OnboardIn, UserIn
from app.services import donations as donation_svc
from app.services import donors as donor_svc
from app.services import facilities as fsvc
from app.services import requests as req_svc
from app.services import users as user_svc
from app.services.errors import PermissionDenied, RecordNotFound, ServiceError
from app.services.inventory import fefo_candidates, stock_summary
from app.services.reports import dashboard_counts
from tests.conftest import PASSWORD, csrf_from, login, screening, vitals

TODAY = date.today()


# ------------------------------------------------------------------ fixtures
def _donate(db, donor, cleared_by, actor, days_ago=2, accept=True):
    d = donation_svc.record_donation(db, donor, DonationIn(
        donor_id=donor.id, donation_date=TODAY - timedelta(days=days_ago), cleared_by_id=cleared_by.id,
        **vitals()), actor)
    if accept:
        donation_svc.screen_donation(db, d, screening("accept"), cleared_by)
    return d


@pytest.fixture()
def b(db, users):
    """Facility B with its own admin / medical officer / staff and operational data."""
    p = users["platform_admin"]
    fac = fsvc.create_facility(db, FacilityIn(display_name="Facility B (fictional test)"), p)
    admin, token = user_svc.onboard_facility_admin(db, fac, OnboardIn(username="b.admin", full_name="Bravo Admin"), p)
    user_svc.complete_setup(db, token, PASSWORD)              # the admin sets their own password via the link
    mo = user_svc.create_user(db, UserIn(username="b.mo", full_name="Bravo Officer", role="medical_officer",
                                         password=PASSWORD), admin)
    staff = user_svc.create_user(db, UserIn(username="b.staff", full_name="Bravo Staff", role="staff",
                                            password=PASSWORD), admin)
    for u in (mo, staff):           # simulate that they already replaced the admin-given password
        u.must_change_password = False
    db.commit()
    donors = [donor_svc.create_donor(db, DonorIn(
        full_name=f"Bravo Secret Donor {i}", gender="male", date_of_birth="1990-01-01", blood_group="AB-",
        phone=f"970000000{i}", weight_kg=70), staff) for i in (1, 2)]
    units = [_donate(db, d, mo, staff).units[0] for d in donors]
    pending = _donate(db, donor_svc.create_donor(db, DonorIn(
        full_name="Bravo Secret Donor 3", gender="female", date_of_birth="1991-01-01", blood_group="AB-",
        phone="9700000003", weight_kg=60), staff), mo, staff, accept=False)
    req = req_svc.create_request(db, BloodRequestIn(
        patient_name="Bravo Secret Patient", patient_age=50, patient_gender="male", blood_group="AB-",
        units_required=2, priority="urgent", hospital_name="Bravo Test Hospital", requester_name="Dr Bravo",
        requester_phone="9700000099"), staff)
    req_svc.approve_and_reserve(db, req, mo)
    issue = req_svc.issue_units(db, req, IssueIn(unit_ids=[units[0].id], received_by="Bravo Nurse",
                                                 crossmatch_confirmed=True), staff)[0]
    db.refresh(req)
    return dict(facility=fac, admin=admin, mo=mo, staff=staff, donor=donors[0], donation=units[0].donation,
                pending=pending, unit=units[1], issued_unit=units[0], req=req, issue=issue)


@pytest.fixture()
def a(db, users, make_donor):
    """Facility A (demo facility) data: one available O+ bag and an open request."""
    donor = make_donor(group="O+", name="Alpha Visible Donor")
    unit = _donate(db, donor, users["medical_officer"], users["staff"]).units[0]
    req = req_svc.create_request(db, BloodRequestIn(
        patient_name="Alpha Visible Patient", patient_age=40, patient_gender="female", blood_group="AB-",
        units_required=1, priority="routine", hospital_name="Alpha Test Hospital", requester_name="Dr Alpha",
        requester_phone="9600000099"), users["staff"])
    return dict(donor=donor, unit=unit, req=req)


def _post(client, url, data):
    page = client.get("/donors").text if "csrf_token" not in data else ""
    return client.post(url, data={"csrf_token": csrf_from(page), **data}, follow_redirects=False)


LEAK_MARKERS = ("Bravo Secret", "9700000001", "Bravo Test Hospital", "Bravo Nurse")


def assert_no_leak(html: str):
    for marker in LEAK_MARKERS:
        assert marker not in html, f"facility B data leaked: {marker!r}"


# =========================================================== ownership & schema
class TestOwnership:
    def test_every_record_has_the_right_facility(self, db, users, a, b):
        fa, fb = users["staff"].facility_id, b["facility"].id
        assert fa != fb
        assert (a["donor"].facility_id, a["unit"].facility_id, a["req"].facility_id) == (fa, fa, fa)
        assert {b["donor"].facility_id, b["donation"].facility_id, b["unit"].facility_id, b["req"].facility_id,
                b["issue"].facility_id, b["staff"].facility_id} == {fb}
        assert users["platform_admin"].facility_id is None
        assert db.get(Facility, fb).is_participating                   # onboarding marks participation

    def test_donor_phone_unique_per_facility_only(self, db, users, b):
        same_phone = DonorIn(full_name="Same Phone A", gender="male", date_of_birth="1990-01-01",
                             blood_group="O+", phone=b["donor"].phone, weight_kg=70)
        donor_svc.create_donor(db, same_phone, users["staff"])          # allowed in A
        with pytest.raises(ServiceError, match="already registered"):
            donor_svc.create_donor(db, same_phone, b["staff"])          # duplicate inside B


# ============================================================ 1-8, 15: HTTP isolation
FOREIGN_GETS = [
    ("/donors/{donor}", "donor"), ("/donors/{donor}/edit", "donor"), ("/donations/{donation}", "donation"),
    ("/inventory/units/{unit}", "unit"), ("/requests/{req}", "req"),
]


class TestCrossFacilityHttp:
    def test_1_lists_never_show_other_facility(self, client, a, b):
        login(client, "staff")
        for path in ("/donors", "/donors?inactive=true", "/donors?q=Bravo", "/donations", "/inventory/units",
                     "/requests", "/issues", "/inventory"):
            r = client.get(path)
            assert r.status_code == 200
            assert_no_leak(r.text)
        assert "Alpha Visible Donor" in client.get("/donors").text     # own data still visible

    @pytest.mark.parametrize("path,key", FOREIGN_GETS)
    def test_2_5_6_7_foreign_ids_are_404_without_leak(self, client, a, b, path, key):
        login(client, "staff")
        r = client.get(path.format(**{key: b[key].id}))
        assert r.status_code == 404
        assert_no_leak(r.text)

    @pytest.mark.parametrize("path,key", FOREIGN_GETS)
    def test_own_records_still_open(self, client, a, b, path, key):
        login(client, "b.staff")
        assert client.get(path.format(**{key: b[key].id})).status_code == 200

    def test_404_is_indistinguishable_from_missing(self, client, a, b):
        login(client, "staff")
        client.get("/donors")                                   # consume the one-time "Welcome" flash message
        foreign = client.get(f"/donors/{b['donor'].id}")
        missing = client.get("/donors/999999")
        assert foreign.status_code == missing.status_code == 404 and foreign.text == missing.text

    def test_3_cannot_modify_foreign_donor(self, client, db, a, b):
        login(client, "admin")
        r = _post(client, f"/donors/{b['donor'].id}/edit", {
            "full_name": "Hijacked", "gender": "male", "date_of_birth": "1990-01-01", "blood_group": "AB-",
            "phone": "9700000001", "weight_kg": "70"})
        assert r.status_code == 404
        db.expire_all()
        assert db.get(Donor, b["donor"].id).full_name == "Bravo Secret Donor 1"

    def test_4_cannot_deactivate_foreign_donor_and_no_delete_route(self, client, db, a, b):
        login(client, "admin")
        assert _post(client, f"/donors/{b['donor'].id}/toggle", {}).status_code == 404
        db.expire_all()
        assert db.get(Donor, b["donor"].id).is_active
        assert client.request("DELETE", f"/donors/{b['donor'].id}").status_code in (403, 404, 405)

    def test_5_cannot_screen_or_donate_for_foreign_records(self, client, db, users, a, b):
        login(client, "medicalofficer")
        r = _post(client, f"/donations/{b['pending'].id}/screening", {
            "hiv_result": "non_reactive", "hbv_result": "non_reactive", "hcv_result": "non_reactive",
            "syphilis_result": "non_reactive", "malaria_result": "non_reactive", "abo_rh_confirmed": "true",
            "decision": "accept"})
        assert r.status_code == 404
        db.expire_all()
        assert db.get(Donation, b["pending"].id).status.value == "quarantine"
        # Donation form for a foreign donor shows no donor, and posting one is refused
        assert_no_leak(client.get(f"/donations/new?donor_id={b['donor'].id}").text)
        before = db.scalar(select(func.count()).select_from(Donation))
        r = _post(client, "/donations/new", {"donor_id": b["donor"].id, "donation_date": TODAY.isoformat(),
                                             "cleared_by_id": users["medical_officer"].id,
                                             **{k: str(v) for k, v in vitals().items()}})
        assert r.status_code == 400 and "Select a registered donor" in r.text
        assert db.scalar(select(func.count()).select_from(Donation)) == before

    def test_6_cannot_touch_foreign_inventory(self, client, db, a, b):
        login(client, "medicalofficer")
        assert _post(client, f"/inventory/units/{b['unit'].id}/discard", {"reason": "sabotage"}).status_code == 404
        db.expire_all()
        assert db.get(BloodUnit, b["unit"].id).status == UnitStatus.RESERVED

    @pytest.mark.parametrize("action,data", [("approve", {}), ("reject", {"reason": "nope nope"}),
                                             ("cancel", {}), ("issue", {"unit_ids[]": "0", "received_by": "X Y",
                                                                        "crossmatch_confirmed": "true"})])
    def test_7_cannot_act_on_foreign_requests(self, client, db, a, b, action, data):
        login(client, "medicalofficer")
        if action == "issue":
            data = {**data, "unit_ids[]": str(b["unit"].id)}
        assert _post(client, f"/requests/{b['req'].id}/{action}", data).status_code == 404
        db.expire_all()
        assert db.get(BloodRequest, b["req"].id).status == RequestStatus.PARTIALLY_ISSUED

    def test_8_issue_history_and_reports_scoped(self, client, a, b):
        login(client, "admin")
        for path in ("/issues", "/issues?start=&end=", "/reports/issues", "/reports/donations",
                     "/reports/requests", "/reports/inventory"):
            r = client.get(path)
            assert r.status_code == 200
            assert_no_leak(r.text)
            assert b["issued_unit"].unit_code not in r.text and b["unit"].unit_code not in r.text

    def test_15_cannot_issue_foreign_bag_to_own_request(self, client, db, users, a, b):
        login(client, "staff")
        # Even on an approved request of A, a bag ID from facility B must be treated as non-existent.
        a["req"].status = RequestStatus.APPROVED
        db.commit()
        r = _post(client, f"/requests/{a['req'].id}/issue", {"unit_ids[]": str(b["unit"].id),
                                                             "received_by": "Alpha Nurse", "crossmatch_confirmed": "true"})
        assert r.status_code == 400 and "do not exist" in r.text
        db.expire_all()
        assert db.get(BloodUnit, b["unit"].id).status == UnitStatus.RESERVED
        assert db.scalar(select(func.count()).select_from(Issue).where(Issue.blood_unit_id == b["unit"].id)) == 0


# ======================================================== 9-10: ownership tampering
class TestOwnershipTampering:
    def test_9_facility_id_in_create_forms_is_ignored(self, client, db, users, b):
        login(client, "staff")
        _post(client, "/donors/new", {"full_name": "Tamper Donor", "gender": "male", "date_of_birth": "1990-01-01",
                                      "blood_group": "O+", "phone": "9500000001", "weight_kg": "70",
                                      "facility_id": str(b["facility"].id)})
        _post(client, "/requests/new", {"patient_name": "Tamper Patient", "patient_age": "30", "patient_gender": "male",
                                        "blood_group": "O+", "units_required": "1", "priority": "routine",
                                        "hospital_name": "Tamper Hospital", "requester_name": "Dr T",
                                        "requester_phone": "9500000002", "facility_id": str(b["facility"].id)})
        own = users["staff"].facility_id
        assert db.scalar(select(Donor.facility_id).where(Donor.full_name == "Tamper Donor")) == own
        assert db.scalar(select(BloodRequest.facility_id).where(BloodRequest.patient_name == "Tamper Patient")) == own

    def test_9b_admin_cannot_create_account_in_other_facility(self, client, db, b):
        login(client, "admin")
        _post(client, "/users/new", {"username": "tamper.user", "full_name": "Tamper User", "role": "staff",
                                     "password": "Password@123", "facility_id": str(b["facility"].id)})
        assert db.scalar(select(User.facility_id).where(User.username == "tamper.user")) != b["facility"].id

    def test_10_update_cannot_change_ownership(self, client, db, users, a, b):
        login(client, "admin")
        _post(client, f"/donors/{a['donor'].id}/edit", {
            "full_name": "Alpha Visible Donor", "gender": "male", "date_of_birth": a["donor"].date_of_birth.isoformat(),
            "blood_group": "O+", "phone": a["donor"].phone, "weight_kg": "65", "facility_id": str(b["facility"].id)})
        db.expire_all()
        assert db.get(Donor, a["donor"].id).facility_id == users["staff"].facility_id

    @pytest.mark.parametrize("table", ["users", "donors", "donations", "blood_units", "blood_requests", "issues"])
    def test_10b_database_refuses_any_facility_change(self, db, b, table):
        row_id = {"users": b["staff"].id, "donors": b["donor"].id, "donations": b["donation"].id,
                  "blood_units": b["unit"].id, "blood_requests": b["req"].id, "issues": b["issue"].id}[table]
        # Rejected either by the immutability trigger or, first, by a cross-facility link rule.
        with pytest.raises(IntegrityError, match="cannot be changed|cross-facility"):
            db.execute(text(f"UPDATE {table} SET facility_id = 1 WHERE id = :i"), {"i": row_id})
        db.rollback()


# ================================================ 11-12, 14: aggregates & API
class TestAggregatesAndApi:
    def test_11_dashboard_counts_are_per_facility(self, client, db, users, a, b):
        fa, fb = users["staff"].facility_id, b["facility"].id
        ca, cb = dashboard_counts(db, fa), dashboard_counts(db, fb)
        assert cb["donors"] == 3 and cb["reserved"] == 1 and cb["quarantine"] == 1
        assert ca["donors"] == 1 and ca["reserved"] == 0 and ca["available"] == 1
        login(client, "staff")
        html = client.get("/").text
        assert_no_leak(html)
        assert "Recent donations" in html

    def test_12_stock_summary_per_facility(self, db, users, a, b):
        a_rows = {r["group"]: r for r in stock_summary(db, users["staff"].facility_id)}
        b_rows = {r["group"]: r for r in stock_summary(db, b["facility"].id)}
        assert a_rows["AB-"]["reserved"] == 0 and a_rows["O+"]["available"] == 1
        assert b_rows["AB-"]["reserved"] == 1 and b_rows["O+"]["available"] == 0

    def test_14_direct_api_requests_are_scoped(self, client, a, b):
        login(client, "staff")
        inv = client.get("/api/stats/inventory").json()
        idx = inv["labels"].index("AB-")
        assert inv["reserved"][idx] == 0 and inv["available"][inv["labels"].index("O+")] == 1
        assert client.get("/api/donors/search?q=Bravo").json() == []
        assert client.get("/api/donors/search?q=9700000001").json() == []
        assert len(client.get("/api/donors/search?q=Alpha").json()) == 1
        rs = client.get("/api/stats/requests-by-status").json()
        assert rs["values"][rs["labels"].index("Partially Issued")] == 0
        assert sum(client.get("/api/stats/donations-trend").json()["values"]) == 1


# ================================================================ 13: platform
class TestPlatformAdmin:
    @pytest.mark.parametrize("path", ["/donors", "/donors/{donor}", "/donations/{donation}", "/inventory/units/{unit}",
                                      "/requests/{req}", "/issues", "/reports/donations", "/api/donors/search?q=Bravo",
                                      "/api/stats/inventory", "/users"])
    def test_13_platform_admin_has_no_operational_access(self, client, a, b, path):
        login(client, "platformadmin")
        r = client.get(path.format(donor=b["donor"].id, donation=b["donation"].id, unit=b["unit"].id, req=b["req"].id),
                       follow_redirects=False)
        assert r.status_code == 403
        assert_no_leak(r.text)

    def test_13b_platform_pages_show_no_operational_data(self, client, a, b):
        login(client, "platformadmin")
        html = client.get(f"/platform/facilities/{b['facility'].id}").text
        assert "Staff accounts: <strong>3</strong>" in html
        assert_no_leak(html)
        assert "Alpha Visible" not in client.get("/platform/facilities").text

    def test_13c_services_refuse_platform_admin(self, db, users, b):
        plat = users["platform_admin"]
        with pytest.raises(PermissionDenied):
            donor_svc.create_donor(db, DonorIn(full_name="Plat Donor", gender="male", date_of_birth="1990-01-01",
                                               blood_group="O+", phone="9400000001", weight_kg=70), plat)
        with pytest.raises(PermissionDenied):
            req_svc.cancel_request(db, b["req"], plat)
        db.expire_all()
        assert db.get(BloodRequest, b["req"].id).status == RequestStatus.PARTIALLY_ISSUED

    def test_onboarding_only_creates_admin_of_that_facility(self, db, users, b):
        assert b["admin"].role == Role.ADMIN and b["admin"].facility_id == b["facility"].id
        with pytest.raises(PermissionDenied):          # a facility admin cannot onboard anyone
            user_svc.onboard_facility_admin(db, b["facility"], OnboardIn(username="x.adm", full_name="X Adm"),
                                            users["admin"])


# ===================================================== 15: related-record lookups
class TestRelatedRecords:
    def test_reservation_never_takes_other_facility_stock(self, db, users, a, b):
        # A needs AB-; only B holds AB- bags.
        assert fefo_candidates(db, users["staff"].facility_id, "AB-") == []
        with pytest.raises(ServiceError, match="No unexpired"):
            req_svc.approve_and_reserve(db, a["req"], users["medical_officer"])
        db.expire_all()
        assert db.get(BloodUnit, b["unit"].id).reserved_for_request_id == b["req"].id

    def test_clearance_by_other_facilitys_officer_refused(self, db, users, make_donor, b):
        donor = make_donor(group="O+", name="Alpha Clearance Donor")
        with pytest.raises(ServiceError, match="of this blood bank"):
            donation_svc.record_donation(db, donor, DonationIn(
                donor_id=donor.id, donation_date=TODAY, cleared_by_id=b["mo"].id, **vitals()), users["staff"])

    def test_donation_form_lists_only_own_officers(self, client, a, b):
        login(client, "staff")
        html = client.get(f"/donations/new?donor_id={a['donor'].id}").text
        assert "Bravo Officer" not in html and "Test Medical Officer" in html

    def test_request_page_counts_only_own_stock(self, client, a, b):
        login(client, "staff")
        html = client.get(f"/requests/{a['req'].id}").text
        assert "Total available compatible bags: <strong>0</strong>" in html

    def test_service_level_cross_facility_calls_refused(self, db, users, a, b):
        with pytest.raises(RecordNotFound):
            req_svc.cancel_request(db, b["req"], users["staff"])
        with pytest.raises(RecordNotFound):
            donation_svc.screen_donation(db, b["pending"], screening("accept"), users["medical_officer"])
        with pytest.raises(RecordNotFound):
            donor_svc.update_donor(db, b["donor"], DonorIn(full_name="X Y", gender="male", date_of_birth="1990-01-01",
                                                           blood_group="AB-", phone="9700000001", weight_kg=70),
                                   users["staff"])

    @pytest.mark.parametrize("sql,match", [
        ("INSERT INTO donations (facility_id, donor_id, donation_date, blood_group, product, volume_ml, units_collected, "
         "status, weight_kg, hemoglobin_g_dl, bp_systolic, bp_diastolic, pulse_bpm, temperature_c, questionnaire_ok, "
         "cleared_by_id, recorded_by_id, created_at) VALUES (:fa, :bdonor, '2026-01-01', 'AB-', 'whole_blood', 350, 1, "
         "'quarantine', 70, 14, 120, 80, 70, 36.8, 1, :amo, :astaff, '2026-01-01')", "donation and donor"),
        ("UPDATE blood_units SET reserved_for_request_id = :areq WHERE id = :bunit", "another facility"),
        ("INSERT INTO issues (facility_id, request_id, blood_unit_id, issued_at, issued_by_id, received_by, "
         "crossmatch_confirmed) VALUES (:fa, :areq, :bunit, '2026-01-01', :astaff, 'X', 1)", "different facilities"),
    ])
    def test_database_rejects_cross_facility_links(self, db, users, a, b, sql, match):
        params = {"fa": users["staff"].facility_id, "bdonor": b["donor"].id, "amo": users["medical_officer"].id,
                  "astaff": users["staff"].id, "areq": a["req"].id, "bunit": b["unit"].id}
        with pytest.raises(IntegrityError, match=match):
            db.execute(text(sql), {k: v for k, v in params.items() if f":{k}" in sql})
        db.rollback()

    @pytest.mark.parametrize("role,fid,ok", [("staff", None, False), ("platform_admin", 1, False),
                                             ("platform_admin", None, True)])
    def test_database_enforces_account_facility_rule(self, db, users, role, fid, ok):
        stmt = text("INSERT INTO users (username, full_name, password_hash, role, facility_id, is_active, created_at) "
                    "VALUES (:u, 'X', 'x', :r, :f, 1, '2026-01-01')")
        if ok:
            db.execute(stmt, {"u": f"ok.{role}", "r": role, "f": fid})
            db.rollback()
        else:
            with pytest.raises(IntegrityError, match="platform administrators have no facility"):
                db.execute(stmt, {"u": f"bad.{role}", "r": role, "f": fid})
            db.rollback()


# ====================================================== users & 17 role permissions
class TestUserManagementIsolation:
    def test_admin_lists_only_own_accounts(self, client, b):
        login(client, "admin")
        html = client.get("/users").text
        assert "b.staff" not in html and "b.admin" not in html and "staff" in html
        client.cookies.clear()
        login(client, "b.admin")
        html = client.get("/users").text
        assert "b.staff" in html and "medicalofficer" not in html

    @pytest.mark.parametrize("action,data", [("toggle", {}), ("password", {"new_password": "Takeover@123"})])
    def test_admin_cannot_manage_other_facility_accounts(self, client, db, b, action, data):
        login(client, "admin")
        before = (b["staff"].is_active, b["staff"].password_hash)
        assert _post(client, f"/users/{b['staff'].id}/{action}", data).status_code == 404
        db.expire_all()
        s = db.get(User, b["staff"].id)
        assert (s.is_active, s.password_hash) == before

    def test_17_role_permissions_unchanged_inside_a_facility(self, client, db, b):
        login(client, "b.staff")
        assert _post(client, f"/requests/{b['req'].id}/approve", {}).status_code == 403   # staff cannot approve
        assert client.get("/users").status_code == 403                                   # staff cannot manage users
        client.cookies.clear()
        login(client, "b.mo")
        assert client.get(f"/requests/{b['req'].id}").status_code == 200


# =========================================================== 16: single facility
def test_16_single_facility_workflow_inside_facility_b(client, db, b):
    """Full register → donate → screen → request → approve → issue loop entirely within facility B over HTTP."""
    login(client, "b.staff")
    _post(client, "/donors/new", {"full_name": "Bravo Flow Donor", "gender": "male", "date_of_birth": "1985-05-05",
                                  "blood_group": "B+", "phone": "9700000050", "weight_kg": "72"})
    donor = db.scalar(select(Donor).where(Donor.full_name == "Bravo Flow Donor"))
    assert donor.facility_id == b["facility"].id
    r = _post(client, "/donations/new", {"donor_id": donor.id, "donation_date": TODAY.isoformat(),
                                         "cleared_by_id": b["mo"].id, **{k: str(v) for k, v in vitals().items()}})
    assert r.status_code == 303, r.text[:300]
    donation = db.scalar(select(Donation).where(Donation.donor_id == donor.id))
    client.cookies.clear()
    login(client, "b.mo")
    assert _post(client, f"/donations/{donation.id}/screening", {
        "hiv_result": "non_reactive", "hbv_result": "non_reactive", "hcv_result": "non_reactive",
        "syphilis_result": "non_reactive", "malaria_result": "non_reactive", "abo_rh_confirmed": "true",
        "decision": "accept"}).status_code == 303
    _post(client, "/requests/new", {"patient_name": "Bravo Flow Patient", "patient_age": "30", "patient_gender": "male",
                                    "blood_group": "B+", "units_required": "1", "priority": "urgent",
                                    "hospital_name": "Bravo Flow Hospital", "requester_name": "Dr Flow",
                                    "requester_phone": "9700000051"})
    req = db.scalar(select(BloodRequest).where(BloodRequest.patient_name == "Bravo Flow Patient"))
    assert _post(client, f"/requests/{req.id}/approve", {}).status_code == 303
    db.expire_all()
    unit = db.scalar(select(BloodUnit).where(BloodUnit.donation_id == donation.id))
    assert unit.status == UnitStatus.RESERVED and unit.reserved_for_request_id == req.id
    assert _post(client, f"/requests/{req.id}/issue", {"unit_ids[]": str(unit.id), "received_by": "Bravo Ward",
                                                       "crossmatch_confirmed": "true"}).status_code == 303
    db.expire_all()
    assert db.get(BloodRequest, req.id).status == RequestStatus.FULFILLED
    assert "Bravo Flow Donor" in client.get("/donors").text


# ============================================================ migration v3
class TestMigrationV3:
    def test_v1_database_upgrades_with_data_assigned_to_demo(self, tmp_path):
        from app.migrations import SCHEMA_VERSION, upgrade
        from tests.test_facilities import _v1_database

        eng = _v1_database(tmp_path)
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (id, username, full_name, password_hash, role, is_active, created_at) "
                           "VALUES (2, 'plat', 'P', 'x', 'platform_admin', 1, '2026-01-01')"))
        assert upgrade(eng) == SCHEMA_VERSION
        with eng.connect() as c:
            demo = c.execute(text("SELECT id FROM facilities WHERE is_demo = 1")).scalar()
            assert c.execute(text("SELECT facility_id FROM users WHERE id = 1")).scalar() == demo
            assert c.execute(text("SELECT facility_id FROM users WHERE id = 2")).scalar() is None
            assert c.execute(text("SELECT facility_id FROM donors WHERE id = 1")).scalar() == demo
            idx = {r[1]: r[2] for r in c.execute(text("PRAGMA index_list(donors)"))}
            assert idx["ux_donors_facility_phone"] == 1 and idx["ix_donors_phone"] == 0
            assert c.execute(text("SELECT is_participating FROM facilities WHERE id = :d"), {"d": demo}).scalar() == 1
        assert upgrade(eng) == SCHEMA_VERSION                             # idempotent
        eng.dispose()

    def test_migrated_schema_matches_fresh_schema_for_all_tables(self, tmp_path):
        from app.database import init_db, make_engine
        from app.migrations import upgrade
        from tests.test_facilities import _v1_database

        migrated = _v1_database(tmp_path)
        upgrade(migrated)
        fresh = make_engine(f"sqlite:///{(tmp_path / 'fresh3.db').as_posix()}")
        init_db(fresh)

        def describe(eng):
            with eng.connect() as c:
                tables = [r[0] for r in c.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))]
                out = {}
                for t in tables:
                    cols = sorted((r[1], r[2], r[3]) for r in c.execute(text(f"PRAGMA table_info({t})")))
                    idx = sorted((r[1], r[2]) for r in c.execute(text(f"PRAGMA index_list({t})"))
                                 if not r[1].startswith("sqlite_"))
                    fks = sorted(tuple(r[2:5]) for r in c.execute(text(f"PRAGMA foreign_key_list({t})")))
                    out[t] = (cols, idx, fks)
                trig = sorted(r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
                return out, trig

        assert describe(migrated) == describe(fresh)
        migrated.dispose(), fresh.dispose()
