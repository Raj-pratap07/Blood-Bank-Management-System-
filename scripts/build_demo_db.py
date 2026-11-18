"""Build the synthetic database for the PUBLIC demonstration (e.g. Render Free). Run on every start.

Usage (the deployment start command runs this before the web server):
    python -m scripts.build_demo_db

What it creates, from scratch, every time:
- synthetic operational data (donors, donations, bags, requests, issues) for the fictional
  "Demo Blood Centre", via scripts/seed.py (service layer, deterministic);
- four dedicated demonstration accounts (demo.admin, demo.doctor, demo.staff, demo.platform) whose passwords
  come ONLY from environment variables (never from the repository; never the local README passwords);
- the four NHM-listed Haldwani facility records with their sources and verification states,
  loaded from data/haldwani_facilities.json through the facility services.

Safety (it deletes a file, so it refuses unless every check passes):
- BBMS_DEMO_MODE must be true;
- BBMS_DATABASE_URL must be a SQLite file that is NOT the local bloodbank.db, not in the project folder's
  backups, and not inside the project folder at all;
- an existing file is replaced only if it carries the seed demo marker AND contains no accounts other than the
  four deployment accounts (so a local or real database can never be wiped).
Render's free filesystem is ephemeral, so the database is rebuilt on every start: demo changes do not persist.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import BASE_DIR, settings
from app.database import engine, init_db
from app.models import (AvailabilityPublication, BloodRequest, BloodUnit, Donation, Donor, Facility, Issue, Role,
                        StockVisibility, User, VerificationScope)
from app.services import availability
from app.services import facilities as fsvc
from app.utils import business_today
from scripts import load_facilities
from scripts.seed import DEMO_APPLICATION_ID, DEMO_USERS, Seeder, _mark_as_demo

FACILITIES_FILE = BASE_DIR / "data" / "haldwani_facilities.json"

# (username, full name, role, password environment variable). The first must be the facility administrator.
ACCOUNTS = [
    ("demo.admin", "Demo Administrator", Role.ADMIN, "BBMS_DEMO_PASSWORD_ADMIN"),
    ("demo.doctor", "Dr. Demo (Medical Officer)", Role.MEDICAL_OFFICER, "BBMS_DEMO_PASSWORD_DOCTOR"),
    ("demo.staff", "Demo Staff", Role.STAFF, "BBMS_DEMO_PASSWORD_STAFF"),
    ("demo.platform", "Demo Platform Administrator", Role.PLATFORM_ADMIN, "BBMS_DEMO_PASSWORD_PLATFORM"),
]
LOCAL_PASSWORDS = {pw for *_, pw in DEMO_USERS}

# Reviewed deployment-data contract for data/haldwani_facilities.json (e-RaktKosh evidence, 2026-11-17).
# Which REAL facilities may be public is a reviewed decision: changing it must change this set in code review,
# not only the data file. The build fails closed if anything below does not hold, EXCEPT that a facility whose
# verification has expired is kept unlisted (automatically hidden) instead of stopping the whole demo.
EXPECTED_FACILITIES = frozenset({
    "haldwani-city-blood-bank", "lt-sh-bal-kishan-devki-joshi-charitable-blood-bank",
    "ssj-base-haldwani", "sushila-tiwari-govt-hosp"})
OPERATIONAL = (User, Donor, Donation, BloodUnit, BloodRequest, Issue, AvailabilityPublication)
EXPIRY_WARNING_DAYS = 30
MIN_PASSWORD = 12


class Refused(Exception):
    pass


def _db_path(eng) -> Path:
    if eng.dialect.name != "sqlite" or not eng.url.database or eng.url.database == ":memory:":
        raise Refused("BBMS_DATABASE_URL must be a SQLite file URL (sqlite:////tmp/bbms-demo.db).")
    path = Path(eng.url.database).resolve()
    if path.name.lower() == "bloodbank.db":
        raise Refused("refusing to use a file named bloodbank.db (the local database name).")
    project = BASE_DIR.resolve()
    if path == project or project in path.parents:
        raise Refused(f"refusing to build inside the project folder ({path}); use e.g. /tmp/bbms-demo.db.")
    return path


def _passwords() -> dict[str, str]:
    out, problems = {}, []
    for username, _, _, env in ACCOUNTS:
        pw = os.getenv(env) or ""
        if len(pw) < MIN_PASSWORD:
            problems.append(f"{env} must be set to at least {MIN_PASSWORD} characters")
        elif pw in LOCAL_PASSWORDS:
            problems.append(f"{env} must not reuse a documented local demo password")
        elif not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)):
            problems.append(f"{env} must contain letters and digits")
        out[username] = pw
    if problems:
        raise Refused("; ".join(problems) + ".")
    return out


def _check_replaceable(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA application_id").fetchone()[0] != DEMO_APPLICATION_ID:
            raise Refused(f"{path} exists and is not a demo database; nothing was changed.")
        names = {r[0] for r in conn.execute("SELECT username FROM users")} \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone() else set()
        others = names - {a[0] for a in ACCOUNTS}
        if others:
            raise Refused(f"{path} contains accounts that are not deployment demo accounts; nothing was changed.")
    finally:
        conn.close()


def _remove(eng, path: Path) -> None:
    eng.dispose()
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def _real_facility_state(db) -> tuple[list[str], list[str]]:
    """(problems, warnings) for the real facilities. Any problem means the demo must not start."""
    today = business_today()
    real = list(db.scalars(select(Facility).where(Facility.is_demo.is_(False))))
    problems, warnings = [], []
    if {f.slug for f in real} != EXPECTED_FACILITIES:
        problems.append(f"expected exactly the reviewed facilities, found {sorted(f.slug for f in real)}")
    for f in real:
        identity = fsvc.scope_status(f, VerificationScope.IDENTITY)
        if identity.status in fsvc.CONFIRMED_IDENTITY:              # current verification: must be publicly listed
            if not f.is_listed or fsvc.listing_blockers(f):
                problems.append(f"{f.slug}: not listed, or does not meet the listing rules")
            expires = identity.entry.verified_on + timedelta(days=settings.verification_max_age_days)
            if (expires - today).days <= EXPIRY_WARNING_DAYS:
                warnings.append(f"{f.slug}: verification expires after {expires:%d %b %Y}; re-verify on e-RaktKosh")
        elif load_facilities.only_blocked_by_expiry(f):               # expired: must be hidden, never published
            if f.is_listed:
                problems.append(f"{f.slug}: verification expired but the facility is listed")
            warnings.append(f"{f.slug}: verification expired; NOT publicly listed until re-verified")
        else:
            problems.append(f"{f.slug}: identity is not confirmed")
        if f.has_coordinates or f.location_source_id is not None or fsvc.map_eligible(f):
            problems.append(f"{f.slug}: has coordinates, but no location evidence has been reviewed")
        if f.is_participating or f.stock_visibility != StockVisibility.HIDDEN:
            problems.append(f"{f.slug}: must not participate or expose stock")
        for model in OPERATIONAL:                                   # no accounts, donors, bags, requests, publications
            if db.scalar(select(func.count()).select_from(model).where(model.facility_id == f.id)):
                problems.append(f"{f.slug}: has {model.__tablename__} rows")
        for c in f.contacts:                                        # public details must have been source-checked
            status = fsvc.contact_status(c).status
            if c.is_active and c.is_public and not (status.is_positive or status.value == "outdated"):
                problems.append(f"{f.slug}: public {c.kind.value} was never confirmed against a source")
    return problems, warnings


def build(eng=None) -> dict:
    eng = eng or engine
    if not settings.demo_mode:
        raise Refused("BBMS_DEMO_MODE must be true.")
    path = _db_path(eng)
    passwords = _passwords()
    _check_replaceable(path)
    _remove(eng, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    init_db(eng)
    accounts = [(u, name, role, passwords[u]) for u, name, role, _ in ACCOUNTS]
    with Session() as db:
        Seeder(db, business_today(), accounts=accounts).run()
    _mark_as_demo(eng)
    with Session() as db:     # the FICTIONAL demo facility publishes its synthetic stock (exact figures) for /demo
        admin = db.scalar(select(User).where(User.username == "demo.admin"))
        doctor = db.scalar(select(User).where(User.username == "demo.doctor"))
        availability.update_settings(db, admin, StockVisibility.EXACT, settings.availability_max_hours)
        availability.publish(db, doctor)

    rc = load_facilities.run(FACILITIES_FILE, "demo.platform", apply=True, eng=eng, backup=False, expired="unlist")
    if rc != 0:
        raise Refused("loading the Haldwani facility records failed; see the messages above.")

    with Session() as db:
        problems, warnings = _real_facility_state(db)
        if problems:
            raise Refused("unexpected real-facility state after loading: " + "; ".join(problems))
        real = list(db.scalars(select(Facility).where(Facility.is_demo.is_(False))))
        users = sorted(db.scalars(select(User.username)))
        counts = {"facilities": len(real), "listed": sum(f.is_listed for f in real),
                  "donors": db.scalar(select(func.count()).select_from(Donor))}
    return {"path": str(path), "users": users, "warnings": warnings, **counts}


def main() -> int:
    try:
        summary = build()
    except Refused as e:
        print(f"Demo database NOT built: {e}", file=sys.stderr)
        return 2
    print(f"Demo database built at {summary['path']}: {summary['donors']} synthetic donors, "
          f"{summary['facilities']} Haldwani facilities ({summary['listed']} publicly listed), "
          f"accounts: {', '.join(summary['users'])}.")
    for w in summary["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
