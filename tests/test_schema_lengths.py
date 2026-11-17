"""Phase 5A: form limits must never exceed the database column, and queries must be portable.

SQLite ignores VARCHAR(n); PostgreSQL rejects longer values with an error (a 500, not a form message).
So every user-entered string that reaches a String(n) column must be bounded by the form first.
"""
from __future__ import annotations

import enum
import types
import typing
from datetime import date

import pytest
from annotated_types import MaxLen
from pydantic import StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app import models as M
from app.config import BASE_DIR
from app.models import Donation
from app.schemas import facilities as FS
from app.schemas import forms as F
from app.services import reports
from app.utils import business_today

PAIRS = [(F.DonorIn, M.Donor), (F.DonationIn, M.Donation), (F.ScreeningIn, M.Donation),
         (F.BloodRequestIn, M.BloodRequest), (F.IssueIn, M.Issue), (F.UserIn, M.User), (F.OnboardIn, M.User),
         (FS.FacilityIn, M.Facility), (FS.LocationIn, M.Facility), (FS.SourceIn, M.FacilitySource),
         (FS.ContactIn, M.FacilityContact), (FS.VerificationIn, M.FacilityVerification)]

# String fields with no declared max_length whose length is bounded by a validator or regex instead.
# Each one has a boundary test below.
VALIDATOR_BOUNDED = {("DonorIn", "phone"), ("DonorIn", "email"), ("BloodRequestIn", "requester_phone"),
                     ("UserIn", "username"), ("OnboardIn", "username"), ("FacilityIn", "pincode"),
                     ("FacilityIn", "eraktkosh_code")}


def _base(ann):
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        return next(a for a in typing.get_args(ann) if a is not type(None))
    return ann


def _declared_max(field) -> int | None:
    for m in field.metadata:
        if isinstance(m, (StringConstraints, MaxLen)) and m.max_length:
            return m.max_length
    return None


def test_every_bounded_column_is_bounded_by_its_form():
    checked, unbounded = 0, set()
    for schema, model in PAIRS:
        cols = model.__table__.columns
        for name, field in schema.model_fields.items():
            if name not in cols:
                continue
            col_len = getattr(cols[name].type, "length", None)
            if col_len is None:
                continue                                               # Text / numeric / date columns
            base = _base(field.annotation)
            if isinstance(base, type) and issubclass(base, enum.Enum):
                longest = max(len(v.value) for v in base)
            elif base is str:
                longest = _declared_max(field)
                if longest is None:
                    unbounded.add((schema.__name__, name))
                    continue
            else:
                continue
            assert longest <= col_len, f"{schema.__name__}.{name}: form allows {longest} > column {col_len}"
            checked += 1
    assert unbounded == VALIDATOR_BOUNDED            # a new unbounded field must get a limit or a boundary test
    assert checked >= 40


# ---------------------------------------------------------------- boundary probes for validator-bounded fields
DONOR = dict(full_name="Len Test", gender="male", date_of_birth="1990-01-01", blood_group="O+",
             phone="9876543210", weight_kg=70)
REQUEST = dict(patient_name="Len Patient", patient_age=40, patient_gender="male", blood_group="O+",
               units_required=1, priority="routine", hospital_name="Test Hospital", requester_name="Dr Len",
               requester_phone="9876543210")


def _errors(schema, data) -> set[str]:
    try:
        schema.model_validate(data)
    except ValidationError as e:
        return {str(err["loc"][0]) for err in e.errors()}
    return set()


def _email(n: int) -> str:
    domain = "@example.com"
    return "a" * (n - len(domain)) + domain


