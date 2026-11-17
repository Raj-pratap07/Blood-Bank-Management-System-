"""Pydantic models that validate submitted HTML forms (server-side validation)."""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.enums import BloodGroup, Gender, RequestPriority, Role, TestResult
from app.utils import business_today

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
OptText = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=1000)]
PHONE_RE = re.compile(r"^\+?[0-9][0-9\- ]{6,18}[0-9]$")
PHONE_MAX_LEN = 20     # donors.phone / blood_requests.requester_phone are String(20)
EMAIL_MAX_LEN = 120    # donors.email is String(120)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FormModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


def _phone(v: str) -> str:
    if not PHONE_RE.match(v):
        raise ValueError("Enter a valid phone number (digits, optional +, spaces or dashes).")
    # Store one canonical form so "98765 43210" and "98765-43210" match "9876543210"
    # in the duplicate-donor check.
    v = re.sub(r"[\s-]", "", v)
    if len(v) > PHONE_MAX_LEN:  # the stored column is VARCHAR(20); PostgreSQL would reject longer values
        raise ValueError(f"Phone number is too long (at most {PHONE_MAX_LEN} characters without spaces or dashes).")
    return v


class DonorIn(FormModel):
    full_name: Name
    gender: Gender
    date_of_birth: date
    blood_group: BloodGroup
    phone: str
    email: str | None = None
    address: OptText = None
    city: Annotated[str | None, StringConstraints(max_length=80)] = None
    weight_kg: float = Field(ge=20, le=250)
    last_external_donation_date: date | None = None

    _phone_ok = field_validator("phone")(_phone)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v and len(v) > EMAIL_MAX_LEN:
            raise ValueError(f"Email address is too long (at most {EMAIL_MAX_LEN} characters).")
        if v and not EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address.")
        return v or None

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date) -> date:
        if v >= business_today():
            raise ValueError("Date of birth must be in the past.")
        if v.year < 1900:
            raise ValueError("Date of birth is not plausible.")
        return v

    @field_validator("last_external_donation_date")
    @classmethod
    def _ext(cls, v: date | None) -> date | None:
        if v and v > business_today():
            raise ValueError("Previous donation date cannot be in the future.")
        return v


class DonationIn(FormModel):
    donor_id: int
    donation_date: date
    volume_ml: int
    weight_kg: float = Field(ge=20, le=250)
    hemoglobin_g_dl: float = Field(ge=3, le=25)
    bp_systolic: int = Field(ge=50, le=260)
    bp_diastolic: int = Field(ge=30, le=160)
    pulse_bpm: int = Field(ge=30, le=220)
    temperature_c: float = Field(ge=34, le=43)
    questionnaire_ok: bool = False
    cleared_by_id: int
    pre_donation_notes: OptText = None

    @field_validator("volume_ml")
    @classmethod
    def _volume(cls, v: int) -> int:
        if v not in (350, 450):
            raise ValueError("Collection volume must be 350 or 450 ml.")
        return v

    @field_validator("donation_date")
    @classmethod
    def _not_future(cls, v: date) -> date:
        if v > business_today():
            raise ValueError("Donation date cannot be in the future.")
        return v


class ScreeningIn(FormModel):
    hiv_result: TestResult
    hbv_result: TestResult
    hcv_result: TestResult
    syphilis_result: TestResult
    malaria_result: TestResult
    abo_rh_confirmed: bool = False
    decision: Literal["accept", "reject"]
    screening_notes: OptText = None

    @model_validator(mode="after")
    def _consistent(self) -> ScreeningIn:
        if self.decision == "accept":
            markers = [self.hiv_result, self.hbv_result, self.hcv_result, self.syphilis_result, self.malaria_result]
            if any(m == TestResult.REACTIVE for m in markers):
                raise ValueError("A donation with any reactive marker cannot be accepted.")
            if not self.abo_rh_confirmed:
                raise ValueError("ABO/Rh grouping must be confirmed before acceptance.")
        elif not self.screening_notes:
            raise ValueError("Give a reason when rejecting a donation.")
        return self


class BloodRequestIn(FormModel):
    patient_name: Name
    patient_age: int = Field(ge=0, le=120)
    patient_gender: Gender
    blood_group: BloodGroup
    units_required: int = Field(ge=1, le=20)
    priority: RequestPriority
    hospital_name: Name
    hospital_address: OptText = None
    doctor_name: Annotated[str | None, StringConstraints(max_length=120)] = None
    requester_name: Name
    requester_phone: str
    reason: OptText = None
    required_by: date | None = None

    _phone_ok = field_validator("requester_phone")(_phone)

    @field_validator("required_by")
    @classmethod
    def _future(cls, v: date | None) -> date | None:
        if v and v < business_today():
            raise ValueError("Required-by date cannot be in the past.")
        return v


def _password_bytes(v: str) -> str:
    # bcrypt hashes at most 72 *bytes*; bcrypt>=5 raises beyond that. Non-English
    # characters take 2-4 bytes each, so a 72-character limit is not enough.
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password is too long: maximum 72 bytes (fewer characters when using "
                         "non-English letters).")
    return v


Password = Annotated[str, StringConstraints(min_length=8, max_length=72), AfterValidator(_password_bytes)]


class UserIn(FormModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9_.]{3,50}$")]
    full_name: Name
    role: Role
    password: Password


class OnboardIn(FormModel):
    """Platform onboarding: no password is set by the platform administrator."""
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9_.]{3,50}$")]
    full_name: Name


class SetupIn(FormModel):
    new_password: Password
    confirm_password: str

    @model_validator(mode="after")
    def _match(self) -> SetupIn:
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self


class PasswordChangeIn(FormModel):
    current_password: str = ""
    new_password: Password
    confirm_password: str

    @model_validator(mode="after")
    def _match(self) -> PasswordChangeIn:
        if self.new_password != self.confirm_password:
            raise ValueError("New passwords do not match.")
        return self


class IssueIn(FormModel):
    unit_ids: list[int] = Field(min_length=1)
    received_by: Name
    crossmatch_confirmed: bool = False
    notes: OptText = None

    @field_validator("crossmatch_confirmed")
    @classmethod
    def _xm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Confirm that the laboratory crossmatch is compatible before issuing.")
        return v
