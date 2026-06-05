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
import secrets
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings

log = logging.getLogger("dtc.config")

SQLITE_URL = "sqlite:///./data/dogar_trading.db"

# Values we MUST refuse in production — these have appeared in the
# public repo / docs at some point so anyone could try them.
_FORBIDDEN_SECRETS = {
    "change-this-secret-key-in-production",
    "change-this-secret-key-in-production-use-openssl-rand-hex-32",
    "",
}
_FORBIDDEN_ADMIN_PASSWORDS = {
    "admin123",
    "admin",
    "password",
    "123456",
    "changeme",
    "change-this-password",
}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    APP_NAME: str = "Dogar Trading Corporation Portal"
    APP_VERSION: str = "2.0.0"
    # SECRET_KEY intentionally has NO usable default — see get_settings(),
    # which auto-generates an ephemeral key in development and HARD-FAILS
    # in production when it is unset.
    SECRET_KEY: str = ""
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

    # ------------------------------------------------------------------
    # Feature flags (all OFF by default so production is safe-by-default)
    # ------------------------------------------------------------------
    # When True, init_db seeds fake demo clients/candidates/agents. This
    # MUST stay False in production — it pollutes the live DB with junk.
    SEED_DEMO_DATA: bool = False
    # When True, the bundled SQL dump is auto-restored into an empty DB
    # on boot. Disable in production to avoid accidental data injection.
    AUTO_RESTORE_BUNDLED_SQL: bool = False
    # When True, forgot-password responses include the raw reset token /
    # URL (useful for internal/dev portals with no email transport).
    # MUST be False in production.
    EXPOSE_RESET_TOKEN: bool = False
    # Optional permanent super-admin (vendor/developer) account. Disabled
    # by default — enable only by setting BOTH email + password via env.
    SUPER_ADMIN_EMAIL: Optional[str] = None
    SUPER_ADMIN_PASSWORD: Optional[str] = None
    SUPER_ADMIN_NAME: str = "Super Admin"
    # Comma-separated list of allowed CORS origins (empty = same-origin only).
    CORS_ORIGINS: str = ""
    # Force HTTPS-only secure cookies. Auto-True in production.
    COOKIE_SECURE: Optional[bool] = None

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
                "SECRET_KEY is unset or set to a known placeholder — "
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

        # A super-admin may only be enabled with BOTH halves set, and the
        # password must be strong — no weak vendor back-doors in prod.
        if self.SUPER_ADMIN_EMAIL or self.SUPER_ADMIN_PASSWORD:
            if not (self.SUPER_ADMIN_EMAIL and self.SUPER_ADMIN_PASSWORD):
                problems.append(
                    "SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must BOTH be set "
                    "to enable the super-admin account (or leave both empty)."
                )
            elif (self.SUPER_ADMIN_PASSWORD or "").strip().lower() in _FORBIDDEN_ADMIN_PASSWORDS:
                problems.append("SUPER_ADMIN_PASSWORD is a known weak default — pick a strong password")
            elif len(self.SUPER_ADMIN_PASSWORD or "") < 12:
                problems.append("SUPER_ADMIN_PASSWORD must be at least 12 characters")

        if self.SEED_DEMO_DATA:
            problems.append("SEED_DEMO_DATA must be False in production (no fake demo records)")
        if self.EXPOSE_RESET_TOKEN:
            problems.append("EXPOSE_RESET_TOKEN must be False in production (never leak reset tokens)")

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

    @property
    def cookie_secure(self) -> bool:
        """Whether auth cookies should be marked Secure (HTTPS-only)."""
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.is_production

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.CORS_ORIGINS or "").split(",") if o.strip()]


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Force SQLite — some hosts inject a PostgreSQL DATABASE_URL that we
    # cannot consume yet. Drop it back to SQLite to avoid silent
    # mis-configuration.
    if s.DATABASE_URL.startswith("postgres"):
        object.__setattr__(s, "DATABASE_URL", SQLITE_URL)

    # SECRET_KEY handling:
    #  • production  → must be supplied via env (assert_production_safe enforces)
    #  • dev/staging → auto-generate a strong ephemeral key so the app boots
    #                  without shipping a hardcoded placeholder. Tokens won't
    #                  survive a restart, which is fine for local dev.
    if not s.SECRET_KEY or s.SECRET_KEY in _FORBIDDEN_SECRETS:
        if (s.ENV or "development").lower() != "production":
            object.__setattr__(s, "SECRET_KEY", secrets.token_hex(32))
            log.warning(
                "SECRET_KEY not set — generated an EPHEMERAL dev key. "
                "Set SECRET_KEY in .env for stable sessions."
            )

    # Ensure directories exist
    db_path = s.DATABASE_URL.replace("sqlite:///", "").replace("./", "")
    os.makedirs(os.path.dirname(db_path) or "data", exist_ok=True)
    os.makedirs(s.UPLOAD_DIR, exist_ok=True)
    os.makedirs(s.PDF_BG_DIR, exist_ok=True)
    # Enforce production guardrails (or log warnings outside prod).
    s.assert_production_safe()
    return s


settings = get_settings()
