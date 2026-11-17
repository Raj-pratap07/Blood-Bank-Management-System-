"""Phase 5A: UTC storage, Asia/Kolkata (IST) business days.

- Stored timestamps are timezone-aware UTC in Python; SQLite keeps the old naive-UTC text format.
- Existing naive rows are read as UTC and never shifted twice.
- Business dates (expiry, eligibility, reports, defaults) switch at 00:00 IST = 18:30 UTC.
- Calendar-day filters use an inclusive lower and an exclusive upper bound.
"""
from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import DateTime, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

import app.utils as utils
from app.config import BASE_DIR, ConfigurationError, Settings, validate_settings
from app.database import Base
from app.db_types import UTCDateTime
from app.models import BloodGroup as G
from app.models import BloodRequest, BloodUnit, Gender, Product, RequestPriority, User
from app.schemas.forms import BloodRequestIn, IssueIn
from app.services import availability as avail
from app.services import requests as req_svc
from app.utils import business_day_bounds_utc, business_today, iso_utc, to_business, to_utc
from app.web import templates
from tests.conftest import login

IST = ZoneInfo("Asia/Kolkata")
LAST_IST_SECOND = datetime(2026, 11, 5, 18, 29, 59, tzinfo=UTC)      # 05 Nov 23:59:59 IST
IST_MIDNIGHT = datetime(2026, 11, 5, 18, 30, 0, tzinfo=UTC)          # 06 Nov 00:00:00 IST


# ================================================================ helpers (pure)
class TestBusinessDate:
    def test_switches_at_ist_midnight_not_utc_midnight(self):
        assert business_today(LAST_IST_SECOND) == date(2026, 11, 5)
        assert business_today(IST_MIDNIGHT) == date(2026, 11, 6)
        # UTC midnight is 05:30 IST: still the same IST day as the previous UTC evening.
        assert business_today(datetime(2026, 11, 6, 0, 0, tzinfo=UTC)) == date(2026, 11, 6)

    def test_year_end_crossing(self):
        assert business_today(datetime(2026, 12, 31, 18, 29, 59, tzinfo=UTC)) == date(2026, 12, 31)
        assert business_today(datetime(2026, 12, 31, 18, 30, tzinfo=UTC)) == date(2027, 1, 1)

    def test_naive_now_is_treated_as_utc(self):
        assert business_today(datetime(2026, 11, 5, 18, 30)) == date(2026, 11, 6)

    def test_aware_non_utc_input(self):
        assert business_today(datetime(2026, 11, 6, 0, 0, tzinfo=IST)) == date(2026, 11, 6)

    def test_default_uses_patched_clock(self, monkeypatch):
        monkeypatch.setattr(utils, "utcnow", lambda: LAST_IST_SECOND)
        assert business_today() == date(2026, 11, 5)
        monkeypatch.setattr(utils, "utcnow", lambda: IST_MIDNIGHT)
        assert business_today() == date(2026, 11, 6)
        assert utils.today is business_today                   # legacy alias follows the business zone

    def test_utcnow_is_aware_utc(self):
        now = utils.utcnow()
        assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


