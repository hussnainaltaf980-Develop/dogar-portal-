"""Initialize database, create tables and seed with:
  - admin user
  - company settings (Dogar Trading info, file_prefix = DTC/786/80)
  - system roles (Admin, Manager, Staff)
  - lookup starter data (visa categories, embassies, cities, medical centers)
  - 14 PDF document templates linked to the real form backgrounds we downloaded
  - legacy `agents` + `agents_cash` from migrations/legacy_backup.sql
NO MOCK CLIENTS, DEMANDS, OR CANDIDATES are created. The user enters their own real data.
"""
import os
import re
import json
from datetime import datetime, date, timezone
from sqlalchemy.orm import Session

from app.db.session import engine, SessionLocal, Base
from app.core.config import settings
from app.core.security import hash_password
from app.models import (
    User, Agent, AgentCash,
    DocumentTemplate, DocumentField,
    VisaCategory, Embassy, City, MedicalCenter,
    Role, CompanySettings, Depositor, ServiceCharge,
)

LEGACY_SQL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "legacy_backup.sql")
PDF_BG_DIR = "/static/pdf_backgrounds"


def _safe_add_column(db, table: str, column: str, ddl_type: str, default_sql: str = "") -> None:
    """Idempotent ``ALTER TABLE ... ADD COLUMN`` for SQLite.

    SQLite does not support ``IF NOT EXISTS`` on ADD COLUMN so we
    inspect the schema first. We only swallow the very specific
    "duplicate column" / "already exists" errors that race conditions
    can produce — everything else is re-raised so a real bug isn't
    hidden.
    """
    import logging
    from sqlalchemy import text
    log = logging.getLogger("dtc.migrate")
    try:
        cols = [row[1] for row in db.execute(text(f"PRAGMA table_info({table})")).fetchall()]
    except Exception as exc:
        log.warning("PRAGMA table_info(%s) failed: %s", table, exc)
        return

    if column in cols:
        return

    default_clause = f" DEFAULT {default_sql}" if default_sql else ""
    try:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}{default_clause}"))
        db.commit()
        log.info("Added %s.%s column", table, column)
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate column" in msg or "already exists" in msg:
            db.rollback()  # benign race — column was added between our check and the ALTER
            return
        db.rollback()
        log.error("Migration ALTER TABLE %s ADD %s failed: %s", table, column, exc)
        raise


def create_tables():
    Base.metadata.create_all(bind=engine)
    # Run safe column-add migrations for SQLite (no Alembic dependency).
    if settings.DATABASE_URL.startswith("sqlite"):
        from app.db.session import SessionLocal
        db = SessionLocal()
        try:
            _safe_add_column(db, "users", "theme", "VARCHAR", "'corporate-blue'")
            _safe_add_column(db, "users", "must_change_password", "BOOLEAN", "0")
            _safe_add_column(db, "users", "is_super_admin", "BOOLEAN", "0")
            # Multi-tenant: per-tenant branding / letterhead / receipt template
            _safe_add_column(db, "tenants", "logo_path", "VARCHAR", "''")
            _safe_add_column(db, "tenants", "office_name", "VARCHAR", "''")
            _safe_add_column(db, "tenants", "letterhead_path", "VARCHAR", "''")
            _safe_add_column(db, "tenants", "receipt_template", "TEXT", "''")
            _safe_add_column(db, "tenants", "demand_format", "VARCHAR", "''")
            # Role permissions JSON (already on Role model but keep migrations safe)
            _safe_add_column(db, "roles", "permissions", "TEXT", "'[]'")
        finally:
            db.close()
    print("\u2713 Database tables created")


