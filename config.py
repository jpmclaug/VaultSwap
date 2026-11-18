"""Application configuration for VaultSwap."""

import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


def _resolve_database_url(raw_url: str | None = None) -> str:
    """Normalizes Neon PostgreSQL URLs across psycopg v3 / psycopg2 or falls back to SQLite."""
    if raw_url is None:
        db_url = (os.getenv("DATABASE_URL_POOLED") or "").strip() or (os.getenv("DATABASE_URL") or "").strip()
    else:
        db_url = raw_url.strip()

    if not db_url:
        return "sqlite:///vaultswap.db"

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    elif db_url.startswith("postgres+"):
        db_url = db_url.replace("postgres+", "postgresql+", 1)

    if db_url.startswith("postgresql+psycopg://"):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            try:
                import psycopg2  # noqa: F401
                db_url = db_url.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
            except ImportError:
                pass
    elif db_url.startswith("postgresql+psycopg2://"):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            try:
                import psycopg  # noqa: F401
                db_url = db_url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
            except ImportError:
                pass
    elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
        try:
            import psycopg  # noqa: F401
            db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
        except ImportError:
            try:
                import psycopg2  # noqa: F401
            except ImportError:
                pass

    return db_url


def _build_engine_options(db_uri: str, pooled_url: str = "") -> dict:
    """Builds SQLAlchemy engine options for SQLite or Neon serverless/pooled PostgreSQL."""
    if db_uri.startswith("sqlite"):
        return {
            "pool_pre_ping": True,
        }

    options: dict = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
    if db_uri.startswith("postgresql+psycopg://") and ("-pooler." in db_uri or bool(pooled_url.strip())):
        options["connect_args"] = {"prepare_threshold": None}
    return options


class Config:
    """Base Flask configuration for VaultSwap."""

    SECRET_KEY = os.getenv("SECRET_KEY", "vaultswap-dev-secret-key-mtg")
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)

    # Neon PostgreSQL & Neon Auth Configuration
    DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
    DATABASE_URL_POOLED = os.getenv("DATABASE_URL_POOLED", "").strip()
    NEON_AUTH_BASE_URL = os.getenv("NEON_AUTH_BASE_URL", "").strip().rstrip("/")

    SQLALCHEMY_DATABASE_URI = _resolve_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB upload limit for ManaBox CSVs
    SQLALCHEMY_ENGINE_OPTIONS = _build_engine_options(SQLALCHEMY_DATABASE_URI, DATABASE_URL_POOLED)

    PORT = int(os.getenv("PORT", 5055))

    # Google OAuth 2.0
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")

    # Local Dev Login Mode (enabled automatically when Google OAuth is unconfigured)
    DEV_LOGIN_ENABLED = os.getenv("DEV_LOGIN_ENABLED", "").strip().lower() in ("1", "true", "yes")

    # Admin & Access Control
    ADMIN_EMAILS = {
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "jpmclaug@gmail.com").split(",")
        if e.strip()
    }
    INVITE_ONLY = os.getenv("INVITE_ONLY", "true").strip().lower() in ("1", "true", "yes")

    # Basic CSRF hardening for all state-changing POSTs
    SESSION_COOKIE_SAMESITE = "Lax"

    # Google Drive Picker import
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
    GOOGLE_CLOUD_PROJECT_NUMBER = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    DRIVE_MAX_FILE_MB = int(os.getenv("DRIVE_MAX_FILE_MB", 500))
    DRIVE_IMPORT_CHUNK_ROWS = int(os.getenv("DRIVE_IMPORT_CHUNK_ROWS", 1000))

    # Mighty Meeple Light-Touch Controls
    MEEPLE_CACHE_TTL_HOURS = float(os.getenv("MEEPLE_CACHE_TTL_HOURS", 24))
    MEEPLE_RATE_LIMIT_MIN_SEC = float(os.getenv("MEEPLE_RATE_LIMIT_MIN_SEC", 1.5))
    MEEPLE_RATE_LIMIT_MAX_SEC = float(os.getenv("MEEPLE_RATE_LIMIT_MAX_SEC", 2.0))

    # Discord Webhook Notification Batching Delay (Default: 300s = 5 minutes)
    DISCORD_WEBHOOK_DELAY_SEC = float(os.getenv("DISCORD_WEBHOOK_DELAY_SEC", 300))
