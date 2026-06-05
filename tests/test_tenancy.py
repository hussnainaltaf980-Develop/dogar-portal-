"""Tests for the multi-tenant subdomain resolver and per-tenant
letterhead/asset endpoints."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.core.tenancy import extract_subdomain, ROOT_HOSTS


# --------------------------- Subdomain parsing ----------------------
def test_extract_subdomain_localhost_is_none():
    """localhost / 127.0.0.1 / bare IPs must NOT be treated as tenants."""
    assert extract_subdomain("localhost") is None
    assert extract_subdomain("localhost:3000") is None
    assert extract_subdomain("127.0.0.1") is None
    assert extract_subdomain("0.0.0.0:3000") is None


def test_extract_subdomain_two_label_root_is_none():
    """A bare 2-label apex (example.com) must not be parsed as a tenant."""
    assert extract_subdomain("dogar.com") is None
    # 3+ labels DO get parsed — that's the intended subdomain pattern
    # used by demo.oep.com.pk → 'demo' (test below).


def test_extract_subdomain_www_app_filtered():
    """www / app / portal are common roots, not tenants."""
    for prefix in ("www", "app"):
        assert extract_subdomain(f"{prefix}.dogar.com") is None


def test_extract_subdomain_picks_first_label_when_3plus():
    """demo.oep.com.pk → 'demo'"""
    val = extract_subdomain("demo.oep.com.pk")
    assert val == "demo"


def test_extract_subdomain_strips_port():
    assert extract_subdomain("demo.oep.com.pk:8080") == "demo"


def test_root_hosts_includes_localhost():
    assert "localhost" in ROOT_HOSTS
    assert "127.0.0.1" in ROOT_HOSTS


# --------------------------- /api/tenants/_meta/current -------------
def test_meta_current_no_subdomain_is_default(client: TestClient):
    """When the Host header is localhost the middleware should
    return the default-tenant metadata."""
    r = client.get("/api/tenants/_meta/current",
                   headers={"Host": "localhost:3000"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_default"] is True
    assert body["slug"] is None
    assert body["company_name"]
    assert body["letterhead_url"] is None or body["letterhead_url"] == ""


# --------------------------- Branding endpoints (smoke) -------------
def test_branding_endpoints_require_auth(client: TestClient):
    """Unauthenticated requests to branding endpoints must 401."""
    # No cookie / no Authorization header → 401
    r = client.put("/api/tenants/999/branding", json={"office_name": "X"})
    assert r.status_code in (401, 403)


def test_branding_404_for_missing_tenant(client: TestClient, auth_headers: dict):
    """Admin-auth'd PUT to a non-existent tenant returns 404."""
    r = client.put("/api/tenants/99999/branding",
                   json={"office_name": "Ghost Office"},
                   headers=auth_headers)
    assert r.status_code == 404


def test_logo_upload_404_for_missing_tenant(client: TestClient, auth_headers: dict):
    """POST /tenants/{id}/logo on a missing tenant must 404."""
    files = {"file": ("logo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16), "image/png")}
    r = client.post("/api/tenants/99999/logo",
                    files=files, headers=auth_headers)
    assert r.status_code == 404
