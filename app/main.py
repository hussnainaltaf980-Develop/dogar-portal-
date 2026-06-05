"""Dogar Trading Corporation Portal — Main FastAPI Application."""
import os
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user_optional
from app.db.session import get_db, engine, Base
from app.api.endpoints import auth, users, clients, demands, candidates, agents, documents, dashboard, lookups, settings as settings_api, ocr, chatbot, tenants as tenants_api, protector_letters, reminders

# Ensure tables exist on startup AND apply idempotent migrations
# (ALTER TABLE ADD COLUMN for new columns like must_change_password)
Base.metadata.create_all(bind=engine)
from app.db.init_db import create_tables  # noqa: E402
create_tables()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Full-stack portal for overseas employment management, visa processing, and PDF document generation.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

def _apply_bundled_sql_dump() -> bool:
    """Apply the bundled SQL backup directly to the SQLite DB.

    Search order (first match wins):
        1. migrations/dogar_full_backup.sql.gz   (shipped in zip — primary)
        2. migrations/dogar_full_backup.sql      (uncompressed fallback)
        3. ../backup-18-05-2026.sql.gz           (legacy operator-supplied path)

    Returns True when a dump was applied, False if no dump file was found
    or the apply failed. Never raises — the app must still boot.
    """
    import gzip
    import logging
    log = logging.getLogger("dtc.startup")

    candidates = [
        "migrations/dogar_full_backup.sql.gz",
        "migrations/dogar_full_backup.sql",
        "../backup-18-05-2026.sql.gz",
    ]
    src = next((p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 1024), None)
    if not src:
        log.warning("AUTO-MIGRATOR: No bundled SQL dump found in %s — starting with empty schema only.", candidates)
        return False

    log.info("AUTO-MIGRATOR: Loading bundled SQL dump from %r ...", src)

    # Read (gzipped or plain)
    opener = gzip.open if src.endswith(".gz") else open
    try:
        with opener(src, "rt", encoding="utf-8", errors="replace") as fh:
            sql_text = fh.read()
    except (OSError, gzip.BadGzipFile) as exc:
        log.error("AUTO-MIGRATOR: Could not read %r: %s", src, exc)
        return False

    # Apply via the raw sqlite3 driver — executescript() handles the full dump.
    from app.db.session import engine
    db_url = str(engine.url)
    db_path = db_url.replace("sqlite:///", "").replace("./", "")
    if not db_path:
        log.error("AUTO-MIGRATOR: Could not resolve SQLite path from %r", db_url)
        return False

    import sqlite3
    try:
        # Drop existing empty tables first so the dump's CREATE TABLE works.
        # SQLAlchemy already created empty tables — we DROP them so the
        # dump's "CREATE TABLE" statements can run cleanly.
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        for (name,) in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.commit()

        cur.executescript(sql_text)
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        log.exception("AUTO-MIGRATOR: SQL apply failed: %s", exc)
        return False

    log.info("AUTO-MIGRATOR: Bundled SQL dump applied successfully from %r.", src)
    return True


