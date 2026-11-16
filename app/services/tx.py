from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def transaction(db: Session) -> Iterator[Session]:
    """Commit on success; roll back *everything* on any exception and re-raise."""
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
