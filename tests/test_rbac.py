"""RBAC tests — permission catalog, require_permission decorator,
and the system-role presets."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.permissions import (
    PERMISSION_CATALOG, ROLE_PRESETS, all_permission_keys,
    user_permissions, user_has_permission, require_permission,
)
from app.db.session import SessionLocal
from app.models import User


# --------------------------- Catalog --------------------------------
def test_catalog_has_no_duplicate_keys():
    keys = all_permission_keys()
    assert len(keys) == len(set(keys)), "permission catalog must have unique keys"


def test_catalog_groups_are_named():
    for group in PERMISSION_CATALOG:
        assert group.get("module"), "module name required"
        assert isinstance(group.get("items"), list)
        assert group["items"], "each module must have at least one item"
        for item in group["items"]:
            assert ":" in item["key"], f"key {item['key']!r} should be module:action"
            assert item.get("label"), "every item needs a human label"


def test_admin_preset_uses_wildcard():
    assert ROLE_PRESETS["admin"] == ["*"]


def test_staff_preset_is_strict_subset_of_manager():
    """Sanity-check the presets so Staff is always ≤ Manager."""
    staff = set(ROLE_PRESETS["staff"])
    manager = set(ROLE_PRESETS["manager"])
    assert staff.issubset(manager | {"*"}), \
        f"staff has perms not in manager: {staff - manager}"


# --------------------------- Resolution -----------------------------
def test_user_permissions_admin_wildcard():
    """A user whose role='admin' must always get the wildcard."""
    db = SessionLocal()
    try:
        u = User(name="x", email="x@x.com", role="admin",
                 password_hash="x", is_active=True, is_super_admin=False)
        perms = user_permissions(u, db)
        assert "*" in perms
    finally:
        db.close()


def test_user_permissions_super_admin_wildcard():
    db = SessionLocal()
    try:
        u = User(name="x", email="x2@x.com", role="staff",
                 password_hash="x", is_active=True, is_super_admin=True)
        perms = user_permissions(u, db)
        # super_admin always trumps role
        assert "*" in perms
    finally:
        db.close()


def test_user_permissions_staff_subset():
    db = SessionLocal()
    try:
        u = User(name="x", email="x3@x.com", role="staff",
                 password_hash="x", is_active=True, is_super_admin=False)
        perms = user_permissions(u, db)
        assert "*" not in perms
        # Some baseline expectations
        assert "candidates:view" in perms
        # Staff should NOT have delete
        assert "candidates:delete" not in perms
        assert "users:delete" not in perms
    finally:
        db.close()


def test_user_has_permission_helper():
    db = SessionLocal()
    try:
        admin = User(name="a", email="a@a.com", role="admin",
                     password_hash="x", is_active=True)
        staff = User(name="b", email="b@b.com", role="staff",
                     password_hash="x", is_active=True)
        assert user_has_permission(admin, db, "anything:goes") is True
        assert user_has_permission(staff, db, "candidates:view") is True
        assert user_has_permission(staff, db, "candidates:delete") is False
    finally:
        db.close()


# --------------------------- API gates ------------------------------
def test_catalog_endpoint_returns_full_catalog(client: TestClient, auth_headers: dict):
    r = client.get("/api/users/roles/_meta/catalog", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "catalog" in body and "presets" in body and "all_keys" in body
    assert len(body["catalog"]) > 0
    assert len(body["all_keys"]) >= 30


def test_my_permissions_endpoint(client: TestClient, auth_headers: dict):
    r = client.get("/api/users/me/permissions", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] in ("admin", "Admin")
    assert body["is_wildcard"] is True


def test_admin_role_cannot_lose_wildcard(client: TestClient, auth_headers: dict):
    """Updating the system 'Admin' role with a permission list that
    drops the wildcard must return 400."""
    # Find the Admin role id
    r = client.get("/api/users/roles", headers=auth_headers)
    assert r.status_code == 200
    admin_role = next((row for row in r.json()
                       if row["name"].lower() == "admin"), None)
    assert admin_role is not None
    rid = admin_role["id"]

    # Try to weaken it — should be rejected
    r2 = client.put(f"/api/users/roles/{rid}",
                    json={"permissions": ["candidates:view"]},
                    headers=auth_headers)
    assert r2.status_code == 400, r2.text
    assert "wildcard" in r2.json().get("detail", "").lower()