@app.on_event("startup")
def on_startup():
    import logging
    log = logging.getLogger("dtc.startup")
    log.info("=== STARTUP DIAGNOSTICS ===")
    log.info("Working directory: %s", os.getcwd())

    from app.db.session import SessionLocal
    from app.models.candidate import Candidate

    # ------------------------------------------------------------------
    # 1) Data bootstrap — only if the DB is empty
    # ------------------------------------------------------------------
    db = SessionLocal()
    try:
        try:
            cand_count = db.query(Candidate).count()
        except Exception as exc:                              # noqa: BLE001 — empty/new schema
            log.warning("Candidate count query failed (likely fresh DB): %s", exc)
            cand_count = 0

        if cand_count == 0:
            log.info("AUTO-MIGRATOR: empty DB detected — attempting bundled SQL restore.")
            db.close()                                        # close session so we can rebuild tables
            applied = _apply_bundled_sql_dump()
            if applied:
                # Recreate any tables the dump might be missing (newer ORM columns)
                from app.db.init_db import create_tables
                create_tables()
                log.info("AUTO-MIGRATOR: post-restore schema sync complete.")
            else:
                log.info("AUTO-MIGRATOR: no dump applied — running with empty schema (admin seeder will still create the bootstrap user).")
            db = SessionLocal()
        else:
            log.info("AUTO-MIGRATOR: candidate count = %d, existing DB kept as-is.", cand_count)
    finally:
        db.close()

    # ------------------------------------------------------------------
    # 2) Always re-seed admin + document templates (idempotent)
    # ------------------------------------------------------------------
    from app.db.init_db import (
        seed_admin_user, seed_super_admin, seed_document_templates, seed_roles,
    )
    db = SessionLocal()
    try:
        try:
            seed_admin_user(db)
            log.info("AUTO-SEEDER: admin user verified.")
        except Exception as exc:                              # noqa: BLE001
            log.exception("AUTO-SEEDER: admin seed failed: %s", exc)

        # Permanent super-admin (developer account) — idempotent.
        # Re-asserts is_super_admin=True / role=admin / is_active=True
        # on every boot so the account can NEVER be permanently locked
        # out or demoted via accidental edits in another tab.
        try:
            seed_super_admin(db)
            log.info("AUTO-SEEDER: super-admin (developer) verified.")
        except Exception as exc:                              # noqa: BLE001
            log.exception("AUTO-SEEDER: super-admin seed failed: %s", exc)

        try:
            seed_document_templates(db)
            log.info("AUTO-SEEDER: document templates verified.")
        except Exception as exc:                              # noqa: BLE001
            log.exception("AUTO-SEEDER: template seed failed: %s", exc)

        # RBAC system roles — re-seed on every boot so newly added
        # permission keys flow through to existing system roles.
        try:
            seed_roles(db)
            log.info("AUTO-SEEDER: system roles verified.")
        except Exception as exc:                              # noqa: BLE001
            log.exception("AUTO-SEEDER: roles seed failed: %s", exc)
    finally:
        db.close()

    log.info("=== END STARTUP DIAGNOSTICS ===")


# ---------------------------------------------------------------------------
# PERFORMANCE MIDDLEWARE
# ---------------------------------------------------------------------------
# Gzip compression for everything > 500 bytes – cuts payloads ~70% for JSON lists
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


