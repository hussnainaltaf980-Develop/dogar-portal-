"""Tests for the permanent super-admin (developer) account.

Covers:
  * seed_super_admin creates the developer row with the locked
    credentials AND keeps it idempotent.
  * The super-admin row cannot be demoted or deleted via the API,
    even by another admin.
  * Super-admin's password is restored if the password_hash is wiped
    by an external process (manual SQL update).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.db.init_db import (
    SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD, seed_super_admin,
)
from app.models import User


# Seed the super-admin once for the whole module.
@pytest.fixture(scope="module", autouse=True)
def _seed_super_admin():
    db = SessionLocal()
    try:
        seed_super_admin(db)
    finally:
        db.close()


def test_super_admin_row_exists():
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        assert u is not None, "super-admin must be seeded"
        assert u.is_super_admin is True
        assert u.role == "admin"
        assert u.is_active is True
        assert u.must_change_password is False
    finally:
        db.close()


def test_super_admin_can_login(client: TestClient):
    r = client.post("/api/auth/login", json={
        "username": SUPER_ADMIN_EMAIL,
        "password": SUPER_ADMIN_PASSWORD,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == SUPER_ADMIN_EMAIL
    assert body["user"]["role"] == "admin"


def test_super_admin_seed_is_idempotent():
    """Calling seed_super_admin twice must not create a duplicate row
    and must not change the stable id."""
    db = SessionLocal()
    try:
        before = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        before_id = before.id
        seed_super_admin(db)
        after = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        assert after.id == before_id
        # And there's still only ONE row with that email
        n = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).count()
        assert n == 1
    finally:
        db.close()


def test_super_admin_password_restored_if_wiped():
    """If the password_hash column is manually cleared the next
    seed_super_admin() call must restore it so the developer can
    always sign in."""
    from app.core.security import hash_password, verify_password
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        u.password_hash = ""
        db.commit()
        seed_super_admin(db)
        u = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        assert u.password_hash, "password hash should be restored"
        assert verify_password(SUPER_ADMIN_PASSWORD, u.password_hash)
    finally:
        db.close()


def test_super_admin_cannot_be_deleted(client: TestClient, auth_headers: dict):
    """Even an admin cannot DELETE the super-admin row."""
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
        uid = u.id
    finally:
        db.close()
    r = client.delete(f"/api/users/{uid}", headers=auth_headers)
    assert r.status_code == 403, r.text
    assert "super-admin" in r.json().get("detail", "").lower()


def test_super_admin_can_use_protected_routes(client: TestClient):
    """Super-admin's wildcard permission must unlock everything."""
    r = client.post("/api/auth/login", json={
        "username": SUPER_ADMIN_EMAIL,
        "password": SUPER_ADMIN_PASSWORD,
    })
    assert r.status_code == 200
    tok = r.json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    # /api/users/me/permissions should report wildcard
    r2 = client.get("/api/users/me/permissions", headers=h)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["is_super_admin"] is True
    assert body["is_wildcard"] is True
    assert "*" in body["permissions"]
