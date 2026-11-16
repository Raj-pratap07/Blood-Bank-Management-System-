"""Enumerations shared by models, services and templates."""
from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def db_enum(cls: type[enum.Enum]) -> SAEnum:
    """Store the enum *value* (e.g. 'A+') as a portable VARCHAR."""
    return SAEnum(
        cls,
        values_callable=lambda c: [m.value for m in c],
        native_enum=False,
        validate_strings=True,
        length=32,
    )


class BloodGroup(enum.StrEnum):
    A_POS = "A+"
    A_NEG = "A-"
    B_POS = "B+"
    B_NEG = "B-"
    AB_POS = "AB+"
    AB_NEG = "AB-"
    O_POS = "O+"
    O_NEG = "O-"


class Role(enum.StrEnum):
    ADMIN = "admin"
    MEDICAL_OFFICER = "medical_officer"
    STAFF = "staff"
    # Manages the facility directory and verification. Has NO access to any
    # facility's operational data (donors, donations, inventory, requests).
    PLATFORM_ADMIN = "platform_admin"

    @property
    def label(self) -> str:
        return {"admin": "Administrator", "medical_officer": "Medical Officer", "staff": "Staff",
                "platform_admin": "Platform Administrator"}[self.value]


FACILITY_ROLES = (Role.ADMIN, Role.MEDICAL_OFFICER, Role.STAFF)


# ---------------------------------------------------------------- facilities
class Ownership(enum.StrEnum):
    GOVERNMENT = "government"
    CHARITABLE = "charitable"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class OperationalStatus(enum.StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class StockVisibility(enum.StrEnum):  # used from Phase 4; always "hidden" until then
    HIDDEN = "hidden"
    BANDED = "banded"
    EXACT = "exact"


class SourceType(enum.StrEnum):
    FACILITY_CONFIRMATION = "facility_confirmation"   # letter/email/recorded call from the facility
    GOVERNMENT_PORTAL = "government_portal"           # e.g. e-RaktKosh
    GOVERNMENT_DOCUMENT = "government_document"       # e.g. NHM / CDSCO list
    HOSPITAL_WEBSITE = "hospital_website"             # facility's own official site
    OPENSTREETMAP = "openstreetmap"
    SITE_VISIT = "site_visit"
    THIRD_PARTY_DIRECTORY = "third_party_directory"   # aggregators: leads only

    @property
    def is_official(self) -> bool:
        return self in (SourceType.GOVERNMENT_PORTAL, SourceType.GOVERNMENT_DOCUMENT, SourceType.HOSPITAL_WEBSITE)


class ContactKind(enum.StrEnum):
    PHONE = "phone"
    EMAIL = "email"
    WEBSITE = "website"


class VerificationScope(enum.StrEnum):
    IDENTITY = "identity"
    LOCATION = "location"
    CONTACT = "contact"
    SERVICES = "services"


class VerificationStatus(enum.StrEnum):
    UNVERIFIED = "unverified"
    SOURCE_CONFIRMED = "source_confirmed"
    FACILITY_CONFIRMED = "facility_confirmed"
    FIELD_VERIFIED = "field_verified"
    DISPUTED = "disputed"
    OUTDATED = "outdated"   # derived only (age of the last positive check); never stored

    @property
    def is_positive(self) -> bool:
        return self in (VerificationStatus.SOURCE_CONFIRMED, VerificationStatus.FACILITY_CONFIRMED,
                        VerificationStatus.FIELD_VERIFIED)


class VerificationMethod(enum.StrEnum):
    OFFICIAL_SOURCE_CHECK = "official_source_check"
    PHONE_CALL = "phone_call"
    EMAIL = "email"
    LETTER = "letter"
    SITE_VISIT = "site_visit"
    OTHER = "other"


class Gender(enum.StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class DonationStatus(enum.StrEnum):
    QUARANTINE = "quarantine"   # collected, awaiting lab screening
    ACCEPTED = "accepted"       # screening passed -> unit available
    REJECTED = "rejected"       # screening failed -> unit discarded


class TestResult(enum.StrEnum):
    NON_REACTIVE = "non_reactive"
    REACTIVE = "reactive"


class Product(enum.StrEnum):
    """Blood products / components. Inventory currently tracks whole blood only (TRACKED_PRODUCTS)."""
    WHOLE_BLOOD = "whole_blood"
    PACKED_RED_CELLS = "packed_red_cells"
    FRESH_FROZEN_PLASMA = "fresh_frozen_plasma"
    PLATELETS = "platelets"
    CRYOPRECIPITATE = "cryoprecipitate"

    @property
    def label(self) -> str:
        return {"whole_blood": "Whole blood", "packed_red_cells": "Packed red cells (PRBC)",
                "fresh_frozen_plasma": "Fresh frozen plasma (FFP)", "platelets": "Platelets",
                "cryoprecipitate": "Cryoprecipitate"}[self.value]


# Products the inventory actually records (bags are created as whole blood at collection).
TRACKED_PRODUCTS = (Product.WHOLE_BLOOD,)


class AvailabilityStatus(enum.StrEnum):
    AVAILABLE = "available"
    LOW = "low"
    NOT_AVAILABLE = "not_available"


class UnitStatus(enum.StrEnum):
    QUARANTINE = "quarantine"
    AVAILABLE = "available"
    RESERVED = "reserved"
    ISSUED = "issued"
    EXPIRED = "expired"
    DISCARDED = "discarded"


class RequestPriority(enum.StrEnum):
    ROUTINE = "routine"
    URGENT = "urgent"
    EMERGENCY = "emergency"


class RequestStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PARTIALLY_ISSUED = "partially_issued"
    FULFILLED = "fulfilled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


OPEN_REQUEST_STATUSES = (RequestStatus.PENDING, RequestStatus.APPROVED, RequestStatus.PARTIALLY_ISSUED)
