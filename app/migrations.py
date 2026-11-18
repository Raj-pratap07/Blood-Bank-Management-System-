"""Minimal, idempotent schema migrations for existing SQLite databases.

`Base.metadata.create_all` creates missing *tables* but never alters existing
ones, so columns added after a database was created are applied here.
Progress is tracked with SQLite's `PRAGMA user_version`. Every step is additive
(ADD COLUMN + targeted UPDATE); no table or row is ever dropped.

Atomicity: Python's sqlite3 driver (legacy transaction mode) auto-commits DDL
such as ALTER TABLE, so the runner takes manual control of the connection and
wraps all pending steps *and* the version bump in one `BEGIN IMMEDIATE`
transaction. Either everything is applied or nothing is. IMMEDIATE also takes
the write lock up front, so two app processes starting together run the
migration one after the other; the second re-reads `user_version` under the lock
and finds nothing left to do.

Version history
---------------
1  blood_requests.rejected_by_id / rejected_at.
   Before v1, rejecting a request overwrote approved_by_id/approved_at with the
   rejecting officer. For legacy REJECTED rows those values therefore identify
   the *rejecting* officer, so they are moved to rejected_by_id/rejected_at.
   (Any genuine earlier approval on such rows was already overwritten and
   cannot be recovered.)
2  Haldwani BloodLink Phase 1: facilities, facility_sources, facility_contacts,
   facility_verifications (+ indexes), same-facility evidence triggers, an
   append-only verification log, and one explicitly fictional, never-listed
   "Demo Blood Centre (fictional)" row. No real facility data is inserted.
3  Phase 3 multi-facility isolation: facility_id on users, donors, donations,
   blood_units, blood_requests and issues (+ indexes); every existing row is
   assigned to the fictional demo facility (platform administrators keep NULL);
   donor phone uniqueness becomes per facility; triggers make facility_id
   required and immutable and reject any cross-facility link.
   Reverting means restoring the verified pre-v3 backup (scripts/backup_db.py):
   SQLite cannot DROP a column that carries a foreign key.
4  Phase 4: availability_publications / availability_entries (facility-published,
   aggregate-only availability snapshots; append-only except a single withdrawal),
   and first-login credential fields on users (must_change_password,
   password_set_at, setup_token_hash, setup_token_expires_at). Additive only.
5  Phase 6: public_requests (anonymous emergency requests; reference + hashed tracking code, no patient
   identity) and public_request_events (append-only audit trail). Triggers: only a participating facility can
   receive a request; facility, reference and tracking hash are immutable; a linked staff request and every
   audit event must belong to the same facility; requests cannot be deleted. Additive only.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

log = logging.getLogger("bloodbank.migrations")

SCHEMA_VERSION = 5


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _v1_rejected_by(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "blood_requests")
    if "rejected_by_id" not in cols:
        conn.execute("ALTER TABLE blood_requests ADD COLUMN rejected_by_id INTEGER REFERENCES users(id)")
    if "rejected_at" not in cols:
        conn.execute("ALTER TABLE blood_requests ADD COLUMN rejected_at DATETIME")
    moved = conn.execute(
        "UPDATE blood_requests SET rejected_by_id = approved_by_id, rejected_at = approved_at, "
        "approved_by_id = NULL, approved_at = NULL "
        "WHERE status = 'rejected' AND rejected_by_id IS NULL AND approved_by_id IS NOT NULL"
    ).rowcount
    if moved:
        log.info("Migration v1: moved rejecting officer on %d legacy rejected request(s).", moved)


_V2_TABLES = [
    """CREATE TABLE IF NOT EXISTS facilities (
        id INTEGER NOT NULL,
        slug VARCHAR(80) NOT NULL,
        display_name VARCHAR(150) NOT NULL,
        official_name VARCHAR(200),
        aliases TEXT,
        ownership VARCHAR(32) NOT NULL,
        parent_hospital VARCHAR(200),
        address_line VARCHAR(300),
        locality VARCHAR(120),
        city VARCHAR(80),
        district VARCHAR(80),
        state VARCHAR(80),
        pincode VARCHAR(6),
        latitude DOUBLE,
        longitude DOUBLE,
        location_source_id INTEGER,
        eraktkosh_code VARCHAR(20),
        cdsco_license_no VARCHAR(60),
        has_component_separation BOOLEAN,
        has_apheresis BOOLEAN,
        operational_status VARCHAR(32) NOT NULL,
        is_listed BOOLEAN NOT NULL,
        is_participating BOOLEAN NOT NULL,
        stock_visibility VARCHAR(32) NOT NULL,
        stock_max_age_hours INTEGER NOT NULL,
        is_demo BOOLEAN NOT NULL,
        notes TEXT,
        created_by_id INTEGER,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_facility_coords_pair CHECK ((latitude IS NULL) = (longitude IS NULL)),
        CONSTRAINT ck_facility_coords_sourced CHECK (latitude IS NULL OR location_source_id IS NOT NULL),
        CONSTRAINT ck_facility_demo_never_listed CHECK (NOT (is_demo AND is_listed)),
        CONSTRAINT fk_facility_location_source FOREIGN KEY(location_source_id) REFERENCES facility_sources (id),
        FOREIGN KEY(created_by_id) REFERENCES users (id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_facilities_slug ON facilities (slug)",
    """CREATE TABLE IF NOT EXISTS facility_sources (
        id INTEGER NOT NULL,
        facility_id INTEGER NOT NULL,
        source_type VARCHAR(32) NOT NULL,
        title VARCHAR(200) NOT NULL,
        reference VARCHAR(500) NOT NULL,
        retrieved_on DATE NOT NULL,
        notes TEXT,
        created_by_id INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        FOREIGN KEY(created_by_id) REFERENCES users (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_facility_sources_facility_id ON facility_sources (facility_id)",
    """CREATE TABLE IF NOT EXISTS facility_contacts (
        id INTEGER NOT NULL,
        facility_id INTEGER NOT NULL,
        kind VARCHAR(32) NOT NULL,
        value VARCHAR(300) NOT NULL,
        label VARCHAR(120),
        source_id INTEGER NOT NULL,
        is_public BOOLEAN NOT NULL,
        is_active BOOLEAN NOT NULL,
        created_by_id INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        FOREIGN KEY(source_id) REFERENCES facility_sources (id),
        FOREIGN KEY(created_by_id) REFERENCES users (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_facility_contacts_facility_id ON facility_contacts (facility_id)",
    """CREATE TABLE IF NOT EXISTS facility_verifications (
        id INTEGER NOT NULL,
        facility_id INTEGER NOT NULL,
        scope VARCHAR(32) NOT NULL,
        contact_id INTEGER,
        status VARCHAR(32) NOT NULL,
        method VARCHAR(32) NOT NULL,
        source_id INTEGER,
        evidence_note TEXT NOT NULL,
        verified_on DATE NOT NULL,
        verified_by_id INTEGER NOT NULL,
        recorded_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        FOREIGN KEY(contact_id) REFERENCES facility_contacts (id),
        FOREIGN KEY(source_id) REFERENCES facility_sources (id),
        FOREIGN KEY(verified_by_id) REFERENCES users (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_facility_verifications_facility_id ON facility_verifications (facility_id)",
]

# Database-level guards (defence in depth; the service layer enforces the same rules).
_V2_TRIGGERS = [
    # Evidence must belong to the same facility it is used for.
    """CREATE TRIGGER IF NOT EXISTS trg_facility_location_source_same_facility
       BEFORE UPDATE OF location_source_id ON facilities
       WHEN NEW.location_source_id IS NOT NULL AND
            (SELECT facility_id FROM facility_sources WHERE id = NEW.location_source_id) IS NOT NEW.id
       BEGIN SELECT RAISE(ABORT, 'location source belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_contact_source_same_facility
       BEFORE INSERT ON facility_contacts
       WHEN (SELECT facility_id FROM facility_sources WHERE id = NEW.source_id) IS NOT NEW.facility_id
       BEGIN SELECT RAISE(ABORT, 'contact source belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_verification_refs_same_facility
       BEFORE INSERT ON facility_verifications
       WHEN (NEW.source_id IS NOT NULL AND
             (SELECT facility_id FROM facility_sources WHERE id = NEW.source_id) IS NOT NEW.facility_id)
         OR (NEW.contact_id IS NOT NULL AND
             (SELECT facility_id FROM facility_contacts WHERE id = NEW.contact_id) IS NOT NEW.facility_id)
       BEGIN SELECT RAISE(ABORT, 'verification evidence belongs to another facility'); END""",
    # The verification log is append-only.
    """CREATE TRIGGER IF NOT EXISTS trg_verifications_no_update
       BEFORE UPDATE ON facility_verifications
       BEGIN SELECT RAISE(ABORT, 'facility_verifications is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_verifications_no_delete
       BEFORE DELETE ON facility_verifications
       BEGIN SELECT RAISE(ABORT, 'facility_verifications is append-only'); END""",
]

DEMO_FACILITY_SLUG = "demo-blood-centre"


def _v2_facilities(conn: sqlite3.Connection) -> None:
    """Facility directory, provenance, verification log and the fictional demo facility.

    Inserts exactly ONE row: an explicitly fictional, never-listed facility that
    will own the pre-existing demo data in Phase 3. No real-world facility data
    is inserted by any migration.
    """
    for ddl in _V2_TABLES + _V2_TRIGGERS:
        conn.execute(ddl)
    exists = conn.execute("SELECT 1 FROM facilities WHERE is_demo = 1").fetchone()
    if not exists:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
        conn.execute(
            "INSERT INTO facilities (slug, display_name, ownership, operational_status, is_listed, "
            "is_participating, stock_visibility, stock_max_age_hours, is_demo, notes, created_at, updated_at) "
            "VALUES (?, ?, 'unknown', 'unknown', 0, 0, 'hidden', 24, 1, ?, ?, ?)",
            (DEMO_FACILITY_SLUG, "Demo Blood Centre (fictional)",
             "Fictional facility that holds the pre-existing demo data. It is not a real blood bank, has no "
             "address or coordinates, and can never be listed publicly.", now, now),
        )
        log.info("Migration v2: created the fictional demo facility.")


OPERATIONAL_TABLES = ("users", "donors", "donations", "blood_units", "blood_requests", "issues")
_SCOPED = OPERATIONAL_TABLES[1:]  # every table except users must always have a facility


def _fac(table: str, id_expr: str) -> str:
    return f"(SELECT facility_id FROM {table} WHERE id = {id_expr})"


# (trigger name, table, columns whose change re-checks the rule, condition that is a VIOLATION, message)
_V3_RULES = [
    ("users_role_facility", "users", "role, facility_id",
     "(NEW.role = 'platform_admin') != (NEW.facility_id IS NULL)",
     "platform administrators have no facility; every other account must have one"),
    ("donations_donor", "donations", "donor_id, facility_id", f"{_fac('donors', 'NEW.donor_id')} IS NOT NEW.facility_id",
     "donation and donor belong to different facilities"),
    ("donations_users", "donations", "cleared_by_id, recorded_by_id, screened_by_id, facility_id",
     f"{_fac('users', 'NEW.cleared_by_id')} IS NOT NEW.facility_id OR "
     f"{_fac('users', 'NEW.recorded_by_id')} IS NOT NEW.facility_id OR "
     f"(NEW.screened_by_id IS NOT NULL AND {_fac('users', 'NEW.screened_by_id')} IS NOT NEW.facility_id)",
     "donation staff belong to another facility"),
    ("units_donation", "blood_units", "donation_id, facility_id",
     f"{_fac('donations', 'NEW.donation_id')} IS NOT NEW.facility_id", "bag and donation belong to different facilities"),
    ("units_reservation", "blood_units", "reserved_for_request_id, facility_id",
     f"NEW.reserved_for_request_id IS NOT NULL AND {_fac('blood_requests', 'NEW.reserved_for_request_id')} "
     "IS NOT NEW.facility_id", "bag reserved for another facility's request"),
    ("requests_users", "blood_requests", "created_by_id, approved_by_id, rejected_by_id, facility_id",
     f"{_fac('users', 'NEW.created_by_id')} IS NOT NEW.facility_id OR "
     f"(NEW.approved_by_id IS NOT NULL AND {_fac('users', 'NEW.approved_by_id')} IS NOT NEW.facility_id) OR "
     f"(NEW.rejected_by_id IS NOT NULL AND {_fac('users', 'NEW.rejected_by_id')} IS NOT NEW.facility_id)",
     "request staff belong to another facility"),
    ("issues_links", "issues", "request_id, blood_unit_id, issued_by_id, facility_id",
     f"{_fac('blood_requests', 'NEW.request_id')} IS NOT NEW.facility_id OR "
     f"{_fac('blood_units', 'NEW.blood_unit_id')} IS NOT NEW.facility_id OR "
     f"{_fac('users', 'NEW.issued_by_id')} IS NOT NEW.facility_id",
     "issue links records of different facilities"),
]


def _v3_triggers() -> list[str]:
    ddl = []
    for t in _SCOPED:
        ddl.append(f"""CREATE TRIGGER IF NOT EXISTS trg_{t}_facility_required BEFORE INSERT ON {t}
            WHEN NEW.facility_id IS NULL BEGIN SELECT RAISE(ABORT, '{t}.facility_id is required'); END""")
    for t in OPERATIONAL_TABLES:
        ddl.append(f"""CREATE TRIGGER IF NOT EXISTS trg_{t}_facility_immutable BEFORE UPDATE OF facility_id ON {t}
            WHEN NEW.facility_id IS NOT OLD.facility_id
            BEGIN SELECT RAISE(ABORT, '{t}.facility_id cannot be changed'); END""")
    for name, table, cols, violation, msg in _V3_RULES:
        msg = msg.replace("'", "''")            # SQL string literal escaping
        ddl.append(f"""CREATE TRIGGER IF NOT EXISTS trg_{name}_ins BEFORE INSERT ON {table} WHEN {violation}
            BEGIN SELECT RAISE(ABORT, 'cross-facility: {msg}'); END""")
        ddl.append(f"""CREATE TRIGGER IF NOT EXISTS trg_{name}_upd BEFORE UPDATE OF {cols} ON {table} WHEN {violation}
            BEGIN SELECT RAISE(ABORT, 'cross-facility: {msg}'); END""")
    return ddl


def _v3_facility_isolation(conn: sqlite3.Connection) -> None:
    demo = conn.execute("SELECT id FROM facilities WHERE is_demo = 1 ORDER BY id LIMIT 1").fetchone()
    if demo is None:
        raise RuntimeError("Migration v3 needs the demo facility created by v2.")
    demo_id = demo[0]
    for t in OPERATIONAL_TABLES:
        if "facility_id" not in _columns(conn, t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN facility_id INTEGER REFERENCES facilities (id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{t}_facility_id ON {t} (facility_id)")
    # Existing operational data belongs to the fictional demo facility; platform admins have none.
    for t in _SCOPED:
        conn.execute(f"UPDATE {t} SET facility_id = ? WHERE facility_id IS NULL", (demo_id,))
    conn.execute("UPDATE users SET facility_id = ? WHERE facility_id IS NULL AND role != 'platform_admin'", (demo_id,))
    # Donor phone: unique per facility instead of globally.
    conn.execute("DROP INDEX IF EXISTS ix_donors_phone")
    conn.execute("CREATE INDEX ix_donors_phone ON donors (phone)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_donors_facility_phone ON donors (facility_id, phone)")
    for ddl in _v3_triggers():
        conn.execute(ddl)
    # The demo facility operates on the platform (it owns the existing operational data).
    conn.execute("UPDATE facilities SET is_participating = 1 WHERE id = ?", (demo_id,))


_V4_DDL = [
    """CREATE TABLE IF NOT EXISTS availability_publications (
        id INTEGER NOT NULL,
        facility_id INTEGER NOT NULL,
        published_by_id INTEGER NOT NULL,
        published_at DATETIME NOT NULL,
        valid_until DATETIME NOT NULL,
        withdrawn_at DATETIME,
        withdrawn_by_id INTEGER,
        PRIMARY KEY (id),
        CONSTRAINT ck_publication_validity CHECK (valid_until > published_at),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        FOREIGN KEY(published_by_id) REFERENCES users (id),
        FOREIGN KEY(withdrawn_by_id) REFERENCES users (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_availability_publications_published_at ON availability_publications (published_at)",
    "CREATE INDEX IF NOT EXISTS ix_availability_publications_facility_id ON availability_publications (facility_id)",
    """CREATE TABLE IF NOT EXISTS availability_entries (
        id INTEGER NOT NULL,
        publication_id INTEGER NOT NULL,
        blood_group VARCHAR(32) NOT NULL,
        product VARCHAR(32) NOT NULL,
        units INTEGER NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_entry_group_product UNIQUE (publication_id, blood_group, product),
        CONSTRAINT ck_entry_units_nonnegative CHECK (units >= 0),
        FOREIGN KEY(publication_id) REFERENCES availability_publications (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_availability_entries_publication_id ON availability_entries (publication_id)",
    """CREATE TRIGGER IF NOT EXISTS trg_publication_publisher_same_facility
       BEFORE INSERT ON availability_publications
       WHEN (SELECT facility_id FROM users WHERE id = NEW.published_by_id) IS NOT NEW.facility_id
       BEGIN SELECT RAISE(ABORT, 'cross-facility: publisher belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_publication_only_withdrawal
       BEFORE UPDATE ON availability_publications
       WHEN OLD.withdrawn_at IS NOT NULL
         OR NEW.facility_id IS NOT OLD.facility_id OR NEW.published_by_id IS NOT OLD.published_by_id
         OR NEW.published_at IS NOT OLD.published_at OR NEW.valid_until IS NOT OLD.valid_until
         OR NEW.withdrawn_at IS NULL
         OR (SELECT facility_id FROM users WHERE id = NEW.withdrawn_by_id) IS NOT NEW.facility_id
       BEGIN SELECT RAISE(ABORT, 'availability publications are append-only (a single withdrawal is allowed)'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_publication_no_delete BEFORE DELETE ON availability_publications
       BEGIN SELECT RAISE(ABORT, 'availability publications are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_entry_no_update BEFORE UPDATE ON availability_entries
       BEGIN SELECT RAISE(ABORT, 'availability entries are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_entry_no_delete BEFORE DELETE ON availability_entries
       BEGIN SELECT RAISE(ABORT, 'availability entries are append-only'); END""",
]


def _v4_availability_and_credentials(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "users")
    if "must_change_password" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT '0'")
    if "password_set_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_set_at DATETIME")
    if "setup_token_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN setup_token_hash VARCHAR(64)")
    if "setup_token_expires_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN setup_token_expires_at DATETIME")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_setup_token_hash ON users (setup_token_hash)")
    for ddl in _V4_DDL:
        conn.execute(ddl)


_V5_DDL = [
    """CREATE TABLE IF NOT EXISTS public_requests (
        id INTEGER NOT NULL,
        reference VARCHAR(16) NOT NULL,
        token_hash VARCHAR(64) NOT NULL,
        facility_id INTEGER NOT NULL,
        blood_group VARCHAR(32) NOT NULL,
        product VARCHAR(32) NOT NULL,
        units INTEGER NOT NULL,
        required_by DATETIME NOT NULL,
        location VARCHAR(150) NOT NULL,
        requester_name VARCHAR(120) NOT NULL,
        contact_phone VARCHAR(20) NOT NULL,
        note VARCHAR(500),
        status VARCHAR(32) NOT NULL,
        public_message VARCHAR(500),
        blood_request_id INTEGER,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_public_request_units CHECK (units >= 1 AND units <= 10),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        UNIQUE (blood_request_id),
        FOREIGN KEY(blood_request_id) REFERENCES blood_requests (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_public_requests_created_at ON public_requests (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_public_requests_status ON public_requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_public_requests_facility_id ON public_requests (facility_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_public_requests_reference ON public_requests (reference)",
    """CREATE TABLE IF NOT EXISTS public_request_events (
        id INTEGER NOT NULL,
        public_request_id INTEGER NOT NULL,
        facility_id INTEGER NOT NULL,
        actor_id INTEGER,
        action VARCHAR(32) NOT NULL,
        to_status VARCHAR(32) NOT NULL,
        note VARCHAR(500),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(public_request_id) REFERENCES public_requests (id),
        FOREIGN KEY(facility_id) REFERENCES facilities (id),
        FOREIGN KEY(actor_id) REFERENCES users (id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_public_request_events_facility_id ON public_request_events (facility_id)",
    "CREATE INDEX IF NOT EXISTS ix_public_request_events_public_request_id ON public_request_events (public_request_id)",
    # Only a participating facility (never a non-participating real directory entry) can receive a request.
    """CREATE TRIGGER IF NOT EXISTS trg_public_requests_participating BEFORE INSERT ON public_requests
       WHEN (SELECT is_participating FROM facilities WHERE id = NEW.facility_id) IS NOT 1
       BEGIN SELECT RAISE(ABORT, 'public requests can only be sent to a participating facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_requests_immutable
       BEFORE UPDATE OF facility_id, reference, token_hash ON public_requests
       WHEN NEW.facility_id IS NOT OLD.facility_id OR NEW.reference IS NOT OLD.reference
         OR NEW.token_hash IS NOT OLD.token_hash
       BEGIN SELECT RAISE(ABORT, 'public request facility, reference and tracking code cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_requests_link_ins BEFORE INSERT ON public_requests
       WHEN NEW.blood_request_id IS NOT NULL
        AND (SELECT facility_id FROM blood_requests WHERE id = NEW.blood_request_id) IS NOT NEW.facility_id
       BEGIN SELECT RAISE(ABORT, 'cross-facility: linked request belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_requests_link_upd BEFORE UPDATE OF blood_request_id ON public_requests
       WHEN NEW.blood_request_id IS NOT NULL
        AND (SELECT facility_id FROM blood_requests WHERE id = NEW.blood_request_id) IS NOT NEW.facility_id
       BEGIN SELECT RAISE(ABORT, 'cross-facility: linked request belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_requests_no_delete BEFORE DELETE ON public_requests
       BEGIN SELECT RAISE(ABORT, 'public requests are kept for the audit trail'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_request_events_same_facility BEFORE INSERT ON public_request_events
       WHEN (SELECT facility_id FROM public_requests WHERE id = NEW.public_request_id) IS NOT NEW.facility_id
         OR (NEW.actor_id IS NOT NULL AND (SELECT facility_id FROM users WHERE id = NEW.actor_id) IS NOT NEW.facility_id)
       BEGIN SELECT RAISE(ABORT, 'cross-facility: audit event belongs to another facility'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_request_events_no_update BEFORE UPDATE ON public_request_events
       BEGIN SELECT RAISE(ABORT, 'public_request_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_public_request_events_no_delete BEFORE DELETE ON public_request_events
       BEGIN SELECT RAISE(ABORT, 'public_request_events is append-only'); END""",
]


def _v5_public_requests(conn: sqlite3.Connection) -> None:
    for ddl in _V5_DDL:
        conn.execute(ddl)


STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _v1_rejected_by, 2: _v2_facilities, 3: _v3_facility_isolation, 4: _v4_availability_and_credentials,
    5: _v5_public_requests}


def upgrade(engine: Engine) -> int:
    """Apply pending steps atomically; returns the resulting schema version."""
    if engine.dialect.name != "sqlite":  # other backends should use a real migration tool
        return SCHEMA_VERSION
    pooled = engine.raw_connection()
    conn: sqlite3.Connection = pooled.driver_connection
    saved_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control: no implicit commits around DDL
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0] or 0
            for target in range(version + 1, SCHEMA_VERSION + 1):
                STEPS[target](conn)
                conn.execute(f"PRAGMA user_version = {int(target)}")
                version = target
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return version
    finally:
        conn.isolation_level = saved_isolation
        pooled.close()
