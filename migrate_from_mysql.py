"""
Full MySQL → SQLite migration script.
Reads the real Dogar Trading MySQL backup and populates the FastAPI SQLAlchemy SQLite database.

Run from the dogar-portal directory:
    DATABASE_URL=sqlite:///./data/dogar_trading.db python migrate_from_mysql.py
"""
import os, re, sys
from datetime import datetime, date
from dateutil import parser as dateparser

# Force SQLite so we never accidentally hit PostgreSQL
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/dogar_trading.db")

from app.db.session import engine, SessionLocal, Base
from app.core.security import hash_password
from app.models import (
    User, Agent, AgentCash,
    DocumentTemplate, DocumentField,
    VisaCategory, Embassy, City, MedicalCenter,
    Role, CompanySettings, Depositor, ServiceCharge,
    Contact,
)
from app.models.client import Client
from app.models.demand import Demand, JobCategory
from app.models.candidate import Candidate, CandidateAssignment
from app.models.lookups import LoginHistory

# Migration source — checked in this order:
#   1. migrations/dogar_full_backup.sql.gz  (bundled with the zip — primary)
#   2. migrations/dogar_full_backup.sql     (uncompressed bundled fallback)
#   3. ../backup-18-05-2026.sql.gz          (legacy operator-supplied path)
_HERE = os.path.dirname(__file__)
_CANDIDATES = [
    os.path.join(_HERE, "migrations/dogar_full_backup.sql.gz"),
    os.path.join(_HERE, "migrations/dogar_full_backup.sql"),
    os.path.join(_HERE, "../backup-18-05-2026.sql.gz"),
]
SQL_FILE = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])

PDF_BG = "/static/pdf_backgrounds"

# ---------------------------------------------------------------------------
# SQL PARSER
# ---------------------------------------------------------------------------