class TestConversions:
    def test_to_utc(self):
        naive = datetime(2026, 1, 1, 10, 0)
        assert to_utc(naive) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)          # assumed UTC, no shift
        assert to_utc(datetime(2026, 1, 1, 15, 30, tzinfo=IST)) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert to_utc(to_utc(naive)) == to_utc(naive)                            # idempotent

    def test_to_business(self):
        b = to_business(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
        assert (b.hour, b.minute, b.tzname()) == (15, 30, "IST")

    def test_iso_utc_single_z_suffix(self):
        s = iso_utc(datetime(2026, 11, 5, 23, 59, tzinfo=IST))
        assert s == "2026-11-05T18:29:00Z"
        assert iso_utc(datetime(2026, 11, 5, 18, 29)) == s                      # naive = UTC
        assert "+00:00" not in s and s.count("Z") == 1
        assert datetime.fromisoformat(s) == datetime(2026, 11, 5, 18, 29, tzinfo=UTC)


class TestDayBounds:
    def test_single_ist_day(self):
        lo, hi = business_day_bounds_utc(date(2026, 11, 5), date(2026, 11, 5))
        assert lo == datetime(2026, 11, 4, 18, 30, tzinfo=UTC)
        assert hi == datetime(2026, 11, 5, 18, 30, tzinfo=UTC)                   # exclusive
        assert lo <= LAST_IST_SECOND < hi and not IST_MIDNIGHT < hi

    def test_open_ends(self):
        assert business_day_bounds_utc(None, None) == (None, None)
        lo, hi = business_day_bounds_utc(date(2026, 12, 31), None)
        assert hi is None and lo == datetime(2026, 12, 30, 18, 30, tzinfo=UTC)
        lo, hi = business_day_bounds_utc(None, date(2026, 12, 31))
        assert lo is None and hi == datetime(2026, 12, 31, 18, 30, tzinfo=UTC)

    def test_upper_bound_microsecond_edge(self):
        _, hi = business_day_bounds_utc(None, date(2026, 11, 5))
        assert hi - timedelta(microseconds=1) < hi                                 # 23:59:59.999999 IST included
        assert to_business(hi).time().isoformat() == "00:00:00"                     # next IST midnight


# ================================================================ configuration
class TestTimezoneSetting:
    def test_default_is_asia_kolkata(self):
        assert Settings().timezone == "Asia/Kolkata"
        validate_settings(Settings())

    @pytest.mark.parametrize("bad", ["Mars/Olympus", "IST+5:30", "", "../../etc/passwd"])
    def test_invalid_timezone_rejected(self, bad):
        s = Settings()
        s.timezone = bad
        with pytest.raises(ConfigurationError, match="BBMS_TIMEZONE"):
            validate_settings(s)


# ================================================================ column type / storage
DATETIME_COLUMNS = {
    "availability_publications": {"published_at", "valid_until", "withdrawn_at"},
    "blood_requests": {"created_at", "approved_at", "rejected_at"},
    "issues": {"issued_at"},
    "blood_units": {"updated_at"},
    "donations": {"screened_at", "created_at"},
    "donors": {"created_at", "updated_at"},
    "facilities": {"created_at", "updated_at"},
    "facility_sources": {"created_at"},
    "facility_contacts": {"created_at"},
    "facility_verifications": {"recorded_at"},
    "users": {"created_at", "password_set_at", "setup_token_expires_at"},
}


class TestUTCDateTimeColumns:
    def test_exactly_the_20_timestamp_columns_use_utc_type(self):
        found: dict[str, set[str]] = {}
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, (DateTime, UTCDateTime)):
                    assert isinstance(col.type, UTCDateTime), f"{table.name}.{col.name} is a plain DateTime"
                    found.setdefault(table.name, set()).add(col.name)
        assert found == DATETIME_COLUMNS
        assert sum(len(v) for v in found.values()) == 20

    def test_sqlite_ddl_unchanged_postgres_gets_timestamptz(self):
        ddl = str(CreateTable(User.__table__).compile(dialect=sqlite.dialect()))
        assert "created_at DATETIME NOT NULL" in ddl and "TIMESTAMP" not in ddl
        pg = str(CreateTable(User.__table__).compile(dialect=postgresql.dialect()))
        assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in pg

    def test_live_sqlite_declared_types_are_datetime(self, engine):
        with engine.connect() as c:
            for table, cols in DATETIME_COLUMNS.items():
                types = {r[1]: r[2] for r in c.exec_driver_sql(f'PRAGMA table_info("{table}")')}
                assert {types[col] for col in cols} == {"DATETIME"}, table


def _raw_created(db, uid: int) -> str:
    return db.execute(text("SELECT created_at FROM users WHERE id = :i"), {"i": uid}).scalar_one()


