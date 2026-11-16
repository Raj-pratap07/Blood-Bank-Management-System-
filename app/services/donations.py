"""Donation recording (collection + quarantine) and laboratory screening."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import BloodUnit, Donation, DonationStatus, Donor, Product, UnitStatus, User
from app.schemas.forms import DonationIn, ScreeningIn
from app.services.eligibility import Vitals, check_donor
from app.services.errors import EligibilityError, PermissionDenied, ServiceError
from app.services.tx import transaction
from app.utils import utcnow


def unit_code_for(donation: Donation) -> str:
    return f"WB{donation.donation_date:%y%m%d}-{donation.id:05d}"


def record_donation(db: Session, donor: Donor, data: DonationIn, actor: User) -> Donation:
    """Record a collection. The bag goes into QUARANTINE until screening is accepted."""
    cleared_by = db.get(User, data.cleared_by_id)
    if cleared_by is None or not cleared_by.is_active or not cleared_by.can_authorize:
        raise ServiceError("Donor clearance must be given by an active medical officer or administrator.")

    vitals = Vitals(
        weight_kg=data.weight_kg, hemoglobin_g_dl=data.hemoglobin_g_dl, bp_systolic=data.bp_systolic,
        bp_diastolic=data.bp_diastolic, pulse_bpm=data.pulse_bpm, temperature_c=data.temperature_c,
        questionnaire_ok=data.questionnaire_ok, volume_ml=data.volume_ml,
    )
    result = check_donor(donor, on=data.donation_date, vitals=vitals)
    if not result.eligible:
        raise EligibilityError("Donor did not pass the preliminary eligibility check.", result.reasons)

    with transaction(db):
        donation = Donation(
            donor=donor, donation_date=data.donation_date, blood_group=donor.blood_group,
            product=Product.WHOLE_BLOOD, volume_ml=data.volume_ml, units_collected=1,
            status=DonationStatus.QUARANTINE, weight_kg=data.weight_kg,
            hemoglobin_g_dl=data.hemoglobin_g_dl, bp_systolic=data.bp_systolic,
            bp_diastolic=data.bp_diastolic, pulse_bpm=data.pulse_bpm, temperature_c=data.temperature_c,
            questionnaire_ok=data.questionnaire_ok, pre_donation_notes=data.pre_donation_notes,
            cleared_by=cleared_by, recorded_by=actor,
        )
        db.add(donation)
        db.flush()  # assigns donation.id for the bag code
        db.add(BloodUnit(
            unit_code=unit_code_for(donation), donation=donation, product=Product.WHOLE_BLOOD,
            blood_group=donation.blood_group, volume_ml=data.volume_ml,
            collected_on=data.donation_date,
            expires_on=data.donation_date + timedelta(days=settings.shelf_life_days),
            status=UnitStatus.QUARANTINE,
        ))
        donor.weight_kg = data.weight_kg
    return donation


def screen_donation(db: Session, donation: Donation, data: ScreeningIn, actor: User,
                    on: date | None = None) -> Donation:
    """Record lab screening. Only an accepted donation releases its bag to AVAILABLE stock."""
    if not actor.can_authorize:
        raise PermissionDenied("Only a medical officer or administrator can complete screening.")
    if donation.status != DonationStatus.QUARANTINE:
        raise ServiceError("Screening has already been completed for this donation.")
    on = on or date.today()

    with transaction(db):
        for field in ("hiv_result", "hbv_result", "hcv_result", "syphilis_result", "malaria_result",
                      "abo_rh_confirmed", "screening_notes"):
            setattr(donation, field, getattr(data, field))
        donation.screened_by = actor
        donation.screened_at = utcnow()
        if data.decision == "accept":
            donation.status = DonationStatus.ACCEPTED
            for unit in donation.units:
                if unit.status != UnitStatus.QUARANTINE:
                    raise ServiceError(f"Bag {unit.unit_code} is not in quarantine.")
                if unit.is_expired(on):
                    unit.status, unit.status_note = UnitStatus.EXPIRED, "Expired before screening release"
                else:
                    unit.status, unit.status_note = UnitStatus.AVAILABLE, "Released after screening"
        else:
            donation.status = DonationStatus.REJECTED
            for unit in donation.units:
                unit.status, unit.status_note = UnitStatus.DISCARDED, "Discarded: screening rejected"
    return donation
