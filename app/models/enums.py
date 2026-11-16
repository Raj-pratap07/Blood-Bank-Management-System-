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

    @property
    def label(self) -> str:
        return {"admin": "Administrator", "medical_officer": "Medical Officer", "staff": "Staff"}[self.value]


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
    WHOLE_BLOOD = "whole_blood"


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
