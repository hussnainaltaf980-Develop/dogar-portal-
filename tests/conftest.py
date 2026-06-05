"""Pytest fixtures — isolated SQLite + FastAPI TestClient.

Every test module gets its own throwaway SQLite database file so the
real ``data/dogar_trading.db`` is never touched. The bootstrap admin is
seeded automatically so authentication-dependent tests can log in.
"""
from __future__ import annotations

import os
import tempfile
import pytest

# IMPORTANT: env vars MUST be set BEFORE importing the app, because
# `app.core.config.Settings` reads them at import time.
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db", prefix="dtc_test_")
os.close(_TMP_DB_FD)

os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_PATH}"
os.environ["SECRET_KEY"] = "test-secret-key-thats-at-least-thirty-two-chars-long-yes"
os.environ["DEFAULT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "TestAdminPass123!"
os.environ["DEFAULT_ADMIN_NAME"] = "Test Admin"
os.environ["ENV"] = "development"   # don't trigger production guard during tests
# Enable the OPTIONAL env-driven super-admin so its test suite has an
# account to exercise. Production deployments leave these unset.
os.environ["SUPER_ADMIN_EMAIL"] = "superadmin@test.local"
os.environ["SUPER_ADMIN_PASSWORD"] = "SuperAdminPass123!"
os.environ["SUPER_ADMIN_NAME"] = "Test Super Admin"

# Now safe to import the app
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.db.session import SessionLocal, engine, Base  # noqa: E402
from app.db.init_db import (  # noqa: E402
    create_tables, seed_admin_user, seed_document_templates, seed_roles,
)


def _bootstrap_db():
    Base.metadata.create_all(bind=engine)
    create_tables()
    db = SessionLocal()
    try:
        seed_admin_user(db)
        # Seed system roles so RBAC tests can find them
        try:
            seed_roles(db)
        except Exception as exc:
            import logging
            logging.getLogger("dtc.tests").warning("role seeding skipped: %s", exc)
        try:
            seed_document_templates(db)
        except Exception as exc:   # template seeding is non-critical for most tests
            import logging
            logging.getLogger("dtc.tests").warning("template seeding skipped: %s", exc)
    finally:
        db.close()


_bootstrap_db()


@pytest.fixture(scope="session")
def client() -> TestClient:
    """Shared TestClient — uses the isolated SQLite from above."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_client_cookies(request):
    """Ensure every test starts UNAUTHENTICATED.

    The login endpoint now sets a non-Secure ``samesite=lax`` cookie in
    development, which the shared session-scoped TestClient persists
    between tests. Without this reset, a login in one test would leak an
    auth cookie into later "requires-auth" tests. Clearing the cookie
    jar before each test keeps tests isolated and deterministic.
    """
    cl = request.getfixturevalue("client") if "client" in request.fixturenames else None
    if cl is not None:
        cl.cookies.clear()
    yield
    if cl is not None:
        cl.cookies.clear()


@pytest.fixture(scope="session")
def admin_credentials() -> dict:
    return {
        "username": os.environ["DEFAULT_ADMIN_EMAIL"],
        "email":    os.environ["DEFAULT_ADMIN_EMAIL"],
        "password": os.environ["DEFAULT_ADMIN_PASSWORD"],
    }


@pytest.fixture
def admin_token(client: TestClient, admin_credentials: dict) -> str:
    """Return a fresh JWT access token for the bootstrap admin."""
    r = client.post(
        "/api/auth/login",
        json={
            "username": admin_credentials["username"],
            "password": admin_credentials["password"],
        },
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


def pytest_sessionfinish(session, exitstatus):
    """Best-effort cleanup of the temp SQLite file at the end of the run."""
    try:
        if os.path.exists(_TMP_DB_PATH):
            os.unlink(_TMP_DB_PATH)
    except OSError:
        pass