def load_sql(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse_rows(content, table):
    """Return list of value-tuples for all INSERT INTO <table> VALUES(...) lines."""
    pattern = re.compile(
        r"INSERT INTO `?" + re.escape(table) + r"`? VALUES\((.+?)\);",
        re.DOTALL,
    )
    rows = []
    for m in pattern.finditer(content):
        raw = m.group(1)
        rows.append(_split_values(raw))
    return rows


def _split_values(raw):
    """Split a MySQL VALUES(...) inner string into a list of strings."""
    values, cur, in_str, i = [], [], False, 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"' and (i == 0 or raw[i - 1] != "\\"):
            in_str = not in_str
            i += 1
            continue
        if ch == "," and not in_str:
            values.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur:
        values.append("".join(cur).strip())
    return values


def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def safe_date(v):
    if not v or v.strip() in ("", "0", "0000-00-00", "NULL"):
        return None
    v = v.strip().replace("\\", "")
    try:
        return dateparser.parse(v, dayfirst=True).date()
    except Exception:
        return None


def safe_dt(v):
    if not v or v.strip() in ("", "NULL"):
        return None
    try:
        return dateparser.parse(v.strip()).replace(tzinfo=None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# STEP 0 — create tables
# ---------------------------------------------------------------------------

def create_tables():
    Base.metadata.create_all(bind=engine)
    print("✓ Tables created / verified")


# ---------------------------------------------------------------------------
# STEP 1 — admin user
# ---------------------------------------------------------------------------

def seed_admin(db):
    if db.query(User).filter_by(email="admin@dogartrading.com").first():
        print("  admin user already exists")
        return
    db.add(User(
        name="Administrator",
        email="admin@dogartrading.com",
        password_hash=hash_password("admin123"),
        role="admin",
        is_active=True,
    ))
    db.commit()
    print("✓ Admin user seeded  (admin@dogartrading.com / admin123)")


# ---------------------------------------------------------------------------
# STEP 2 — company settings
# ---------------------------------------------------------------------------

def seed_company(db):
    if db.query(CompanySettings).count():
        return
    db.add(CompanySettings(
        company_name="Dogar Trading Corporation",
        company_name_arabic="",
        oep_license_number="1338/SKT",
        owner_name="",
        address="Ghalla Mandi, Circular Road, Daska District Sialkot (Pakistan)",
        phone="0092-52-6613893-6615393",
        fax="0092-52-6610893",
        email="dogar@saudia.com",
        website="www.doggars.com",
        subdomain="dogar",
        slug="dogar",
        file_prefix="DTC/786/",
        starting_point=0,
        status="active",
        plan="OEP Yearly",
    ))
    db.commit()
    print("✓ Company settings seeded")


# ---------------------------------------------------------------------------
# STEP 3 — roles
# ---------------------------------------------------------------------------

ALL_PERMS = [
    "users.view","users.manage","roles.manage","login_history.view",
    "clients.view","clients.manage","demands.view","demands.manage",
    "candidates.view","candidates.manage",
    "documents.view","documents.manage","documents.print",
    "agents.view","agents.manage","settings.manage",
]
import json as _json

def seed_roles(db):
    if db.query(Role).count():
        return
    db.add_all([
        Role(name="Admin",   description="Full access",           is_system=True, permissions=_json.dumps(ALL_PERMS)),
        Role(name="Manager", description="Manage day-to-day",     is_system=True,
             permissions=_json.dumps([p for p in ALL_PERMS if "settings" not in p and "roles" not in p])),
        Role(name="Staff",   description="View & basic edits",    is_system=True,
             permissions=_json.dumps(["clients.view","demands.view","candidates.view",
                                      "candidates.manage","documents.view","documents.print"])),
    ])
    db.commit()
    print("✓ Roles seeded")


# ---------------------------------------------------------------------------
# STEP 4 — Visa Categories (preserve original IDs)
# ---------------------------------------------------------------------------

def migrate_visa_categories(db, content):
    if db.query(VisaCategory).count():
        db.query(VisaCategory).delete()
        db.commit()

    rows = parse_rows(content, "visa_categories")
    # Disable autoincrement check for explicit IDs in SQLite
    objs = []
    for r in rows:
        if len(r) < 2:
            continue
        vid = safe_int(r[0])
        name = r[1].strip()
        if not name:
            continue
        objs.append(VisaCategory(id=vid, name=name, code=name[:20].upper().replace(" ", "_"), is_active=True))
    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {len(objs)} visa categories")
    return {vc.id: vc.name for vc in objs}


# ---------------------------------------------------------------------------
# STEP 5 — Embassies (map old IDs 3,4,5,6)
# ---------------------------------------------------------------------------

EMBASSY_COUNTRY = {3: "Pakistan", 4: "Pakistan", 5: "UAE", 6: "Pakistan"}
EMBASSY_CITY    = {3: "Islamabad", 4: "Karachi", 5: "Abu Dhabi", 6: "Lahore"}

def migrate_embassies(db, content):
    if db.query(Embassy).count():
        db.query(Embassy).delete()
        db.commit()

    rows = parse_rows(content, "embassies")
    objs = []
    emb_map = {}
    for r in rows:
        if len(r) < 2:
            continue
        eid = safe_int(r[0])
        name = r[1].strip()
        country = EMBASSY_COUNTRY.get(eid, "Pakistan")
        city    = EMBASSY_CITY.get(eid, "")
        e = Embassy(id=eid, name=name, country=country, city=city)
        objs.append(e)
        emb_map[eid] = name
    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {len(objs)} embassies")
    return emb_map


# ---------------------------------------------------------------------------
# STEP 6 — Cities
# ---------------------------------------------------------------------------

def migrate_cities(db, content):
    if db.query(City).count():
        db.query(City).delete()
        db.commit()

    rows = parse_rows(content, "cities")
    objs = []
    for r in rows:
        if len(r) < 2:
            continue
        name = r[1].strip()
        province = r[2].strip() if len(r) > 2 else ""
        country  = r[3].strip() if len(r) > 3 else "Pakistan"
        if not name:
            continue
        objs.append(City(name=name, province=province, country=country or "Pakistan"))
    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {len(objs)} cities")


# ---------------------------------------------------------------------------
# STEP 7 — Medical Centers
# ---------------------------------------------------------------------------

def migrate_medical_centers(db, content):
    if db.query(MedicalCenter).count():
        db.query(MedicalCenter).delete()
        db.commit()

    rows = parse_rows(content, "medical_centers")
    objs, mcenter_map = [], {}
    for r in rows:
        if len(r) < 2:
            continue
        mid  = safe_int(r[0])
        name = r[1].strip()
        city = r[2].strip() if len(r) > 2 else ""
        if not name:
            continue
        m = MedicalCenter(id=mid, name=name, city=city)
        objs.append(m)
        mcenter_map[mid] = name
    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {len(objs)} medical centers")
    return mcenter_map


# ---------------------------------------------------------------------------
# STEP 8 — Phone book → Contacts
# ---------------------------------------------------------------------------

def migrate_contacts(db, content):
    db.query(Contact).delete()
    db.commit()

    rows = parse_rows(content, "phone_book")
    objs = []
    for r in rows:
        if len(r) < 2:
            continue
        name   = r[1].strip()
        phone  = r[2].strip() if len(r) > 2 else ""
        mobile = r[3].strip() if len(r) > 3 else ""
        objs.append(Contact(name=name, phone=phone or mobile, note=""))
    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {len(objs)} contacts from phone_book")


# ---------------------------------------------------------------------------
# STEP 9 — Parties → Clients (1243 rows)
# ---------------------------------------------------------------------------

COUNTRY_MAP = {"1": "SAUDI ARABIA", "2": "UAE", 1: "SAUDI ARABIA", 2: "UAE"}

def migrate_clients(db, content):
    db.query(Client).delete()
    db.commit()

    rows = parse_rows(content, "parties")
    objs, count = [], 0
    for r in rows:
        if len(r) < 2:
            continue
        try:
            cid     = safe_int(r[0])
            name    = r[1].strip() or "Unknown"
            phone   = r[2].strip() if len(r) > 2 else ""
            mobile  = r[3].strip() if len(r) > 3 else ""
            address = r[4].strip() if len(r) > 4 else ""
            city    = r[5].strip() if len(r) > 5 else ""
            country = r[6].strip() if len(r) > 6 else "SAUDI ARABIA"
            name2   = r[7].strip() if len(r) > 7 else ""
            addr2   = r[8].strip() if len(r) > 8 else ""
            tel2    = r[9].strip() if len(r) > 9 else ""
            status  = r[10].strip() if len(r) > 10 else "active"

            c = Client(
                id=cid,
                company_name=name,
                client_type="Company",
                status=status if status in ("active", "inactive") else "active",
                phone=phone or mobile,
                address=address,
                city=city,
                country=country,
                contact_person=name2,
                sponsor_name=name,
                sponsor_address=address,
                sponsor_phone=phone,
                sponsor_alt_phone=mobile,
            )
            objs.append(c)
            count += 1
        except (ValueError, TypeError, AttributeError) as e:
            # Skip malformed legacy rows but log so operators can audit them.
            import logging
            logging.getLogger("dtc.migrate").warning("Skipping malformed client row: %s", e)

    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {count} clients (parties)")
    return {c.id for c in objs}


# ---------------------------------------------------------------------------
# STEP 10 — Visa Receipts → Demands (2265 rows)
# ---------------------------------------------------------------------------

def migrate_demands(db, content, emb_map, client_ids):
    db.query(Demand).delete()
    db.commit()

    rows = parse_rows(content, "visa_receipts")
    objs, count, skipped = [], 0, 0

    for r in rows:
        if len(r) < 20:
            continue
        try:
            did         = safe_int(r[0])
            file_number = r[1].strip()            # raw number like "5983"
            recv_date   = safe_date(r[2])
            hijri       = r[3].strip() if len(r) > 3 else ""
            party_id    = safe_int(r[4])
            sponsor     = r[5].strip()            if len(r) > 5  else ""
            sponsor_ar  = r[6].strip()            if len(r) > 6  else ""
            sp_addr     = r[7].strip()            if len(r) > 7  else ""
            sp_addr_ar  = r[8].strip()            if len(r) > 8  else ""
            sp_ph1      = r[9].strip()            if len(r) > 9  else ""
            sp_ph2      = r[10].strip()           if len(r) > 10 else ""
            visa_no     = r[11].strip()           if len(r) > 11 else ""
            bataka      = r[12].strip()           if len(r) > 12 else ""
            # r[13]-r[18] are sender/ref/transfer fields (not in new schema)
            country_id  = safe_int(r[19])         if len(r) > 19 else 1
            emb_id      = safe_int(r[20])         if len(r) > 20 else 0
            # r[21]-r[24] fees
            perm_date   = safe_date(r[25])        if len(r) > 25 else None
            perm_no     = r[26].strip()           if len(r) > 26 else ""
            status      = r[27].strip()           if len(r) > 27 else "active"

            # Skip if client doesn't exist (orphan record)
            if party_id not in client_ids:
                skipped += 1
                continue

            country_name = COUNTRY_MAP.get(country_id, "SAUDI ARABIA")
            emb_name     = emb_map.get(emb_id, "")

            # Normalize status
            if status not in ("active", "processing", "filled", "expired", "cancelled"):
                status = "active"

            d = Demand(
                id=did,
                file_number=file_number,
                demand_code=file_number,
                client_id=party_id,
                receiving_date=recv_date,
                permission_no=perm_no,
                permission_date=perm_date,
                sponsor_name=sponsor,
                sponsor_name_arabic=sponsor_ar,
                sponsor_address=sp_addr,
                sponsor_address_arabic=sp_addr_ar,
                sponsor_phone=sp_ph1,
                sponsor_alt_phone=sp_ph2,
                visa_number=visa_no,
                bataka_number=bataka,
                visa_issue_date_hijri=hijri,
                country=country_name,
                embassy=emb_name,
                status=status,
            )
            objs.append(d)
            count += 1
        except (ValueError, TypeError, AttributeError) as e:
            import logging
            logging.getLogger("dtc.migrate").warning("Skipping malformed demand row: %s", e)

    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {count} demands (visa_receipts), skipped {skipped} orphans")
    return {d.id for d in objs}


# ---------------------------------------------------------------------------
# STEP 11 — Visa Receipt Items → Job Categories (2381 rows)
# ---------------------------------------------------------------------------

def migrate_job_categories(db, content, cat_map, demand_ids):
    db.query(JobCategory).delete()
    db.commit()

    rows = parse_rows(content, "visa_receipts_items")
    objs, count, skipped = [], 0, 0

    for r in rows:
        if len(r) < 5:
            continue
        try:
            jid        = safe_int(r[0])
            demand_id  = safe_int(r[1])
            cat_id     = safe_int(r[2])
            total      = safe_int(r[3], 1)
            salary     = safe_float(r[4])
            # r[5] = sold (assigned count — ignored, calculated from assignments)

            if demand_id not in demand_ids:
                skipped += 1
                continue

            trade_name = cat_map.get(cat_id, f"Category {cat_id}")

            jc = JobCategory(
                id=jid,
                demand_id=demand_id,
                trade=trade_name,
                quantity=total,
                salary=salary,
                salary_currency="SAR",
            )
            objs.append(jc)
            count += 1
        except (ValueError, TypeError, AttributeError) as exc:
            import logging
            logging.getLogger("dtc.migrate").warning("Skipping malformed job_category row: %s", exc)

    db.add_all(objs)
    db.commit()
    print(f"✓ Migrated {count} job categories, skipped {skipped}")
    return {jc.id for jc in objs}


# ---------------------------------------------------------------------------
# STEP 12 — Passport Receivings → Candidates + Assignments (2672 rows)
# ---------------------------------------------------------------------------

STATUS_MAP = {
    "pending": "pending",
    "active": "pending",
    "processing": "processing",
    "issued": "issued",
    "deployed": "deployed",
    "documents_pending": "documents_pending",
    "cancelled": "pending",
}


def migrate_candidates(db, content, mcenter_map, job_cat_ids, demand_ids):
    db.query(CandidateAssignment).delete()
    db.query(Candidate).delete()
    db.commit()

    rows = parse_rows(content, "passport_recevings")
    cands, assignments, count, skipped = [], [], 0, 0

    for r in rows:
        # id vid rid datetime name name_arabic fname fname_arabic mname gender
        # address dob pob pob_arabic pnum nationality religion pid ped issueauth
        # issueauth_arabic cnic marstatus qualification phone nokinname nokinnic
        # nokinrel tehsil district per_no per_date destination car_name province
        # date nadra visa_stamp_date protector_no d_of_departure flight_no
        # ticket_no p_send_d p_consign_no p_courrier_n p_receiv_d
        # medical_send_d medical_consign_no medical_courier_n age enumber
        # visa_category_id mcenter_id gamcano medicaldate photo recv_by status
        if len(r) < 10:
            continue
        try:
            cid         = safe_int(r[0])
            vid         = safe_int(r[1])   # job_category_id
            rid         = safe_int(r[2])   # demand_id
            full_name   = r[4].strip()  if len(r) > 4  else ""
            name_ar     = r[5].strip()  if len(r) > 5  else ""
            father      = r[6].strip()  if len(r) > 6  else ""
            father_ar   = r[7].strip()  if len(r) > 7  else ""
            mother      = r[8].strip()  if len(r) > 8  else ""
            gender      = r[9].strip()  if len(r) > 9  else "Male"
            address     = r[10].strip() if len(r) > 10 else ""
            dob         = safe_date(r[11]) if len(r) > 11 else None
            pob         = r[12].strip() if len(r) > 12 else ""
            pob_ar      = r[13].strip() if len(r) > 13 else ""
            passport_no = r[14].strip() if len(r) > 14 else ""
            nationality = r[15].strip() if len(r) > 15 else "PAKISTANI"
            religion    = r[16].strip() if len(r) > 16 else ""
            p_issue     = safe_date(r[17]) if len(r) > 17 else None
            p_expiry    = safe_date(r[18]) if len(r) > 18 else None
            iss_auth    = r[19].strip() if len(r) > 19 else ""
            iss_auth_ar = r[20].strip() if len(r) > 20 else ""
            cnic        = r[21].strip() if len(r) > 21 else ""
            marital     = r[22].strip() if len(r) > 22 else "Single"
            qual        = r[23].strip() if len(r) > 23 else ""
            phone       = r[24].strip() if len(r) > 24 else ""
            nok_name    = r[25].strip() if len(r) > 25 else ""
            nok_nic     = r[26].strip() if len(r) > 26 else ""
            nok_rel     = r[27].strip() if len(r) > 27 else ""
            tehsil      = r[28].strip() if len(r) > 28 else ""
            district    = r[29].strip() if len(r) > 29 else ""
            per_no      = r[30].strip() if len(r) > 30 else ""
            per_date    = safe_date(r[31]) if len(r) > 31 else None
            destination = r[32].strip() if len(r) > 32 else ""
            province    = r[34].strip() if len(r) > 34 else ""
            nadra       = r[36].strip() if len(r) > 36 else ""
            visa_stamp  = safe_date(r[37]) if len(r) > 37 else None
            prot_no     = r[38].strip() if len(r) > 38 else ""
            departure   = safe_date(r[39]) if len(r) > 39 else None
            flight_no   = r[40].strip() if len(r) > 40 else ""
            ticket_no   = r[41].strip() if len(r) > 41 else ""
            med_send    = safe_date(r[46]) if len(r) > 46 else None
            med_consign = r[47].strip() if len(r) > 47 else ""
            med_courier = r[48].strip() if len(r) > 48 else ""
            age_v       = safe_int(r[49]) if len(r) > 49 else None
            enumber     = r[50].strip() if len(r) > 50 else ""
            mcenter_id  = safe_int(r[52]) if len(r) > 52 else 0
            gamca_no    = r[53].strip() if len(r) > 53 else ""
            med_date    = safe_date(r[54]) if len(r) > 54 else None
            photo       = r[55].strip() if len(r) > 55 else ""
            raw_status  = r[57].strip() if len(r) > 57 else "pending"

            if not full_name:
                continue

            status = STATUS_MAP.get(raw_status.lower(), "pending")
            mc_name = mcenter_map.get(mcenter_id, "")

            c = Candidate(
                id=cid,
                full_name=full_name,
                name_arabic=name_ar,
                father_name=father,
                father_name_arabic=father_ar,
                mother_name=mother,
                gender="Male" if gender.lower() in ("male", "m") else "Female",
                marital_status=marital or "Single",
                religion=religion or "Islam",
                date_of_birth=dob,
                place_of_birth=pob,
                place_of_birth_arabic=pob_ar,
                nationality=nationality or "PAKISTANI",
                address=address,
                phone=phone,
                tehsil=tehsil,
                district=district,
                province=province,
                passport_no=passport_no,
                passport_issue_date=p_issue,
                passport_expiry_date=p_expiry,
                issuing_authority=iss_auth,
                issuing_authority_arabic=iss_auth_ar,
                cnic=cnic,
                nadra_token_no=nadra,
                qualification=qual,
                next_of_kin_name=nok_name,
                next_of_kin_nic=nok_nic,
                next_of_kin_relation=nok_rel,
                permission_no=per_no,
                permission_date=per_date,
                destination=destination,
                protector_no=prot_no,
                date_of_departure=departure,
                flight_no=flight_no,
                ticket_no=ticket_no,
                medical_center=mc_name,
                gamca_number=gamca_no,
                medical_date=med_date,
                medical_send_date=med_send,
                medical_consignment_no=med_consign,
                medical_courier_name=med_courier,
                age_employee=age_v if age_v else None,
                e_number=enumber,
                visa_stamp_date=visa_stamp,
                photo=photo,
                status=status,
            )
            cands.append(c)
            count += 1

            # Create assignment if both demand and job_category exist
            if rid in demand_ids and vid in job_cat_ids:
                assignments.append(CandidateAssignment(
                    candidate_id=cid,
                    job_category_id=vid,
                    status=status,
                ))

        except Exception as e:
            skipped += 1

    db.add_all(cands)
    db.commit()
    print(f"✓ Migrated {count} candidates, skipped {skipped}")

    db.add_all(assignments)
    db.commit()
    print(f"✓ Created {len(assignments)} candidate assignments")


# ---------------------------------------------------------------------------
# STEP 13 — Agents + Agent Cash (from SQL dump)
# ---------------------------------------------------------------------------

def migrate_agents(db, content):
    db.query(AgentCash).delete()
    db.query(Agent).delete()
    db.commit()

    rows = parse_rows(content, "agents")
    agents, count = [], 0
    for r in rows:
        if len(r) < 6:
            continue
        aid = safe_int(r[0])
        name = r[1].strip()
        phone = r[2].strip() if len(r) > 2 else ""
        mobile = r[3].strip() if len(r) > 3 else ""
        address = r[4].strip() if len(r) > 4 else ""
        company = r[5].strip() if len(r) > 5 else ""
        status = r[6].strip() if len(r) > 6 else "active"
        agents.append(Agent(id=aid, name=name, phone=phone, mobile=mobile,
                            address=address, company_name=company,
                            status=status if status in ("active","inactive") else "active"))
        count += 1
    db.add_all(agents)
    db.commit()
    print(f"✓ Migrated {count} agents")

    agent_ids = {a.id for a in agents}
    cash_rows = parse_rows(content, "agents_cash")
    cash_objs, cc = [], 0
    for r in cash_rows:
        if len(r) < 8:
            continue
        cid = safe_int(r[0])
        dt = safe_dt(r[1]) or datetime.utcnow()
        agent_id = safe_int(r[2])
        if agent_id not in agent_ids:
            continue
        details = r[3].strip() if len(r) > 3 else ""
        debit = safe_float(r[4])
        credit = safe_float(r[5])
        method = r[6].strip() if len(r) > 6 else "cash"
        ref_id = r[7].strip() if len(r) > 7 else ""
        cash_objs.append(AgentCash(id=cid, datetime=dt, agent_id=agent_id,
                                   details=details, debit=debit, credit=credit,
                                   method=method or "cash", ref_id=ref_id))
        cc += 1
    db.add_all(cash_objs)
    db.commit()
    print(f"✓ Migrated {cc} agent cash entries")


# ---------------------------------------------------------------------------
# STEP 14 — Document templates (14 PDF-background templates)
# ---------------------------------------------------------------------------

TEMPLATE_DEFS = [
    ("Allied Bank Deposit Form",         "Allied Bank Form-7 deposit slip",                    "Protector Documents", "candidate", "allied_bank_form7.jpg"),
    ("Demand Letter",                    "Letter sent to ministry to register demand",           "Visa Process",        "demand",    "dogar_letterhead.jpg"),
    ("E-Number Enrollment Request Form", "Request form for E-Number enrollment",                 "Visa Process",        "candidate", "dogar_letterhead.jpg"),
    ("HBL - Overseas Employment Corporation","HBL Form 32-A deposit slip",                      "Protector Documents", "candidate", "hbl_form_32a.jpg"),
    ("NBP Deposit Slip",                 "National Bank Pakistan deposit slip",                  "Protector Documents", "candidate", "nbp_deposit_slip.jpg"),
    ("NBP Deposit Slip - New",           "National Bank Pakistan deposit slip (new format)",     "Protector Documents", "candidate", "nbp_deposit_slip_new.jpg"),
    ("OEP Form Page 1",                  "Emigrant / Employer Registration Through OEP Form P1","Protector Documents", "candidate", "oep_form_p1.jpg"),
    ("OEP Form Page 2",                  "Emigrant / Employer Registration Through OEP Form P2","Protector Documents", "candidate", "oep_form_p2.jpg"),
    ("Permission Undertaking 1",         "Permission undertaking variant 1",                     "Protector Documents", "candidate", "permission_undertaking_1.jpg"),
    ("Permission Undertaking 2",         "Permission undertaking variant 2",                     "Protector Documents", "candidate", "permission_undertaking_2.jpg"),
    ("Permission Undertaking 3",         "Permission undertaking variant 3",                     "Protector Documents", "candidate", "permission_undertaking_3.jpg"),
    ("Permissions for Recruitment",      "Recruitment permission undertaking",                   "Protector Documents", "candidate", "permissions_for_recruitment.jpg"),
    ("Saudi Visa Application",           "Saudi Arabia Visa Application Form",                   "Visa Process",        "candidate", "saudi_visa_application.jpg"),
    ("Visa Application Form - Karachi",  "Saudi Arabia Visa Application (Karachi Embassy)",      "Visa Process",        "candidate", "visa_application_karachi.jpg"),
    # Extracted from uploaded PDF
    ("PDF Template 01",  "Extracted from uploaded PDF - Page 1",  "Visa Process",        "candidate", "pdf_extract_p01.jpg"),
    ("PDF Template 02",  "Extracted from uploaded PDF - Page 2",  "Visa Process",        "candidate", "pdf_extract_p02.jpg"),
    ("PDF Template 03",  "Extracted from uploaded PDF - Page 3",  "Visa Process",        "candidate", "pdf_extract_p03.jpg"),
    ("PDF Template 04",  "Extracted from uploaded PDF - Page 4",  "Protector Documents", "candidate", "pdf_extract_p04.jpg"),
    ("PDF Template 05",  "Extracted from uploaded PDF - Page 5",  "Protector Documents", "candidate", "pdf_extract_p05.jpg"),
    ("PDF Template 06",  "Extracted from uploaded PDF - Page 6",  "Protector Documents", "candidate", "pdf_extract_p06.jpg"),
    ("PDF Template 07",  "Extracted from uploaded PDF - Page 7",  "Protector Documents", "candidate", "pdf_extract_p07.jpg"),
    ("PDF Template 08",  "Extracted from uploaded PDF - Page 8",  "Protector Documents", "candidate", "pdf_extract_p08.jpg"),
    ("PDF Template 09",  "Extracted from uploaded PDF - Page 9",  "Protector Documents", "candidate", "pdf_extract_p09.jpg"),
    ("PDF Template 10",  "Extracted from uploaded PDF - Page 10", "Protector Documents", "candidate", "pdf_extract_p10.jpg"),
    ("PDF Template 11",  "Extracted from uploaded PDF - Page 11", "Protector Documents", "candidate", "pdf_extract_p11.jpg"),
    ("PDF Template 12",  "Extracted from uploaded PDF - Page 12", "Protector Documents", "candidate", "pdf_extract_p12.jpg"),
    ("PDF Template 13",  "Extracted from uploaded PDF - Page 13", "Visa Process",        "candidate", "pdf_extract_p13.jpg"),
    ("PDF Template 14",  "Extracted from uploaded PDF - Page 14", "Visa Process",        "candidate", "pdf_extract_p14.jpg"),
]


def seed_document_templates(db):
    from app.models.document import DocumentTemplate, DocumentField
    if db.query(DocumentTemplate).count():
        return
    for (name, desc, cat, ds, bg) in TEMPLATE_DEFS:
        bg_path = f"{PDF_BG}/{bg}"
        tpl = DocumentTemplate(
            name=name, description=desc, category=cat,
            data_source=ds, background_image=bg_path, is_active=True,
        )
        db.add(tpl)
    db.commit()
    print(f"✓ Seeded {len(TEMPLATE_DEFS)} document templates")


# ---------------------------------------------------------------------------
# STEP 15 — Service Charges
# ---------------------------------------------------------------------------

def seed_service_charges(db):
    if db.query(ServiceCharge).count():
        return
    db.add_all([
        ServiceCharge(name="OEP Service Fee",       amount=4500,  description="Per-candidate OEP processing"),
        ServiceCharge(name="Protector Fee",          amount=2500,  description="Protector certificate"),
        ServiceCharge(name="State Life Insurance",   amount=3500,  description="Insurance fee"),
        ServiceCharge(name="Medical Fee",            amount=6000,  description="Medical examination fee"),
        ServiceCharge(name="Visa Stamping Fee",      amount=2000,  description="Visa stamping charges"),
    ])
    db.commit()
    print("✓ Service charges seeded")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run():
    print("=" * 65)
    print("  Dogar Trading Corporation — Full MySQL → SQLite Migration")
    print("=" * 65)

    if not os.path.exists(SQL_FILE):
        print(f"✗ SQL backup not found at {SQL_FILE}")
        sys.exit(1)

    print(f"  Reading SQL dump: {SQL_FILE}")
    content = load_sql(SQL_FILE)
    print(f"  Loaded {len(content):,} characters")

    # Ensure data dir
    os.makedirs("data", exist_ok=True)

    create_tables()
    db = SessionLocal()
    try:
        seed_admin(db)
        seed_company(db)
        seed_roles(db)
        seed_service_charges(db)
        seed_document_templates(db)

        cat_map  = migrate_visa_categories(db, content)
        emb_map  = migrate_embassies(db, content)
        migrate_cities(db, content)
        mc_map   = migrate_medical_centers(db, content)
        migrate_contacts(db, content)
        migrate_agents(db, content)

        client_ids = migrate_clients(db, content)
        demand_ids = migrate_demands(db, content, emb_map, client_ids)
        jc_ids     = migrate_job_categories(db, content, cat_map, demand_ids)
        migrate_candidates(db, content, mc_map, jc_ids, demand_ids)

    finally:
        db.close()

    print("=" * 65)
    print("  Migration complete!")
    print("  Login → admin@dogartrading.com / admin123")
    print("=" * 65)


if __name__ == "__main__":
    run()
