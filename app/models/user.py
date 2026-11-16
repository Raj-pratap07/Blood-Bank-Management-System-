from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

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
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def can_authorize(self) -> bool:
        """Medical/authorising actions: screening, donor clearance, request approval."""
        return self.role in (Role.ADMIN, Role.MEDICAL_OFFICER)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} ({self.role})>"
