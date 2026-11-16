"""Regression tests for defects found in the application-wide functional audit."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import BloodGroup as G
from app.models import BloodUnit, DonationStatus, Gender, RequestPriority, RequestStatus, UnitStatus
from app.schemas.forms import BloodRequestIn, DonationIn, IssueIn
from app.services import donations as donation_svc
from app.services import requests as req_svc
from app.services.eligibility import check_donor
from app.services.errors import EligibilityError, InventoryError, ServiceError
from app.services.inventory import discard_unit
from app.services.reports import dashboard_counts
from tests.conftest import csrf_from, login, screening, vitals


def _request(db, users, group=G.A_POS, units=2):
    return req_svc.create_request(db, BloodRequestIn(
        patient_name="Audit Patient", patient_age=40, patient_gender=Gender.FEMALE, blood_group=group,
        units_required=units, priority=RequestPriority.URGENT, hospital_name="Audit Hospital",
        requester_name="Dr Audit", requester_phone="9876500002"), users["staff"])


def _post(client, url, data, page="/"):
    return client.post(url, data={**data, "csrf_token": csrf_from(client.get(page).text)}, follow_redirects=False)


# Defect 1: back-dated donation bypassed the interval rule against a LATER donation.
class TestBackdatedDonationInterval:
    def test_backdated_donation_within_interval_of_later_one_is_blocked(self, db, users, make_donor):
        donor = make_donor(gender=Gender.MALE)
        recent = date.today() - timedelta(days=5)
        donation_svc.record_donation(db, donor, DonationIn(
            donor_id=donor.id, donation_date=recent, cleared_by_id=users["medical_officer"].id, **vitals()),
            users["staff"])
        backdated = recent - timedelta(days=10)
        with pytest.raises(EligibilityError) as exc:
            donation_svc.record_donation(db, donor, DonationIn(
                donor_id=donor.id, donation_date=backdated, cleared_by_id=users["medical_officer"].id, **vitals()),
                users["staff"])
        assert any("later donation" in r for r in exc.value.details)

    def test_backdated_donation_outside_interval_still_allowed(self, db, users, make_donor):
        donor = make_donor(gender=Gender.MALE)
        recent = date.today() - timedelta(days=5)
        donation_svc.record_donation(db, donor, DonationIn(
            donor_id=donor.id, donation_date=recent, cleared_by_id=users["medical_officer"].id, **vitals()),
            users["staff"])
        assert check_donor(donor, on=recent - timedelta(days=100)).eligible


# Defect 2: discarding a quarantined bag left its donation stuck "awaiting screening".
class TestQuarantineDiscard:
    def test_quarantine_bag_cannot_be_discarded_directly(self, db, users, make_unit):
        unit = make_unit(accept=False)
        with pytest.raises(InventoryError, match="Reject it through donation screening"):
            discard_unit(db, unit, "bag leak", users["medical_officer"])
        assert unit.status == UnitStatus.QUARANTINE

    def test_screening_rejection_still_discards_and_closes_record(self, db, users, make_unit):
        unit = make_unit(accept=False)
        donation_svc.screen_donation(db, unit.donation, screening("reject", screening_notes="Bag leak"),
                                     users["medical_officer"])
        assert unit.status == UnitStatus.DISCARDED and unit.donation.status == DonationStatus.REJECTED

    def test_available_bag_can_still_be_discarded(self, db, users, make_unit):
        unit = make_unit()
        discard_unit(db, unit, "haemolysis", users["medical_officer"])
        assert unit.status == UnitStatus.DISCARDED

    def test_unit_page_offers_screening_route_not_discard(self, client, make_unit):
        unit = make_unit(accept=False)
        login(client, "medicalofficer")
        html = client.get(f"/inventory/units/{unit.id}").text
        assert f"/inventory/units/{unit.id}/discard" not in html
        assert "donation screening" in html


# Defect 3: a request with issued bags could be rejected.
class TestRejectAfterIssue:
    def _partially_issued(self, db, users, make_unit):
        u1, _ = make_unit(G.A_POS, days_ago=5), make_unit(G.A_POS, days_ago=3)
        req = _request(db, users, G.A_POS, 2)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        req_svc.issue_units(db, req, IssueIn(unit_ids=[u1.id], received_by="Nurse A", crossmatch_confirmed=True),
                            users["staff"])
        assert req.status == RequestStatus.PARTIALLY_ISSUED
        return req

    def test_reject_blocked_once_bags_issued(self, db, users, make_unit):
        req = self._partially_issued(db, users, make_unit)
        with pytest.raises(ServiceError, match="already been issued"):
            req_svc.reject_request(db, req, users["medical_officer"], "Changed mind")
        assert req.status == RequestStatus.PARTIALLY_ISSUED
        assert req.units_reserved == 1  # reservation untouched

    def test_cancel_still_closes_remaining_units(self, db, users, make_unit):
        req = self._partially_issued(db, users, make_unit)
        req_svc.cancel_request(db, req, users["staff"], "Patient stable")
        assert req.status == RequestStatus.CANCELLED
        assert db.scalar(select(func.count()).select_from(BloodUnit).where(
            BloodUnit.status == UnitStatus.RESERVED)) == 0

    def test_reject_before_issue_still_works(self, db, users, make_unit):
        make_unit(G.A_POS)
        req = _request(db, users, G.A_POS, 1)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        req_svc.reject_request(db, req, users["medical_officer"], "Sample haemolysed")
        assert req.status == RequestStatus.REJECTED

    def test_reject_form_hidden_after_issue(self, client, db, users, make_unit):
        req = self._partially_issued(db, users, make_unit)
        login(client, "medicalofficer")
        html = client.get(f"/requests/{req.id}").text
        assert f'/requests/{req.id}/reject' not in html
        assert "can no longer be rejected" in html


# Defect 4: screening submitted without Accept/Reject returned 400 with no visible message.
class TestScreeningWithoutDecision:
    def test_missing_decision_shows_message(self, client, make_unit):
        unit = make_unit(accept=False)
        login(client, "medicalofficer")
        page = f"/donations/{unit.donation_id}"
        r = _post(client, f"/donations/{unit.donation_id}/screening", {
            "hiv_result": "non_reactive", "hbv_result": "non_reactive", "hcv_result": "non_reactive",
            "syphilis_result": "non_reactive", "malaria_result": "non_reactive", "abo_rh_confirmed": "true"}, page)
        assert r.status_code == 400
        assert "Choose Accept or Reject" in r.text


# Defect 5: dashboard "Registered donors" excluded inactive donors.
class TestDashboardDonorCount:
    def test_counts_all_registered_donors(self, db, fid, make_donor):
        make_donor(); inactive = make_donor()
        inactive.is_active = False
        db.commit()
        assert dashboard_counts(db, fid)["donors"] == 2
