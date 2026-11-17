"""'Under verification' section: real facilities named in an official source but not yet listable.

They are public by name and provenance only: never a phone, address, coordinates, map pin or stock.
All facility data here is fictional (example.org / 98000... numbers).
"""
from __future__ import annotations

import pytest

from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, VerificationIn
from app.services import facilities as fsvc
from app.services import public as pub
from tests.test_public import TODAY, _check, _src, make_listed

SECRET_PHONE = "9800000777"


def _candidate(db, actor, name, *, identity="source_confirmed", with_extras=False, status="unknown"):
    f = fsvc.create_facility(db, FacilityIn(display_name=name, official_name=f"{name} Official", aliases="Alias One",
                                            district="Exampledist", state="Examplestate",
                                            operational_status=status), actor)
    gov = _src(db, f, actor, stype="government_portal", title="Fictional state list")
    if identity == "source_confirmed":
        _check(db, f, actor, "identity", gov)
    elif identity == "disputed":
        _check(db, f, actor, "identity", gov, status="disputed")
    if with_extras:   # unverified details that must never leak through the candidate view
        fsvc.add_contact(db, f, ContactIn(kind="phone", value=SECRET_PHONE, source_id=gov.id, is_public=True), actor)
        fsvc.set_location(db, f, LocationIn(latitude=29.25, longitude=79.55, location_source_id=gov.id), actor)
    db.refresh(f)
    return f


@pytest.fixture()
def pa(users):
    return users["platform_admin"]


def test_candidate_listed_on_find_with_badges(client, db, pa):
    _candidate(db, pa, "Fictional Candidate One")
    r = client.get("/find")
    assert r.status_code == 200
    assert 'id="under-verification"' in r.text and "Fictional Candidate One" in r.text
    assert "Identity checked against an official source" in r.text
    assert "Contact &amp; location not yet verified" in r.text


def test_landing_counts_candidates_separately(client, db, pa):
    _candidate(db, pa, "Fictional Candidate Landing")
    html = client.get("/").text
    assert "under verification</a>" in html and "<strong>1</strong> more blood" in html


def test_candidate_page_shows_sources_but_no_contact_or_location(client, db, pa):
    f = _candidate(db, pa, "Fictional Candidate Two", with_extras=True)
    r = client.get(f"/find/{f.slug}")
    assert r.status_code == 200
    assert "https://example.org/test" in r.text and "Fictional state list" in r.text
    assert "Alias One" in r.text and "Exampledist" in r.text
    assert SECRET_PHONE not in r.text and "tel:" not in r.text
    assert "29.25" not in r.text and "79.55" not in r.text and "leaflet" not in r.text.lower()
    assert SECRET_PHONE not in client.get("/find").text


def test_candidates_never_reach_map_data_or_api(client, db, pa):
    f = _candidate(db, pa, "Fictional Candidate Three", with_extras=True)
    assert client.get("/api/public/facilities").json()["features"] == []
    assert all(x.slug != f.slug for x in pub.public_facilities(db))
    assert f.slug not in client.get("/api/public/availability").text


@pytest.mark.parametrize("identity,status", [("none", "unknown"), ("disputed", "unknown"),
                                             ("source_confirmed", "closed")])
def test_unconfirmed_disputed_or_closed_are_hidden(client, db, pa, identity, status):
    f = _candidate(db, pa, "Fictional Hidden", identity=identity, status=status)
    assert pub.candidate_facilities(db) == []
    assert "Fictional Hidden" not in client.get("/find").text
    assert client.get(f"/find/{f.slug}").status_code == 404


def test_demo_facility_is_never_a_candidate(client, db, users):
    assert pub.candidate_facilities(db) == []
    assert client.get("/find/demo-blood-centre").status_code == 404


def test_listed_facility_is_not_duplicated_as_candidate(client, db, pa):
    f = make_listed(db, pa, "Fictional Listed")
    _candidate(db, pa, "Fictional Candidate Four")
    assert [c.name for c in pub.candidate_facilities(db)] == ["Fictional Candidate Four"]
    assert client.get(f"/find/{f.slug}").status_code == 200           # normal listed page still works


def test_candidate_becomes_listed_page_once_verified(client, db, pa):
    f = _candidate(db, pa, "Fictional Promoted")
    f.address_line, f.city = "1 Example Road", "Exampleton"
    db.commit()
    gov = f.sources[0]
    fsvc.add_contact(db, f, ContactIn(kind="phone", value="9800000555", source_id=gov.id, is_public=True), pa)
    fsvc.set_listed(db, f, True, pa)
    assert pub.candidate_facilities(db) == []
    assert "9800000555" in client.get(f"/find/{f.slug}").text


def test_public_pages_need_no_login_and_staff_pages_still_do(client, db, pa):
    f = _candidate(db, pa, "Fictional Anonymous")
    for url in ("/", "/find", f"/find/{f.slug}", "/find/about-data", "/api/public/facilities"):
        assert client.get(url, follow_redirects=False).status_code == 200, url
    for url in ("/donors", "/inventory", "/requests", "/platform/facilities"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code in (302, 303) and "/login" in r.headers["location"], url
