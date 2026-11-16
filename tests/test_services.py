from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import (
    BloodGroup as G,
)
from app.models import (
    BloodUnit,
    DonationStatus,
    Gender,
    Issue,
    RequestPriority,
    RequestStatus,
    UnitStatus,
)
from app.schemas.forms import BloodRequestIn, DonationIn, DonorIn, IssueIn
from app.services import donations as donation_svc
from app.services import donors as donor_svc
from app.services import requests as req_svc
from app.services.compatibility import is_compatible
from app.services.eligibility import Vitals, check_donor
from app.services.errors import EligibilityError, InventoryError, PermissionDenied, ServiceError
from app.services.inventory import expire_overdue_units, fefo_candidates, stock_summary
from tests.conftest import screening, vitals


def make_request(db, users, group=G.A_POS, units=2) -> object:
    data = BloodRequestIn(patient_name="Patient X", patient_age=40, patient_gender=Gender.MALE, blood_group=group,
                          units_required=units, priority=RequestPriority.URGENT, hospital_name="City Hospital",
                          requester_name="Dr Rao", requester_phone="9876543210")
    return req_svc.create_request(db, data, users["staff"])


# ------------------------------------------------------------------ donors
class TestDonorRegistration:
    def test_register_donor(self, db, make_donor):
        donor = make_donor(group=G.B_NEG, name="Asha Verma")
        assert donor.id and donor.blood_group == G.B_NEG and donor.full_name == "Asha Verma"

    def test_duplicate_phone_rejected(self, db, users, make_donor):
        donor = make_donor()
        data = DonorIn(full_name="Other Person", gender="female", date_of_birth=date(1995, 1, 1),
                       blood_group="A+", phone=donor.phone, weight_kg=60)
        with pytest.raises(ServiceError, match="already registered"):
            donor_svc.create_donor(db, data, users['staff'])

    @pytest.mark.parametrize("field,value", [
        ("blood_group", "Z+"), ("phone", "abc"), ("weight_kg", 5), ("email", "not-an-email"),
        ("date_of_birth", (date.today() + timedelta(days=1)).isoformat()),
    ])
    def test_invalid_input_rejected(self, field, value):
        data = dict(full_name="Valid Name", gender="male", date_of_birth="1990-01-01",
                    blood_group="O+", phone="9876500000", weight_kg=60)
        data[field] = value
        with pytest.raises(ValidationError):
            DonorIn(**data)


# ------------------------------------------------------------ eligibility
class TestEligibility:
    def test_first_time_donor_over_60_ineligible(self, make_donor):
        donor = make_donor(age=62)
        r = check_donor(donor)
        assert not r.eligible and r.is_first_time
        assert any("First-time" in x for x in r.reasons)

    def test_repeat_donor_over_60_eligible(self, make_donor):
        donor = make_donor(age=62, last_external_donation_date=date.today() - timedelta(days=200))
        assert check_donor(donor).eligible

    def test_underage_and_underweight(self, make_donor):
        r = check_donor(make_donor(age=17, weight=40))
        assert len(r.reasons) == 2

    def test_interval_by_gender(self, make_donor):
        last = date.today() - timedelta(days=100)
        assert check_donor(make_donor(gender=Gender.MALE, last_external_donation_date=last)).eligible
        r = check_donor(make_donor(gender=Gender.FEMALE, last_external_donation_date=last))
        assert not r.eligible and r.next_eligible_date == last + timedelta(days=120)

    def test_vitals_checked(self, make_donor):
        donor = make_donor(weight=50)
        v = Vitals(**{**vitals(weight_kg=50, hemoglobin_g_dl=11.0, volume_ml=450)})
        r = check_donor(donor, vitals=v)
        assert any("Haemoglobin" in x for x in r.reasons)
        assert any("450 ml" in x for x in r.reasons)

    def test_ineligible_donation_blocked(self, db, users, make_donor):
        donor = make_donor()
        data = DonationIn(donor_id=donor.id, donation_date=date.today(), cleared_by_id=users["medical_officer"].id,
                          **vitals(hemoglobin_g_dl=10))
        with pytest.raises(EligibilityError):
            donation_svc.record_donation(db, donor, data, users["staff"])

    def test_clearance_requires_authorised_user(self, db, users, make_donor):
        donor = make_donor()
        data = DonationIn(donor_id=donor.id, donation_date=date.today(), cleared_by_id=users["staff"].id, **vitals())
        with pytest.raises(ServiceError, match="medical officer"):
            donation_svc.record_donation(db, donor, data, users["staff"])


