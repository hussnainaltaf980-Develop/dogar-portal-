"""Tenant provisioning service.

Creates an isolated SQLite database for a new portal instance ("Copy Portal"
for another business partner), seeds it with the same schema as the main app,
and creates a fresh admin user inside it.
"""
import os
import re
import shutil
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import Base
import app.models  # noqa: F401  — ensures all models are registered on Base


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "tenants"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "tenant"


def tenant_db_path(slug: str) -> str:
    return str(DATA_DIR / f"tenant_{slug}.db")


def provision_tenant_db(slug: str, admin_email: str, admin_password: str,
                        admin_name: str, company_name: str) -> str:
    """Create a new SQLite file at data/tenants/tenant_<slug>.db, run all
    model DDL into it, and seed an admin user.

    Returns the absolute db_path.
    """
    db_path = tenant_db_path(slug)
    if os.path.exists(db_path):
        raise ValueError(f"Tenant DB already exists: {db_path}")

    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

    # Create all tables (except the Tenant table itself — that lives in the
    # main control DB)
    tables = [t for name, t in Base.metadata.tables.items() if name != "tenants"]
    Base.metadata.create_all(bind=engine, tables=tables)

    # Seed admin user in the new tenant DB
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        from app.models import User, CompanySettings
        admin = User(
            name=admin_name or "Administrator",
            email=admin_email,
            password_hash=hash_password(admin_password),
            role="admin",
            is_active=True,
        )
        db.add(admin)

        # Seed company settings row
        try:
            cs = CompanySettings(
                company_name=company_name,
                short_name=company_name[:40],
            )
            db.add(cs)
        except (TypeError, AttributeError) as exc:
            # CompanySettings might have different columns on legacy DBs — non-fatal.
            import logging
            logging.getLogger("dtc.tenant").warning(
                "Could not seed CompanySettings for new tenant: %s", exc
            )

        db.commit()
    finally:
        db.close()

    return db_path


def archive_tenant_db(db_path: str) -> str:
    """Move tenant DB file to /data/tenants/archive/ — used when deleting."""
    if not os.path.exists(db_path):
        return ""
    archive_dir = DATA_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / Path(db_path).name
    shutil.move(db_path, dest)
    return str(dest)
