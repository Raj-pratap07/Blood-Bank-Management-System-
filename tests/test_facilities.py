"""Haldwani BloodLink Phase 1: migration v2, facility directory, platform_admin role, verification rules.

All facility data here is fictional test data (example.org references); no real facility is used.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import Base, init_db, make_engine
from app.migrations import DEMO_FACILITY_SLUG, SCHEMA_VERSION, upgrade
from app.models import (
    Facility,
    FacilityVerification,
    OperationalStatus,
    Role,
    User,
    VerificationScope,
    VerificationStatus,
)
from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, SourceIn, VerificationIn
from app.services import facilities as svc
from app.services import users as user_svc
from app.services.errors import PermissionDenied, ServiceError
from tests.conftest import PASSWORD, csrf_from, login

V = VerificationStatus
TODAY = date.today()


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def platform(users):
    return users["platform_admin"]


def _facility(db, platform, name="Test Facility Alpha", **kw):
    return svc.create_facility(db, FacilityIn(display_name=name, **kw), platform)


def _source(db, f, platform, source_type="government_document", title="Test official list", ref="Test doc ref 1"):
    if source_type in ("government_portal", "hospital_website", "openstreetmap", "third_party_directory"):
        ref = "https://example.org/test-source"
    return svc.add_source(db, f, SourceIn(source_type=source_type, title=title, reference=ref,
                                          retrieved_on=TODAY), platform)


def _verify(db, f, platform, scope="identity", status="source_confirmed", method="official_source_check",
            source=None, contact=None, on=TODAY, note="Checked against the test source"):
    return svc.record_verification(db, f, VerificationIn(
        scope=scope, status=status, method=method, source_id=source.id if source else None,
        contact_id=contact.id if contact else None, verified_on=on, evidence_note=note), platform)


def _listable(db, platform, name="Test Facility Listable"):
    f = _facility(db, platform, name, address_line="1 Test Road", city="Testville")
    gov = _source(db, f, platform)
    _verify(db, f, platform, source=gov)
    phone = svc.add_contact(db, f, ContactIn(kind="phone", value="98765 00011", source_id=gov.id, is_public=True),
                            platform)
    return f, gov, phone


# =================================================================== migration v2
V1_TABLES = ["users", "donors", "donations", "blood_units", "blood_requests", "issues"]


def v1_schema_ddl() -> list[str]:
    """The schema exactly as it was at version 1: today's DDL minus every Phase 3 facility_id
    column / foreign key / index, plus the original globally unique donor-phone index."""
    import re

    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable

    import app.models  # noqa: F401

    out = []
    for t in V1_TABLES:
        table = Base.metadata.tables[t]
        sql = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        sql = "\n".join(line for line in sql.splitlines() if "facility_id" not in line)
        out.append(re.sub(r",\s*\n\)", "\n)", sql))   # drop a trailing comma left before ")"
        # The ordinary indexes create_all built at v1 (everything except Phase 3 facility indexes).
        for idx in sorted(table.indexes, key=lambda i: i.name):
            if "facility_id" in idx.columns or idx.name == "ix_donors_phone":
                continue
            out.append(str(CreateIndex(idx).compile(dialect=sqlite.dialect())))
    out.append("CREATE UNIQUE INDEX ix_donors_phone ON donors (phone)")   # v1: phone was globally unique
    return out


def _v1_database(tmp_path):
    """A database exactly as it was at schema version 1 (before Phase 1), with data."""
    import app.models  # noqa: F401

    eng = make_engine(f"sqlite:///{(tmp_path / 'v1.db').as_posix()}")
    with eng.begin() as c:
        for ddl in v1_schema_ddl():
            c.execute(text(ddl))
        c.execute(text("PRAGMA user_version = 1"))
        c.execute(text("INSERT INTO users (id, username, full_name, password_hash, role, is_active, created_at) "
                       "VALUES (1, 'admin', 'A', 'x', 'admin', 1, '2026-01-01')"))
        c.execute(text("INSERT INTO donors (id, full_name, gender, date_of_birth, blood_group, phone, weight_kg, "
                       "is_active, created_at, updated_at) VALUES (1, 'Donor One', 'male', '1990-01-01', 'A+', "
                       "'9800000000', 70, 1, '2026-01-01', '2026-01-01')"))
    return eng


class TestMigrationV2:
    def test_upgrade_from_v1_preserves_data_and_adds_only_demo_facility(self, tmp_path):
        eng = _v1_database(tmp_path)
        assert upgrade(eng) == SCHEMA_VERSION
        with eng.connect() as c:
            tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert {"facilities", "facility_sources", "facility_contacts", "facility_verifications"} <= tables
            assert c.execute(text("SELECT count(*) FROM users")).scalar() == 1           # existing rows kept
            assert c.execute(text("SELECT count(*) FROM donors")).scalar() == 1
            rows = c.execute(text("SELECT slug, is_demo, is_listed, latitude, longitude, address_line "
                                  "FROM facilities")).all()
        assert rows == [(DEMO_FACILITY_SLUG, 1, 0, None, None, None)]                     # nothing real inserted
        eng.dispose()

    def test_upgrade_is_idempotent(self, tmp_path):
        eng = _v1_database(tmp_path)
        upgrade(eng)
        upgrade(eng)
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM facilities WHERE is_demo = 1")).scalar() == 1
        eng.dispose()

    def test_migrated_schema_matches_models(self, tmp_path):
        """Guards against drift between the explicit v2 DDL and the SQLAlchemy models."""
        migrated = _v1_database(tmp_path)
        upgrade(migrated)
        fresh = make_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
        init_db(fresh)

        def describe(eng):
            with eng.connect() as c:
                out = {}
                for t in ("facilities", "facility_sources", "facility_contacts", "facility_verifications"):
                    cols = [tuple(r[1:4]) + (r[5],) for r in c.execute(text(f"PRAGMA table_info({t})"))]
                    idx = sorted((r[1], r[2]) for r in c.execute(text(f"PRAGMA index_list({t})"))
                                 if not r[1].startswith("sqlite_"))
                    fks = sorted(tuple(r[2:5]) for r in c.execute(text(f"PRAGMA foreign_key_list({t})")))
                    out[t] = (cols, idx, fks)
                triggers = sorted(r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
                return out, triggers

        assert describe(migrated) == describe(fresh)
        migrated.dispose(), fresh.dispose()


# =========================================================== database-level guards
class TestDatabaseGuards:
    def test_contact_cannot_cite_another_facilitys_source(self, db, platform):
        a, b = _facility(db, platform, "Test Facility A"), _facility(db, platform, "Test Facility B")
        src_a = _source(db, a, platform)
        with pytest.raises(IntegrityError, match="another facility"):
            db.execute(text("INSERT INTO facility_contacts (facility_id, kind, value, source_id, is_public, "
                            "is_active, created_by_id, created_at) VALUES (:f, 'phone', '9800000001', :s, 1, 1, :u, "
                            "'2026-01-01')"), {"f": b.id, "s": src_a.id, "u": platform.id})
        db.rollback()

    def test_verification_log_is_append_only(self, db, platform):
        f = _facility(db, platform)
        entry = _verify(db, f, platform, source=_source(db, f, platform))
        for stmt in ("UPDATE facility_verifications SET status = 'disputed' WHERE id = :i",
                     "DELETE FROM facility_verifications WHERE id = :i"):
            with pytest.raises(IntegrityError, match="append-only"):
                db.execute(text(stmt), {"i": entry.id})
            db.rollback()

    def test_coordinates_require_a_source_in_the_database(self, db, platform):
        f = _facility(db, platform)
        with pytest.raises(IntegrityError, match="ck_facility_coords_sourced"):
            db.execute(text("UPDATE facilities SET latitude = 29.2, longitude = 79.5 WHERE id = :i"), {"i": f.id})
        db.rollback()

    def test_demo_facility_can_never_be_listed_in_the_database(self, db):
        with pytest.raises(IntegrityError, match="ck_facility_demo_never_listed"):
            db.execute(text("UPDATE facilities SET is_listed = 1 WHERE is_demo = 1"))
        db.rollback()


# ================================================================= roles & access
OPERATIONAL_PATHS = ["/donors", "/donations", "/donations/new", "/inventory", "/inventory/units", "/requests",
                     "/issues", "/reports", "/reports/inventory", "/users"]


class TestRoleSeparation:
    @pytest.mark.parametrize("path", OPERATIONAL_PATHS)
    def test_platform_admin_has_no_operational_access(self, client, path):
        login(client, "platformadmin")
        assert client.get(path, follow_redirects=False).status_code == 403

    def test_platform_admin_has_no_operational_api(self, client):
        login(client, "platformadmin")
        assert client.get("/api/stats/inventory").status_code == 403

    def test_platform_admin_home_redirects_to_directory(self, client):
        login(client, "platformadmin")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/platform/facilities"

    @pytest.mark.parametrize("username", ["admin", "medicalofficer", "staff"])
    def test_facility_roles_cannot_use_platform(self, client, username):
        login(client, username)
        assert client.get("/platform/facilities").status_code == 403
        r = client.post("/platform/facilities/new", data={"display_name": "Sneaky Facility",
                                                          "csrf_token": csrf_from(client.get("/").text)})
        assert r.status_code == 403

    def test_anonymous_redirected_to_login(self, client):
        r = client.get("/platform/facilities", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")

    def test_services_reject_non_platform_actors(self, db, users):
        with pytest.raises(PermissionDenied):
            svc.create_facility(db, FacilityIn(display_name="Test Facility X"), users["admin"])


class TestNoPrivilegeEscalation:
    def test_facility_admin_cannot_create_platform_admin(self, db, users):
        from app.schemas.forms import UserIn

        with pytest.raises(PermissionDenied):
            user_svc.create_user(db, UserIn(username="sneaky", full_name="Sneaky One", role="platform_admin",
                                            password="Password@1"), users["admin"])

    def test_facility_admin_cannot_reset_or_deactivate_platform_admin(self, db, users, platform):
        with pytest.raises(PermissionDenied):
            user_svc.reset_password(db, platform, "NewPass@123", users["admin"])
        with pytest.raises(PermissionDenied):
            user_svc.set_active(db, platform, False, users["admin"])

    def test_platform_admins_hidden_from_user_management(self, client):
        login(client, "admin")
        html = client.get("/users").text
        assert "platformadmin" not in html
        assert 'value="platform_admin"' not in client.get("/users/new").text

    def test_http_reset_of_platform_admin_is_refused(self, client, db, platform):
        login(client, "admin")
        before = platform.password_hash
        client.post(f"/users/{platform.id}/password", data={"new_password": "Takeover@123",
                                                            "csrf_token": csrf_from(client.get("/users").text)})
        db.expire_all()
        assert db.get(User, platform.id).password_hash == before


# ============================================================ facility rules
class TestFacilityRecords:
    def test_new_facility_is_unverified_and_unlisted(self, db, platform):
        f = _facility(db, platform)
        assert f.slug == "test-facility-alpha" and not f.is_listed and not f.is_participating
        assert all(e.status == V.UNVERIFIED for e in svc.verification_summary(f).values())
        assert f.stock_visibility.value == "hidden"

    def test_duplicate_names_get_unique_slugs(self, db, platform):
        assert _facility(db, platform).slug != _facility(db, platform).slug

    @pytest.mark.parametrize("field,value", [("pincode", "12345"), ("pincode", "012345"), ("eraktkosh_code", "AB12"),
                                             ("display_name", "ab")])
    def test_invalid_facility_fields(self, field, value):
        with pytest.raises(ValidationError):
            FacilityIn(**{"display_name": "Valid Name", field: value})

    def test_demo_facility_is_protected(self, db, platform):
        demo = db.scalar(select(Facility).where(Facility.is_demo.is_(True)))
        src = _source(db, demo, platform)
        with pytest.raises(ServiceError):
            svc.add_contact(db, demo, ContactIn(kind="phone", value="9800000002", source_id=src.id), platform)
        with pytest.raises(ServiceError):
            svc.set_location(db, demo, LocationIn(latitude=29.2, longitude=79.5, location_source_id=src.id), platform)
        with pytest.raises(ServiceError):
            _verify(db, demo, platform, source=src)
        with pytest.raises(ServiceError, match="never be listed"):
            svc.set_listed(db, demo, True, platform)


class TestLocation:
    def test_coordinates_need_a_source(self):
        with pytest.raises(ValidationError, match="source"):
            LocationIn(latitude=29.2, longitude=79.5)

    @pytest.mark.parametrize("lat,lon", [(79.5, 29.2), (0.0, 0.0), (29.2, None)])
    def test_swapped_partial_or_out_of_country_rejected(self, lat, lon):
        with pytest.raises(ValidationError):
            LocationIn(latitude=lat, longitude=lon, location_source_id=1)

    def test_source_must_belong_to_facility(self, db, platform):
        a, b = _facility(db, platform, "Test Facility A"), _facility(db, platform, "Test Facility B")
        src_b = _source(db, b, platform)
        with pytest.raises(ServiceError, match="does not belong"):
            svc.set_location(db, a, LocationIn(latitude=29.2, longitude=79.5, location_source_id=src_b.id), platform)

    def test_clearing_coordinates_clears_source(self, db, platform):
        f = _facility(db, platform)
        src = _source(db, f, platform)
        svc.set_location(db, f, LocationIn(latitude=29.2, longitude=79.5, location_source_id=src.id), platform)
        svc.set_location(db, f, LocationIn(), platform)
        assert f.latitude is None and f.location_source_id is None


class TestSourcesAndContacts:
    def test_web_source_requires_url(self):
        with pytest.raises(ValidationError, match="URL"):
            SourceIn(source_type="government_portal", title="Portal entry", reference="not a url", retrieved_on=TODAY)

    def test_future_retrieval_rejected(self):
        with pytest.raises(ValidationError):
            SourceIn(source_type="government_document", title="Doc", reference="Ref 1",
                     retrieved_on=TODAY + timedelta(days=1))

    def test_contact_normalised_and_duplicates_refused(self, db, platform):
        f = _facility(db, platform)
        src = _source(db, f, platform)
        c = svc.add_contact(db, f, ContactIn(kind="phone", value="98765-00012", source_id=src.id), platform)
        assert c.value == "9876500012" and not c.is_public              # private unless explicitly public
        with pytest.raises(ServiceError, match="already recorded"):
            svc.add_contact(db, f, ContactIn(kind="phone", value="98765 00012", source_id=src.id), platform)

    @pytest.mark.parametrize("kind,value", [("email", "not-an-email"), ("website", "example.org"), ("phone", "abc")])
    def test_invalid_contact_values(self, kind, value):
        with pytest.raises(ValidationError):
            ContactIn(kind=kind, value=value, source_id=1)


class TestVerificationRules:
    def test_third_party_cannot_source_confirm(self, db, platform):
        f = _facility(db, platform)
        lead = _source(db, f, platform, "third_party_directory")
        with pytest.raises(ServiceError, match="official source"):
            _verify(db, f, platform, source=lead)

    @pytest.mark.parametrize("stype", ["government_portal", "government_document", "hospital_website"])
    def test_official_sources_can_source_confirm(self, db, platform, stype):
        f = _facility(db, platform)
        _verify(db, f, platform, source=_source(db, f, platform, stype))
        assert svc.scope_status(f, VerificationScope.IDENTITY).status == V.SOURCE_CONFIRMED

    def test_facility_confirmed_needs_direct_contact_evidence(self, db, platform):
        f = _facility(db, platform)
        gov = _source(db, f, platform)
        with pytest.raises(ServiceError, match="direct contact"):
            _verify(db, f, platform, status="facility_confirmed", method="phone_call", source=gov)
        letter = _source(db, f, platform, "facility_confirmation", "Email from blood bank officer", "Email 2026-11-10")
        with pytest.raises(ServiceError, match="direct contact"):
            _verify(db, f, platform, status="facility_confirmed", method="official_source_check", source=letter)
        _verify(db, f, platform, status="facility_confirmed", method="email", source=letter)
        assert svc.scope_status(f, VerificationScope.IDENTITY).status == V.FACILITY_CONFIRMED

    def test_field_verified_only_for_location_after_site_visit(self, db, platform):
        f = _facility(db, platform)
        visit = _source(db, f, platform, "site_visit", "Site visit notes", "Visit 2026-11-12, GPS reading")
        with pytest.raises(ServiceError, match="location"):
            _verify(db, f, platform, scope="identity", status="field_verified", method="site_visit", source=visit)
        with pytest.raises(ServiceError, match="coordinates"):  # no coordinates yet
            _verify(db, f, platform, scope="location", status="field_verified", method="site_visit", source=visit)
        svc.set_location(db, f, LocationIn(latitude=29.2, longitude=79.5, location_source_id=visit.id), platform)
        _verify(db, f, platform, scope="location", status="field_verified", method="site_visit", source=visit)
        assert svc.scope_status(f, VerificationScope.LOCATION).status == V.FIELD_VERIFIED

    def test_outdated_cannot_be_recorded_and_future_dates_rejected(self):
        base = dict(scope="identity", method="other", evidence_note="Some evidence text")
        with pytest.raises(ValidationError, match="calculated automatically"):
            VerificationIn(status="outdated", verified_on=TODAY, **base)
        with pytest.raises(ValidationError, match="future"):
            VerificationIn(status="unverified", verified_on=TODAY + timedelta(days=1), **base)

    def test_contact_scope_requires_contact(self):
        with pytest.raises(ValidationError, match="contact"):
            VerificationIn(scope="contact", status="unverified", method="other", verified_on=TODAY,
                           evidence_note="Some evidence text")

    def test_latest_check_wins_and_old_checks_become_outdated(self, db, platform):
        f = _facility(db, platform)
        gov = _source(db, f, platform)
        _verify(db, f, platform, source=gov, on=TODAY - timedelta(days=10))
        _verify(db, f, platform, status="disputed", method="phone_call", note="Number rings at a different hospital")
        assert svc.scope_status(f, VerificationScope.IDENTITY).status == V.DISPUTED

        g = _facility(db, platform, "Test Facility Old")
        _verify(db, g, platform, source=_source(db, g, platform),
                on=TODAY - timedelta(days=settings.verification_max_age_days + 1))
        assert svc.scope_status(g, VerificationScope.IDENTITY).status == V.OUTDATED

    def test_contacts_are_verified_individually(self, db, platform):
        f = _facility(db, platform)
        gov = _source(db, f, platform)
        c1 = svc.add_contact(db, f, ContactIn(kind="phone", value="9800000011", source_id=gov.id), platform)
        c2 = svc.add_contact(db, f, ContactIn(kind="phone", value="9800000012", source_id=gov.id), platform)
        _verify(db, f, platform, scope="contact", contact=c1, source=gov)
        db.refresh(f)
        assert svc.contact_status(c1).status == V.SOURCE_CONFIRMED
        assert svc.contact_status(c2).status == V.UNVERIFIED


class TestListingAndMap:
    def test_blockers_until_requirements_met(self, db, platform):
        f = _facility(db, platform)
        assert len(svc.listing_blockers(f)) == 3        # identity, address, phone
        with pytest.raises(ServiceError, match="Cannot list"):
            svc.set_listed(db, f, True, platform)

    def test_listable_facility_can_be_listed_and_unlisted(self, db, platform):
        f, _, _ = _listable(db, platform)
        svc.set_listed(db, f, True, platform)
        assert f.is_listed
        svc.set_listed(db, f, False, platform)
        assert not f.is_listed

    def test_private_phone_does_not_count(self, db, platform):
        f = _facility(db, platform, address_line="1 Test Road", city="Testville")
        gov = _source(db, f, platform)
        _verify(db, f, platform, source=gov)
        svc.add_contact(db, f, ContactIn(kind="phone", value="9800000013", source_id=gov.id), platform)
        assert any("public phone" in b for b in svc.listing_blockers(f))

    def test_disputed_identity_auto_unlists(self, db, platform):
        f, _, _ = _listable(db, platform)
        svc.set_listed(db, f, True, platform)
        _verify(db, f, platform, status="disputed", method="phone_call", note="Facility says it has closed")
        assert not f.is_listed

    def test_retiring_last_public_phone_auto_unlists(self, db, platform):
        f, _, phone = _listable(db, platform)
        svc.set_listed(db, f, True, platform)
        svc.retire_contact(db, f, phone.id, platform)
        assert not f.is_listed

    def test_closed_facility_cannot_be_listed(self, db, platform):
        f, _, _ = _listable(db, platform)
        svc.update_facility(db, f, FacilityIn(display_name=f.display_name, address_line="1 Test Road",
                                              city="Testville", operational_status=OperationalStatus.CLOSED), platform)
        assert any("closed" in b for b in svc.listing_blockers(f))

    def test_map_marker_needs_confirmed_location(self, db, platform):
        f, gov, _ = _listable(db, platform)
        svc.set_listed(db, f, True, platform)
        assert not svc.map_eligible(f)                  # no coordinates
        osm = _source(db, f, platform, "openstreetmap", "OSM node")
        svc.set_location(db, f, LocationIn(latitude=29.2, longitude=79.5, location_source_id=osm.id), platform)
        db.refresh(f)
        assert not svc.map_eligible(f)                  # coordinates but location unverified
        with pytest.raises(ServiceError):               # OSM alone cannot confirm
            _verify(db, f, platform, scope="location", source=osm)
        _verify(db, f, platform, scope="location", source=gov)
        db.refresh(f)
        assert svc.map_eligible(f)


# ================================================================ HTTP workflow
class TestPlatformWorkflowOverHttp:
    def _post(self, client, url, data, page):
        return client.post(url, data={**data, "csrf_token": csrf_from(client.get(page).text)}, follow_redirects=False)

    def test_full_workflow(self, client, db):
        login(client, "platformadmin")
        r = self._post(client, "/platform/facilities/new", {"display_name": "Test Facility Http", "ownership":
                       "charitable", "operational_status": "active", "address_line": "2 Test Lane", "city": "Testville",
                       "has_apheresis": "true"}, "/platform/facilities/new")
        assert r.status_code == 303
        f = db.scalar(select(Facility).where(Facility.display_name == "Test Facility Http"))
        assert f.has_apheresis is True and f.has_component_separation is None
        page = f"/platform/facilities/{f.id}"
        assert self._post(client, f"{page}/sources", {"source_type": "government_document", "title": "Test list",
                          "reference": "Test ref", "retrieved_on": TODAY.isoformat()}, page).status_code == 303
        db.expire_all()
        src = db.get(Facility, f.id).sources[0]
        assert self._post(client, f"{page}/contacts", {"kind": "phone", "value": "9800000014", "source_id": src.id,
                          "is_public": "true"}, page).status_code == 303
        assert self._post(client, f"{page}/verifications", {"scope": "identity", "status": "source_confirmed",
                          "method": "official_source_check", "source_id": src.id, "verified_on": TODAY.isoformat(),
                          "evidence_note": "Name and address match the test list"}, page).status_code == 303
        assert self._post(client, f"{page}/listing", {"listed": "true"}, page).status_code == 303
        db.expire_all()
        assert db.get(Facility, f.id).is_listed
        html = client.get(page).text
        assert "Source confirmed" in html and "Listed" in html

    def test_invalid_section_form_shows_errors(self, client, db, platform):
        f = _facility(db, platform)
        login(client, "platformadmin")
        page = f"/platform/facilities/{f.id}"
        r = self._post(client, f"{page}/verifications", {"scope": "identity", "status": "source_confirmed",
                       "method": "official_source_check", "verified_on": TODAY.isoformat(),
                       "evidence_note": "No source given here"}, page)
        assert r.status_code == 400 and "official source" in r.text
        assert db.scalar(select(func.count()).select_from(FacilityVerification)) == 0


# ===================================================================== scripts
def test_create_platform_admin_script(db, capsys):
    from scripts.create_platform_admin import main

    answers = iter(["Strong@Pass1", "Different@1"])
    assert main(["--username", "plat.one", "--full-name", "Plat One"], lambda _: next(answers)) == 1
    answers = iter(["Strong@Pass1", "Strong@Pass1"])
    assert main(["--username", "plat.one", "--full-name", "Plat One"], lambda _: next(answers)) == 0
    out = capsys.readouterr()
    assert "Strong@Pass1" not in out.out + out.err                     # password never printed
    from app.database import SessionLocal
    with SessionLocal() as s:
        assert s.scalar(select(User.role).where(User.username == "plat.one")) == Role.PLATFORM_ADMIN


def test_backup_script_verifies_copy(tmp_path, monkeypatch):
    from scripts import backup_db

    src = tmp_path / "live.db"
    eng = make_engine(f"sqlite:///{src.as_posix()}")
    init_db(eng)
    eng.dispose()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{src.as_posix()}")
    dest = backup_db.backup("test", tmp_path / "bk")
    import sqlite3
    with sqlite3.connect(dest) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT count(*) FROM facilities").fetchone()[0] == 1
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_login_page_and_password_change_work_for_platform_admin(client):
    login(client, "platformadmin")
    assert client.get("/account/password").status_code == 200
    assert client.get("/platform/facilities").status_code == 200
    assert PASSWORD  # fixture password used for every role
