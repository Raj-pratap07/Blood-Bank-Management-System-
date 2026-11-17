"""PRELIMINARY donor eligibility check.

Thresholds come from ``app.config.settings`` and are based on India's donor
selection guidelines (age 18-65, first-time donors up to 60, weight >= 45 kg
for 350 ml and > 55 kg for 450 ml, whole-blood interval 90 days male /
120 days female, Hb >= 12.5 g/dL). They must be verified against the current
official document and local SOP. The result is advisory: final donor
acceptance is always made by an authorised medical officer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.config import settings
from app.models.donor import Donor
from app.models.enums import Gender
from app.utils import business_today

DISCLAIMER = (
    "Preliminary software check only, not a medical clearance. Final donor acceptance "
    "rests with an authorised medical officer."
)


@dataclass
class Vitals:
    weight_kg: float
    hemoglobin_g_dl: float
    bp_systolic: int
    bp_diastolic: int
    pulse_bpm: int
    temperature_c: float
    questionnaire_ok: bool
    volume_ml: int = 350


@dataclass
class EligibilityResult:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    is_first_time: bool = False
    age: int = 0
    interval_days: int = 0
    last_donation: date | None = None
    next_eligible_date: date | None = None


def interval_for(gender: Gender) -> int:
    # 'other' uses the longer interval as the conservative default.
    return settings.interval_male_days if gender == Gender.MALE else settings.interval_female_days


def check_donor(donor: Donor, on: date | None = None, vitals: Vitals | None = None) -> EligibilityResult:
    on = on or business_today()
    s = settings
    reasons: list[str] = []

    prior_dates = [d.donation_date for d in donor.donations if d.donation_date <= on]
    if donor.last_external_donation_date and donor.last_external_donation_date <= on:
        prior_dates.append(donor.last_external_donation_date)
    last = max(prior_dates) if prior_dates else None
    first_time = last is None

    age = donor.age(on)
    if age < s.min_age:
        reasons.append(f"Age {age} is below the minimum of {s.min_age} years.")
    elif first_time and age > s.max_age_first_time:
        reasons.append(f"First-time donors must be at most {s.max_age_first_time} years old (age {age}).")
    elif age > s.max_age:
        reasons.append(f"Age {age} is above the maximum of {s.max_age} years.")

    weight = vitals.weight_kg if vitals else donor.weight_kg
    if weight < s.min_weight_kg:
        reasons.append(f"Weight {weight:g} kg is below the minimum of {s.min_weight_kg:g} kg.")
    elif vitals and vitals.volume_ml == 450 and weight <= s.min_weight_450ml_kg:
        reasons.append(f"450 ml collection needs weight above {s.min_weight_450ml_kg:g} kg; use 350 ml.")

    interval = interval_for(donor.gender)
    next_date = None
    if last is not None:
        next_date = last + timedelta(days=interval)
        if on < next_date:
            reasons.append(
                f"Only {(on - last).days} days since last donation on {last:%d %b %Y}; "
                f"minimum interval is {interval} days (eligible from {next_date:%d %b %Y})."
            )
    # A back-dated entry must also respect donations recorded *after* that date.
    later = [d.donation_date for d in donor.donations if d.donation_date > on]
    if later and (min(later) - on).days < interval:
        reasons.append(
            f"A later donation on {min(later):%d %b %Y} is within the {interval}-day interval of this date."
        )

    if vitals:
        if vitals.hemoglobin_g_dl < s.min_hemoglobin_g_dl:
            reasons.append(f"Haemoglobin {vitals.hemoglobin_g_dl:g} g/dL is below {s.min_hemoglobin_g_dl:g} g/dL.")
        lo, hi = s.bp_systolic_range
        if not lo <= vitals.bp_systolic <= hi:
            reasons.append(f"Systolic BP {vitals.bp_systolic} outside {lo}-{hi} mmHg.")
        lo, hi = s.bp_diastolic_range
        if not lo <= vitals.bp_diastolic <= hi:
            reasons.append(f"Diastolic BP {vitals.bp_diastolic} outside {lo}-{hi} mmHg.")
        lo, hi = s.pulse_range
        if not lo <= vitals.pulse_bpm <= hi:
            reasons.append(f"Pulse {vitals.pulse_bpm} outside {lo}-{hi} bpm.")
        if vitals.temperature_c > s.max_temperature_c:
            reasons.append(f"Temperature {vitals.temperature_c:g} °C indicates fever (> {s.max_temperature_c:g} °C).")
        if not vitals.questionnaire_ok:
            reasons.append("Donor health questionnaire not satisfactory.")

    if not donor.is_active:
        reasons.append("Donor record is inactive.")

    return EligibilityResult(
        eligible=not reasons,
        reasons=reasons,
        is_first_time=first_time,
        age=age,
        interval_days=interval,
        last_donation=last,
        next_eligible_date=next_date,
    )
