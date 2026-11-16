from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import Role, db_enum
from app.utils import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[Role] = mapped_column(db_enum(Role))
    # Facility the account belongs to. NULL only for platform administrators (enforced by trigger).
    facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id"), index=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Phase 4 hardening: accounts created/reset by someone else must choose their own password.
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default="0")
    password_set_at: Mapped[datetime | None]
    # One-time setup link for onboarded facility admins: only a SHA-256 hash is stored.
    setup_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    setup_token_expires_at: Mapped[datetime | None]

    facility = relationship("Facility", foreign_keys=[facility_id], lazy="joined")

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def can_authorize(self) -> bool:
        """Medical/authorising actions: screening, donor clearance, request approval."""
        return self.role in (Role.ADMIN, Role.MEDICAL_OFFICER)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} ({self.role})>"
