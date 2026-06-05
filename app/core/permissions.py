"""Role-Based Access Control (RBAC) — permission catalog + helpers.

A Role row has a ``permissions`` TEXT column storing a JSON array of
permission *keys* (e.g. ``["candidates:view", "documents:print"]``).
This module defines:

  * ``PERMISSION_CATALOG`` — the full menu of permission keys the UI
    can offer when editing a role, grouped by module.
  * ``ROLE_PRESETS`` — default permission sets for the three system
    roles (Admin / Manager / Staff) so first-launch is sensible.
  * ``user_permissions(user, db)`` — resolve the *effective* permission
    set for a user (joining their role to the Role row).
  * ``user_has_permission(user, db, perm)`` — boolean check.
  * ``require_permission(perm)`` — FastAPI dependency factory. Use as
    ``Depends(require_permission("candidates:edit"))``. Admin and
    super-admin **always** bypass; an explicit "*" permission also
    grants everything.

Design notes
------------
* The decorator is intentionally simple — no nested groups, no
  inheritance — because the user explicitly asked for *granular
  per-tenant role configuration*, not a heavyweight policy engine.
* The catalog is the SOURCE OF TRUTH consumed by the role editor UI
  via ``GET /api/roles/_meta/catalog`` — adding a new permission key
  here automatically surfaces it in the UI.
* Permission keys follow the pattern ``module:action`` so they're
  self-documenting in logs and JWT-claims-style usage.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.models.lookups import Role

log = logging.getLogger("dtc.rbac")


# ---------------------------------------------------------------------
# CATALOG — every UI module / capability the role editor can toggle
# ---------------------------------------------------------------------
PERMISSION_CATALOG: list[dict] = [
    {"module": "Dashboard", "items": [
        {"key": "dashboard:view", "label": "View dashboard"},
    ]},
    {"module": "Clients", "items": [
        {"key": "clients:view",   "label": "View clients"},
        {"key": "clients:create", "label": "Create client"},
        {"key": "clients:edit",   "label": "Edit client"},
        {"key": "clients:delete", "label": "Delete client"},
    ]},
    {"module": "Demand Files", "items": [
        {"key": "demands:view",   "label": "View demand files"},
        {"key": "demands:create", "label": "Create demand"},
        {"key": "demands:edit",   "label": "Edit demand"},
        {"key": "demands:delete", "label": "Delete demand"},
        {"key": "demands:print",  "label": "Print demand documents"},
    ]},
    {"module": "Candidates", "items": [
        {"key": "candidates:view",        "label": "View candidates"},
        {"key": "candidates:create",      "label": "Create candidate"},
        {"key": "candidates:edit",        "label": "Edit candidate"},
        {"key": "candidates:delete",      "label": "Delete candidate"},
        {"key": "candidates:stage",       "label": "Change workflow stage"},
        {"key": "candidates:assign",      "label": "Assign to demand"},
        {"key": "candidates:documents",   "label": "Upload / view candidate documents"},
        {"key": "candidates:print",       "label": "Print candidate profile / barcode"},
        {"key": "candidates:export",      "label": "Export candidates"},
    ]},
    {"module": "Documents", "items": [
        {"key": "documents:view",    "label": "View document templates"},
        {"key": "documents:edit",    "label": "Edit document templates"},
        {"key": "documents:designer","label": "Use coordinate designer"},
        {"key": "documents:print",   "label": "Print generated documents"},
    ]},
    {"module": "Protector Letters", "items": [
        {"key": "protector:view",  "label": "View protector workflow"},
        {"key": "protector:print", "label": "Print protector packet"},
        {"key": "protector:edit",  "label": "Edit protector data (E-number etc.)"},
    ]},
    {"module": "Receipts & Finance", "items": [
        {"key": "receipts:view",   "label": "View receipts"},
        {"key": "receipts:create", "label": "Issue receipt"},
        {"key": "receipts:print",  "label": "Print receipt"},
        {"key": "depositors:view", "label": "View depositors"},
        {"key": "depositors:edit", "label": "Manage depositors"},
        {"key": "agents:view",     "label": "View sub-agent cashbook"},
        {"key": "agents:edit",     "label": "Edit sub-agent entries"},
    ]},
    {"module": "Reports", "items": [
        {"key": "reports:view", "label": "View reports"},
        {"key": "reports:export", "label": "Export reports"},
    ]},
    {"module": "Users & Roles", "items": [
        {"key": "users:view",   "label": "View users"},
        {"key": "users:create", "label": "Create user"},
        {"key": "users:edit",   "label": "Edit user"},
        {"key": "users:delete", "label": "Delete user"},
        {"key": "roles:view",   "label": "View roles"},
        {"key": "roles:edit",   "label": "Create / edit roles"},
    ]},
    {"module": "Settings", "items": [
        {"key": "settings:view",     "label": "View settings"},
        {"key": "settings:company",  "label": "Edit company settings"},
        {"key": "settings:lookups",  "label": "Edit lookups (cities, embassies…)"},
        {"key": "settings:tenants",  "label": "Manage tenant portals (Copy Portal)"},
        {"key": "settings:branding", "label": "Manage per-tenant branding"},
    ]},
    {"module": "Audit & Security", "items": [
        {"key": "audit:view",         "label": "View audit log"},
        {"key": "loginhistory:view",  "label": "View login history"},
    ]},
]


def all_permission_keys() -> list[str]:
    """Flat list of every permission key in the catalog."""
    out: list[str] = []
    for group in PERMISSION_CATALOG:
        for item in group["items"]:
            out.append(item["key"])
    return out


# ---------------------------------------------------------------------
# PRESETS — sensible defaults for the three system roles
# ---------------------------------------------------------------------
ROLE_PRESETS: dict[str, list[str]] = {
    # Admin — everything (the asterisk wildcard is preferred so new
    # permissions added later automatically apply to admins).
    "admin": ["*"],

    # Manager — full operational access but no user/role management,
    # no tenant management, no audit log.
    "manager": [
        "dashboard:view",
        "clients:view", "clients:create", "clients:edit",
        "demands:view", "demands:create", "demands:edit", "demands:print",
        "candidates:view", "candidates:create", "candidates:edit",
        "candidates:stage", "candidates:assign",
        "candidates:documents", "candidates:print", "candidates:export",
        "documents:view", "documents:edit", "documents:designer", "documents:print",
        "protector:view", "protector:print", "protector:edit",
        "receipts:view", "receipts:create", "receipts:print",
        "depositors:view", "depositors:edit",
        "agents:view", "agents:edit",
        "reports:view", "reports:export",
        "settings:view", "settings:lookups",
    ],

    # Staff — day-to-day data entry. Can view + create + edit own
    # work but cannot delete, change company settings, or print
    # financial documents.
    "staff": [
        "dashboard:view",
        "clients:view",
        "demands:view",
        "candidates:view", "candidates:create", "candidates:edit",
        "candidates:documents", "candidates:print",
        "documents:view", "documents:print",
        "protector:view",
        "receipts:view",
        "reports:view",
    ],
}


# ---------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------
def _parse_perms(raw: Optional[str]) -> list[str]:
    """Safely parse the Role.permissions JSON column."""
    if not raw:
        return []
    try:
        out = json.loads(raw)
        if isinstance(out, list):
            return [str(x) for x in out]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


def user_permissions(user: User, db: Session) -> set[str]:
    """Return the effective permission set for ``user``.

    Resolution order:
      1. Super-admin / role='admin' → ``{"*"}`` (wildcard).
      2. Otherwise look up the Role row whose ``name`` matches
         ``user.role`` (case-insensitive) and parse its
         ``permissions`` JSON list.
      3. If no Role row matches, fall back to ``ROLE_PRESETS`` for
         the role name (so a brand-new DB without seeded Role rows
         still has sane defaults).
      4. If still empty, return ``set()`` (deny everything).
    """
    if getattr(user, "is_super_admin", False):
        return {"*"}
    role_name = (user.role or "").strip().lower()
    if role_name == "admin":
        return {"*"}

    # Look up the Role row
    row = (db.query(Role)
             .filter(Role.name.ilike(role_name))
             .first())
    if row is not None:
        perms = _parse_perms(row.permissions)
        if perms:
            return set(perms)

    # Fallback to presets
    preset = ROLE_PRESETS.get(role_name)
    if preset:
        return set(preset)

    return set()


def user_has_permission(user: User, db: Session, perm: str) -> bool:
    """Cheap convenience wrapper."""
    perms = user_permissions(user, db)
    return "*" in perms or perm in perms


# ---------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------
def require_permission(*perms: str, mode: str = "any"):
    """Return a FastAPI dependency that 403's if the user lacks any
    of the requested permissions.

    Parameters
    ----------
    perms : str
        One or more permission keys. With ``mode="any"`` (the default)
        the user needs **any one** of them; with ``mode="all"`` the
        user needs **all** of them.

    Notes
    -----
    * Admin / super-admin / wildcard (``"*"``) always pass.
    * Failures return **403** with a human-readable detail so the
      frontend can surface the missing permission directly.
    """
    if mode not in ("any", "all"):
        raise ValueError("mode must be 'any' or 'all'")
    required: Sequence[str] = tuple(perms)

    def _dep(user: User = Depends(get_current_user),
             db: Session = Depends(get_db)) -> User:
        granted = user_permissions(user, db)
        if "*" in granted:
            return user
        if mode == "any":
            ok = any(p in granted for p in required)
        else:
            ok = all(p in granted for p in required)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=("Permission denied — requires "
                        + (" or ".join(required) if mode == "any"
                           else " + ".join(required))),
            )
        return user

    return _dep
