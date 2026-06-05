"""Application configuration loaded from environment variables.

Production-hardening rules enforced here (when ``ENV=production``):
  * SECRET_KEY must NOT be the placeholder / "change-this-…" value
  * SECRET_KEY must be at least 32 chars (256 bits)
  * DEFAULT_ADMIN_PASSWORD must NOT be one of the known weak defaults
  * DEFAULT_ADMIN_PASSWORD must be at least 12 chars

Violations raise ``RuntimeError`` at startup so a misconfigured
production deploy fails loudly instead of silently exposing the
documented credentials.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from pydantic_settings import BaseSettings

log = logging.getLogger("dtc.config")

SQLITE_URL = "sqlite:///./data/dogar_trading.db"

# Values we MUST refuse in production — these have appeared in the
# public repo / docs at some point so anyone could try them.
_FORBIDDEN_SECRETS = {
    "change-this-secret-key-in-production",
    "change-this-secret-key-in-production-use-openssl-rand-hex-32",
}
_FORBIDDEN_ADMIN_PASSWORDS = {
    "admin123",
    "admin",
    "password",
    "123456",
    "changeme",
    "change-this-password",
}


class Settings(BaseSettings):
    APP_NAME: str = "Dogar Trading Corporation Portal"
    APP_VERSION: str = "1.0.0"
    SECRET_KEY: str = "change-this-secret-key-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    DATABASE_URL: str = SQLITE_URL
    UPLOAD_DIR: str = "app/static/uploads"
    PDF_BG_DIR: str = "app/static/pdf_backgrounds"
    DEFAULT_ADMIN_EMAIL: str = "admin@dogartrading.com"
    DEFAULT_ADMIN_PASSWORD: str = "admin123"
    DEFAULT_ADMIN_NAME: str = "Administrator"
    COMPANY_NAME: str = "Dogar Trading Corporation"
    COMPANY_TAGLINE: str = "Global Trading & Overseas Employment Solutions"
    ALGORITHM: str = "HS256"
    # Runtime mode — controls strictness of the security guardrails.
    # Allowed: "development", "staging", "production"
    ENV: str = "development"

    class Config:
        env_file = ".env"
        extra = "ignore"

    # ------------------------------------------------------------------
    # Production-readiness guard
    # ------------------------------------------------------------------
    def assert_production_safe(self) -> None:
        """Raise RuntimeError if the live config still ships weak defaults.

        Only enforced when ``ENV=production``. Development / staging
        deploys log warnings instead so the team can iterate.
        """
        problems: list[str] = []

        if not self.SECRET_KEY or self.SECRET_KEY in _FORBIDDEN_SECRETS:
            problems.append(
                "SECRET_KEY is set to the documented placeholder — "
                "regenerate with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be at least 32 chars (256 bits)")

        if (self.DEFAULT_ADMIN_PASSWORD or "").strip().lower() in _FORBIDDEN_ADMIN_PASSWORDS:
            problems.append(
                f"DEFAULT_ADMIN_PASSWORD is a known weak default "
                f"({self.DEFAULT_ADMIN_PASSWORD!r}) — pick a strong password"
            )
        if len(self.DEFAULT_ADMIN_PASSWORD or "") < 12:
            problems.append("DEFAULT_ADMIN_PASSWORD must be at least 12 characters")

        if not problems:
            return

        mode = (self.ENV or "development").lower()
        msg = (
            "Production-readiness check FAILED:\n  - "
            + "\n  - ".join(problems)
            + "\nFix the listed values in .env, or set ENV=development to bypass."
        )
        if mode == "production":
            raise RuntimeError(msg)
        log.warning("[%s] %s", mode, msg)

    @property
    def is_production(self) -> bool:
        return (self.ENV or "").lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Force SQLite — some hosts inject a PostgreSQL DATABASE_URL that we
    # cannot consume yet. Drop it back to SQLite to avoid silent
    # mis-configuration.
    if s.DATABASE_URL.startswith("postgres"):
        object.__setattr__(s, "DATABASE_URL", SQLITE_URL)
    # Ensure directories exist
    db_path = s.DATABASE_URL.replace("sqlite:///", "").replace("./", "")
    os.makedirs(os.path.dirname(db_path) or "data", exist_ok=True)
    os.makedirs(s.UPLOAD_DIR, exist_ok=True)
    os.makedirs(s.PDF_BG_DIR, exist_ok=True)
    # Enforce production guardrails (or log warnings outside prod).
    s.assert_production_safe()
    return s


settings = get_settings()
