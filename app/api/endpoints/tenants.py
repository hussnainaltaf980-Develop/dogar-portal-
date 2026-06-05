"""Multi-tenant Copy Portal endpoints (admin-only).

POST   /api/tenants                       — provision new tenant (creates isolated DB)
GET    /api/tenants                       — list all tenants
GET    /api/tenants/{id}                  — tenant detail
PUT    /api/tenants/{id}                  — update branding / contact
PUT    /api/tenants/{id}/features         — update feature flags
POST   /api/tenants/{id}/logo             — upload tenant logo (image)
POST   /api/tenants/{id}/letterhead       — upload tenant letterhead (image)
PUT    /api/tenants/{id}/branding         — set office_name, demand_format, receipt_template
DELETE /api/tenants/{id}                  — soft-archive tenant (moves DB to archive/)
GET    /api/tenants/_meta/current         — info about the tenant resolved for this request
"""
import os
import shutil
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field

from app.db.session import get_db
from app.core.deps import get_current_user
from app.models import Tenant, User
from app.services.tenant_service import (
    slugify, tenant_db_path, provision_tenant_db, archive_tenant_db,
)

router = APIRouter()


# -------- Schemas --------
class TenantCreate(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=200)
    short_name: Optional[str] = None
    subtitle: Optional[str] = "Overseas Employment Promoters"
    admin_name: str = Field(..., min_length=2, max_length=150)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=6, max_length=100)
    contact_phone: Optional[str] = None
    plan: Optional[str] = "standard"
    primary_color: Optional[str] = "#2563eb"
    features: Optional[dict] = None
    notes: Optional[str] = None


class TenantUpdate(BaseModel):
    company_name: Optional[str] = None
    short_name: Optional[str] = None
    subtitle: Optional[str] = None
    primary_color: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    plan: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class FeaturesUpdate(BaseModel):
    features: dict


class BrandingUpdate(BaseModel):
    """Per-tenant enterprise branding payload — separate from
    TenantUpdate so the UI can save 'office identity' independently
    from the partner-contact fields."""
    office_name: Optional[str] = None
    demand_format: Optional[str] = None
    receipt_template: Optional[str] = None
    subtitle: Optional[str] = None
    primary_color: Optional[str] = None
    short_name: Optional[str] = None


