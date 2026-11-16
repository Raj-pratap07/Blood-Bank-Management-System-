"""Phase 4: facility-published availability + Phase 3 hardening (setup links, forced change, closure).

Fictional facility "Lima (fictional test)" is listed and participating. Its A+ inventory deliberately
contains one bag in every state, so exactly ONE A+ bag is eligible:
    available (eligible) · reserved · issued · expired-by-date · discarded · quarantined
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import (
    AvailabilityPublication,
    AvailabilityStatus,
    BloodGroup,
    BloodUnit,
    Donation,
    Facility,
    Product,
    StockVisibility,
    UnitStatus,
    User,
)
from app.schemas.facilities import ContactIn, FacilityIn, SourceIn, VerificationIn
from app.schemas.forms import BloodRequestIn, DonationIn, DonorIn, IssueIn, OnboardIn, UserIn
from app.services import availability as avail
from app.services import donations as donation_svc
from app.services import donors as donor_svc
from app.services import facilities as fsvc
from app.services import requests as req_svc
from app.services import users as user_svc
from app.services.errors import PermissionDenied, ServiceError
from app.utils import utcnow
from tests.conftest import PASSWORD, csrf_from, login, screening, vitals

TODAY = date.today()
_phone = iter(range(9300000000, 9300009999))


def _bag(db, group, mo, staff, days_ago=2, outcome="accept"):
    donor = donor_svc.create_donor(db, DonorIn(full_name=f"Lima Secret Donor {next(_phone)}", gender="male",
                                               date_of_birth="1990-01-01", blood_group=group,
                                               phone=str(next(_phone)), weight_kg=70), staff)
    d = donation_svc.record_donation(db, donor, DonationIn(
        donor_id=donor.id, donation_date=TODAY - timedelta(days=days_ago), cleared_by_id=mo.id, **vitals()), staff)
    if outcome == "accept":
        donation_svc.screen_donation(db, d, screening("accept"), mo, on=d.donation_date)
    elif outcome == "reject":
        donation_svc.screen_donation(db, d, screening("reject", hbv_result="reactive"), mo)
    return d.units[0]


def _request(db, staff, group, units=1, patient="Lima Secret Patient"):
    return req_svc.create_request(db, BloodRequestIn(
        patient_name=patient, patient_age=40, patient_gender="female", blood_group=group, units_required=units,
        priority="urgent", hospital_name="Lima Test Hospital", requester_name="Dr Lima",
        requester_phone="9300000999"), staff)


@pytest.fixture()
def lima(db, users):
    p = users["platform_admin"]
    fac = fsvc.create_facility(db, FacilityIn(display_name="Lima (fictional test)", address_line="1 Test Road",
                                              city="Testville", operational_status="active"), p)
    gov = fsvc.add_source(db, fac, SourceIn(source_type="government_document", title="Test official list",
                                            reference="Test ref", retrieved_on=TODAY), p)
    fsvc.record_verification(db, fac, VerificationIn(scope="identity", status="source_confirmed",
                                                      method="official_source_check", source_id=gov.id,
                                                      verified_on=TODAY, evidence_note="Matches the test list"), p)
    fsvc.add_contact(db, fac, ContactIn(kind="phone", value="9300000001", source_id=gov.id, is_public=True), p)
    fsvc.set_listed(db, fac, True, p)
    admin, token = user_svc.onboard_facility_admin(db, fac, OnboardIn(username="l.admin", full_name="Lima Admin"), p)
    user_svc.complete_setup(db, token, PASSWORD)
    mo = user_svc.create_user(db, UserIn(username="l.mo", full_name="Lima Officer", role="medical_officer",
                                         password=PASSWORD), admin)
    staff = user_svc.create_user(db, UserIn(username="l.staff", full_name="Lima Staff", role="staff",
                                            password=PASSWORD), admin)
    for u in (mo, staff):
        u.must_change_password = False
    db.commit()
    eligible = _bag(db, "A+", mo, staff)
    reserved, issued = _bag(db, "A+", mo, staff, days_ago=5), _bag(db, "A+", mo, staff, days_ago=6)
    expired = _bag(db, "A+", mo, staff, days_ago=40)          # accepted when fresh; past expiry now, not yet swept
    discarded = _bag(db, "A+", mo, staff, outcome="reject")
    quarantined = _bag(db, "A+", mo, staff, outcome="none")
    ab = [_bag(db, "AB+", mo, staff) for _ in range(3)]
    req = _request(db, staff, "A+", units=2)
    req_svc.approve_and_reserve(db, req, mo)     # FEFO reserves the two earliest-expiring eligible A+ bags
    db.expire_all()                              # (collected 6 and 5 days ago); issue the first, keep one reserved
    reserved_now = [u for u in db.get(type(req), req.id).reserved_units]
    req_svc.issue_units(db, db.get(type(req), req.id),
                        IssueIn(unit_ids=[reserved_now[0].id], received_by="Lima Nurse", crossmatch_confirmed=True),
                        staff)
    avail.update_settings(db, admin, StockVisibility.BANDED, 12)
    db.expire_all()
    return dict(facility=db.get(Facility, fac.id), admin=db.get(User, admin.id), mo=db.get(User, mo.id),
                staff=db.get(User, staff.id), req=req, ab=ab, quarantined=quarantined, discarded=discarded,
                expired=expired, eligible=eligible, reserved=reserved, issued=issued)


def _post(client, url, data=None, page="/availability"):
    return client.post(url, data={"csrf_token": csrf_from(client.get(page).text), **(data or {})},
                       follow_redirects=False)


# ========================================================= source of truth (4-8)
class TestEligibility:
    def test_only_available_unexpired_bags_count(self, db, lima):
        statuses = {db.get(BloodUnit, u.id).status for u in
                    (lima["quarantined"], lima["discarded"], lima["expired"])}
        assert statuses == {UnitStatus.QUARANTINE, UnitStatus.DISCARDED, UnitStatus.EXPIRED}   # swept during approval
        a_pos = db.scalars(select(BloodUnit).where(BloodUnit.facility_id == lima["facility"].id,
                                                   BloodUnit.blood_group == "A+")).all()
        by_status = sorted(u.status.value for u in a_pos)
        assert by_status.count("reserved") == 1 and by_status.count("issued") == 1
        counts = avail.eligible_counts(db, lima["facility"].id)
        # 6 A+ bags exist: available(eligible), available-but-expired, reserved, issued, discarded, quarantined
        assert counts[(BloodGroup.A_POS, Product.WHOLE_BLOOD)] == 1
        assert counts[(BloodGroup.AB_POS, Product.WHOLE_BLOOD)] == 3
        assert counts[(BloodGroup.O_NEG, Product.WHOLE_BLOOD)] == 0

    def test_unswept_expired_bag_is_excluded_by_date(self, db, lima):
        # A bag past expiry that the sweep has not reached yet still says AVAILABLE: it must not count.
        late = _bag(db, "A+", lima["mo"], lima["staff"], days_ago=36)
        assert db.get(BloodUnit, late.id).status == UnitStatus.AVAILABLE and late.expires_on < TODAY
        assert avail.eligible_counts(db, lima["facility"].id)[(BloodGroup.A_POS, Product.WHOLE_BLOOD)] == 1

    def test_publication_snapshot_uses_eligible_counts(self, db, lima):
        pub = avail.publish(db, lima["mo"])
        units = {(e.blood_group.value, e.product.value): e.units for e in pub.entries}
        assert units[("A+", "whole_blood")] == 1 and units[("AB+", "whole_blood")] == 3
        assert len(pub.entries) == 8                                          # 8 groups x whole blood only
        assert pub.published_by_id == lima["mo"].id and pub.facility_id == lima["facility"].id


# =========================================================== authorisation (1-3)
class TestAuthorisation:
    def test_1_publish_always_targets_own_facility(self, db, users, lima):
        # The demo facility's own officer publishes: the snapshot belongs to the demo facility, never to Lima.
        avail.update_settings(db, users["admin"], StockVisibility.BANDED, 12)
        pub = avail.publish(db, users["medical_officer"])
        assert pub.facility_id == users["medical_officer"].facility_id != lima["facility"].id
        assert db.scalar(select(func.count()).select_from(AvailabilityPublication).where(
            AvailabilityPublication.facility_id == lima["facility"].id)) == 0

    def test_1b_database_refuses_publication_for_other_facility(self, db, users, lima):
        with pytest.raises(IntegrityError, match="publisher belongs to another facility"):
            db.execute(text("INSERT INTO availability_publications (facility_id, published_by_id, published_at, "
                            "valid_until) VALUES (:f, :u, '2026-01-01 00:00:00', '2026-01-02 00:00:00')"),
                       {"f": lima["facility"].id, "u": users["medical_officer"].id})
        db.rollback()

    def test_2_other_facility_cannot_withdraw_or_read(self, client, db, users, lima):
        avail.publish(db, lima["mo"])
        with pytest.raises(ServiceError, match="no published availability"):
            avail.withdraw(db, users["medical_officer"])       # acts on its own (demo) facility only
        assert avail.publication_state(avail.latest_publication(db, lima["facility"].id)) == "current"
        login(client, "medicalofficer")
        html = client.get("/availability").text
        assert "Lima" not in html and "Demo Blood Centre (fictional)" in html

    def test_2b_publications_are_append_only(self, db, lima):
        pub = avail.publish(db, lima["mo"])
        for sql in ("UPDATE availability_entries SET units = 99 WHERE publication_id = :p",
                    "DELETE FROM availability_entries WHERE publication_id = :p",
                    "UPDATE availability_publications SET valid_until = '2099-01-01 00:00:00' WHERE id = :p",
                    "DELETE FROM availability_publications WHERE id = :p"):
            with pytest.raises(IntegrityError, match="append-only"):
                db.execute(text(sql), {"p": pub.id})
            db.rollback()

    def test_3_platform_admin_cannot_publish_or_view(self, client, db, users, lima):
        with pytest.raises(PermissionDenied):
            avail.publish(db, users["platform_admin"])
        login(client, "platformadmin")
        assert client.get("/availability", follow_redirects=False).status_code == 403
        tok = csrf_from(client.get("/platform/facilities").text)
        for path in ("/availability/publish", "/availability/withdraw", "/availability/settings"):
            assert client.post(path, data={"csrf_token": tok}, follow_redirects=False).status_code == 403

    def test_staff_cannot_publish_and_only_admin_changes_settings(self, client, db, lima):
        with pytest.raises(PermissionDenied):
            avail.publish(db, lima["staff"])
        with pytest.raises(PermissionDenied):
            avail.update_settings(db, lima["mo"], StockVisibility.EXACT, 6)
        login(client, "l.staff")
        assert client.get("/availability").status_code == 200        # staff may view internal stock
        assert _post(client, "/availability/publish").status_code == 403
        client.cookies.clear()
        login(client, "l.mo")
        assert _post(client, "/availability/settings", {"stock_visibility": "exact",
                                                        "stock_max_age_hours": "6"}).status_code == 403

    def test_settings_bounds(self, db, lima):
        with pytest.raises(ServiceError, match="between 1 and"):
            avail.update_settings(db, lima["admin"], StockVisibility.EXACT, settings.availability_max_hours + 1)


# ============================================================ freshness (9-10)
class TestFreshness:
    def test_9_transition_current_to_stale(self, db, lima):
        pub = avail.publish(db, lima["mo"])
        assert pub.valid_until - pub.published_at == timedelta(hours=12)        # facility chose 12 h
        f = lima["facility"]
        assert avail.public_view(db, f, now=pub.valid_until - timedelta(minutes=1)).state == "current"
        stale = avail.public_view(db, f, now=pub.valid_until + timedelta(seconds=1))
        assert stale.state == "stale" and stale.cells == []                     # no figures once stale

    def test_9b_stale_hidden_from_public_pages_and_api(self, client, db, lima):
        old = utcnow() - timedelta(hours=13)
        avail.publish(db, lima["mo"], now=old)
        page = client.get(f"/find/{lima['facility'].slug}").text
        assert "Not recently updated" in page and "Available" not in page.split("Blood availability")[1][:2000]
        assert client.get("/api/public/availability").json()["results"] == []

    def test_validity_capped_by_platform_ceiling(self, db, lima, monkeypatch):
        monkeypatch.setattr(settings, "availability_max_hours", 6)
        pub = avail.publish(db, lima["mo"])
        assert pub.valid_until - pub.published_at == timedelta(hours=6)

    def test_10_unpublished_never_exposed(self, client, db, lima):
        page = client.get(f"/find/{lima['facility'].slug}").text
        assert "Not published" in page
        assert client.get("/api/public/availability").json()["results"] == []
        assert "No blood bank currently reports this as available" in client.get("/find?group=A%2B").text

    def test_withdrawal_and_hidden_setting_remove_public_figures(self, client, db, lima):
        avail.publish(db, lima["mo"])
        assert client.get("/api/public/availability").json()["results"]
        avail.withdraw(db, lima["mo"])
        assert client.get("/api/public/availability").json()["results"] == []
        avail.publish(db, lima["mo"])
        avail.update_settings(db, lima["admin"], StockVisibility.HIDDEN, 12)
        assert client.get("/api/public/availability").json()["results"] == []
        with pytest.raises(ServiceError, match="switched off"):
            avail.publish(db, lima["mo"])


# ====================================================== public display (11-12)
class TestPublicDisplay:
    def test_banded_display_and_never_overstate(self, client, db, lima):
        avail.publish(db, lima["mo"])
        f = lima["facility"]
        v = avail.public_view(db, f)
        assert v.cell("A+").status == AvailabilityStatus.LOW and v.cell("A+").units is None       # banded hides counts
        assert settings.availability_low_max == 2
        assert v.cell("AB+").status == AvailabilityStatus.AVAILABLE    # 3 bags > low_max (2)
        # Issue the single eligible A+ bag after publishing: the public figure must drop, never stay high.
        req = _request(db, lima["staff"], "A+", units=1, patient="Lima Secret Patient Two")
        req_svc.approve_and_reserve(db, req, lima["mo"])
        assert avail.public_view(db, f).cell("A+").status == AvailabilityStatus.NOT_AVAILABLE

    def test_exact_display(self, db, lima):
        avail.update_settings(db, lima["admin"], StockVisibility.EXACT, 12)
        avail.publish(db, lima["mo"])
        v = avail.public_view(db, lima["facility"])
        assert (v.cell("AB+").units, v.cell("A+").units, v.cell("O-").units) == (3, 1, 0)
        assert v.cell("A+", Product.PLATELETS).status is None                   # untracked component

    def test_11_public_responses_contain_no_operational_data(self, client, db, lima):
        avail.update_settings(db, lima["admin"], StockVisibility.EXACT, 12)
        avail.publish(db, lima["mo"])
        api = client.get("/api/public/availability")
        assert api.status_code == 200 and "set-cookie" not in api.headers
        for item in api.json()["results"]:
            assert set(item) <= {"facility", "facility_name", "blood_group", "component", "status", "units",
                                 "published_at", "valid_until"}
        secrets_ = [u.unit_code for u in db.scalars(select(BloodUnit))] + \
            ["Lima Secret", "Lima Test Hospital", "Lima Nurse", "Lima Officer", "Lima Admin", "l.mo", "RQ-0"]
        for path in ("/api/public/availability", "/api/public/facilities", "/find", "/find?group=A%2B",
                     f"/find/{lima['facility'].slug}", "/"):
            body = client.get(path).text
            for s in secrets_:
                assert s not in body, (path, s)

    def test_12_filters(self, client, db, users, lima):
        avail.publish(db, lima["mo"])
        r = client.get("/find?group=AB%2B&component=whole_blood").text
        assert "Lima (fictional test)" in r and "Available" in r and "updated" in r
        assert "No blood bank currently reports this as available" in client.get("/find?group=O-").text
        assert "isn't tracked here" in client.get("/find?group=A%2B&component=platelets").text
        api = client.get("/api/public/availability?group=AB%2B&component=whole_blood&facility="
                         + lima["facility"].slug).json()["results"]
        assert [(i["blood_group"], i["status"]) for i in api] == [("AB+", "available")]
        assert client.get("/api/public/availability?group=Z%2B").status_code == 422
        assert client.get("/api/public/availability?facility=demo-blood-centre").status_code == 422   # not public
        assert client.get("/api/public/availability?component=platelets").json()["results"] == []

    def test_demo_and_unlisted_facilities_never_publish_publicly(self, client, db, users, lima):
        assert avail.public_view(db, db.scalar(select(Facility).where(Facility.is_demo.is_(True)))).state == "off"
        fsvc.set_listed(db, lima["facility"], False, users["platform_admin"])
        avail.publish(db, lima["mo"])                          # allowed internally, but nothing public
        assert client.get("/api/public/availability").json()["results"] == []
        assert client.get(f"/find/{lima['facility'].slug}").status_code == 404


# ============================================================ 13: closure
class TestClosure:
    def test_13_closure_hides_availability_and_deactivates_accounts(self, client, db, users, lima):
        avail.publish(db, lima["mo"])
        f = lima["facility"]
        before = {t: db.scalar(text(f"SELECT count(*) FROM {t} WHERE facility_id = :f"), {"f": f.id})
                  for t in ("donors", "donations", "blood_units", "blood_requests", "issues", "availability_publications")}
        n = fsvc.update_facility(db, f, FacilityIn(display_name=f.display_name, address_line="1 Test Road",
                                                   city="Testville", operational_status="closed"), users["platform_admin"])
        assert n == 3                                                   # admin, MO, staff
        db.expire_all()
        assert all(not u.is_active for u in db.scalars(select(User).where(User.facility_id == f.id)))
        after = {t: db.scalar(text(f"SELECT count(*) FROM {t} WHERE facility_id = :f"), {"f": f.id}) for t in before}
        assert before == after                                          # history preserved
        assert client.get("/api/public/availability").json()["results"] == []
        assert client.get(f"/find/{f.slug}").status_code == 404
        assert login(client, "l.mo").status_code == 401                 # deactivated
        assert avail.public_view(db, db.get(Facility, f.id)).state == "off"

    def test_reopening_does_not_silently_reactivate(self, db, users, lima):
        p, f = users["platform_admin"], lima["facility"]
        base = dict(display_name=f.display_name, address_line="1 Test Road", city="Testville")
        fsvc.update_facility(db, f, FacilityIn(**base, operational_status="closed"), p)
        fsvc.update_facility(db, f, FacilityIn(**base, operational_status="active"), p)
        db.expire_all()
        assert not db.get(User, lima["admin"].id).is_active


# ========================================================= 14: workflows unchanged
def test_14_reservation_and_issue_unchanged_by_publishing(db, lima):
    avail.publish(db, lima["mo"])
    req = _request(db, lima["staff"], "AB+", units=2, patient="Lima Flow Patient")
    assert req_svc.approve_and_reserve(db, req, lima["mo"]) == 2
    req_svc.issue_units(db, req, IssueIn(unit_ids=[u.id for u in req.reserved_units], received_by="Lima Ward",
                                         crossmatch_confirmed=True), lima["staff"])
    assert req.status.value == "fulfilled"
    assert avail.public_view(db, lima["facility"]).cell("AB+").status == AvailabilityStatus.LOW   # 3 -> 1 left


# ================================================= hardening: setup links & forced change
class TestFirstLoginCredentials:
    def test_onboarded_admin_cannot_sign_in_before_setup(self, client, db, users):
        f = fsvc.create_facility(db, FacilityIn(display_name="Mike (fictional test)"), users["platform_admin"])
        admin, token = user_svc.onboard_facility_admin(db, f, OnboardIn(username="m.admin", full_name="Mike Admin"),
                                                       users["platform_admin"])
        assert admin.setup_token_hash and admin.setup_token_hash != token        # only the hash is stored
        assert login(client, "m.admin", "anything-at-all").status_code == 401
        assert client.get(f"/account/setup/{token}").status_code == 200
        r = client.post(f"/account/setup/{token}", data={"new_password": "Mike@12345", "confirm_password": "Mike@12345",
                                                          "csrf_token": csrf_from(client.get(f"/account/setup/{token}").text)},
                        follow_redirects=False)
        assert r.status_code == 303
        assert login(client, "m.admin", "Mike@12345").status_code == 303
        client.cookies.clear()
        assert client.get(f"/account/setup/{token}").status_code == 404           # single use

    def test_expired_and_reissued_links(self, db, users):
        p = users["platform_admin"]
        f = fsvc.create_facility(db, FacilityIn(display_name="November (fictional test)"), p)
        admin, old = user_svc.onboard_facility_admin(db, f, OnboardIn(username="n.admin", full_name="Nov Admin"), p)
        _, new = user_svc.reissue_setup_link(db, f, admin.id, p)
        assert user_svc.user_for_setup_token(db, old) is None                      # old link invalidated
        admin.setup_token_expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
        with pytest.raises(ServiceError, match="invalid, expired"):
            user_svc.complete_setup(db, new, "Nov@123456")
        _, fresh = user_svc.reissue_setup_link(db, f, admin.id, p)
        user_svc.complete_setup(db, fresh, "Nov@123456")
        with pytest.raises(ServiceError, match="already set up"):
            user_svc.reissue_setup_link(db, f, admin.id, p)

    def test_platform_onboarding_page_shows_link_not_password(self, client, db, users):
        f = fsvc.create_facility(db, FacilityIn(display_name="Oscar (fictional test)"), users["platform_admin"])
        login(client, "platformadmin")
        page = f"/platform/facilities/{f.id}"
        assert 'name="password"' not in client.get(page).text
        r = client.post(f"{page}/onboard", data={"username": "o.admin", "full_name": "Oscar Admin",
                                                 "csrf_token": csrf_from(client.get(page).text)})
        assert r.status_code == 200 and "/account/setup/" in r.text and "set-cookie" not in r.headers

    def test_admin_created_account_must_change_password(self, client, db, users):
        login(client, "admin")
        client.post("/users/new", data={"username": "new.staff", "full_name": "New Staff", "role": "staff",
                                        "password": "Initial@123", "csrf_token": csrf_from(client.get("/users/new").text)})
        client.cookies.clear()
        r = login(client, "new.staff", "Initial@123")
        assert r.headers["location"] == "/account/password"
        assert client.get("/donors", follow_redirects=False).headers["location"] == "/account/password"
        assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"
        assert client.get("/api/stats/inventory").status_code == 403
        tok = csrf_from(client.get("/account/password").text)
        same = client.post("/account/password", data={"current_password": "Initial@123", "new_password": "Initial@123",
                                                      "confirm_password": "Initial@123", "csrf_token": tok})
        assert same.status_code == 400 and "different" in same.text
        client.post("/account/password", data={"current_password": "Initial@123", "new_password": "Mine@12345",
                                               "confirm_password": "Mine@12345", "csrf_token": tok})
        assert client.get("/donors").status_code == 200

    def test_admin_reset_forces_change(self, db, users):
        target = user_svc.create_user(db, UserIn(username="reset.me", full_name="Reset Me", role="staff",
                                                 password="Initial@123"), users["admin"])
        user_svc.change_own_password(db, target, "Initial@123", "Mine@12345")
        assert not target.must_change_password
        user_svc.reset_password(db, target, "Admin@Given1", users["admin"])
        assert target.must_change_password


# ===================================================== regression: no stale browser cache
@pytest.mark.parametrize("path", ["/", "/find", "/find?group=A%2B", "/api/public/availability", "/api/public/facilities",
                                  "/find/{slug}"])
def test_availability_pages_are_never_served_from_browser_cache(client, lima, path):
    """Found in browser testing: a 60 s public cache kept showing a facility after it was closed and hid newly
    published availability. Every page carrying listing/availability data must be revalidated."""
    r = client.get(path.format(slug=lima["facility"].slug))
    assert r.status_code == 200
    assert "no-cache" in r.headers["cache-control"] and "public" not in r.headers["cache-control"]