class TestStorageRoundTrip:
    def test_aware_ist_value_stored_as_naive_utc_text(self, db, users):
        u = users["staff"]
        u.created_at = datetime(2026, 11, 5, 23, 59, tzinfo=IST)
        db.commit()
        assert _raw_created(db, u.id).startswith("2026-11-05 18:29:00")          # same format as before 5A
        db.expire_all()
        assert db.get(User, u.id).created_at == datetime(2026, 11, 5, 18, 29, tzinfo=UTC)
        assert db.get(User, u.id).created_at.tzinfo is not None

    def test_naive_value_is_treated_as_utc(self, db, users):
        u = users["staff"]
        u.created_at = datetime(2026, 11, 5, 18, 29)
        db.commit()
        assert _raw_created(db, u.id).startswith("2026-11-05 18:29:00")

    def test_existing_naive_row_read_as_utc_and_not_shifted_on_resave(self, db, users):
        uid = users["staff"].id
        legacy = "2026-01-01 10:00:00.000000"                                    # written by pre-5A code
        db.execute(text("UPDATE users SET created_at = :v WHERE id = :i"), {"v": legacy, "i": uid})
        db.commit()
        db.expire_all()
        u = db.get(User, uid)
        assert u.created_at == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        # Re-save through the ORM, even with an IST-zoned copy of the same instant: stored text unchanged.
        u.created_at = to_business(u.created_at)
        u.full_name = "Renamed Staff"
        db.commit()
        assert _raw_created(db, uid) == legacy

    def test_untouched_rows_keep_their_text(self, db, users):
        uid = users["staff"].id
        before = _raw_created(db, uid)
        u = db.get(User, uid)
        u.full_name = "Another Name"
        db.commit()
        assert _raw_created(db, uid) == before

    def test_defaults_are_aware_and_comparable(self, db, users):
        db.expire_all()
        created = db.get(User, users["staff"].id).created_at
        assert created.tzinfo is not None and created <= utils.utcnow()

    def test_utc_query_comparison(self, db, users):
        uid = users["staff"].id
        u = db.get(User, uid)
        u.created_at = LAST_IST_SECOND
        db.commit()
        lo, hi = business_day_bounds_utc(date(2026, 11, 5), date(2026, 11, 5))
        hit = db.scalar(select(User.id).where(User.id == uid, User.created_at >= lo, User.created_at < hi))
        assert hit == uid
        lo, hi = business_day_bounds_utc(date(2026, 11, 6), date(2026, 11, 6))
        assert db.scalar(select(User.id).where(User.id == uid, User.created_at >= lo, User.created_at < hi)) is None

    def test_non_datetime_rejected(self):
        with pytest.raises(TypeError):
            UTCDateTime().process_bind_param("2026-01-01", sqlite.dialect())


# ================================================================ expiry at IST midnight
class TestExpiryAtIstMidnight:
    def test_bag_expires_at_ist_midnight(self, db, make_unit, monkeypatch, fid):
        unit = make_unit(G.B_NEG)
        unit.expires_on = date(2026, 11, 5)
        db.commit()
        monkeypatch.setattr(utils, "utcnow", lambda: LAST_IST_SECOND)
        assert not unit.is_expired() and unit.days_to_expiry() == 0
        assert avail.eligible_counts(db, fid)[(G.B_NEG, Product.WHOLE_BLOOD)] == 1
        monkeypatch.setattr(utils, "utcnow", lambda: IST_MIDNIGHT)
        assert unit.is_expired() and unit.days_to_expiry() == -1
        assert avail.eligible_counts(db, fid)[(G.B_NEG, Product.WHOLE_BLOOD)] == 0
        assert db.get(BloodUnit, unit.id).expires_on == date(2026, 11, 5)      # stored date untouched


# ================================================================ reports over HTTP
def _make_request(db, users, created_at: datetime) -> BloodRequest:
    req = req_svc.create_request(db, BloodRequestIn(
        patient_name="Boundary Patient", patient_age=50, patient_gender=Gender.MALE, blood_group=G.A_POS,
        units_required=1, priority=RequestPriority.ROUTINE, hospital_name="Test Hospital",
        requester_name="Dr Test", requester_phone="9876500002"), users["staff"])
    req.created_at = created_at
    db.commit()
    return req


@pytest.fixture()
def boundary_requests(db, users):
    """One request at 23:59:59 IST on 5 Nov and one at 00:00:00 IST on 6 Nov."""
    return _make_request(db, users, LAST_IST_SECOND).code, _make_request(db, users, IST_MIDNIGHT).code


