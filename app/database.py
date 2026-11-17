"""Database engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings
from app.db_types import UTCDateTime


class Base(DeclarativeBase):
    # Every Mapped[datetime] column is an aware-UTC timestamp (SQLite DDL stays DATETIME).
    type_annotation_map = {datetime: UTCDateTime}


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # timeout: wait for a competing writer instead of failing immediately.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    eng = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind: Engine | None = None) -> None:
    import app.models  # noqa: F401  (register models)
    from app.migrations import upgrade

    eng = bind or engine
    Base.metadata.create_all(bind=eng)   # new tables (fresh DBs get the latest columns)
    upgrade(eng)                         # additive changes to existing tables
