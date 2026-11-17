"""Haldwani BloodLink Phase 2: public landing page, directory, map data and facility pages.

All facilities here are fictional test data (example.org sources).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from app.config import settings
from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, SourceIn, VerificationIn
from app.services import facilities as fsvc
from app.services import public as pub
from app.utils import business_today
from tests.conftest import login

TODAY = business_today()


def _src(db, f, actor, stype="government_document", title="Test official list", ref="Test ref"):
    if stype in ("government_portal", "hospital_website", "openstreetmap", "third_party_directory"):
        ref = "https://example.org/test"
    return fsvc.add_source(db, f, SourceIn(source_type=stype, title=title, reference=ref, retrieved_on=TODAY), actor)


def _check(db, f, actor, scope, source, contact=None, on=TODAY, status="source_confirmed",
           method="official_source_check"):
    fsvc.record_verification(db, f, VerificationIn(
        scope=scope, status=status, method=method, source_id=source.id, contact_id=contact.id if contact else None,
        verified_on=on, evidence_note="Checked against the test source"), actor)


def make_listed(db, actor, name, *, coords=None, location_verified=False, phone="9800000101",
                phone_confirmed=False, list_it=True, identity_on=TODAY):
    f = fsvc.create_facility(db, FacilityIn(display_name=name, official_name=f"{name} Official",
                                            ownership="charitable", address_line="1 Test Road", city="Testville"), actor)
    gov = _src(db, f, actor)
    _check(db, f, actor, "identity", gov, on=identity_on)
    c = fsvc.add_contact(db, f, ContactIn(kind="phone", value=phone, source_id=gov.id, is_public=True), actor)
    if phone_confirmed:
        _check(db, f, actor, "contact", gov, contact=c)
    if coords:
        fsvc.set_location(db, f, LocationIn(latitude=coords[0], longitude=coords[1], location_source_id=gov.id), actor)
        if location_verified:
            _check(db, f, actor, "location", gov)
    if list_it:
        fsvc.set_listed(db, f, True, actor)
    db.refresh(f)
    return f


@pytest.fixture()
def world(db, users):
    """A: listed + verified location; B: listed, coordinates NOT verified; C: confirmed but unlisted."""
    p = users["platform_admin"]
    a = make_listed(db, p, "Test Facility Alpha", coords=(29.21, 79.51), location_verified=True, phone_confirmed=True)
    b = make_listed(db, p, "Test Facility Beta", coords=(29.2345, 79.4876), location_verified=False, phone="9800000102")
    c = make_listed(db, p, "Test Facility Gamma", phone="9800000103", list_it=False)
    return {"a": a, "b": b, "c": c, "platform": p}


def _map_data(html: str) -> list[dict]:
    m = re.search(r'<script id="facility-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


# ===================================================== pre-tasks (Phase 1 fixes)
def test_location_form_prefills_saved_coordinates(client, world):
    login(client, "platformadmin")
    html = client.get(f"/platform/facilities/{world['a'].id}").text
    assert 'name="latitude" value="29.21"' in html and 'name="longitude" value="79.51"' in html
    assert re.search(r'<option value="\d+" selected>Test official list', html)


def test_branding(client):
    assert settings.app_name == "Blood Bank Management System"
    assert settings.app_subtitle == "Haldwani Blood Bank Directory and Management Platform"
    page = client.get("/login").text
    assert "Blood Bank Management System" in page and settings.app_subtitle in page
    for url in ("/", "/find", "/find/about-data"):
        html = client.get(url).text
        assert "BloodLink" not in html and "Blood Bank Management System" in html and settings.app_subtitle in html
    login(client, "staff")
    staff = client.get("/donors").text
    assert "Blood Bank Management System</a>" in staff and "BloodLink" not in staff


# ================================================================ public access
class TestPublicAccess:
    @pytest.mark.parametrize("path", ["/", "/find", "/find/about-data", "/api/public/facilities"])
    def test_open_without_login_and_sets_no_cookie(self, client, world, path):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200
        assert "set-cookie" not in r.headers            # anonymous visitors get no session cookie

    def test_facility_page_open_without_login(self, client, world):
        r = client.get(f"/find/{world['a'].slug}")
        assert r.status_code == 200 and "set-cookie" not in r.headers

    def test_staff_dashboard_and_platform_redirect_unchanged(self, client, world):
        login(client, "staff")
        assert "Recent donations" in client.get("/").text
        client.cookies.clear()
        login(client, "platformadmin")
        assert client.get("/", follow_redirects=False).headers["location"] == "/platform/facilities"

    def test_empty_directory_is_honest(self, client):
        html = client.get("/find").text
        assert "No blood banks are listed yet" in html
        assert _map_data(html) == []
        assert "0</div>" in client.get("/").text        # landing shows zero listed


# ============================================================ visibility rules
class TestVisibility:
    def test_only_listed_facilities_are_public(self, client, world):
        html = client.get("/find").text
        assert "Test Facility Alpha" in html and "Test Facility Beta" in html
        assert "Demo Blood Centre" not in html
        # Gamma (identity confirmed, NOT listed) appears only under "Under verification": name and sources,
        # never its unverified phone, and never in the map data.
        listed_part, _, candidate_part = html.partition('id="under-verification"')
        assert "Test Facility Gamma" not in listed_part and "Test Facility Gamma" in candidate_part
        assert "9800000103" not in html
        assert world["c"].slug not in {d["slug"] for d in _map_data(html)}
        page = client.get(f"/find/{world['c'].slug}")
        assert page.status_code == 200 and "Under verification" in page.text and "9800000103" not in page.text
        assert client.get("/find/demo-blood-centre").status_code == 404

    def test_facility_with_outdated_identity_is_hidden(self, client, db, users, monkeypatch):
        f = make_listed(db, users["platform_admin"], "Test Facility Ageing", identity_on=TODAY - timedelta(days=40))
        assert client.get(f"/find/{f.slug}").status_code == 200
        monkeypatch.setattr(settings, "verification_max_age_days", 30)       # the check is now outdated
        assert client.get(f"/find/{f.slug}").status_code == 404
        assert "Test Facility Ageing" not in client.get("/find").text

    def test_reserved_slug_never_shadows_public_pages(self, db, users):
        f = fsvc.create_facility(db, FacilityIn(display_name="About Data"), users["platform_admin"])
        assert f.slug == "about-data-2"


# ================================================================== map data
class TestMapData:
    def test_markers_only_for_verified_locations(self, client, world):
        data = {d["slug"]: d for d in _map_data(client.get("/find").text)}
        assert (data[world["a"].slug]["lat"], data[world["a"].slug]["lon"]) == (29.21, 79.51)
        assert data[world["b"].slug]["lat"] is None and data[world["b"].slug]["lon"] is None   # has coords, unverified
        assert data[world["b"].slug]["location_verified"] is False

    def test_unverified_coordinates_never_reach_the_page(self, client, world):
        for path in ("/find", f"/find/{world['b'].slug}", "/api/public/facilities"):
            html = client.get(path).text
            assert "79.4876" not in html and "29.2345" not in html

    def test_geojson(self, client, world):
        fc = client.get("/api/public/facilities").json()
        feats = {f["properties"]["slug"]: f for f in fc["features"]}
        assert set(feats) == {world["a"].slug, world["b"].slug}
        assert feats[world["a"].slug]["geometry"] == {"type": "Point", "coordinates": [79.51, 29.21]}
        assert feats[world["b"].slug]["geometry"] is None

    def test_directions_links(self, client, world):
        html = client.get(f"/find/{world['a'].slug}").text
        assert "https://www.google.com/maps/dir/?api=1&amp;destination=29.210000%2C79.510000" in html
        assert "https://www.openstreetmap.org/directions?route=%3B29.210000%2C79.510000" in html
        b = client.get(f"/find/{world['b'].slug}").text
        assert "google.com/maps/dir" not in b and "openstreetmap.org/search?query=" in b

    def test_leaflet_pinned_and_osm_attributed(self, client, world):
        html = client.get("/find").text
        assert 'leaflet/1.9.4/leaflet.js"' in html and 'integrity="sha512-' in html
        # Default base map: OpenFreeMap vector style via MapLibre (pinned + SRI), raster fallback configured.
        assert 'data-provider="openfreemap"' in html and "tiles.openfreemap.org/styles/liberty" in html
        assert 'maplibre-gl/4.7.1/maplibre-gl.js"' in html and "maplibre-gl-leaflet@0.0.22" in html
        assert html.count('integrity="sha384-') >= 3
        assert "OpenFreeMap" in html and "OpenMapTiles" in html and "OpenStreetMap" in html


# ================================================================ contacts
class TestContacts:
    def test_unverified_phone_is_never_presented_as_confirmed(self, client, world):
        a = client.get(f"/find/{world['a'].slug}").text
        b = client.get(f"/find/{world['b'].slug}").text
        assert "9800000101" in a and "Confirmed" in a
        assert "9800000102" in b and "Not yet confirmed" in b
        # Beta's only phone is unverified, so its page must not carry a "Confirmed" chip at all.
        assert "bi-patch-check-fill\"></i> Confirmed" not in b

    def test_private_and_retired_contacts_hidden(self, client, db, world):
        p, a = world["platform"], world["a"]
        gov = a.sources[0]
        fsvc.add_contact(db, a, ContactIn(kind="phone", value="9800000199", source_id=gov.id, is_public=False), p)
        retired = fsvc.add_contact(db, a, ContactIn(kind="phone", value="9800000198", source_id=gov.id, is_public=True), p)
        fsvc.retire_contact(db, a, retired.id, p)
        html = client.get(f"/find/{a.slug}").text
        assert "9800000199" not in html and "9800000198" not in html

    def test_direct_communication_reference_not_exposed(self, client, db, world):
        p, a = world["platform"], world["a"]
        fsvc.add_source(db, a, SourceIn(source_type="facility_confirmation", title="Call with Mr Test Officer",
                                        reference="Mobile 9800000777, call log 12", retrieved_on=TODAY), p)
        html = client.get(f"/find/{a.slug}").text
        assert "Confirmed directly with the facility" in html
        assert "Mr Test Officer" not in html and "9800000777" not in html


# ============================================================ availability
class TestAvailability:
    def test_filters_never_invent_stock(self, client, world):
        # No facility in this fixture has published anything (Phase 4 publishing is tested in test_availability.py).
        html = client.get("/find?group=B%2B&component=whole_blood").text
        assert "No participating blood bank has published current stock" in html
        assert "does not mean blood is unavailable" in html
        assert "We never estimate stock" in html
        assert "Test Facility Alpha" in html                     # still listed to call directly
        html = client.get("/find?group=B%2B&component=platelets").text
        assert "isn't tracked here" in html                      # untracked component: explicit, not "available"

    def test_invalid_filter_ignored(self, client, world):
        r = client.get("/find?group=Z%2B&component=gold")
        assert r.status_code == 200 and "isn't recognised" in r.text

    def test_unpublished_facilities_have_no_current_availability(self, db, world):
        for f in (world["a"], world["b"]):
            assert not pub.availability(db, pub.to_public(f)).is_current


# ======================================================== isolation & safety
class TestIsolationAndSafety:
    def test_no_operational_data_on_public_surfaces(self, client, world, make_donor):
        make_donor(name="Zed Operational Donor")
        for path in ("/", "/find", f"/find/{world['a'].slug}", "/find/about-data", "/api/public/facilities"):
            assert "Zed Operational Donor" not in client.get(path).text

    def test_names_are_escaped_in_html_and_json(self, client, db, users):
        f = make_listed(db, users["platform_admin"], "<script>alert(1)</script> Test",
                        coords=(29.21, 79.51), location_verified=True)
        for path in ("/find", f"/find/{f.slug}"):
            html = client.get(path).text
            assert "<script>alert(1)</script>" not in html
        assert any(d["name"].startswith("<script>") for d in _map_data(client.get("/find").text))  # JSON round-trips safely

    def test_operational_api_still_protected(self, client):
        assert client.get("/api/stats/inventory").status_code == 401
