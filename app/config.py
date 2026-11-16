"""Application settings.

All clinical thresholds below are *configurable software defaults* used for a
preliminary check only. They must be verified against the current national
donor-selection guidelines and the blood centre's own SOP before real use.
Override any value with an environment variable of the same name prefixed by
``BBMS_`` (e.g. ``BBMS_SHELF_LIFE_DAYS=42``).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigurationError(RuntimeError):
    """Raised at start-up when a setting is missing, malformed or out of range."""


class InsecureConfigurationError(ConfigurationError):
    """Raised at start-up when production settings are unsafe."""


_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def _bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(raw)


_TYPE_NAMES = {int: "a whole number", float: "a number", _bool: "true or false"}


def _env(name: str, default, cast=str):
    raw = os.getenv(f"BBMS_{name}")
    if raw is None:
        return default
    try:
        value = cast(raw.strip()) if cast is not str else raw
    except ValueError:
        raise ConfigurationError(
            f"BBMS_{name}={raw!r} is invalid: expected {_TYPE_NAMES.get(cast, cast.__name__)}."
        ) from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationError(f"BBMS_{name}={raw!r} is invalid: expected a finite number.")
    return value


# Signs session cookies in local development only. Anyone who knows it can forge
# a session, so production start-up refuses it (see `check_secret_key`).
DEV_SECRET_KEY = "dev-only-insecure-secret-change-me"
MIN_PRODUCTION_SECRET_LENGTH = 32


def is_production(app_env: str | None) -> bool:
    return (app_env or "").strip().lower() in ("production", "prod")


def check_secret_key(app_env: str | None, secret_key: str | None) -> None:
    """Refuse to run in production with the development, blank or a short secret key.

    Development (APP_ENV unset or anything other than production/prod) is never blocked.
    """
    if not is_production(app_env):
        return
    key = (secret_key or "").strip()
    if key == DEV_SECRET_KEY or len(key) < MIN_PRODUCTION_SECRET_LENGTH:
        problem = ("is set to the built-in development value" if key == DEV_SECRET_KEY
                   else "is missing" if not key
                   else f"is shorter than {MIN_PRODUCTION_SECRET_LENGTH} characters")
        raise InsecureConfigurationError(
            f"Refusing to start: APP_ENV=production but BBMS_SECRET_KEY {problem}. "
            "This key signs login sessions; a known or weak key lets anyone forge a session. "
            "Generate one with:  python -c \"import secrets; print(secrets.token_urlsafe(48))\"  "
            "and set it in the server environment (e.g. BBMS_SECRET_KEY=<value>). "
            "Keep it out of source control. For local development, unset APP_ENV."
        )


class Settings:
    app_name: str = "Haldwani BloodLink"
    app_env: str = os.getenv("APP_ENV", "development")
    secret_key: str = _env("SECRET_KEY", DEV_SECRET_KEY)
    database_url: str = _env("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'bloodbank.db').as_posix()}")
    session_max_age_seconds: int = _env("SESSION_MAX_AGE", 8 * 60 * 60, int)
    # Secure cookie flag. None = automatic: on when APP_ENV=production, off otherwise
    # (local development runs over plain HTTP). Set true/false to override.
    session_https_only: bool | None = _env("SESSION_HTTPS_ONLY", None, _bool)

    # --- Inventory --------------------------------------------------------
    # Whole blood in CPDA-1 is the only product supported in this MVP.
    # 35 days is the initial CPDA-1 assumption, subject to the centre's SOP.
    shelf_life_days: int = _env("SHELF_LIFE_DAYS", 35, int)
    near_expiry_days: int = _env("NEAR_EXPIRY_DAYS", 7, int)

    # --- Preliminary donor eligibility (verify against current guidelines) --
    min_age: int = _env("MIN_AGE", 18, int)
    max_age: int = _env("MAX_AGE", 65, int)
    max_age_first_time: int = _env("MAX_AGE_FIRST_TIME", 60, int)
    min_weight_kg: float = _env("MIN_WEIGHT_KG", 45.0, float)          # 350 ml collection
    min_weight_450ml_kg: float = _env("MIN_WEIGHT_450ML_KG", 55.0, float)  # 450 ml collection
    interval_male_days: int = _env("INTERVAL_MALE_DAYS", 90, int)
    interval_female_days: int = _env("INTERVAL_FEMALE_DAYS", 120, int)
    min_hemoglobin_g_dl: float = _env("MIN_HB", 12.5, float)
    bp_systolic_range: tuple[int, int] = (100, 140)
    bp_diastolic_range: tuple[int, int] = (60, 90)
    pulse_range: tuple[int, int] = (60, 100)
    max_temperature_c: float = _env("MAX_TEMP_C", 37.5, float)

    # --- Facility directory (Haldwani BloodLink) ----------------------------
    # A positive verification older than this is shown as "outdated" until re-checked.
    verification_max_age_days: int = _env("VERIFICATION_MAX_AGE_DAYS", 180, int)

    # --- Public map (Haldwani BloodLink Phase 2) ------------------------------
    # OpenStreetMap's tile server is for light use only and requires attribution:
    # https://operations.osmfoundation.org/policies/tiles/ . Point this at another
    # provider (and update the attribution) if traffic grows.
    map_tile_url: str = _env("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    map_attribution: str = _env(
        "MAP_ATTRIBUTION",
        '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a> contributors')
    # Initial map view when no facility location is verified. NOT a facility location.
    # Source: OpenStreetMap Nominatim, "Haldwani, Nainital, Uttarakhand, India" (way 112883732).
    map_default_center: tuple[float, float] = (29.2144809, 79.5279012)
    map_default_zoom: int = 13

    # --- Published availability (Phase 4) -----------------------------------
    # A publication is valid for min(facility.stock_max_age_hours, this ceiling). 24 h matches the
    # daily stock-update duty of licensed blood centres on e-RaktKosh; facilities may choose less.
    availability_max_hours: int = _env("AVAILABILITY_MAX_HOURS", 24, int)
    # "banded" display: 0 = Not available, 1..LOW_MAX = Low, more = Available.
    availability_low_max: int = _env("AVAILABILITY_LOW_MAX", 2, int)
    # One-time setup links for onboarded facility administrators.
    setup_link_valid_hours: int = _env("SETUP_LINK_VALID_HOURS", 72, int)

    page_size: int = 20


def session_cookie_secure(s: Settings) -> bool:
    """Whether the session cookie gets the Secure flag (sent over HTTPS only)."""
    return s.session_https_only if s.session_https_only is not None else is_production(s.app_env)


def validate_settings(s: Settings) -> None:
    """Range/consistency checks; raises one ConfigurationError listing every problem.

    These are software sanity limits (no zero shelf life, min age below max age...),
    not clinical validation of the chosen thresholds.
    """
    problems: list[str] = []

    def need(ok: bool, env: str, msg: str) -> None:
        if not ok:
            problems.append(f"{env}: {msg}")

    need(s.session_max_age_seconds >= 60, "BBMS_SESSION_MAX_AGE", "must be at least 60 seconds")
    need(1 <= s.shelf_life_days <= 365, "BBMS_SHELF_LIFE_DAYS", "must be between 1 and 365 days")
    need(0 <= s.near_expiry_days < max(s.shelf_life_days, 1), "BBMS_NEAR_EXPIRY_DAYS",
         "must be 0 or more and less than the shelf life")
    need(1 <= s.min_age <= s.max_age_first_time <= s.max_age <= 100,
         "BBMS_MIN_AGE / BBMS_MAX_AGE_FIRST_TIME / BBMS_MAX_AGE",
         "must satisfy 1 <= min age <= first-time max age <= max age <= 100")
    need(0 < s.min_weight_kg <= s.min_weight_450ml_kg <= 250, "BBMS_MIN_WEIGHT_KG / BBMS_MIN_WEIGHT_450ML_KG",
         "must satisfy 0 < 350 ml minimum <= 450 ml minimum <= 250 kg")
    need(s.interval_male_days >= 1, "BBMS_INTERVAL_MALE_DAYS", "must be at least 1 day")
    need(s.interval_female_days >= 1, "BBMS_INTERVAL_FEMALE_DAYS", "must be at least 1 day")
    need(0 < s.min_hemoglobin_g_dl < 25, "BBMS_MIN_HB", "must be between 0 and 25 g/dL")
    need(35 <= s.max_temperature_c <= 42, "BBMS_MAX_TEMP_C", "must be between 35 and 42 °C")
    need(30 <= s.verification_max_age_days <= 730, "BBMS_VERIFICATION_MAX_AGE_DAYS",
         "must be between 30 and 730 days")
    need(1 <= s.availability_max_hours <= 48, "BBMS_AVAILABILITY_MAX_HOURS", "must be between 1 and 48 hours")
    need(1 <= s.availability_low_max <= 20, "BBMS_AVAILABILITY_LOW_MAX", "must be between 1 and 20 units")
    need(1 <= s.setup_link_valid_hours <= 168, "BBMS_SETUP_LINK_VALID_HOURS", "must be between 1 and 168 hours")
    if problems:
        raise ConfigurationError("Invalid configuration: " + "; ".join(problems))


settings = Settings()
validate_settings(settings)  # fail fast for every entry point (web app, seed script, tests)
