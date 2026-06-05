"""Multi-tenant subdomain resolver + per-tenant DB session swap.

Strategy
--------
The control DB (``data/dogar_trading.db``) holds the ``tenants`` table.
Each *operational* portal — clients, demands, candidates, documents,
agents, users — lives in its OWN SQLite file under
``data/tenants/tenant_<slug>.db`` (already provisioned by
``app.services.tenant_service``).

When a request comes in we:

1.  Parse the ``Host`` header and extract the leftmost label
    (``demo.oep.com.pk`` → ``demo``, ``acme.oep.com.pk`` → ``acme``).
    The ``DEFAULT_TENANT_SLUG`` setting (or the value ``dogar``) is
    treated as the *root* portal and we keep using the main DB so
    legacy single-tenant behaviour is unchanged.

2.  Look the slug up in the control DB. If a matching active tenant
    is found we store a small ``ResolvedTenant`` snapshot on
    ``request.state.tenant`` (id, slug, company_name, db_path,
    primary_color, logo_filename, letterhead_path, office_name).

3.  ``get_db()`` (in ``app.db.session``) is unchanged — it stays bound
    to the control DB. For tenant-scoped queries the endpoints
    inject the new ``get_tenant_db()`` dependency which:
        • returns the control DB if no tenant was resolved
          (single-tenant / "root" deployment), OR
        • opens a connection to the tenant's SQLite file and yields a
          session bound to a cached per-tenant engine.

This means existing endpoints can OPT IN to tenant scoping by
swapping ``Session = Depends(get_db)`` → ``Session = Depends(get_tenant_db)``
in a single line, without rewriting any query logic.

The middleware is a no-op when ``Host`` cannot be parsed (e.g. direct
IP access, ``localhost``, sandbox preview URLs) so local dev keeps
working exactly as before.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import SessionLocal as ControlSessionLocal, Base

log = logging.getLogger("dtc.tenancy")


# ---------------------------------------------------------------------------
# Reserved subdomains that always map to the root / control portal.
# ---------------------------------------------------------------------------
ROOT_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0",
    "dogar", "www", "app", "portal", "admin",
}

# Top-level domain parts we ignore when looking for the tenant label —
# anything with two or fewer labels (``oep.com``, ``dogar.local``) is
# treated as a root host.
MIN_LABELS_FOR_SUBDOMAIN = 3


@dataclass(frozen=True)
class ResolvedTenant:
    """Snapshot of a tenant attached to ``request.state.tenant``.

    A frozen dataclass keeps the value immutable for the lifetime of
    the request and makes it cheap to pass into render helpers.
    """
    id: int
    slug: str
    company_name: str
    short_name: str
    primary_color: str
    logo_filename: Optional[str]
    db_path: str
    office_name: Optional[str] = None
    letterhead_path: Optional[str] = None
    receipt_template: Optional[str] = None
    demand_format: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-tenant SQLAlchemy engine cache (one engine per tenant, lazily created)
# ---------------------------------------------------------------------------
_ENGINES: dict[str, "sessionmaker"] = {}
_ENGINES_LOCK = threading.Lock()


def _make_tenant_sessionmaker(db_path: str):
    """Build (or reuse) a sessionmaker bound to the given tenant SQLite file.

    Uses the same WAL / synchronous=NORMAL / foreign-keys PRAGMAs we
    apply in ``app.db.session`` so tenant DBs perform identically to
    the control DB.
    """
    with _ENGINES_LOCK:
        if db_path in _ENGINES:
            return _ENGINES[db_path]
        url = f"sqlite:///{db_path}"
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.close()

        # Ensure the schema exists in the tenant DB. ``provision_tenant_db``
        # already does this when the tenant is first created, but we
        # repeat it idempotently in case the file was hand-restored.
        try:
            tables = [t for name, t in Base.metadata.tables.items() if name != "tenants"]
            Base.metadata.create_all(bind=engine, tables=tables)
        except Exception as exc:                              # noqa: BLE001
            log.warning("tenant %r: schema create_all failed: %s", db_path, exc)

        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        _ENGINES[db_path] = SessionLocal
        return SessionLocal


def reset_tenant_engine(db_path: str) -> None:
    """Drop the cached engine for a tenant — used after archive/restore."""
    with _ENGINES_LOCK:
        SessionLocal = _ENGINES.pop(db_path, None)
    if SessionLocal is not None:
        try:
            SessionLocal.kw["bind"].dispose()
        except Exception:                                     # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Host parsing
# ---------------------------------------------------------------------------
def extract_subdomain(host: str) -> Optional[str]:
    """Extract the tenant subdomain from a ``Host`` header.

    Returns ``None`` when the host has no usable subdomain
    (``localhost``, ``oep.com.pk``, IP literals, sandbox URLs).
    """
    if not host:
        return None
    host = host.split(":", 1)[0].lower().strip()
    if not host or host[0].isdigit():
        return None
    labels = host.split(".")
    if len(labels) < MIN_LABELS_FOR_SUBDOMAIN:
        return None
    first = labels[0]
    if first in ROOT_HOSTS:
        return None
    # Reject ``www`` style prefixes even when nested deeper
    if first in {"www", "app"}:
        return None
    return first


def resolve_tenant_from_host(host: str) -> Optional[ResolvedTenant]:
    """Look up the active tenant matching the host's subdomain.

    Returns ``None`` for the root deployment or when no matching
    active tenant exists. Failures are logged and swallowed —
    tenancy errors must never break the request.
    """
    slug = extract_subdomain(host)
    if not slug:
        return None
    db = ControlSessionLocal()
    try:
        from app.models.tenant import Tenant                  # local import — model registered
        row = (
            db.query(Tenant)
            .filter(Tenant.slug == slug)
            .filter(Tenant.status == "active")
            .first()
        )
        if not row:
            return None
        # Make sure the SQLite file actually exists before we hand back
        # a tenant — otherwise the per-tenant get_tenant_db() would
        # crash on the first query.
        if not os.path.exists(row.db_path):
            log.warning(
                "tenant %r resolved but db_path %r is missing — falling back to control DB",
                slug, row.db_path,
            )
            return None
        return ResolvedTenant(
            id=row.id,
            slug=row.slug,
            company_name=row.company_name or row.short_name or slug,
            short_name=row.short_name or slug,
            primary_color=row.primary_color or "#2563eb",
            logo_filename=row.logo_filename,
            db_path=row.db_path,
            # Optional columns added in v6 — read defensively
            office_name=getattr(row, "office_name", None),
            letterhead_path=getattr(row, "letterhead_path", None),
            receipt_template=getattr(row, "receipt_template", None),
            demand_format=getattr(row, "demand_format", None),
        )
    except Exception as exc:                                  # noqa: BLE001
        log.exception("tenant resolver failed: %s", exc)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------
class TenantResolverMiddleware(BaseHTTPMiddleware):
    """Attach a ``ResolvedTenant`` (or ``None``) to ``request.state``."""

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        request.state.tenant = resolve_tenant_from_host(host)
        request.state.tenant_host = host
        return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI dependency — yields the right SQLAlchemy session for the request
# ---------------------------------------------------------------------------
def get_tenant_db(request: Request) -> Session:                 # pragma: no cover — exercised at request time
    """Return a SQLAlchemy session bound to the right DB for this request.

    • When ``request.state.tenant`` is ``None`` we yield the control DB
      session — i.e. legacy single-tenant behaviour is preserved.
    • When a tenant is attached we open a session against that
      tenant's isolated SQLite file (engine cached per-process).

    Endpoints that want tenant scoping just declare:
        ``db: Session = Depends(get_tenant_db)``
    """
    tenant: Optional[ResolvedTenant] = getattr(request.state, "tenant", None)
    if tenant is None:
        db = ControlSessionLocal()
    else:
        SessionLocal = _make_tenant_sessionmaker(tenant.db_path)
        db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_tenant(request: Request) -> Optional[ResolvedTenant]:
    """Convenience FastAPI dependency for getting the resolved tenant."""
    return getattr(request.state, "tenant", None)
