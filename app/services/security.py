"""Password hashing and CSRF token helpers."""
from __future__ import annotations

import secrets

import bcrypt

CSRF_SESSION_KEY = "_csrf"
CSRF_FORM_FIELD = "csrf_token"


def hash_password(plain: str) -> str:
    # bcrypt only uses the first 72 bytes; schema validation caps length at 72.
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# A pre-computed hash so failed lookups of unknown usernames cost the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def get_csrf_token(session: dict) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_token_valid(session: dict, submitted: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    return bool(expected and submitted and secrets.compare_digest(expected, submitted))