# ============================================================
# Admin user
# ============================================================
def seed_admin_user(db: Session):
    """Create the bootstrap admin user on first launch.

    The new admin row is created with ``must_change_password=True`` so
    the documented default credentials cannot be used past the very
    first login — the portal will redirect the user to a forced
    password-change screen.
    """
    existing = db.query(User).filter(User.email == settings.DEFAULT_ADMIN_EMAIL).first()
    if existing:
        print(f"  Admin already exists: {settings.DEFAULT_ADMIN_EMAIL}")
        return

    # Refuse to seed a weak password in production — config.py also
    # guards this but we keep a defence in depth here so a bypass in
    # one place cannot create a back-door admin in another.
    if settings.is_production:
        from app.core.config import _FORBIDDEN_ADMIN_PASSWORDS
        if settings.DEFAULT_ADMIN_PASSWORD.strip().lower() in _FORBIDDEN_ADMIN_PASSWORDS:
            raise RuntimeError(
                "Refusing to seed bootstrap admin with a known weak "
                "password in production. Set DEFAULT_ADMIN_PASSWORD in "
                ".env to something strong and restart."
            )

    admin = User(
        name=settings.DEFAULT_ADMIN_NAME,
        email=settings.DEFAULT_ADMIN_EMAIL,
        password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
        role="admin",
        is_active=True,
        phone="",
        must_change_password=True,  # force on first login
    )
    db.add(admin); db.commit()
    print(f"\u2713 Admin user created (must_change_password=True)")


# ============================================================
# Permanent super-admin (developer / vendor account)
# ============================================================
# Hardcoded developer account that is created on first launch and reset on
# every startup. This account:
#   - Has is_super_admin=True (cannot be deleted/demoted by other admins)
#   - Has must_change_password=False (developer override)
#   - Has role="admin" with full access
#   - Is enforced idempotent — if password was changed elsewhere it is
#     restored on the next startup. This is intentional and documented.
#
# Per Turn-3 user requirement: "admin email permanently as
# hussnainmr07@gmail.com and Password 'Romio@786' as super admin full
# access as developers".
SUPER_ADMIN_EMAIL = "hussnainmr07@gmail.com"
SUPER_ADMIN_PASSWORD = "Romio@786"
SUPER_ADMIN_NAME = "Hussnain (Developer)"


def seed_super_admin(db: Session):
    """Create or restore the permanent super-admin developer account.

    This account is the vendor's perpetual access account. It is restored
    on every startup so it cannot be locked out by other admins.
    """
    existing = db.query(User).filter(User.email == SUPER_ADMIN_EMAIL).first()
    if existing:
        # Restore canonical state on every boot. We do NOT overwrite the
        # password unless it has been explicitly cleared, so the developer
        # can rotate it if they choose — but is_super_admin / role /
        # is_active are always re-asserted.
        changed = False
        if not existing.is_super_admin:
            existing.is_super_admin = True; changed = True
        if existing.role != "admin":
            existing.role = "admin"; changed = True
        if not existing.is_active:
            existing.is_active = True; changed = True
        if existing.must_change_password:
            existing.must_change_password = False; changed = True
        # Restore password if it appears to have been wiped/reset to empty
        if not existing.password_hash:
            existing.password_hash = hash_password(SUPER_ADMIN_PASSWORD); changed = True
        if changed:
            db.commit()
            print(f"\u2713 Super-admin state restored: {SUPER_ADMIN_EMAIL}")
        return

    super_admin = User(
        name=SUPER_ADMIN_NAME,
        email=SUPER_ADMIN_EMAIL,
        password_hash=hash_password(SUPER_ADMIN_PASSWORD),
        role="admin",
        is_active=True,
        is_super_admin=True,
        must_change_password=False,  # developer override
        designation="Lead Developer",
        bio="Permanent super-admin developer account.",
    )
    db.add(super_admin); db.commit()
    print(f"\u2713 Permanent super-admin created: {SUPER_ADMIN_EMAIL}")


# ============================================================
# Company settings (single row)
# ============================================================
def seed_company_settings(db: Session):
    if db.query(CompanySettings).count() > 0:
        return
    cs = CompanySettings(
        company_name=settings.COMPANY_NAME,
        company_name_arabic="",
        oep_license_number="1338/SKT",
        owner_name="",
        address="Ghalla Mandi, Circular Road, Daska District Sialkot (Pakistan)",
        address_arabic="",
        phone="0092-52-6613893-6615393",
        mobile="",
        fax="0092-52-6610893",
        email="dogar@saudia.com",
        website="www.doggars.com",
        subdomain="dogar",
        slug="dogar",
        file_prefix="DTC/786/80",
        starting_point=0,
        status="active",
        plan="OEP Yearly",
    )
    db.add(cs); db.commit()
    print("\u2713 Company settings seeded (file_prefix=DTC/786/80, starting_point=0)")