# --------------------------------------------- donation acceptance -> inventory
class TestDonationScreening:
    def test_donation_enters_quarantine(self, db, make_unit):
        unit = make_unit(accept=False)
        assert unit.status == UnitStatus.QUARANTINE
        assert unit.donation.status == DonationStatus.QUARANTINE
        assert unit.expires_on == unit.collected_on + timedelta(days=35)
        assert unit.unit_code.startswith("WB")

    def test_acceptance_creates_available_inventory(self, db, users, make_unit):
        unit = make_unit(accept=False)
        donation_svc.screen_donation(db, unit.donation, screening("accept"), users["medical_officer"])
        assert unit.status == UnitStatus.AVAILABLE
        assert stock_summary(db, users['staff'].facility_id)[[g.value for g in G].index("O+")]["available"] == 1

    def test_rejection_discards_bag(self, db, users, make_unit):
        unit = make_unit(accept=False)
        donation_svc.screen_donation(db, unit.donation, screening("reject", hbv_result="reactive"),
                                     users["medical_officer"])
        assert unit.status == UnitStatus.DISCARDED and unit.donation.status == DonationStatus.REJECTED

    def test_staff_cannot_accept_screening(self, db, users, make_unit):
        unit = make_unit(accept=False)
        with pytest.raises(PermissionDenied):
            donation_svc.screen_donation(db, unit.donation, screening("accept"), users["staff"])
        assert unit.status == UnitStatus.QUARANTINE

    def test_reactive_cannot_be_accepted(self):
        with pytest.raises(ValidationError, match="reactive"):
            screening("accept", hiv_result="reactive")

    def test_unique_bag_codes(self, db, make_unit):
        codes = {make_unit().unit_code for _ in range(3)}
        assert len(codes) == 3


# ------------------------------------------------------------- expiry
class TestExpiry:
    def test_sweep_marks_expired(self, db, make_unit):
        unit = make_unit(days_ago=40)          # accepted while fresh, now past 35 days
        assert unit.status == UnitStatus.AVAILABLE
        assert expire_overdue_units(db) == 1
        db.refresh(unit)
        assert unit.status == UnitStatus.EXPIRED

    def test_expired_not_offered_or_reserved(self, db, users, make_unit):
        make_unit(G.A_POS, days_ago=36)
        req = make_request(db, users, G.A_POS, 1)
        assert fefo_candidates(db, users['staff'].facility_id, G.A_POS) == []
        with pytest.raises(InventoryError, match="No unexpired"):
            req_svc.approve_and_reserve(db, req, users["medical_officer"])

    def test_expiry_on_last_day_is_still_usable(self, db, users, make_unit):
        unit = make_unit(days_ago=35)
        assert not unit.is_expired()
        assert fefo_candidates(db, users['staff'].facility_id, G.O_POS) == [unit]

    def test_expired_reserved_unit_cannot_be_issued(self, db, users, make_unit):
        unit = make_unit(G.A_POS, days_ago=30)
        req = make_request(db, users, G.A_POS, 1)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        later = date.today() + timedelta(days=10)
        with pytest.raises(InventoryError):
            req_svc.issue_units(db, req, IssueIn(unit_ids=[unit.id], received_by="Ward Nurse",
                                                 crossmatch_confirmed=True), users["staff"], on=later)
        db.refresh(unit)
        assert unit.status == UnitStatus.EXPIRED


# ------------------------------------------------------- compatibility
class TestCompatibility:
    @pytest.mark.parametrize("recipient,donor,ok", [
        ("O-", "O-", True), ("O-", "O+", False), ("A+", "O-", True), ("A+", "B+", False),
        ("AB+", "B-", True), ("AB-", "A+", False), ("B-", "O-", True), ("B+", "AB+", False),
    ])
    def test_matrix(self, recipient, donor, ok):
        assert is_compatible(recipient, donor) is ok

    def test_universal_rules(self):
        assert all(is_compatible(r, "O-") for r in G)
        assert all(is_compatible("AB+", d) for d in G)

    def test_fefo_prefers_identical_then_earliest_expiry(self, db, users, make_unit):
        o_neg = make_unit(G.O_NEG, days_ago=30)
        a_old = make_unit(G.A_POS, days_ago=20)
        a_new = make_unit(G.A_POS, days_ago=2)
        b_pos = make_unit(G.B_POS, days_ago=25)  # incompatible with A+
        assert fefo_candidates(db, users['staff'].facility_id, G.A_POS) == [a_old, a_new, o_neg]
        assert b_pos not in fefo_candidates(db, users['staff'].facility_id, G.A_POS)


