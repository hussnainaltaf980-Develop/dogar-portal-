import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date, datetime
import json

from app.db.session import get_db
from app.core.deps import get_current_user, require_admin
from app.core.security import hash_password, verify_password
from app.core.config import settings
from app.models import User
from app.models.lookups import LoginHistory, Role
from app.schemas.schemas import (
    UserCreate, UserUpdate, UserOut,
    ProfileUpdate, PasswordChange,
)

router = APIRouter()


# ============================================================================
# Self-service profile endpoints — the logged-in user editing their own
# account. NO admin permission required.
# ============================================================================
@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.put("/me", response_model=UserOut)
def update_my_profile(
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update the currently-logged-in user's own profile.
    Role / is_active / email / password are intentionally NOT editable here —
    they must go through the admin user-management screen, or via the
    `/change-password` endpoint for password.
    """
    obj = db.query(User).filter(User.id == user.id).first()
    if not obj:
        raise HTTPException(404, "User not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/me/photo", response_model=UserOut)
async def upload_my_photo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a new profile photo for the current user. Stored under
    /static/uploads/avatars/<userid>_<uuid>.<ext> and the URL is saved to
    `users.photo`.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    # Decide extension
    ext_map = {
        "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/png": "png", "image/webp": "webp", "image/gif": "gif",
    }
    ext = ext_map.get(file.content_type.lower(), "jpg")
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()[:5] or ext

    # Save file
    avatar_dir = os.path.join(settings.UPLOAD_DIR, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    fname = f"{user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    fpath = os.path.join(avatar_dir, fname)
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:    # 5 MB limit
        raise HTTPException(400, "Image too large (max 5MB)")
    with open(fpath, "wb") as f:
        f.write(contents)

    # Update user
    obj = db.query(User).filter(User.id == user.id).first()
    if not obj:
        raise HTTPException(404, "User not found")
    obj.photo = f"/static/uploads/avatars/{fname}"
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/me/change-password")
def change_my_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change the current user's password. Requires the current password."""
    obj = db.query(User).filter(User.id == user.id).first()
    if not obj:
        raise HTTPException(404, "User not found")
    if not verify_password(payload.current_password, obj.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    obj.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True, "message": "Password changed successfully"}


@router.get("/", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return db.query(User).order_by(User.id.desc()).all()


@router.post("/", response_model=UserOut)
def create_user(payload: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(400, "Email already registered")
    data = payload.model_dump()
    pwd = data.pop("password")
    obj = User(**data, password_hash=hash_password(pwd))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    obj = db.query(User).filter(User.id == user_id).first()
    if not obj:
        raise HTTPException(404, "User not found")

    # Super-admin protection: only the super-admin themself may modify a
    # super-admin row, and even then they cannot drop their own role or
    # super-admin flag.
    if obj.is_super_admin and not admin.is_super_admin:
        raise HTTPException(403, "Cannot modify the permanent super-admin account")

    data = payload.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        obj.password_hash = hash_password(data.pop("password"))
    elif "password" in data:
        data.pop("password")

    # Even the super-admin cannot demote themselves or disable themselves —
    # otherwise a confused click could lock the vendor out of the system.
    if obj.is_super_admin:
        data.pop("role", None)
        data.pop("is_active", None)
        data.pop("is_super_admin", None)

    for k, v in data.items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(400, "Cannot delete yourself")
    obj = db.query(User).filter(User.id == user_id).first()
    if not obj:
        raise HTTPException(404, "User not found")
    # Permanent super-admin can never be deleted, full stop.
    if obj.is_super_admin:
        raise HTTPException(403, "The permanent super-admin account cannot be deleted")
    db.delete(obj)
    db.commit()
    return {"ok": True}


# ===== Login History =====
@router.get("/login-history")
def login_history(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    status: Optional[str] = Query(None, description="Success | Failed | All"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = Query(None, description="search email"),
    skip: int = 0,
    limit: int = 100,
):
    query = db.query(LoginHistory)
    if status and status.lower() not in ("all", ""):
        query = query.filter(LoginHistory.status == status)
    if date_from:
        query = query.filter(LoginHistory.occurred_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(LoginHistory.occurred_at <= datetime.combine(date_to, datetime.max.time()))
    if q:
        query = query.filter(LoginHistory.email.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(LoginHistory.occurred_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "email": r.email,
                "status": r.status,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            }
            for r in rows
        ],
    }


# ===== Roles =====
@router.get("/roles")
def list_roles(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rows = db.query(Role).order_by(Role.id.asc()).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "is_system": r.is_system,
            "permissions": json.loads(r.permissions or "[]"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/roles")
def create_role(payload: dict, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if not payload.get("name"):
        raise HTTPException(400, "Name is required")
    if db.query(Role).filter(Role.name == payload["name"]).first():
        raise HTTPException(400, "Role name already exists")
    role = Role(
        name=payload["name"],
        description=payload.get("description", ""),
        is_system=False,
        permissions=json.dumps(payload.get("permissions", [])),
    )
    db.add(role); db.commit(); db.refresh(role)
    return {"id": role.id, "name": role.name}


@router.put("/roles/{role_id}")
def update_role(role_id: int, payload: dict, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role: raise HTTPException(404, "Role not found")
    # System roles (Admin/Manager/Staff) — only permissions and
    # description are editable, name is locked so we don't break the
    # FK-style join in user.role.
    is_admin_role = (role.name or "").strip().lower() == "admin"
    if role.is_system:
        if "name" in payload:
            raise HTTPException(400, "System role name cannot be changed")
        if is_admin_role and "permissions" in payload:
            # Admin role MUST always be the wildcard — refuse to weaken it
            perms = payload.get("permissions") or []
            if "*" not in perms:
                raise HTTPException(
                    400, "Admin role must keep the wildcard '*' permission. "
                         "Create a custom role instead if you need to scope down."
                )
    if "name" in payload and not role.is_system:
        role.name = payload["name"]
    if "description" in payload:
        role.description = payload["description"]
    if "permissions" in payload:
        role.permissions = json.dumps(payload["permissions"])
    db.commit(); return {"ok": True}


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role: raise HTTPException(404, "Role not found")
    if role.is_system:
        raise HTTPException(400, "Cannot delete system roles")
    db.delete(role); db.commit(); return {"ok": True}


# ===== RBAC meta endpoints =====
@router.get("/roles/_meta/catalog")
def roles_catalog(user: User = Depends(get_current_user)):
    """Public to authenticated users — returns the permission catalog
    so the role editor UI can render its checkbox matrix.

    Also returns the default presets so the UI can offer a
    "Reset to Manager default" / "Reset to Staff default" button.
    """
    from app.core.permissions import PERMISSION_CATALOG, ROLE_PRESETS, all_permission_keys
    return {
        "catalog": PERMISSION_CATALOG,
        "presets": ROLE_PRESETS,
        "all_keys": all_permission_keys(),
    }


@router.get("/me/permissions")
def my_permissions(db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Frontend permission gate — returns the effective permission set
    for the currently signed-in user. UI elements use this to hide /
    disable controls the user can't action."""
    from app.core.permissions import user_permissions
    perms = user_permissions(user, db)
    return {
        "user_id": user.id,
        "role": user.role,
        "is_super_admin": bool(getattr(user, "is_super_admin", False)),
        "permissions": sorted(perms),
        "is_wildcard": "*" in perms,
    }