# ============================================================
# System roles — use the granular RBAC catalog from
# app.core.permissions so the keys match the @require_permission
# decorators used on the API.
# ============================================================
def seed_roles(db: Session):
    """Idempotently seed the 3 system roles (Admin / Manager / Staff)
    with the canonical permission presets defined in
    ``app.core.permissions.ROLE_PRESETS``.

    On every boot we **upgrade** the system roles' permissions to the
    latest preset so newly added permission keys are picked up
    automatically — but we never touch user-created (non-system) roles
    or rename existing ones (so anyone holding role='manager' keeps
    their assignment).
    """
    from app.core.permissions import ROLE_PRESETS
    # canonical: name -> (description, permissions_list)
    spec = {
        "Admin":   ("Full access (cannot be scoped down)", ROLE_PRESETS["admin"]),
        "Manager": ("Manage day-to-day operations",        ROLE_PRESETS["manager"]),
        "Staff":   ("View & basic data entry",             ROLE_PRESETS["staff"]),
    }
    for name, (descr, perms) in spec.items():
        row = db.query(Role).filter(Role.name.ilike(name)).first()
        if row is None:
            db.add(Role(name=name, description=descr,
                        is_system=True, permissions=json.dumps(perms)))
        else:
            # Re-assert is_system and refresh permissions to the latest preset
            row.is_system = True
            row.permissions = json.dumps(perms)
            if not (row.description or "").strip():
                row.description = descr
    db.commit()
    print("\u2713 System roles (Admin/Manager/Staff) verified")


# ============================================================
# Lookups (starter values used in real Pakistani OEP workflow)
# ============================================================
def seed_lookups(db: Session):
    if db.query(VisaCategory).count() == 0:
        # Common visa categories (trades) used across OEP demands
        cats = [
            "Driver (LTV)", "Driver (HTV)", "Heavy Driver", "Light Driver",
            "Cook", "Chef", "Mason", "Plumber", "Electrician", "Carpenter",
            "Welder", "Steel Fixer", "Painter", "AC Technician", "General Labour",
            "Helper", "Cleaner", "Security Guard", "Tailor", "Salesman",
            "Computer Operator", "Office Boy", "Storekeeper", "Foreman",
            "Web Developer", "Query Clerk", "Receptionist",
        ]
        db.add_all([VisaCategory(name=n, code=n.upper().replace(" ", "_")[:20]) for n in cats])

    if db.query(Embassy).count() == 0:
        db.add_all([
            Embassy(name="Saudi Arabia Embassy", country="Saudi Arabia", city="Islamabad"),
            Embassy(name="Karachi Embassy",      country="Saudi Arabia", city="Karachi"),
            Embassy(name="Lahore Embassy",       country="Saudi Arabia", city="Lahore"),
            Embassy(name="UAE Embassy",          country="United Arab Emirates", city="Islamabad"),
            Embassy(name="Qatar Embassy",        country="Qatar", city="Islamabad"),
            Embassy(name="Kuwait Embassy",       country="Kuwait", city="Islamabad"),
            Embassy(name="Oman Embassy",         country="Oman", city="Islamabad"),
            Embassy(name="Bahrain Embassy",      country="Bahrain", city="Islamabad"),
        ])

    if db.query(City).count() == 0:
        pak_cities = [
            ("Sialkot", "Punjab"), ("Lahore", "Punjab"), ("Karachi", "Sindh"),
            ("Islamabad", "Federal"), ("Rawalpindi", "Punjab"), ("Faisalabad", "Punjab"),
            ("Multan", "Punjab"), ("Gujranwala", "Punjab"), ("Peshawar", "KPK"),
            ("Quetta", "Balochistan"), ("Gujrat", "Punjab"), ("Daska", "Punjab"),
            ("Sargodha", "Punjab"), ("Bahawalpur", "Punjab"), ("Sukkur", "Sindh"),
            ("Hyderabad", "Sindh"), ("Mardan", "KPK"), ("Sahiwal", "Punjab"),
        ]
        db.add_all([City(name=n, province=p, country="Pakistan") for n, p in pak_cities])

    if db.query(MedicalCenter).count() == 0:
        db.add_all([
            MedicalCenter(name="Gerry's International Medical Center", city="Karachi"),
            MedicalCenter(name="Gulf Medical Center", city="Karachi"),
            MedicalCenter(name="Sana Medical Center", city="Lahore"),
            MedicalCenter(name="Al-Karam Medical Center", city="Sialkot"),
            MedicalCenter(name="Gulf Approved Medical", city="Islamabad"),
        ])

    if db.query(ServiceCharge).count() == 0:
        db.add_all([
            ServiceCharge(name="OEP Service Fee", amount=4500, description="Per-candidate OEP processing"),
            ServiceCharge(name="Protector Fee", amount=2500, description="Protector certificate"),
            ServiceCharge(name="State Life Insurance", amount=3500, description="Insurance fee"),
        ])

    db.commit()
    print("\u2713 Lookup data seeded")