class CacheHeadersMiddleware(BaseHTTPMiddleware):
    """Long-lived caching for /static/, no-cache for HTML pages,
    short cache for read-only API endpoints (so the candidate/demand
    list snaps back instantly on second visit)."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=86400, immutable")
        elif path.startswith("/api/") and request.method == "GET":
            # 30-second private cache for read-only API endpoints — keeps lists snappy
            # without staleness issues (we always invalidate on POST/PUT/DELETE).
            if not any(seg in path for seg in ("/auth/", "/me", "/login", "/logout", "/chatbot/", "/forgot-password", "/reset-password", "/contact-support")):
                response.headers.setdefault("Cache-Control", "private, max-age=30")
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        # Tell browsers we serve modern, secure responses
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


app.add_middleware(CacheHeadersMiddleware)

# Multi-tenant resolver — attaches request.state.tenant for every request.
# Adding it AFTER CacheHeadersMiddleware so cache decisions can see the tenant.
from app.core.tenancy import TenantResolverMiddleware  # noqa: E402
app.add_middleware(TenantResolverMiddleware)

# Per-tenant uploads / logos / letterheads — served as static assets.
# Each tenant's branding lives under data/tenants/<slug>/(logo|letterhead).png.
os.makedirs("data/tenants", exist_ok=True)

# Static files (1-day public cache via CacheHeadersMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/tenant-assets", StaticFiles(directory="data/tenants", check_dir=False), name="tenant_assets")

# Templates
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["company_name"] = settings.COMPANY_NAME
templates.env.globals["company_tagline"] = settings.COMPANY_TAGLINE
templates.env.globals["app_version"] = settings.APP_VERSION
templates.env.globals["short_name"] = "DOGAR TRADING"
templates.env.globals["copyright_year"] = "2025"
templates.env.globals["powered_by"] = "HussnainTechVertex Pvt Ltd."


def _tenant_template_ctx(request: Request) -> dict:
    """Build the per-request tenant context for TemplateResponse.

    Returns a dict that is safe to ``**`` into a TemplateResponse
    context — exposes ``tenant`` (may be ``None``) and tenant-aware
    helpers like ``brand_name`` and ``brand_logo_url``.
    """
    t = getattr(request.state, "tenant", None) if request else None
    if t is None:
        return {
            "tenant": None,
            "brand_name": settings.COMPANY_NAME,
            "brand_short_name": "DOGAR TRADING",
            "brand_logo_url": "/static/img/logo-sm.png",
            "brand_primary_color": "#2563eb",
        }
    logo_url = "/static/img/logo-sm.png"
    if t.logo_filename:
        logo_url = f"/tenant-assets/{t.slug}/{t.logo_filename}"
    return {
        "tenant": t,
        "brand_name": t.company_name,
        "brand_short_name": t.short_name,
        "brand_logo_url": logo_url,
        "brand_primary_color": t.primary_color,
    }


# Make _tenant_template_ctx available globally to Jinja for the rare case
# where a template needs to peek without an explicit pass from the route.
templates.env.globals["tenant_ctx"] = _tenant_template_ctx

# API routers
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])
app.include_router(clients.router, prefix="/api/clients", tags=["Clients"])
app.include_router(demands.router, prefix="/api/demands", tags=["Demands"])
app.include_router(candidates.router, prefix="/api/candidates", tags=["Candidates"])
app.include_router(agents.router, prefix="/api/agents", tags=["Agents"])
app.include_router(documents.router, prefix="/api/documents", tags=["Documents"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(lookups.router, prefix="/api/lookups", tags=["Lookups"])
app.include_router(settings_api.router, prefix="/api/settings", tags=["Settings"])
app.include_router(ocr.router, prefix="/api/ocr", tags=["OCR"])
app.include_router(chatbot.router, prefix="/api/chatbot", tags=["DtcBot"])
app.include_router(tenants_api.router, prefix="/api/tenants", tags=["Tenants"])
app.include_router(protector_letters.router, prefix="/api/protector-letters", tags=["ProtectorLetters"])
app.include_router(reminders.router, prefix="/api/reminders", tags=["Reminders"])


# ===== Page Routes =====
@app.get("/", response_class=HTMLResponse)
def root(request: Request, user=Depends(get_current_user_optional)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


@app.get("/contact-support", response_class=HTMLResponse)
def contact_support_page(request: Request):
    return templates.TemplateResponse("contact_support.html", {"request": request})


def _require_login(request: Request, user):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return None


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "active": "dashboard"})


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("clients.html", {"request": request, "user": user, "active": "clients"})


@app.get("/demands", response_class=HTMLResponse)
def demands_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("demands.html", {"request": request, "user": user, "active": "demands"})


@app.get("/demands/{demand_id}", response_class=HTMLResponse)
def demand_detail_page(demand_id: int, request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("demand_detail.html", {"request": request, "user": user, "active": "demands", "demand_id": demand_id})


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    flyout = request.query_params.get("flyout") == "1"
    # wizard_mode = the page is being opened ONLY to host the 7-step data-entry
    # modal (Assign Candidate from a Demand File). In that case we must hide the
    # right-side candidate drawer that would otherwise show empty "New
    # Candidate" tabs behind the wizard.
    wizard_mode = bool(
        request.query_params.get("wizard") == "1"
        or request.query_params.get("edit")
    )
    return templates.TemplateResponse("candidates.html", {
        "request": request,
        "user": user,
        "active": "candidates",
        "flyout": flyout,
        "wizard_mode": wizard_mode,
    })


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("agents.html", {"request": request, "user": user, "active": "agents"})


@app.get("/agents/{agent_id}/cashbook", response_class=HTMLResponse)
def agent_cashbook_page(agent_id: int, request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("agent_cashbook.html", {"request": request, "user": user, "active": "agents", "agent_id": agent_id})


@app.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("documents.html", {"request": request, "user": user, "active": "documents"})


@app.get("/documents/builder/{tpl_id}", response_class=HTMLResponse)
def document_builder_page(tpl_id: int, request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("document_builder.html", {"request": request, "user": user, "active": "documents", "tpl_id": tpl_id})


@app.get("/protector-letter", response_class=HTMLResponse)
def protector_letter_page(request: Request, user=Depends(get_current_user_optional)):
    """Protector Letter workflow page: build a batch of emigrants by
    passport number, then auto-print the three Protector documents on
    the company letterhead."""
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("protector_letter.html", {"request": request, "user": user, "active": "protector_letter"})


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("users.html", {"request": request, "user": user, "active": "users"})


@app.get("/portal-admin", response_class=HTMLResponse)
def portal_admin_page(request: Request, user=Depends(get_current_user_optional)):
    """Multi-tenant Copy Portal admin — provision new portal instances
    for other business partners with isolated DBs + RBAC feature flags."""
    redirect = _require_login(request, user)
    if redirect: return redirect
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("portal_admin.html",
        {"request": request, "user": user, "active": "portal-admin"})


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("reports.html", {"request": request, "user": user, "active": "reports"})


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail_page(client_id: int, request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("client_detail.html", {"request": request, "user": user, "active": "clients", "client_id": client_id})


# ===== Sidebar simple-list pages (Visa Categories, Embassies, Cities, etc.) =====
@app.get("/visa-categories", response_class=HTMLResponse)
def visa_categories_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "visa-categories",
        "title": "Visa Categories", "icon": "fa-tags",
        "api": "/api/lookups/visa-categories", "singular": "Visa Category",
        "columns": [("name", "Name"), ("code", "Code"), ("description", "Description")],
    })


@app.get("/embassies", response_class=HTMLResponse)
def embassies_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "embassies",
        "title": "Embassies", "icon": "fa-landmark",
        "api": "/api/lookups/embassies", "singular": "Embassy",
        "columns": [("name", "Embassy Name"), ("country", "Country"), ("city", "City")],
    })


@app.get("/cities", response_class=HTMLResponse)
def cities_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "cities",
        "title": "Cities", "icon": "fa-location-dot",
        "api": "/api/lookups/cities", "singular": "City",
        "columns": [("name", "City"), ("province", "Province"), ("country", "Country")],
    })


@app.get("/medical-centers", response_class=HTMLResponse)
def medical_centers_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "medical-centers",
        "title": "Medical Centers", "icon": "fa-stethoscope",
        "api": "/api/lookups/medical-centers", "singular": "Medical Center",
        "columns": [("name", "Center Name"), ("city", "City"), ("phone", "Phone")],
    })


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "contacts",
        "title": "Contacts", "icon": "fa-address-book",
        "api": "/api/lookups/contacts", "singular": "Contact",
        "columns": [("name", "Name"), ("email", "Email"), ("phone", "Phone"), ("company", "Company")],
    })


# ===== Settings group =====
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "active": "settings", "tab": "general"})


@app.get("/depositors", response_class=HTMLResponse)
def depositors_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "depositors",
        "title": "Depositors", "icon": "fa-user-tie",
        "api": "/api/lookups/depositors", "singular": "Depositor",
        "columns": [("first_name", "First Name"), ("last_name", "Last Name"), ("cnic", "CNIC"), ("mobile", "Mobile")],
    })


@app.get("/service-charges", response_class=HTMLResponse)
def service_charges_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("simple_list.html", {
        "request": request, "user": user, "active": "service-charges",
        "title": "Service Charges", "icon": "fa-dollar-sign",
        "api": "/api/lookups/service-charges", "singular": "Service Charge",
        "columns": [("name", "Name"), ("amount", "Amount"), ("description", "Description")],
    })


# ===== Users sub-pages =====
@app.get("/login-history", response_class=HTMLResponse)
def login_history_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login_history.html", {"request": request, "user": user, "active": "login-history"})


@app.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("roles.html", {"request": request, "user": user, "active": "roles"})


@app.get("/documents/customize/{tpl_id}", response_class=HTMLResponse)
def document_customize_page(tpl_id: int, request: Request, user=Depends(get_current_user_optional)):
    redirect = _require_login(request, user)
    if redirect: return redirect
    return templates.TemplateResponse("document_customize.html", {"request": request, "user": user, "active": "documents", "tpl_id": tpl_id})


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}