@pytest.fixture()
def boundary_issues(db, users, make_unit):
    codes = []
    for when in (LAST_IST_SECOND, IST_MIDNIGHT):
        unit = make_unit(G.A_POS)
        req = _make_request(db, users, when)
        req_svc.approve_and_reserve(db, req, users["medical_officer"])
        issue = req_svc.issue_units(db, req, IssueIn(unit_ids=[unit.id], received_by="Nurse T",
                                                     crossmatch_confirmed=True), users["staff"])[0]
        issue.issued_at = when
        db.commit()
        codes.append(unit.unit_code)
    return tuple(codes)


class TestReportBoundaries:
    @pytest.mark.parametrize("query,expect_last,expect_midnight", [
        ("?start=2026-11-05&end=2026-11-05", True, False),
        ("?start=2026-11-06&end=2026-11-06", False, True),
        ("?start=2026-11-05&end=2026-11-06", True, True),
        ("?start=2026-11-04&end=2026-11-04", False, False),
    ])
    def test_request_report(self, client, boundary_requests, query, expect_last, expect_midnight):
        login(client, "staff")
        html = client.get("/reports/requests" + query).text
        last, midnight = boundary_requests
        assert (last in html, midnight in html) == (expect_last, expect_midnight)

    @pytest.mark.parametrize("path", ["/issues", "/reports/issues"])
    @pytest.mark.parametrize("query,expect_last,expect_midnight", [
        ("?start=2026-11-05&end=2026-11-05", True, False),
        ("?start=2026-11-06&end=2026-11-06", False, True),
    ])
    def test_issue_filters(self, client, boundary_issues, path, query, expect_last, expect_midnight):
        login(client, "staff")
        html = client.get(path + query).text
        last, midnight = boundary_issues
        assert (last in html, midnight in html) == (expect_last, expect_midnight)

    def test_times_displayed_in_ist(self, client, boundary_issues):
        login(client, "staff")
        html = client.get("/issues?start=2026-11-05&end=2026-11-06").text
        assert "05 Nov 2026, 23:59 IST" in html and "06 Nov 2026, 00:00 IST" in html
        assert "18:29" not in html and "18:30" not in html                      # raw UTC never shown


# ================================================================ display filters
class TestDisplayFilters:
    def test_dt_and_ist_filters(self):
        f = templates.env.filters
        aware = datetime(2026, 11, 5, 18, 29, tzinfo=UTC)
        assert f["dt"](aware) == "05 Nov 2026, 23:59 IST"
        assert f["dt"](datetime(2026, 11, 5, 18, 29)) == "05 Nov 2026, 23:59 IST"   # naive = UTC
        assert f["ist"](aware) == "05 Nov 2026, 23:59 IST"
        assert f["dt"](None) == "—"

    def test_d_filter_uses_ist_day_for_timestamps_only(self):
        f = templates.env.filters
        assert f["d"](IST_MIDNIGHT) == "06 Nov 2026"                              # timestamp -> IST day
        assert f["d"](date(2026, 11, 5)) == "05 Nov 2026"                         # plain dates unchanged
        assert f["d"](None) == "—"

    def test_today_global_is_business_today(self):
        assert templates.env.globals["today"] is business_today


# ================================================================ static guards
APP_DIR = BASE_DIR / "app"


def _py_files(*dirs: Path):
    for d in dirs:
        yield from (p for p in d.rglob("*.py") if "__pycache__" not in p.parts)


class TestNoServerLocalDates:
    def test_no_date_today_or_naive_now(self):
        offenders = []
        for p in _py_files(APP_DIR, BASE_DIR / "scripts"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
                    attr = node.func.attr
                    if (name, attr) in {("date", "today"), ("datetime", "utcnow"), ("datetime", "today")}:
                        offenders.append(f"{p.name}:{node.lineno} {name}.{attr}()")
                    if (name, attr) == ("datetime", "now") and not node.args and not node.keywords:
                        offenders.append(f"{p.name}:{node.lineno} datetime.now() without tz")
        # backup_db names files with the operator's local clock; it is a local CLI tool, not business logic.
        offenders = [o for o in offenders if not o.startswith("backup_db.py")]
        assert offenders == []

    def test_no_hand_rolled_ist_offset(self):
        for p in _py_files(APP_DIR):
            assert "hours=5, minutes=30" not in p.read_text(encoding="utf-8"), p