# ---------------------------------------------------- reservation + issue
class TestReservationAndIssue:
    def test_full_flow(self, db, users, make_unit):
        u1, u2 = make_unit(G.A_POS, days_ago=10), make_unit(G.A_POS, days_ago=3)
        req = make_request(db, users, G.A_POS, 2)
        assert req_svc.approve_and_reserve(db, req, users["medical_officer"]) == 2
        assert req.status == RequestStatus.APPROVED and req.units_reserved == 2
        db.refresh(u1)
        assert u1.status == UnitStatus.RESERVED

        req_svc.issue_units(db, req, IssueIn(unit_ids=[u1.id], received_by="Nurse A", crossmatch_confirmed=True),
                            users["staff"])
        assert req.status == RequestStatus.PARTIALLY_ISSUED
        req_svc.issue_units(db, req, IssueIn(unit_ids=[u2.id], received_by="Nurse A", crossmatch_confirmed=True),
                            users["staff"])
        assert req.status == RequestStatus.FULFILLED
        assert {u.status for u in (u1, u2)} == {UnitStatus.ISSUED}

    def test_staff_cannot_approve(self, db, users, make_unit):
        make_unit(G.A_POS)
        req = make_request(db, users, G.A_POS, 1)
        with pytest.raises(PermissionDenied):
            req_svc.approve_and_reserve(db, req, users["staff"])

    def test_cannot_issue_unapproved_request(self, db, users, make_unit):
        unit = make_unit(G.A_POS)
        req = make_request(db, users, G.A_POS, 1)
        with pytest.raises(ServiceError, match="approved"):
            req_svc.issue_units(db, req, IssueIn(unit_ids=[unit.id], received_by="X Y", crossmatch_confirmed=True),
                                users["staff"])

    def test_crossmatch_confirmation_required(self):
        with pytest.raises(ValidationError, match="crossmatch"):
            IssueIn(unit_ids=[1], received_by="Nurse A", crossmatch_confirmed=False)

    def test_partial_stock_then_top_up(self, db, users, make_unit):
        make_unit(G.B_POS)
        req = make_request(db, users, G.B_POS, 3)
        assert req_svc.approve_and_reserve(db, req, users["medical_officer"]) == 1
        make_unit(G.B_NEG)
        assert req_svc.approve_and_reserve(db, req, users["medical_officer"]) == 1
        assert req.units_reserved == 2 and req.units_outstanding == 1

    def test_no_negative_inventory(self, db, users, make_unit):
        unit = make_unit(G.AB_NEG)
        r1, r2 = make_request(db, users, G.AB_NEG, 1), make_request(db, users, G.AB_NEG, 1)
        req_svc.approve_and_reserve(db, r1, users["medical_officer"])
        with pytest.raises(InventoryError):
            req_svc.approve_and_reserve(db, r2, users["medical_officer"])
        # A unit reserved for r1 can't be issued against r2
        r2.status = RequestStatus.APPROVED
        db.commit()
        with pytest.raises(InventoryError, match="not reserved"):
            req_svc.issue_units(db, r2, IssueIn(unit_ids=[unit.id], received_by="X Y", crossmatch_confirmed=True),
                                users["staff"])

    def test_cancel_releases_reservation(self, db, users, make_unit):
        unit = make_unit(G.O_NEG)
        req = make_request(db, users, G.O_NEG, 1)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        req_svc.cancel_request(db, req, users["staff"])
        db.refresh(unit)
        assert unit.status == UnitStatus.AVAILABLE and unit.reserved_for_request_id is None
        assert req.status == RequestStatus.CANCELLED

    def test_rollback_when_issue_fails_midway(self, db, users, make_unit):
        """Bag 1 is processed (and updated) before bag 2 fails -> everything must roll back."""
        u1, u2 = make_unit(G.A_POS, days_ago=10), make_unit(G.A_POS, days_ago=2)
        req = make_request(db, users, G.A_POS, 2)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        # Simulate corrupted data on the *later*-expiring bag so it fails second.
        u2.blood_group = G.B_POS
        db.commit()

        with pytest.raises(InventoryError, match="not compatible"):
            req_svc.issue_units(db, req, IssueIn(unit_ids=[u1.id, u2.id], received_by="Nurse",
                                                 crossmatch_confirmed=True), users["staff"])
        db.expire_all()
        assert db.get(BloodUnit, u1.id).status == UnitStatus.RESERVED
        assert db.scalar(select(func.count()).select_from(Issue)) == 0
        assert db.get(type(req), req.id).status == RequestStatus.APPROVED

    def test_cannot_over_issue(self, db, users, make_unit):
        u1 = make_unit(G.A_POS)
        req = make_request(db, users, G.A_POS, 1)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        req_svc.issue_units(db, req, IssueIn(unit_ids=[u1.id], received_by="Nurse", crossmatch_confirmed=True),
                            users["staff"])
        with pytest.raises(ServiceError):
            req_svc.issue_units(db, req, IssueIn(unit_ids=[u1.id], received_by="Nurse", crossmatch_confirmed=True),
                                users["staff"])
