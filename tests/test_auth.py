"""Auth: login, must_change_password flag, change-password flow."""
from __future__ import annotations


def test_login_success_returns_token_and_must_change_flag(client, admin_credentials):
    r = client.post(
        "/api/auth/login",
        json={
            "username": admin_credentials["username"],
            "password": admin_credentials["password"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("access_token"), "missing access_token"
    user = body.get("user") or {}
    # Bootstrap admin MUST be flagged for forced password change
    assert user.get("must_change_password") is True, \
        f"bootstrap admin should be must_change_password=True, got {user}"
    assert user.get("email") == admin_credentials["email"]
    assert user.get("role") == "admin"


def test_login_invalid_password_rejected(client, admin_credentials):
    r = client.post(
        "/api/auth/login",
        json={"username": admin_credentials["username"], "password": "wrong-pw"},
    )
    assert r.status_code == 401


def test_login_missing_identifier_rejected(client):
    r = client.post("/api/auth/login", json={"password": "whatever"})
    assert r.status_code in (400, 422)


def test_change_password_requires_auth(client):
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
    )
    assert r.status_code == 401


def test_change_password_wrong_current_rejected(client, auth_headers, admin_credentials):
    r = client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": "definitely-not-correct",
            "new_password":     "BrandNewStrongPass123!",
        },
    )
    assert r.status_code == 400
    assert "current password" in r.json().get("detail", "").lower()


def test_change_password_too_short_rejected(client, auth_headers, admin_credentials):
    r = client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": admin_credentials["password"],
            "new_password":     "short",
        },
    )
    assert r.status_code == 400
    assert "12 characters" in r.json().get("detail", "")


def test_change_password_weak_listed_rejected(client, auth_headers, admin_credentials):
    r = client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": admin_credentials["password"],
            "new_password":     "admin123",       # in weak list
        },
    )
    assert r.status_code == 400


def test_change_password_full_flow_clears_must_change_flag(client, auth_headers, admin_credentials):
    """End-to-end: change PW, re-login, verify must_change_password=False."""
    new_pw = "ProductionGradePass!2026"

    # 1. change it
    r = client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": admin_credentials["password"],
            "new_password":     new_pw,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    # 2. old PW should now fail
    r2 = client.post(
        "/api/auth/login",
        json={"username": admin_credentials["username"],
              "password": admin_credentials["password"]},
    )
    assert r2.status_code == 401

    # 3. new PW should succeed AND must_change_password should be False
    r3 = client.post(
        "/api/auth/login",
        json={"username": admin_credentials["username"], "password": new_pw},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["user"]["must_change_password"] is False

    # 4. restore original PW so subsequent tests using `admin_token` keep working
    new_token = r3.json()["access_token"]
    r4 = client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {new_token}"},
        json={
            "current_password": new_pw,
            "new_password":     admin_credentials["password"],
        },
    )
    assert r4.status_code == 200