def _admin_only(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only Admin can manage tenants")


def _serialize(t: Tenant) -> dict:
    logo_url = None
    if t.logo_filename:
        logo_url = f"/tenant-assets/{t.slug}/{t.logo_filename}"
    letterhead_url = None
    if getattr(t, "letterhead_path", None):
        letterhead_url = f"/tenant-assets/{t.slug}/{t.letterhead_path}"
    return {
        "id": t.id,
        "slug": t.slug,
        "company_name": t.company_name,
        "short_name": t.short_name,
        "subtitle": t.subtitle,
        "primary_color": t.primary_color,
        "logo_filename": t.logo_filename,
        "logo_url": logo_url,
        "letterhead_path": getattr(t, "letterhead_path", None),
        "letterhead_url": letterhead_url,
        "office_name": getattr(t, "office_name", None),
        "demand_format": getattr(t, "demand_format", None),
        "receipt_template": getattr(t, "receipt_template", None),
        "db_path": t.db_path,
        "features": t.features or {},
        "contact_name": t.contact_name,
        "contact_email": t.contact_email,
        "contact_phone": t.contact_phone,
        "status": t.status,
        "plan": t.plan,
        "admin_email": t.admin_email,
        "notes": t.notes,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# Per-tenant asset upload helpers
_TENANT_ASSETS_ROOT = "data/tenants"
_ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg"}


def _save_tenant_asset(slug: str, file: UploadFile, kind: str) -> str:
    """Save an uploaded image as data/tenants/<slug>/<kind>_<uuid>.<ext>.

    Returns the filename (not the full path) so it can be stored in
    Tenant.logo_filename / Tenant.letterhead_path. We deliberately
    return only the bare filename so the asset is reachable via the
    /tenant-assets/<slug>/<filename> mount.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    ext = ""
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()[:5]
    if ext not in _ALLOWED_IMAGE_EXT:
        # Map content_type fallback
        ct_map = {"image/jpeg": "jpg", "image/png": "png",
                  "image/webp": "webp", "image/gif": "gif",
                  "image/svg+xml": "svg"}
        ext = ct_map.get(file.content_type, "png")
    dir_path = os.path.join(_TENANT_ASSETS_ROOT, slug)
    os.makedirs(dir_path, exist_ok=True)
    fname = f"{kind}_{uuid.uuid4().hex[:8]}.{ext}"
    fpath = os.path.join(dir_path, fname)
    with open(fpath, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return fname


@router.get("")
def list_tenants(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _admin_only(user)
    rows = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    return {"items": [_serialize(r) for r in rows], "total": len(rows)}


@router.get("/{tenant_id}")
def get_tenant(tenant_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    return _serialize(t)


@router.post("", status_code=201)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Provision a NEW portal copy for another business partner.

    Steps:
    1. Generate a unique slug
    2. Create an isolated SQLite database file at data/tenants/tenant_<slug>.db
    3. Run schema migrations (all model DDL) into that DB
    4. Seed the requested admin user inside the new DB
    5. Insert a Tenant row in the control DB with branding + feature flags
    """
    _admin_only(user)

    # Unique slug
    base_slug = slugify(payload.short_name or payload.company_name)
    slug = base_slug
    n = 2
    while db.query(Tenant).filter(Tenant.slug == slug).first():
        slug = f"{base_slug}-{n}"; n += 1

    # Provision isolated DB (this is the heavy step)
    try:
        db_path = provision_tenant_db(
            slug=slug,
            admin_email=payload.admin_email,
            admin_password=payload.admin_password,
            admin_name=payload.admin_name,
            company_name=payload.company_name,
        )
    except Exception as exc:
        raise HTTPException(500, f"Failed to provision tenant DB: {exc}")

    # Build feature flag map (start from defaults, override with payload)
    features = Tenant.default_features()
    if payload.features:
        for k, v in payload.features.items():
            features[k] = bool(v)

    # Insert Tenant row
    t = Tenant(
        slug=slug,
        company_name=payload.company_name,
        short_name=payload.short_name or payload.company_name[:40],
        subtitle=payload.subtitle or "Overseas Employment Promoters",
        primary_color=payload.primary_color or "#2563eb",
        db_path=db_path,
        features=features,
        contact_phone=payload.contact_phone,
        plan=payload.plan or "standard",
        admin_email=payload.admin_email,
        admin_password_set=True,
        status="active",
        notes=payload.notes,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return _serialize(t)


@router.put("/{tenant_id}")
def update_tenant(tenant_id: int, payload: TenantUpdate,
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        if v is not None:
            setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return _serialize(t)


@router.put("/{tenant_id}/features")
def update_features(tenant_id: int, payload: FeaturesUpdate,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    # Start from defaults so any missing keys keep sensible defaults
    flags = Tenant.default_features()
    flags.update(t.features or {})
    flags.update({k: bool(v) for k, v in (payload.features or {}).items()})
    t.features = flags
    db.commit()
    db.refresh(t)
    return _serialize(t)


@router.delete("/{tenant_id}")
def delete_tenant(tenant_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    archived = ""
    try:
        archived = archive_tenant_db(t.db_path)
    except (OSError, IOError) as exc:
        # Archiving is best-effort — failing to move the DB file
        # should not block tenant deletion. Log so the operator can
        # archive manually.
        import logging
        logging.getLogger("dtc.tenants").exception(
            "Could not archive tenant DB %r: %s", t.db_path, exc
        )
    db.commit()
    return {"ok": True, "archived_db": archived}


@router.get("/_meta/feature-defaults")
def feature_defaults(user: User = Depends(get_current_user)):
    _admin_only(user)
    return Tenant.default_features()


# ----------------------------------------------------------------------
# Enterprise branding — per-tenant logo / letterhead / office name
# ----------------------------------------------------------------------
@router.post("/{tenant_id}/logo")
def upload_tenant_logo(tenant_id: int,
                       file: UploadFile = File(...),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Upload the tenant's company logo (PNG/JPG/SVG).

    Stored as ``data/tenants/<slug>/logo_<rand>.<ext>`` and served via
    the ``/tenant-assets`` static mount.  The filename is persisted
    on the Tenant row in ``logo_filename`` so we keep the asset
    history-friendly (uploading a new logo doesn't break the old URL).
    """
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    fname = _save_tenant_asset(t.slug, file, kind="logo")
    t.logo_filename = fname
    # Also keep the convenience column in sync for callers that
    # read ``logo_path`` instead of ``logo_filename``.
    try:
        t.logo_path = fname
    except Exception:
        pass
    db.commit()
    db.refresh(t)
    return _serialize(t)


@router.post("/{tenant_id}/letterhead")
def upload_tenant_letterhead(tenant_id: int,
                             file: UploadFile = File(...),
                             db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """Upload the tenant's letterhead background image.

    The letterhead is used by the per-tenant document renderer
    (receipts, protector letters, demand letters, candidate
    print-profile) so every printed page carries the *tenant's* own
    company header instead of Dogar's default.
    """
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    fname = _save_tenant_asset(t.slug, file, kind="letterhead")
    t.letterhead_path = fname
    db.commit()
    db.refresh(t)
    # Letterhead bytes are cached per-process — wipe the cache so the
    # new asset is picked up on the next render without a restart.
    try:
        from app.services import letterhead_renderer as _lh
        if hasattr(_lh, "_tenant_letterhead_data_url"):
            _lh._tenant_letterhead_data_url.cache_clear()
    except Exception:
        pass
    return _serialize(t)


@router.put("/{tenant_id}/branding")
def update_tenant_branding(tenant_id: int, payload: BrandingUpdate,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Set the per-tenant enterprise identity (office name, demand
    file format, receipt template, subtitle, primary color)."""
    _admin_only(user)
    t = db.query(Tenant).get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return _serialize(t)


@router.get("/_meta/current")
def current_tenant_meta(request: Request):
    """Return the ResolvedTenant info attached by the middleware for
    THIS request (host-based subdomain resolution).

    Public endpoint — frontend templates use this to render the
    correct logo / office name / brand color in the navbar without
    leaking any sensitive tenant configuration.
    """
    rt = getattr(request.state, "tenant", None)
    if rt is None:
        # Default (control) tenant — return the Dogar defaults so the
        # UI still has *something* to render.
        return {
            "is_default": True,
            "slug": None,
            "company_name": "Dogar Trading Corporation",
            "short_name": "DTC",
            "primary_color": "#2563eb",
            "logo_url": None,
            "letterhead_url": None,
            "office_name": "",
            "demand_format": "",
        }
    logo_url = None
    if rt.logo_filename:
        logo_url = f"/tenant-assets/{rt.slug}/{rt.logo_filename}"
    letterhead_url = None
    if rt.letterhead_path:
        letterhead_url = f"/tenant-assets/{rt.slug}/{rt.letterhead_path}"
    return {
        "is_default": False,
        "id": rt.id,
        "slug": rt.slug,
        "company_name": rt.company_name,
        "short_name": rt.short_name,
        "primary_color": rt.primary_color,
        "logo_url": logo_url,
        "letterhead_url": letterhead_url,
        "office_name": rt.office_name or "",
        "demand_format": rt.demand_format or "",
    }