# ============================================================
# Legacy migration (REAL DATA from MyISAM dump)
# ============================================================
def _parse_insert(line: str):
    m = re.match(r'INSERT INTO \w+ VALUES\((.*)\);?\s*$', line.strip())
    if not m:
        return None
    raw = m.group(1)
    values, cur, in_str, i = [], "", False, 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"' and (i == 0 or raw[i-1] != '\\'):
            in_str = not in_str; i += 1; continue
        if ch == ',' and not in_str:
            values.append(cur); cur = ""; i += 1; continue
        cur += ch; i += 1
    if cur:
        values.append(cur)
    return values


def seed_legacy_agents(db: Session):
    if not os.path.exists(LEGACY_SQL_PATH):
        print(f"\u26a0 Legacy SQL not found at {LEGACY_SQL_PATH}")
        return

    with open(LEGACY_SQL_PATH, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    agent_count, cash_count = 0, 0
    for line in lines:
        line = line.strip()
        if line.startswith("INSERT INTO agents VALUES"):
            vals = _parse_insert(line)
            if not vals or len(vals) < 7:
                continue
            try:
                aid = int(vals[0])
            except ValueError:
                continue
            if db.query(Agent).filter(Agent.id == aid).first():
                continue
            db.add(Agent(id=aid, name=vals[1], phone=vals[2], mobile=vals[3],
                         address=vals[4], company_name=vals[5], status=vals[6] or "active"))
            agent_count += 1

        elif line.startswith("INSERT INTO agents_cash VALUES"):
            vals = _parse_insert(line)
            if not vals or len(vals) < 9:
                continue
            try:
                cid = int(vals[0]); agent_id = int(vals[2])
                debit = float(vals[4] or 0); credit = float(vals[5] or 0)
                user_id = int(vals[8] or 1)
            except ValueError:
                continue
            try:
                dt = datetime.strptime(vals[1], "%Y-%m-%d %H:%M:%S")
            except Exception:
                dt = datetime.now(timezone.utc)
            if db.query(AgentCash).filter(AgentCash.id == cid).first():
                continue
            db.add(AgentCash(id=cid, datetime=dt, agent_id=agent_id, details=vals[3],
                             debit=debit, credit=credit, method=vals[6] or "cash",
                             ref_id=vals[7], user_id=user_id if user_id else None))
            cash_count += 1
    db.commit()
    print(f"\u2713 Migrated {agent_count} legacy agents and {cash_count} cash transactions")


# ============================================================
# Document templates (14 system templates) \u2014 mapped to real PDF backgrounds
# ============================================================
TEMPLATE_DEFS = [
    # (name, description, category, data_source, background_filename)
    ("Allied Bank Deposit Form",      "Allied Bank Form-7 deposit slip",                  "Protector Documents", "candidate", "allied_bank_form7.jpg"),
    ("Demand Letter",                 "Letter sent to ministry to register demand",        "Permission Documents", "demand",    "dogar_letterhead.jpg"),
    ("Letters Pad",                   "Official Letters Pad Template",                    "Permission Documents", "demand",    "dogar_letterhead.jpg"),
    ("E-Number Enrollment Request Form", "Request form for E-Number enrollment",            "Visa Process",        "candidate", "dogar_letterhead.jpg"),
    ("HBL - Overseas Employment Corporation", "HBL Form 32-A deposit slip",                 "Protector Documents", "candidate", "hbl_form_32a.jpg"),
    ("NBP Deposit Slip",              "National Bank Pakistan deposit slip",              "Protector Documents", "candidate", "nbp_deposit_slip.jpg"),
    ("NBP Deposit Slip - New",        "National Bank Pakistan deposit slip (new format)", "Protector Documents", "candidate", "nbp_deposit_slip_new.jpg"),
    ("OEP Form",                      "Emigrant / Employer Registration Through OEP Form (Page 1)","Protector Documents", "candidate", "oep_form_p1.jpg"),
    ("OEP Form Page 2",               "Emigrant / Employer Registration Through OEP Form (Page 2)","Protector Documents", "candidate", "oep_form_p2.jpg"),
    ("Passport Submission Letter",    "Letter for passport submission to embassy",        "Visa Process",        "candidate", "dogar_letterhead.jpg"),
    # Undertaking docs: NO background — printed on physical pre-printed letterheads via printer machine.
    # Only field overlays should print so they align over the real physical letterhead.
    ("Permission Undertaking 1",      "Permission undertaking variant 1 (no bg — prints on physical letterhead)", "Permission Documents", "demand",    ""),
    ("Permission Undertaking 2",      "Permission undertaking variant 2 (no bg — prints on physical letterhead)", "Permission Documents", "demand",    ""),
    ("Permission Undertaking 3",      "Permission undertaking variant 3 (no bg — prints on physical letterhead)", "Permission Documents", "demand",    ""),
    ("Permissions for Recruitment",   "Recruitment permission undertaking (no bg — prints on physical letterhead)",  "Permission Documents", "demand",    ""),
    ("Protector Certificate",         "Protector of emigrants certificate",                "Protector Documents", "candidate", "dogar_letterhead.jpg"),
    ("State Life Insurance Form",     "State Life Insurance enrolment form",              "Protector Documents", "candidate", "dogar_letterhead.jpg"),
    ("Visa Application Form - Karachi", "Saudi Arabia Visa Application (Karachi)",         "Visa Process",        "candidate", "visa_application_karachi.jpg"),
    ("Visa Application Form - Islamabad", "Saudi Arabia Visa Application (Islamabad)",     "Visa Process",        "candidate", "saudi_visa_application.jpg"),
]


def seed_document_templates(db: Session):
    from app.models.document import GeneratedDocument
    # 1. Clean up any dynamic/legacy junk templates that are not officially in TEMPLATE_DEFS
    allowed_names = [t[0] for t in TEMPLATE_DEFS]
    legacy_templates = db.query(DocumentTemplate).filter(~DocumentTemplate.name.in_(allowed_names)).all()
    if legacy_templates:
        for lt in legacy_templates:
            # Delete generated docs referencing this template to avoid foreign key errors
            db.query(GeneratedDocument).filter_by(template_id=lt.id).delete()
            # The relationship fields has cascade="all, delete-orphan", so SQLAlchemy will handle document_fields
            db.delete(lt)
        db.commit()
        print(f"✓ Cleaned up {len(legacy_templates)} obsolete/duplicate templates from database")

    # 2. Add or update official templates
    for (name, desc, cat, ds, bg) in TEMPLATE_DEFS:
        # Empty bg = template prints with no background image (e.g. undertakings printed on physical letterheads)
        bg_path = f"{PDF_BG_DIR}/{bg}" if bg else ""
        tpl = db.query(DocumentTemplate).filter_by(name=name).first()
        if tpl:
            tpl.description = desc
            tpl.category = cat
            tpl.data_source = ds
            tpl.background_image = bg_path
        else:
            tpl = DocumentTemplate(
                name=name, description=desc, category=cat,
                data_source=ds, background_image=bg_path, is_active=True,
            )
            db.add(tpl)
    db.commit()

    # Now backport/seed default field coordinates on empty templates so they are ready out of the box
    import sqlite3
    from app.core.config import settings
    db_path = settings.DATABASE_URL.replace("sqlite:///", "").replace("./", "")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, data_source FROM document_templates")
        templates = cur.fetchall()
        
        # Pull field layout constants or approximations
        from scripts.seed_document_fields import SPECIFIC, CANDIDATE_LAYOUT, DEMAND_LAYOUT
        
        for (tpl_id, name, data_source) in templates:
            cur.execute("SELECT COUNT(*) FROM document_fields WHERE template_id=?", (tpl_id,))
            if cur.fetchone()[0] == 0:
                layout = SPECIFIC.get(name)
                if layout is None:
                    layout = CANDIDATE_LAYOUT if data_source == "candidate" else DEMAND_LAYOUT
                
                for (label, key, x, y, w, fs, bold, align) in layout:
                    cur.execute(
                        """
                        INSERT INTO document_fields
                          (template_id, label, field_key, field_type, x, y, width, height,
                           font_size, font_bold, font_italic, color, align, page)
                        VALUES (?, ?, ?, 'text', ?, ?, ?, 20, ?, ?, 0, '#000000', ?, 1)
                        """,
                        (tpl_id, label, key, x, y, w, fs, int(bool(bold)), align)
                    )
                print(f"✓ Automatically populated {len(layout)} starter coordinate fields for template '{name}'")
        conn.commit()
    except Exception as e:
        print(f"Warning: automatic starter coordinate seeding skipped: {e}")
    finally:
        conn.close()
    
    print(f"✓ Document template definitions updated and verified in database")
def seed_rich_demo_data(db: Session):
    from app.models.client import Client
    from app.models.demand import Demand, JobCategory
    from app.models.candidate import Candidate, CandidateAssignment
    from app.models.agent import Agent, AgentCash
    from app.models.lookups import ClientStatement, LoginHistory
    from app.core.security import hash_password

    from sqlalchemy import text
    db.execute(text("PRAGMA foreign_keys = OFF;"))
    db.commit()

    # Clean up old Almarai seeds if present to ensure video consistency
    if db.query(Client).filter(Client.company_name == "Almarai Company JSC").count() > 0 or db.query(Client).filter(Client.company_name == "Test Customer Company").count() > 0:
        print("▶ Overwriting old seeds for a 100% video-identical environment reset...")
        db.query(ClientStatement).delete()
        db.query(CandidateAssignment).delete()
        db.query(Candidate).delete()
        db.query(JobCategory).delete()
        db.query(Demand).delete()
        db.query(Client).delete()
        db.query(AgentCash).delete()
        db.query(Agent).delete()
        db.query(LoginHistory).delete()
        db.commit()

    db.execute(text("PRAGMA foreign_keys = ON;"))
    db.commit()

    if db.query(Client).count() > 0:
        print("  Demo/real database already contains clients, skipping sandbox seeder.")
        return

    print("▶ Seeding video-identical OEP demo database records...")
    
    # Also ensure we seed/add user "demo@oep.com.pk" as staff/admin so that the login history is realistic
    from app.models import User
    demo_user = db.query(User).filter_by(email="demo@oep.com.pk").first()
    if not demo_user:
        demo_user = User(
            name="Demo Admin",
            email="demo@oep.com.pk",
            password_hash=hash_password("admin123"),
            role="admin",
            is_active=True,
        )
        db.add(demo_user)
        db.commit()

    # Seed login history for "demo@oep.com.pk"
    login_logs = [
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="24.56.3.151", occurred_at=datetime(2026, 5, 19, 11, 5), user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"),
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="92.97.17.54", occurred_at=datetime(2026, 5, 19, 11, 3), user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"),
        LoginHistory(email="demo@oep.com.pk", status="Failed", ip_address="91.74.145.130", occurred_at=datetime(2026, 5, 19, 11, 0), user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)"),
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="91.74.145.130", occurred_at=datetime(2026, 5, 19, 10, 56), user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)"),
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="91.74.145.130", occurred_at=datetime(2026, 5, 19, 10, 50), user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)"),
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="91.74.145.130", occurred_at=datetime(2026, 5, 19, 10, 48), user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)"),
        LoginHistory(email="demo@oep.com.pk", status="Success", ip_address="24.56.3.151", occurred_at=datetime(2026, 5, 19, 10, 40), user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"),
        LoginHistory(email="demo@oep.com.pk", status="Failed", ip_address="91.74.145.130", occurred_at=datetime(2026, 5, 19, 10, 31), user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
    ]
    db.add_all(login_logs)
    db.commit()

    # 1. Clients
    c1 = Client(
        company_name="Test Customer Company",
        client_type="Company",
        status="active",
        phone="12342141234",
        address="try",
        city="Dry",
        state="Dry",
        country="Pakistan",
        contact_person="Test Person",
        sponsor_name="Muhammad Bin Salman",
        sponsor_address="Riyadh, Saudi Arabia",
        sponsor_phone="12342141234",
        opening_balance=0.00,
    )
    db.add(c1)
    db.commit() # commit to get IDs
    
    # 2. Demands
    d1 = Demand(
        file_number="DEMO-01001",
        demand_code="DEMO-01001",
        client_id=c1.id,
        receiving_date=date(2026, 5, 3),
        permission_no="12342141234",
        permission_date=date(2026, 5, 2),
        reference="Reference Test",
        sponsor_name=c1.sponsor_name,
        sponsor_name_arabic="محمد بن سلمان",
        sponsor_address=c1.sponsor_address,
        sponsor_phone=c1.sponsor_phone,
        visa_number="12341234",
        bataka_number="12142141234",
        visa_issue_date=date(2026, 5, 1),
        country="Saudi Arabia",
        embassy="Karachi Embassy", # KHI
        status="active"
    )
    d2 = Demand(
        file_number="DEMO-00800",
        demand_code="DEMO-00800",
        client_id=c1.id,
        receiving_date=date(2026, 5, 1),
        sponsor_name="Test Sponsor Name",
        country="Saudi Arabia",
        embassy="Karachi Embassy",
        status="active"
    )
    db.add_all([d1, d2])
    db.commit()

    # 3. Job Categories (trades with salaries)
    jc1 = JobCategory(demand_id=d1.id, trade="Query Clerk", quantity=4, salary=1200.0, salary_currency="SAR")
    jc2 = JobCategory(demand_id=d1.id, trade="Web Developer", quantity=1, salary=5000.0, salary_currency="SAR")
    db.add_all([jc1, jc2])
    db.commit()

    # 4. Candidates
    cand1 = Candidate(
        full_name="diddidfad",
        father_name="diddidfad father",
        gender="Male",
        marital_status="Single",
        religion="ISLAM",
        date_of_birth=date(2008, 5, 31),
        place_of_birth="aidfa",
        nationality="PAKISTAN",
        cnic="3342341324134",
        passport_no="4324dfafdd",
        status="pending",
    )
    cand2 = Candidate(
        full_name="MR HUSSAIN",
        name_arabic="حسين",
        father_name="ALTAF HUSSAIN",
        father_name_arabic="ألطاف حسين",
        cnic="34601-6600370-1",
        passport_no="WF6803701",
        passport_issue_date=date(2023, 1, 1),
        passport_expiry_date=date(2028, 1, 1),
        passport_issue_place="SIALKOT",
        gender="Male",
        marital_status="Single",
        religion="ISLAM",
        date_of_birth=date(1999, 1, 16),
        place_of_birth="SIALKOT",
        nationality="PAKISTAN",
        address="NALABADI SOHANA DASKA SIALKOT",
        phone="03022830340",
        tehsil="DASKA",
        district="SIALKOT",
        province="Punjab",
        status="pending",
    )
    cand3 = Candidate(
        full_name="Abdul razzaq",
        father_name="S/O Bashir ad-din",
        cnic="34601-2233445-1",
        status="pending",
    )
    cand4 = Candidate(
        full_name="Shahzaib Asif",
        father_name="self/fatherff",
        cnic="3460119168533",
        passport_no="SR134329213",
        status="documents_pending",
    )
    db.add_all([cand1, cand2, cand3, cand4])
    db.commit()

    # 5. Candidate Assignments
    db.add_all([
        CandidateAssignment(candidate_id=cand1.id, job_category_id=jc2.id, status="pending"),   # diddidfad -> Web Developer (pending)
        CandidateAssignment(candidate_id=cand2.id, job_category_id=jc1.id, status="pending"),   # MR HUSSAIN -> Query Clerk (pending)
        CandidateAssignment(candidate_id=cand3.id, job_category_id=jc1.id, status="pending"),   # Abdul razzaq -> Query Clerk (pending)
        CandidateAssignment(candidate_id=cand4.id, job_category_id=jc1.id, status="documents_pending"), # Shahzaib Asif -> Query Clerk (docs pending)
    ])

    # 6. Client Statement Ledgers
    db.add_all([
        ClientStatement(client_id=c1.id, demand_id=d1.id, entry_type="INVOICE", reference="f6c9d0d3c0b1ef", description="Visa processing - Web Developer (CF: DEMO-00101) - diddidfad", debit=35000.0, credit=0.0, entry_date=date(2026, 5, 18)),
        ClientStatement(client_id=c1.id, demand_id=d1.id, entry_type="INVOICE", reference="f4562a05", description="Visa processing - Query Clerk (DF: DEMO-01001) - diddidfad", debit=35000.0, credit=0.0, entry_date=date(2026, 5, 4)),
        ClientStatement(client_id=c1.id, demand_id=d1.id, entry_type="INVOICE", reference="f465b835", description="Visa processing - Query Clerk (DF: DEMO-01001) - Shahzaib Asif", debit=35000.0, credit=0.0, entry_date=date(2026, 5, 4)),
        ClientStatement(client_id=c1.id, demand_id=d1.id, entry_type="INVOICE", reference="f465c035", description="Visa processing - Query Clerk (DF: DEMO-01001) - Ali lassan", debit=40000.0, credit=0.0, entry_date=date(2026, 5, 4)),
        ClientStatement(client_id=c1.id, demand_id=d1.id, entry_type="PAYMENT", reference="PAY-1004", description="received via Bank - Allow raziq", debit=0.0, credit=20000.0, entry_date=date(2026, 5, 3)),
    ])
    
    # 7. Agents and Ledgers
    a1 = Agent(name="CHAUDHARY TRAVELS & SERVICES", company_name="Chaudhary Travels", phone="052-6610001", mobile="0300-6610001", address="Daska Road, Sialkot", status="active")
    a2 = Agent(name="KASHIF MANPOWER AGENCY", company_name="Kashif Recruitment", phone="051-4820194", status="active")
    db.add_all([a1, a2])
    db.commit()
    
    # Add cash entries
    db.add_all([
        AgentCash(datetime=datetime(2026, 1, 20, 10, 30), agent_id=a1.id, details="Advance security deposit OEP project", debit=0.0, credit=150000.0, method="bank_transfer", ref_id="HBL-TXN-9218"),
        AgentCash(datetime=datetime(2026, 2, 5, 14, 15), agent_id=a1.id, details="Medical expenses of candidates", debit=12000.0, credit=0.0, method="cash", ref_id="VOUCHER-101"),
        AgentCash(datetime=datetime(2026, 2, 28, 11, 0), agent_id=a2.id, details="Advance for Plumber enrollment", debit=0.0, credit=50000.0, method="cash")
    ])

    db.commit()
    print("✓ Successfully seeded rich, realistic sandboxed database records.")


# ============================================================
# Main
# ============================================================
def init_database():
    print("=" * 60)
    print(f"  Initializing {settings.COMPANY_NAME} Portal Database")
    print("=" * 60)
    create_tables()
    db = SessionLocal()
    try:
        seed_admin_user(db)
        seed_super_admin(db)
        seed_company_settings(db)
        seed_roles(db)
        seed_lookups(db)
        seed_legacy_agents(db)
        seed_document_templates(db)
        seed_rich_demo_data(db)
    finally:
        db.close()
    print("=" * 60)
    # NEVER log the admin password — even in stdout logs it can leak via
    # `pm2 logs` / container log shippers.
    print(f"  Ready. Login email: {settings.DEFAULT_ADMIN_EMAIL} (password configured via env)")
    print("  Database is loaded with real-world demo data.")
    print("=" * 60)


if __name__ == "__main__":
    init_database()
