"""scripts/load_facilities.py: dry run writes nothing, rules are enforced, unverified entries get no marker,
demo stock is never attributed to a loaded facility, and re-running is idempotent.

All facility data here is fictional (example.org).
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import BloodUnit, Facility
from app.services import facilities as fsvc
from app.services import public as pub
from app.utils import business_today
from scripts import load_facilities as loader

TODAY = business_today().isoformat()
GOV = {"key": "gov", "source_type": "government_document", "title": "Fictional state list", "reference": "Doc 1",
       "retrieved_on": TODAY}
OSM = {"key": "osm", "source_type": "openstreetmap", "title": "Fictional OSM node",
       "reference": "https://example.org/osm/1", "retrieved_on": TODAY}


def _verified(name="Fictional Verified Centre"):
    return {"display_name": name, "official_name": f"{name} (official)", "aliases": ["FVC", "Verified BC"],
            "ownership": "government", "address_line": "1 Example Road", "city": "Exampleton",
            "sources": [GOV, OSM],
            "contacts": [{"key": "p", "kind": "phone", "value": "9800000999", "source": "gov", "is_public": True}],
            "location": {"latitude": 29.22, "longitude": 79.52, "source": "osm"},
            "verifications": [
                {"scope": "identity", "status": "source_confirmed", "method": "official_source_check",
                 "source": "gov", "verified_on": TODAY, "evidence_note": "Named in the fictional state list"},
                {"scope": "location", "status": "source_confirmed", "method": "official_source_check",
                 "source": "gov", "verified_on": TODAY, "evidence_note": "Address matches the fictional list"}],
            "list": True}


def _candidate(name="Fictional Candidate Centre"):
    """Name + source only: no address, no phone, no coordinates, no verification."""
    return {"display_name": name, "aliases": ["FCC"], "sources": [GOV]}


def _write(tmp_path, entries):
    p = tmp_path / "facilities.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def _count(db):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Facility).where(Facility.is_demo.is_(False)))


@pytest.fixture()
def admin(users):
    return users["platform_admin"].username


def test_dry_run_writes_nothing(engine, db, admin, tmp_path, capsys):
    assert loader.run(_write(tmp_path, [_verified(), _candidate()]), admin, apply=False, eng=engine) == 0
    assert "Dry run" in capsys.readouterr().out
    assert _count(db) == 0


def test_apply_creates_and_enforces_marker_rules(engine, db, admin, tmp_path):
    assert loader.run(_write(tmp_path, [_verified(), _candidate()]), admin, apply=True, eng=engine) == 0
    assert _count(db) == 2
    db.expire_all()
    ver = db.scalar(select(Facility).where(Facility.display_name == "Fictional Verified Centre"))
    cand = db.scalar(select(Facility).where(Facility.display_name == "Fictional Candidate Centre"))
    assert ver.alias_list == ["FVC", "Verified BC"]
    assert ver.is_listed and fsvc.map_eligible(ver)
    assert not cand.is_listed and not cand.has_coordinates and not fsvc.map_eligible(cand)
    # Only the listed + verified facility is public; the candidate never reaches the map data.
    public = {f.slug: f for f in pub.public_facilities(db)}
    assert set(public) == {ver.slug}
    assert public[ver.slug].location_verified


def test_loaded_facilities_never_receive_demo_stock(engine, db, admin, tmp_path):
    loader.run(_write(tmp_path, [_verified(), _candidate()]), admin, apply=True, eng=engine)
    db.expire_all()
    real_ids = list(db.scalars(select(Facility.id).where(Facility.is_demo.is_(False))))
    assert db.scalar(select(func.count()).select_from(BloodUnit).where(BloodUnit.facility_id.in_(real_ids))) == 0


def test_rerun_is_idempotent(engine, db, admin, tmp_path, capsys):
    path = _write(tmp_path, [_verified(), _candidate()])
    loader.run(path, admin, apply=True, eng=engine)
    assert loader.run(path, admin, apply=True, eng=engine) == 0
    assert capsys.readouterr().out.count("skipped (already present)") == 2
    assert _count(db) == 2


@pytest.mark.parametrize("bad", [
    {**_verified("Bad Coords"), "location": {"latitude": 29.2, "longitude": 79.5}},            # no source key
    {**_candidate("Bad Listing"), "list": True},                                                # listing blockers
    {**_candidate("Bad Status"), "verifications": [{"scope": "identity", "status": "source_confirmed",
     "method": "official_source_check", "source": None, "verified_on": TODAY,
     "evidence_note": "No source attached at all"}]},                                           # unsourced confirm
])
def test_invalid_entry_rolls_back_whole_file(engine, db, admin, tmp_path, bad):
    assert loader.run(_write(tmp_path, [_verified(), bad]), admin, apply=True, eng=engine) == 1
    assert _count(db) == 0                                                                       # nothing kept


def _expired(name="Fictional Expired Centre"):
    e = _verified(name)
    old = (business_today() - timedelta(days=400)).isoformat()
    for v in e["verifications"]:
        v["verified_on"] = old
    return e


def test_expired_listing_fails_by_default(engine, db, admin, tmp_path):
    assert loader.run(_write(tmp_path, [_expired()]), admin, apply=True, eng=engine) == 1
    assert _count(db) == 0                                                # CLI stays strict: nothing loaded


def test_expired_listing_can_be_loaded_unlisted(engine, db, admin, tmp_path, capsys):
    assert loader.run(_write(tmp_path, [_expired()]), admin, apply=True, eng=engine, expired="unlist") == 0
    assert "NOT listed: verification expired" in capsys.readouterr().out
    db.expire_all()
    f = db.scalar(select(Facility).where(Facility.display_name == "Fictional Expired Centre"))
    assert not f.is_listed and f.verifications                          # evidence kept, dates unchanged


def test_unlist_mode_never_hides_data_errors(engine, db, admin, tmp_path):
    bad = {**_expired("Fictional Broken Centre"), "address_line": None}
    assert loader.run(_write(tmp_path, [bad]), admin, apply=True, eng=engine, expired="unlist") == 1
    assert _count(db) == 0


def test_requires_platform_admin(engine, db, users, tmp_path):
    assert loader.run(_write(tmp_path, [_candidate()]), users["admin"].username, apply=True, eng=engine) == 1
    assert _count(db) == 0
