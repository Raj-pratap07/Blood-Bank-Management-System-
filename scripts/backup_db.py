"""Create a verified, consistent backup of the SQLite database.

Usage:
    python -m scripts.backup_db [--label pre-v2]

Uses SQLite's online backup API (safe while the app is running), then verifies
the copy: PRAGMA integrity_check must be "ok" and every table's row count and
the schema version must match the source. Backups go to ./backups/ (git-ignored),
or to BBMS_BACKUP_DIR when set.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.config import BASE_DIR, settings


def _db_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("Only SQLite databases are supported by this script.")
    return Path(url.removeprefix("sqlite:///"))


def _snapshot(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    return {"user_version": conn.execute("PRAGMA user_version").fetchone()[0], "counts": counts}


def backup(label: str = "manual", dest_dir: Path | None = None) -> Path:
    src_path = _db_path()
    if not src_path.exists():
        raise SystemExit(f"Database not found: {src_path}")
    dest_dir = dest_dir or Path(os.getenv("BBMS_BACKUP_DIR") or BASE_DIR / "backups")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"bloodbank-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"

    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            before, after = _snapshot(src), _snapshot(dst)
            integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()

    if integrity != "ok" or before != after:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"Backup verification FAILED (integrity={integrity}); no backup kept.")
    print(f"Backup verified: {dest}")
    print(f"  integrity_check=ok  schema version={after['user_version']}  rows={after['counts']}")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="manual", help="short tag added to the file name")
    args = parser.parse_args(argv)
    backup(args.label.replace(" ", "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
