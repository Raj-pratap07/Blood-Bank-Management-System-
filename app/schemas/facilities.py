"""Server-side validation for the facility directory forms (platform administration)."""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from pydantic import StringConstraints, field_validator, model_validator

from app.models.enums import (
    ContactKind,
    OperationalStatus,
    Ownership,
    SourceType,
    VerificationMethod,
    VerificationScope,
    VerificationStatus,
)
from app.schemas.forms import EMAIL_RE, FormModel, _phone
from app.utils import business_today

OptStr = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=300)]
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)

# Rough bounding box of India. Catches swapped latitude/longitude and typos; it can
# NOT tell whether coordinates are correct, which is why every pair needs a source
# and a separate location verification.
INDIA_LAT = (6.0, 38.0)
INDIA_LON = (68.0, 98.0)


class FacilityIn(FormModel):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=150)]
    official_name: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None
    aliases: Annotated[str | None, StringConstraints(max_length=1000)] = None
    ownership: Ownership = Ownership.UNKNOWN
    parent_hospital: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None
    address_line: OptStr = None
    locality: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=120)] = None
    city: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=80)] = None
    district: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=80)] = None
    state: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=80)] = None
    pincode: str | None = None
    eraktkosh_code: str | None = None
    cdsco_license_no: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=60)] = None
    has_component_separation: bool | None = None
    has_apheresis: bool | None = None
    operational_status: OperationalStatus = OperationalStatus.UNKNOWN
    notes: Annotated[str | None, StringConstraints(max_length=2000)] = None

    @field_validator("pincode")
    @classmethod
    def _pincode(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"[1-9][0-9]{5}", v):
            raise ValueError("PIN code must be 6 digits.")
        return v or None

    @field_validator("eraktkosh_code")
    @classmethod
    def _eraktkosh(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"[0-9]{1,12}", v):
            raise ValueError("e-RaktKosh code must contain digits only.")
        return v or None


class LocationIn(FormModel):
    """Coordinates are accepted only together with the source they were taken from."""

    latitude: float | None = None
    longitude: float | None = None
    location_source_id: int | None = None

    @model_validator(mode="after")
    def _check(self) -> LocationIn:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Enter both latitude and longitude, or neither.")
        if self.latitude is None:
            return self
        if not (INDIA_LAT[0] <= self.latitude <= INDIA_LAT[1] and INDIA_LON[0] <= self.longitude <= INDIA_LON[1]):
            raise ValueError("Coordinates are outside India. Check that latitude and longitude are not swapped.")
        if self.location_source_id is None:
            raise ValueError("Select the source the coordinates were taken from.")
        return self


class SourceIn(FormModel):
    source_type: SourceType
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    retrieved_on: date
    notes: Annotated[str | None, StringConstraints(max_length=2000)] = None

    @field_validator("retrieved_on")
    @classmethod
    def _not_future(cls, v: date) -> date:
        if v > business_today():
            raise ValueError("Retrieval date cannot be in the future.")
        return v

    @model_validator(mode="after")
    def _url_for_web_sources(self) -> SourceIn:
        web = (SourceType.GOVERNMENT_PORTAL, SourceType.HOSPITAL_WEBSITE, SourceType.OPENSTREETMAP,
               SourceType.THIRD_PARTY_DIRECTORY)
        if self.source_type in web and not URL_RE.match(self.reference):
            raise ValueError("For web sources, the reference must be the http(s) URL that was consulted.")
        return self


class ContactIn(FormModel):
    kind: ContactKind
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]
    label: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=120)] = None
    source_id: int
    is_public: bool = False

    @model_validator(mode="after")
    def _check_value(self) -> ContactIn:
        if self.kind == ContactKind.PHONE:
            self.value = _phone(self.value)
        elif self.kind == ContactKind.EMAIL and not EMAIL_RE.match(self.value):
            raise ValueError("Enter a valid email address.")
        elif self.kind == ContactKind.WEBSITE and not URL_RE.match(self.value):
            raise ValueError("Website must be a full http(s) URL.")
        return self


class VerificationIn(FormModel):
    scope: VerificationScope
    status: VerificationStatus
    method: VerificationMethod
    source_id: int | None = None
    contact_id: int | None = None
    verified_on: date
    evidence_note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]

    @field_validator("status")
    @classmethod
    def _not_derived(cls, v: VerificationStatus) -> VerificationStatus:
        if v == VerificationStatus.OUTDATED:
            raise ValueError("'Outdated' is calculated automatically and cannot be recorded.")
        return v

    @field_validator("verified_on")
    @classmethod
    def _not_future(cls, v: date) -> date:
        if v > business_today():
            raise ValueError("Verification date cannot be in the future.")
        return v

    @model_validator(mode="after")
    def _contact_scope(self) -> VerificationIn:
        if (self.scope == VerificationScope.CONTACT) != (self.contact_id is not None):
            raise ValueError("Select a contact for contact verifications (and only for them).")
        return self
