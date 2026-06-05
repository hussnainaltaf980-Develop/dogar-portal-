"""Production guardrail: assert_production_safe() must refuse weak secrets."""
from __future__ import annotations
import pytest


def test_forbidden_secrets_set_includes_known_placeholder():
    from app.core.config import _FORBIDDEN_SECRETS
    assert "change-this-secret-key-in-production" in _FORBIDDEN_SECRETS


def test_forbidden_admin_passwords_includes_admin123():
    from app.core.config import _FORBIDDEN_ADMIN_PASSWORDS
    for weak in ("admin123", "admin", "password", "123456", "changeme"):
        assert weak in _FORBIDDEN_ADMIN_PASSWORDS


def test_assert_production_safe_raises_on_weak_secret_in_production():
    from app.core.config import Settings

    s = Settings(
        SECRET_KEY="change-this-secret-key-in-production",
        DEFAULT_ADMIN_PASSWORD="admin123",
        ENV="production",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_safe()
    msg = str(exc.value).lower()
    assert "secret_key" in msg
    assert "default_admin_password" in msg


def test_assert_production_safe_passes_with_strong_values():
    from app.core.config import Settings

    s = Settings(
        SECRET_KEY="a" * 48,                           # 48 chars, not placeholder
        DEFAULT_ADMIN_PASSWORD="SuperStrongPass!9000", # 20 chars, not in weak list
        ENV="production",
    )
    s.assert_production_safe()   # must not raise


def test_assert_production_safe_only_warns_in_development():
    from app.core.config import Settings

    s = Settings(
        SECRET_KEY="change-this-secret-key-in-production",
        DEFAULT_ADMIN_PASSWORD="admin123",
        ENV="development",
    )
    s.assert_production_safe()   # development mode: warn-only, must not raise


def test_is_production_property():
    from app.core.config import Settings
    assert Settings(ENV="production").is_production is True
    assert Settings(ENV="development").is_production is False
    assert Settings(ENV="").is_production is False