class TestBoundaries:
    @pytest.mark.parametrize("value,ok", [
        ("1" * 20, True),                                  # 20 digits = column length
        ("+" + "1" * 19, True),                            # 20 chars with '+'
        ("+" + "1" * 20, False),                           # 21 after normalising -> rejected (was accepted)
        ("+91 98765-43210", True),                         # separators are removed before the length check
    ])
    @pytest.mark.parametrize("schema,base,field", [(F.DonorIn, DONOR, "phone"),
                                                   (F.BloodRequestIn, REQUEST, "requester_phone")])
    def test_phone(self, schema, base, field, value, ok):
        assert (field not in _errors(schema, {**base, field: value})) is ok
        if ok:
            assert len(getattr(schema.model_validate({**base, field: value}), field)) <= 20

    def test_phone_too_long_message(self):
        with pytest.raises(ValidationError, match="too long"):
            F.DonorIn.model_validate({**DONOR, "phone": "+" + "1" * 20})

    @pytest.mark.parametrize("n,ok", [(120, True), (121, False)])
    def test_email(self, n, ok):
        assert len(_email(n)) == n
        assert ("email" not in _errors(F.DonorIn, {**DONOR, "email": _email(n)})) is ok

    @pytest.mark.parametrize("n,ok", [(50, True), (51, False)])
    @pytest.mark.parametrize("schema", [F.UserIn, F.OnboardIn])
    def test_username(self, schema, n, ok):
        data = {"username": "u" * n, "full_name": "Len User", "role": "staff", "password": "Password@123"}
        assert ("username" not in _errors(schema, data)) is ok

    @pytest.mark.parametrize("field,value,ok", [("pincode", "263139", True), ("pincode", "2631390", False),
                                                ("eraktkosh_code", "1" * 12, True),
                                                ("eraktkosh_code", "1" * 13, False)])
    def test_facility_codes(self, field, value, ok):
        data = {"display_name": "Len Facility", field: value}
        assert (field not in _errors(FS.FacilityIn, data)) is ok


def test_too_long_email_shows_form_error_not_500(client):
    from tests.conftest import csrf_from, login
    login(client, "staff")
    page = client.get("/donors/new")
    r = client.post("/donors/new", data={**DONOR, "email": _email(121), "csrf_token": csrf_from(page.text)},
                    follow_redirects=False)
    assert r.status_code in (200, 400, 422) and "too long" in r.text


# ---------------------------------------------------------------- SQL portability
class _Capture:
    def __init__(self):
        self.stmt = None

    def execute(self, stmt):
        self.stmt = stmt
        return self

    def all(self):
        return []


class TestPortability:
    def test_monthly_donations_query_compiles_for_postgresql(self):
        cap = _Capture()
        reports.donations_by_month(cap, 1, on=date(2026, 11, 16))
        sql = str(cap.stmt.compile(dialect=postgresql.dialect())).lower()
        assert "strftime" not in sql and "extract(year from" in sql and "extract(month from" in sql

    def test_monthly_donations_counts_on_sqlite(self, db, make_unit, fid):
        for days in (1, 2, 40):
            make_unit(days_ago=days)
        on = business_today()
        out = reports.donations_by_month(db, fid, months=6, on=on)
        assert len(out["labels"]) == len(out["values"]) == 6
        dates = db.scalars(select(Donation.donation_date).where(Donation.facility_id == fid)).all()
        expected = {}
        for d in dates:
            expected[d.strftime("%b %Y")] = expected.get(d.strftime("%b %Y"), 0) + 1
        assert dict(zip(out["labels"], out["values"])) == {lbl: expected.get(lbl, 0) for lbl in out["labels"]}
        assert sum(out["values"]) == 3 and out["labels"][-1] == on.strftime("%b %Y")

    def test_month_buckets_across_year_end(self):
        cap = _Capture()
        out = reports.donations_by_month(cap, 1, months=3, on=date(2027, 1, 10))
        assert out["labels"] == ["Nov 2026", "Dec 2026", "Jan 2027"]

    def test_no_sqlite_only_sql_in_application_code(self):
        allowed = {"migrations.py"}                                    # SQLite v1-v4 runner (replaced in 5B)
        offenders = []
        for p in (BASE_DIR / "app").rglob("*.py"):
            if "__pycache__" in p.parts or p.name in allowed:
                continue
            src = p.read_text(encoding="utf-8")
            for token in ("func.strftime", "sqlite_master", "julianday(", "datetime('now"):
                if token in src:
                    offenders.append(f"{p.name}: {token}")
            if "PRAGMA" in src and p.name != "database.py":                # database.py: SQLite-only connect hook
                offenders.append(f"{p.name}: PRAGMA")
        assert offenders == []
