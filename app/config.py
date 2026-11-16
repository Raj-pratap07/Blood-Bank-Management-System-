"""Application settings.

All clinical thresholds below are *configurable software defaults* used for a
preliminary check only. They must be verified against the current national
donor-selection guidelines and the blood centre's own SOP before real use.
Override any value with an environment variable of the same name prefixed by
``BBMS_`` (e.g. ``BBMS_SHELF_LIFE_DAYS=42``).
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default, cast=str):
    raw = os.getenv(f"BBMS_{name}")
    if raw is None:
        return default
    return cast(raw)


class Settings:
    app_name: str = "Blood Bank Management System"
    secret_key: str = _env("SECRET_KEY", "dev-only-insecure-secret-change-me")
    database_url: str = _env("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'bloodbank.db').as_posix()}")
    session_max_age_seconds: int = _env("SESSION_MAX_AGE", 8 * 60 * 60, int)

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

    page_size: int = 20


settings = Settings()
