"""Load reviewed, real facility directory entries from a JSON file.

Usage:
    python -m scripts.load_facilities data/haldwani_facilities.json --actor <platform-admin-username>            # dry run
    python -m scripts.load_facilities data/haldwani_facilities.json --actor <platform-admin-username> --apply    # write

Every write goes through app.services.facilities, so all provenance rules apply unchanged:
coordinates need a source, "source confirmed" needs an official source, listing needs a confirmed
identity + address + a sourced public phone, and a map marker needs a positive location check.
Nothing is inferred: a field that is absent in the file stays empty.

Idempotent: a facility whose official_name (or display_name) already exists is skipped.
A dry run validates everything inside a transaction that is rolled back.
Before --apply on a SQLite database, a verified backup is made (scripts/backup_db.py).

File format (a JSON list; only display_name is required):
{
  "display_name": "...", "official_name": "...", "aliases": ["..."], "ownership": "government",
  "parent_hospital": "...", "address_line": "...", "locality": "...", "city": "Haldwani",
  "district": "Nainital", "state": "Uttarakhand", "pincode": "263139", "eraktkosh_code": "...",
  "cdsco_license_no": "...", "operational_status": "unknown", "notes": "...",
  "sources": [{"key": "s1", "source_type": "government_document", "title": "...", "reference": "...",
               "retrieved_on": "2026-11-17", "notes": "..."}],
  "contacts": [{"key": "c1", "kind": "phone", "value": "...", "label": "...", "source": "s1", "is_public": true}],
  "location": {"latitude": 29.2, "longitude": 79.5, "source": "s1"},
  "verifications": [{"scope": "identity", "status": "source_confirmed", "method": "official_source_check",
                     "source": "s1", "contact": null, "verified_on": "2026-11-17", "evidence_note": "..."}],
  "list": false
}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import or_, select

from app.database import SessionLocal, engine, init_db
from app.models import Facility, Role, User, VerificationScope
from app.schemas.facilities import ContactIn, FacilityIn, LocationIn, SourceIn, VerificationIn
from app.services import facilities as fsvc
from app.services.errors import ServiceError

FACILITY_FIELDS = set(FacilityIn.model_fields)


class _DryRun(Exception):
    pass


def _facility_in(entry: dict) -> FacilityIn:
    data = {k: v for k, v in entry.items() if k in FACILITY_FIELDS}
    if isinstance(data.get("aliases"), list):
        data["aliases"] = "\n".join(a.strip() for a in data["aliases"] if a and a.strip()) or None
    return FacilityIn.model_validate(data)


def _exists(db, data: FacilityIn) -> Facility | None:
    names = [n for n in (data.official_name, data.display_name) if n]
    return db.scalar(select(Facility).where(or_(Facility.official_name.in_(names), Facility.display_name.in_(names))))


def only_blocked_by_expiry(f: Facility) -> bool:
    """True if the facility met every listing rule on the day its identity was last confirmed, so the only
    reason it cannot be listed today is that the verification has expired (re-verification due)."""
    entry = fsvc.scope_status(f, VerificationScope.IDENTITY).entry
    return (entry is not None and entry.status in fsvc.CONFIRMED_IDENTITY
            and not fsvc.listing_blockers(f, today=entry.verified_on))


def load_entry(db, entry: dict, actor: User, expired: str = "fail") -> tuple[str, Facility | None]:
    """Create one facility with its evidence. Returns (outcome, facility).

    expired="fail" (default, the CLI): a facility marked "list" that cannot be listed aborts the whole load.
    expired="unlist" (the public demo build): if the ONLY reason is an expired verification, the facility is
    loaded but left unlisted; every other listing blocker still aborts the load.
    """
    data = _facility_in(entry)
    found = _exists(db, data)
    if found is not None:
        return "skipped (already present)", found
    f = fsvc.create_facility(db, data, actor)
    sources, contacts = {}, {}
    for s in entry.get("sources", []):
        sources[s["key"]] = fsvc.add_source(db, f, SourceIn.model_validate(
            {k: v for k, v in s.items() if k != "key"}), actor)
    for c in entry.get("contacts", []):
        contacts[c["key"]] = fsvc.add_contact(db, f, ContactIn.model_validate(
            {"kind": c["kind"], "value": c["value"], "label": c.get("label"),
             "source_id": sources[c["source"]].id, "is_public": c.get("is_public", False)}), actor)
    loc = entry.get("location")
    if loc:
        fsvc.set_location(db, f, LocationIn(latitude=loc["latitude"], longitude=loc["longitude"],
                                            location_source_id=sources[loc["source"]].id), actor)
    for v in entry.get("verifications", []):
        fsvc.record_verification(db, f, VerificationIn.model_validate({
            "scope": v["scope"], "status": v["status"], "method": v["method"],
            "source_id": sources[v["source"]].id if v.get("source") else None,
            "contact_id": contacts[v["contact"]].id if v.get("contact") else None,
            "verified_on": v["verified_on"], "evidence_note": v["evidence_note"]}), actor)
    outcome = "created"
    if entry.get("list"):
        try:
            fsvc.set_listed(db, f, True, actor)  # raises with the exact blockers if rules are not met
        except ServiceError:
            if expired != "unlist" or not only_blocked_by_expiry(f):
                raise
            outcome = "created (NOT listed: verification expired, re-verification due)"
    db.refresh(f)
    return outcome, f


def _describe(f: Facility) -> str:
    blockers = fsvc.listing_blockers(f)
    state = "LISTED" if f.is_listed else "not listed"
    marker = "map marker" if fsvc.map_eligible(f) else "no map marker"
    extra = "" if f.is_listed or not blockers else " | listing blockers: " + " ".join(blockers)
    return f"{f.display_name} [/find/{f.slug}] -> {state}, {marker}{extra}"


def run(path: Path, actor_name: str, apply: bool, eng=None, backup: bool = True, expired: str = "fail") -> int:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        print("The file must contain a non-empty JSON list.", file=sys.stderr)
        return 1
    eng = eng or engine
    init_db(eng)
    if apply and backup and eng is engine and eng.dialect.name == "sqlite":
        from scripts.backup_db import backup
        print(f"Backup: {backup('pre-facilities')}")

    conn = eng.connect()
    outer = conn.begin()
    if eng.dialect.name == "sqlite":
        # pysqlite does not emit BEGIN for conn.begin(); without it the first SAVEPOINT would open the
        # transaction and its RELEASE would commit it, defeating dry runs and all-or-nothing loading.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    # Service functions call commit(); join_transaction_mode keeps them inside `outer` (savepoints),
    # so a dry run can roll everything back and a failed --apply leaves the database untouched.
    db = SessionLocal(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        actor = db.scalar(select(User).where(User.username == actor_name))
        if actor is None or actor.role != Role.PLATFORM_ADMIN or not actor.is_active:
            print(f"'{actor_name}' is not an active platform administrator.", file=sys.stderr)
            return 1
        for i, entry in enumerate(entries, 1):
            try:
                outcome, f = load_entry(db, entry, actor, expired=expired)
            except (ValidationError, ServiceError, KeyError) as e:
                msg = getattr(e, "message", None) or str(e)
                print(f"Entry {i} ({entry.get('display_name')!r}) rejected: {msg}", file=sys.stderr)
                outer.rollback()
                print("Nothing was written.", file=sys.stderr)
                return 1
            print(f"{outcome}: {_describe(f)}")
        if apply:
            outer.commit()
            print("Applied.")
        else:
            outer.rollback()
            print("Dry run only: nothing was written. Re-run with --apply.")
        return 0
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", type=Path)
    p.add_argument("--actor", required=True, help="username of an active platform administrator")
    p.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    a = p.parse_args(argv)
    return run(a.file, a.actor, a.apply)


if __name__ == "__main__":
    sys.exit(main())
