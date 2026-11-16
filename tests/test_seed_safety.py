"""H2 regression: `scripts.seed --reset` can never wipe a real database.

Every test runs the script as a separate process against COPIES in temporary folders. The real
bloodbank.db is only ever opened read-only (to copy it) and its hash is checked before and after.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import BASE_DIR
from scripts.seed import DEMO_APPLICATION_ID

REAL_DB = BASE_DIR / "bloodbank.db"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(db: Path, *args: str, backup_dir: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    env["BBMS_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    env["BBMS_BACKUP_DIR"] = str(backup_dir or db.parent / "backups")
    return subprocess.run([sys.executable, "-m", "scripts.seed", *args], cwd=BASE_DIR, env=env,
                          capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory) -> Path:
    """One genuine seeded demo database, copied by each test (seeding takes several seconds)."""
    db = tmp_path_factory.mktemp("seed-template") / "demo.db"
    r = _run(db)
    assert r.returncode == 0, r.stderr[-500:]
    return db


@pytest.fixture()
def demo_db(seeded_template, tmp_path) -> Path:
    target = tmp_path / "demo.db"
    shutil.copyfile(seeded_template, target)
    return target


def _app_id(db: Path) -> int:
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as c:
        return c.execute("PRAGMA application_id").fetchone()[0]


def test_seeded_databases_carry_the_demo_marker(demo_db):
    assert _app_id(demo_db) == DEMO_APPLICATION_ID


@pytest.mark.parametrize("args", [("--reset",), ("--reset", "--confirm-reset", "wrong.db"),
                                  ("--reset", "--confirm-reset", "DEMO.DB")])
def test_reset_requires_exact_confirmation(demo_db, args):
    before = _sha(demo_db)
    r = _run(demo_db, *args)
    assert r.returncode == 2 and "--confirm-reset demo.db" in r.stderr
    assert _sha(demo_db) == before


def test_database_not_created_by_seed_is_refused(tmp_path):
    db = tmp_path / "prod.db"
    from app.database import init_db, make_engine
    eng = make_engine(f"sqlite:///{db.as_posix()}")
    init_db(eng)
    with eng.begin() as c:
        c.exec_driver_sql("INSERT INTO users (username, full_name, password_hash, role, is_active, created_at, "
                          "must_change_password) VALUES ('platform', 'P', 'x', 'platform_admin', 1, '2026-01-01', 0)")
    eng.dispose()
    before = _sha(db)
    r = _run(db, "--reset", "--confirm-reset", "prod.db")
    assert r.returncode == 3 and "no demo marker" in r.stderr and "Nothing was changed" in r.stderr
    assert _sha(db) == before


@pytest.mark.parametrize("sql,reason", [
    ("INSERT INTO users (username, full_name, password_hash, role, is_active, created_at, must_change_password) "
     "VALUES ('real.person', 'R', 'x', 'platform_admin', 1, '2026-01-01', 0)", "not seed demo accounts"),
    ("INSERT INTO facilities (slug, display_name, ownership, operational_status, is_listed, is_participating, "
     "stock_visibility, stock_max_age_hours, is_demo, created_at, updated_at) VALUES ('real-bank', 'Real Bank', "
     "'unknown', 'unknown', 0, 0, 'hidden', 24, 0, '2026-01-01', '2026-01-01')", "non-demo facility"),
])
def test_marked_database_with_real_content_is_refused(demo_db, sql, reason):
    with sqlite3.connect(demo_db) as c:
        c.execute(sql)
    before = _sha(demo_db)
    r = _run(demo_db, "--reset", "--confirm-reset", "demo.db")
    assert r.returncode == 3 and reason in r.stderr
    assert _sha(demo_db) == before


def test_copy_of_the_real_database_is_refused(tmp_path):
    """The real bloodbank.db (opened read-only) is copied; reset is attempted only on the copy."""
    if not REAL_DB.exists():
        pytest.skip("no local bloodbank.db")
    real_before = _sha(REAL_DB)
    copy = tmp_path / "bloodbank.db"
    src = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    dst.close()
    src.close()
    copy_before = _sha(copy)
    r = _run(copy, "--reset", "--confirm-reset", "bloodbank.db")
    assert r.returncode == 3 and "Refusing to reset" in r.stderr
    assert _sha(copy) == copy_before
    assert _sha(REAL_DB) == real_before                     # the real database was never touched


def test_successful_reset_takes_verified_backup_first(demo_db, tmp_path):
    with sqlite3.connect(f"file:{demo_db.as_posix()}?mode=ro", uri=True) as c:
        donors_before = c.execute("SELECT count(*) FROM donors").fetchone()[0]
    backups = tmp_path / "bk"
    r = _run(demo_db, "--reset", "--confirm-reset", "demo.db", backup_dir=backups)
    assert r.returncode == 0, r.stderr[-500:]
    [backup] = list(backups.glob("*.db"))
    with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT count(*) FROM donors").fetchone()[0] == donors_before
    assert _app_id(demo_db) == DEMO_APPLICATION_ID                # re-seeded and re-marked


def test_failed_backup_aborts_before_anything_is_dropped(demo_db, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")                                        # backup dir path is a file -> backup fails
    before = _sha(demo_db)
    r = _run(demo_db, "--reset", "--confirm-reset", "demo.db", backup_dir=blocker)
    assert r.returncode != 0
    assert _sha(demo_db) == before
