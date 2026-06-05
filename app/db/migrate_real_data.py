"""
Full-scale migration from the real legacy MySQL backup (`dogar_full_backup.sql`)
into the portal's SQLAlchemy schema.

Source tables (legacy MySQL)         →  Target tables (portal)
─────────────────────────────────────────────────────────────────
parties                              →  clients
passport_recevings                   →  candidates
visa_receipts                        →  demands
visa_receipts_items                  →  job_categories
agents                               →  agents
agents_cash                          →  agent_cash
visa_categories                      →  visa_categories
embassies                            →  embassies
cities                               →  cities
medical_centers                      →  medical_centers
users                                →  users  (legacy users imported as staff;
                                                admin@dogartrading.com preserved)
phone_book                           →  contacts
countries                            →  (informational only)

Usage:
    cd dogar_trading_portal
    source venv/bin/activate
    python -m app.db.migrate_real_data
"""

from __future__ import annotations

import re
import sys
import json
from pathlib import Path
from datetime import datetime, date
from typing import Iterator, List, Optional

from sqlalchemy import text

from app.db.session import SessionLocal, engine, Base
from app.core.security import hash_password as get_password_hash
from app.models import (
    User, Client, Candidate, CandidateAssignment,
    Demand, JobCategory,
    Agent, AgentCash,
    VisaCategory, Embassy, City, MedicalCenter, Contact,
)

# ─────────────────────────────────────────────────────────────────
# 1.  SQL DUMP TOKENISER  (MySQL `INSERT INTO t VALUES("..",...)`)
# ─────────────────────────────────────────────────────────────────

DUMP_PATH = Path(__file__).resolve().parent.parent.parent / "migrations" / "dogar_full_backup.sql"


def _parse_values(values_str: str) -> List[Optional[str]]:
    """
    Parse a single MySQL `VALUES(...)` payload — each field is wrapped in double-quotes,
    escapes are `\"`  `\\`  `\n` etc.  Returns a list of raw string values
    (or None for SQL NULL — rare in this dump).
    """
    out: List[Optional[str]] = []
    i, n = 0, len(values_str)
    while i < n:
        # skip whitespace + commas
        while i < n and values_str[i] in ' ,\t':
            i += 1
        if i >= n:
            break
        if values_str[i] == '"':
            # quoted string
            i += 1
            buf = []
            while i < n:
                c = values_str[i]
                if c == '\\' and i + 1 < n:
                    nxt = values_str[i + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r',
                                '0': '\0', '\\': '\\', '"': '"', "'": "'"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf.append(c)
                i += 1
            out.append(''.join(buf))
        elif values_str[i:i + 4].upper() == 'NULL':
            out.append(None)
            i += 4
        else:
            # bare token (number, etc.)
            j = i
            while j < n and values_str[j] not in ',)':
                j += 1
            out.append(values_str[i:j].strip())
            i = j
    return out


def iter_inserts(table: str) -> Iterator[List[Optional[str]]]:
    """Yield each VALUES tuple as a list of raw strings for the given table."""
    if not DUMP_PATH.exists():
        raise FileNotFoundError(f"Legacy backup not found at {DUMP_PATH}")
    pat = re.compile(rf'INSERT INTO {re.escape(table)} VALUES\((.*)\);\s*$')
    with DUMP_PATH.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = pat.match(line.rstrip('\n'))
            if m:
                yield _parse_values(m.group(1))


# ─────────────────────────────────────────────────────────────────
# 2.  VALUE COERCION HELPERS
# ─────────────────────────────────────────────────────────────────

def s(v) -> str:
    """String, never None."""
    return '' if v is None else str(v).strip()


def i(v, default: int = 0) -> int:
    try:
        return int(float(s(v)))
    except (ValueError, TypeError):
        return default


def f(v, default: float = 0.0) -> float:
    try:
        return float(s(v))
    except (ValueError, TypeError):
        return default


_DATE_FORMATS = (
    '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d',
    '%d-%b-%Y', '%d %b %Y',
)


def parse_date(v) -> Optional[date]:
    txt = s(v)
    if not txt or txt in ('0000-00-00', '00-00-0000', '0', 'NULL'):
        return None
    # cut off any time portion
    txt = txt.split(' ')[0].split('T')[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(v) -> Optional[datetime]:
    txt = s(v)
    if not txt or txt.startswith('0000-00-00'):
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y'):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────
# 3.  MIGRATION FUNCTIONS
# ─────────────────────────────────────────────────────────────────

def wipe_existing(db, *models) -> None:
    """Truncate target tables (FK-safe order)."""
    for m in models:
        db.query(m).delete(synchronize_session=False)
    db.commit()


def migrate_visa_categories(db) -> dict[int, int]:
    """legacy_id → new_id map"""
    print('▶ Migrating visa_categories ...')
    db.query(VisaCategory).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    rows = list(iter_inserts('visa_categories'))
    for r in rows:
        old_id, name, name_arabic, status = r[0], r[1], r[2], r[3] if len(r) > 3 else 'active'
        if not s(name):
            continue
        obj = VisaCategory(
            name=s(name)[:150],
            code=s(name_arabic)[:50] if s(name_arabic) and not s(name_arabic).startswith('?') else '',
            description=s(name_arabic) if s(name_arabic) and not s(name_arabic).startswith('?') else '',
            is_active=(s(status).lower() == 'active'),
        )
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} visa categories')
    return id_map


def migrate_embassies(db) -> dict[int, int]:
    print('▶ Migrating embassies ...')
    db.query(Embassy).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    for r in iter_inserts('embassies'):
        old_id, name, city_code, status = r[0], r[1], r[2], r[3] if len(r) > 3 else 'active'
        obj = Embassy(
            name=s(name)[:150],
            city=s(city_code)[:100],
            country='Saudi Arabia' if 'embassy' in s(name).lower() else '',
        )
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} embassies')
    return id_map


def migrate_cities(db) -> dict[int, int]:
    print('▶ Migrating cities ...')
    db.query(City).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    for r in iter_inserts('cities'):
        old_id, name, name_arabic, status = r[0], r[1], r[2] if len(r) > 2 else '', r[3] if len(r) > 3 else 'active'
        if not s(name):
            continue
        obj = City(name=s(name)[:150], country='Saudi Arabia')
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} cities')
    return id_map


def migrate_medical_centers(db) -> dict[int, int]:
    print('▶ Migrating medical_centers ...')
    db.query(MedicalCenter).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    for r in iter_inserts('medical_centers'):
        old_id, name, code, status = r[0], r[1], r[2] if len(r) > 2 else '', r[3] if len(r) > 3 else 'active'
        if not s(name):
            continue
        obj = MedicalCenter(name=s(name)[:200], phone=s(code)[:50])
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} medical centers')
    return id_map


def migrate_phone_book(db) -> int:
    print('▶ Migrating phone_book → contacts ...')
    db.query(Contact).delete(synchronize_session=False)
    db.commit()
    n = 0
    for r in iter_inserts('phone_book'):
        # id, name, phone, mobile, status
        _, name, phone, mobile, _ = (r + [''] * 5)[:5]
        if not s(name):
            continue
        db.add(Contact(name=s(name)[:150], phone=(s(mobile) or s(phone))[:50]))
        n += 1
    db.commit()
    print(f'  ✓ {n} contacts')
    return n


def migrate_agents(db) -> dict[int, int]:
    """Reset and re-seed agents with their legacy IDs preserved (as much as possible)."""
    print('▶ Migrating agents ...')
    # Wipe agent_cash first (FK)
    db.query(AgentCash).delete(synchronize_session=False)
    db.query(Agent).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    for r in iter_inserts('agents'):
        # id, name, phone, mobile, address, comname, status
        old_id, name, phone, mobile, address, comname, status = (r + [''] * 7)[:7]
        if not s(name):
            continue
        obj = Agent(
            name=s(name)[:255],
            company_name=s(comname)[:255],
            phone=s(phone)[:50],
            mobile=s(mobile)[:50],
            address=s(address),
            status=s(status) or 'active',
        )
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id

    # legacy agents_cash references agent_ids 5,6,7 that aren't present in `agents` table.
    # Create placeholder agents so we don't lose those 89 ledger entries.
    missing_agent_ids: set[int] = set()
    for r in iter_inserts('agents_cash'):
        aid = i(r[2])
        if aid and aid not in id_map:
            missing_agent_ids.add(aid)
    for aid in sorted(missing_agent_ids):
        placeholder = Agent(name=f'Legacy Agent #{aid}', status='active',
                            notes='Auto-created during 2026-05-18 migration for orphan ledger entries.')
        db.add(placeholder)
        db.flush()
        id_map[aid] = placeholder.id
    db.commit()
    print(f'  ✓ {len(id_map)} agents (incl. {len(missing_agent_ids)} placeholders)')
    return id_map


def migrate_agents_cash(db, agent_id_map: dict[int, int]) -> int:
    print('▶ Migrating agents_cash ...')
    n = 0
    for r in iter_inserts('agents_cash'):
        # id, datetime, agent_id, details, debit, credit, method, ref_id, user_id
        _, dt, agent_id, details, debit, credit, method, ref_id, user_id = (r + [''] * 9)[:9]
        mapped = agent_id_map.get(i(agent_id))
        if not mapped:
            continue
        db.add(AgentCash(
            datetime=parse_datetime(dt) or datetime.utcnow(),
            agent_id=mapped,
            details=s(details)[:500],
            debit=f(debit),
            credit=f(credit),
            method=s(method) or 'cash',
            ref_id=s(ref_id)[:100],
            user_id=None,  # legacy user_id refs the old user table, not ours
        ))
        n += 1
    db.commit()
    print(f'  ✓ {n} agent cash entries')
    return n


def migrate_parties(db) -> dict[int, int]:
    """parties → clients (foreign sponsors / employers)"""
    print('▶ Migrating parties → clients ...')
    # FK guard: wipe demands+job_cats+assignments+statements/contacts first
    db.execute(text('DELETE FROM candidate_assignments'))
    db.execute(text('DELETE FROM job_categories'))
    db.execute(text('DELETE FROM demands'))
    db.execute(text('DELETE FROM client_contacts'))
    db.execute(text('DELETE FROM client_statements'))
    db.query(Client).delete(synchronize_session=False)
    db.commit()
    id_map: dict[int, int] = {}
    for r in iter_inserts('parties'):
        # id, name, phone, mobile, address, city, country, name_two, address_two, tel_two, status
        (old_id, name, phone, mobile, address, city, country,
         name_two, address_two, tel_two, status) = (r + [''] * 11)[:11]
        if not s(name):
            continue
        obj = Client(
            company_name=s(name)[:255],
            client_type='Company',
            status=s(status) or 'active',
            phone=s(phone)[:50],
            sponsor_name=s(name)[:255],
            sponsor_address=s(address),
            sponsor_phone=s(phone)[:50],
            sponsor_alt_phone=s(mobile)[:50] or s(tel_two)[:50],
            city=s(city)[:100],
            country=s(country)[:100] or 'Saudi Arabia',
            address=s(address),
            contact_person=s(name_two)[:150],
            notes=(f"Secondary address: {s(address_two)}" if s(address_two) else ''),
        )
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} clients')
    return id_map


def migrate_visa_receipts(db, client_map: dict[int, int], embassy_map: dict[int, int]) -> dict[int, int]:
    """visa_receipts → demands"""
    print('▶ Migrating visa_receipts → demands ...')
    id_map: dict[int, int] = {}
    used_file_numbers: set[str] = set()
    n_skipped = 0
    for r in iter_inserts('visa_receipts'):
        # 28 cols
        (old_id, file_number, dte, visa_issue_date_h, party_id,
         sponsor_name, sponsor_name_arabic, sponsor_address, sponsor_address_arabic,
         sponsor_phone_1, sponsor_phone_2, visa_number, bataka_number,
         visa_sender_name, visa_sender_phone, visa_sender_address,
         ref_name, ref_phone, transfer_to,
         visa_country_id, emb_id,
         visa_process_fees, extra_charges, total_amount, total_visas,
         permission_date, permission_number, status) = (r + [''] * 28)[:28]

        client_id = client_map.get(i(party_id))
        if not client_id:
            # Create an orphan placeholder client so we don't lose the demand
            placeholder = Client(
                company_name=s(sponsor_name) or f'Unknown Sponsor #{old_id}',
                client_type='Company',
                sponsor_name=s(sponsor_name),
                sponsor_name_arabic=s(sponsor_name_arabic),
                sponsor_address=s(sponsor_address),
                sponsor_phone=s(sponsor_phone_1)[:50],
                notes='Auto-created from orphan visa_receipt during migration.',
            )
            db.add(placeholder)
            db.flush()
            client_id = placeholder.id
            client_map[i(party_id)] = client_id

        fn = s(file_number) or f'LEGACY-{old_id}'
        # Ensure uniqueness — file_number is unique in our schema
        base = fn
        k = 1
        while fn in used_file_numbers:
            k += 1
            fn = f'{base}-{k}'
        used_file_numbers.add(fn)

        embassy_name = ''
        emb_new = embassy_map.get(i(emb_id))
        if emb_new:
            # cheap lookup
            row = db.query(Embassy.name).filter(Embassy.id == emb_new).first()
            embassy_name = row.name if row else ''

        obj = Demand(
            file_number=fn[:50],
            demand_code=fn[:50],
            client_id=client_id,
            receiving_date=parse_date(dte),
            permission_no=s(permission_number)[:100],
            permission_date=parse_date(permission_date),
            reference=s(ref_name)[:150],
            sponsor_name=s(sponsor_name)[:255],
            sponsor_name_arabic=s(sponsor_name_arabic)[:255],
            sponsor_address=s(sponsor_address),
            sponsor_address_arabic=s(sponsor_address_arabic),
            sponsor_phone=s(sponsor_phone_1)[:50],
            sponsor_alt_phone=s(sponsor_phone_2)[:50],
            visa_number=s(visa_number)[:100],
            bataka_number=s(bataka_number)[:100],
            visa_issue_date=parse_date(dte),
            visa_issue_date_hijri=s(visa_issue_date_h)[:50],
            country='Saudi Arabia' if i(visa_country_id) == 1 else ('UAE' if i(visa_country_id) == 2 else ''),
            embassy=embassy_name[:150],
            visa_quota=i(total_visas),
            status=(s(status) or 'active').lower(),
            notes=(
                (f'Transfer To: {s(transfer_to)}\n' if s(transfer_to) else '') +
                (f'Sender: {s(visa_sender_name)} ({s(visa_sender_phone)})\n' if s(visa_sender_name) else '') +
                (f'Process Fees: {s(visa_process_fees)}, Extra: {s(extra_charges)}, Total: {s(total_amount)}\n'
                 if s(total_amount) else '')
            ).strip(),
        )
        db.add(obj)
        db.flush()
        id_map[i(old_id)] = obj.id
    db.commit()
    print(f'  ✓ {len(id_map)} demands ({n_skipped} skipped)')
    return id_map


def migrate_visa_receipt_items(db, demand_map: dict[int, int],
                               vcat_map: dict[int, int]) -> int:
    print('▶ Migrating visa_receipts_items → job_categories ...')
    n = 0
    # Build vcat name lookup
    vcat_name: dict[int, str] = {}
    for old_id, new_id in vcat_map.items():
        row = db.query(VisaCategory.name).filter(VisaCategory.id == new_id).first()
        if row:
            vcat_name[old_id] = row.name

    for r in iter_inserts('visa_receipts_items'):
        # id, visa_receipt_id, category_id, total, salary, sold
        _, receipt_id, cat_id, total, salary, sold = (r + [''] * 6)[:6]
        d_id = demand_map.get(i(receipt_id))
        if not d_id:
            continue
        trade = vcat_name.get(i(cat_id)) or f'Category #{i(cat_id)}'
        db.add(JobCategory(
            demand_id=d_id,
            trade=trade[:150],
            quantity=max(1, i(total)),
            salary=f(salary),
            salary_currency='SAR',
            contract_years=2,
            notes=(f'Sold/Deployed: {i(sold)}' if i(sold) else ''),
        ))
        n += 1
    db.commit()
    print(f'  ✓ {n} job categories / trade lines')
    return n


def migrate_candidates(db, demand_map: dict[int, int],
                      vcat_map: dict[int, int]) -> int:
    print('▶ Migrating passport_recevings → candidates ...')
    n = 0
    # Build a lookup: demand_id → first job_category_id (for assignment)
    first_jc: dict[int, int] = {}
    for jc in db.query(JobCategory.demand_id, JobCategory.id).order_by(JobCategory.id).all():
        first_jc.setdefault(jc.demand_id, jc.id)

    for r in iter_inserts('passport_recevings'):
        # 58 cols — see schema
        cols = (r + [''] * 58)[:58]
        (_, vid, rid, dt, name, name_arabic, fname, fname_arabic, mname,
         gender, address, dob, pob, pob_arabic, pnum, nationality, religion,
         pid, ped, issueauth, issueauth_arabic, cnic, marstatus,
         qualification, phone, nokinname, nokinnic, nokinrel,
         tehsil, district, per_no, per_date, destination, car_name, province,
         dte, nadra, visa_stamp_date, protector_no, d_of_departure, flight_no,
         ticket_no, p_send_d, p_consign_no, p_courrier_n, p_receiv_d,
         medical_send_d, medical_consign_no, medical_courier_n,
         age, enumber, visa_category_id, mcenter_id, gamcano, medicaldate,
         photo, recv_by, status) = cols

        if not s(name):
            continue

        # Medical center name lookup
        mc_name = ''
        if i(mcenter_id):
            row = db.query(MedicalCenter.name).filter(MedicalCenter.id == i(mcenter_id)).first()
            mc_name = row.name if row else ''

        obj = Candidate(
            full_name=s(name)[:255],
            name_arabic=s(name_arabic)[:255],
            father_name=s(fname)[:255],
            father_name_arabic=s(fname_arabic)[:255],
            mother_name=s(mname)[:255],
            gender=(s(gender).capitalize() or 'Male')[:10],
            marital_status=(s(marstatus).capitalize() or 'Single')[:20],
            religion=(s(religion) or 'Islam')[:30],
            date_of_birth=parse_date(dob),
            place_of_birth=s(pob)[:100],
            place_of_birth_arabic=s(pob_arabic)[:100],
            nationality=(s(nationality) or 'PAKISTANI')[:50],
            address=s(address),
            phone=s(phone)[:30],
            tehsil=s(tehsil)[:100],
            district=s(district)[:100],
            province=s(province)[:100],
            photo=s(photo)[:255],

            passport_no=s(pnum)[:50],
            passport_issue_date=parse_date(pid),
            passport_expiry_date=parse_date(ped),
            issuing_authority=(s(issueauth) or 'PAKISTAN')[:100],
            issuing_authority_arabic=s(issueauth_arabic)[:100],
            cnic=s(cnic)[:30],
            nadra_token_no=s(nadra)[:50],

            permission_no=s(per_no)[:100],
            permission_date=parse_date(per_date),
            qualification=s(qualification)[:150],
            age_employee=(i(age) or None),
            profession='',  # not in source — comes from visa_category if needed

            next_of_kin_name=s(nokinname)[:255],
            next_of_kin_nic=s(nokinnic)[:30],
            next_of_kin_relation=s(nokinrel)[:50],

            protector_no=s(protector_no)[:50],
            medical_center=mc_name[:150],
            gamca_number=s(gamcano)[:50],
            medical_date=parse_date(medicaldate),
            medical_send_date=parse_date(medical_send_d),
            medical_consignment_no=s(medical_consign_no)[:50],
            medical_courier_name=s(medical_courier_n)[:100],
            e_number=s(enumber)[:50],
            date_of_departure=parse_date(d_of_departure),
            flight_no=s(flight_no)[:50],
            destination=s(destination)[:100],
            ticket_no=s(ticket_no)[:50],
            visa_stamp_date=parse_date(visa_stamp_date),
            status=(s(status) or 'pending').lower(),
            notes=(
                (f'Carrier: {s(car_name)}\n' if s(car_name) else '') +
                (f'Received by user ID: {s(recv_by)}\n' if s(recv_by) else '') +
                (f'Legacy passport receiving ID: {cols[0]}, vid={s(vid)}, rid={s(rid)}\n')
            ).strip(),
        )
        db.add(obj)
        db.flush()

        # Create assignment if we can resolve a demand
        d_new = demand_map.get(i(vid))
        if d_new:
            jc_id = first_jc.get(d_new)
            if not jc_id:
                # Create a default job category for the demand
                vcat_new = vcat_map.get(i(visa_category_id))
                trade_name = 'General Worker'
                if vcat_new:
                    row = db.query(VisaCategory.name).filter(VisaCategory.id == vcat_new).first()
                    if row:
                        trade_name = row.name
                jc = JobCategory(demand_id=d_new, trade=trade_name[:150],
                                 quantity=1, salary=0, salary_currency='SAR')
                db.add(jc)
                db.flush()
                jc_id = jc.id
                first_jc[d_new] = jc_id
            db.add(CandidateAssignment(
                candidate_id=obj.id,
                job_category_id=jc_id,
                status=(s(status) or 'pending').lower(),
            ))

        n += 1
        if n % 500 == 0:
            db.commit()
            print(f'    ... {n} candidates committed')
    db.commit()
    print(f'  ✓ {n} candidates')
    return n


def migrate_legacy_users(db) -> int:
    """Add legacy users as staff (preserve our admin@dogartrading.com).
    Legacy passwords are MD5 hashes that won't decrypt — we set a default
    password "changeme123" they can use to log in, then must reset."""
    print('▶ Migrating legacy users ...')
    existing_emails = {u.email for u in db.query(User.email).all()}
    n = 0
    default_pw_hash = get_password_hash('changeme123')
    for r in iter_inserts('users'):
        # id, username, password, name, type, allowed_ips, profile_image, status
        _, username, _pw, name, utype, _ips, _img, status = (r + [''] * 8)[:8]
        if not s(username):
            continue
        email = f'{s(username).lower()}@legacy.dogartrading.com'
        if email in existing_emails:
            continue
        db.add(User(
            name=s(name) or s(username),
            email=email,
            password_hash=default_pw_hash,
            role='admin' if s(utype).lower() == 'admin' else 'staff',
            is_active=(i(status) == 1),
        ))
        n += 1
    db.commit()
    print(f'  ✓ {n} legacy users (default password: changeme123)')
    return n


# ─────────────────────────────────────────────────────────────────
# 4.  ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    print('═' * 60)
    print(' Dogar Trading Corporation — Real Data Migration')
    print(f' Source: {DUMP_PATH.name}')
    print('═' * 60)

    # Ensure schema exists
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Lookups first
        vcat_map = migrate_visa_categories(db)
        emb_map = migrate_embassies(db)
        migrate_cities(db)
        migrate_medical_centers(db)
        migrate_phone_book(db)

        # Agents + ledger
        agent_map = migrate_agents(db)
        migrate_agents_cash(db, agent_map)

        # Core business records
        client_map = migrate_parties(db)
        demand_map = migrate_visa_receipts(db, client_map, emb_map)
        migrate_visa_receipt_items(db, demand_map, vcat_map)
        migrate_candidates(db, demand_map, vcat_map)

        # Users (additional)
        migrate_legacy_users(db)

        # Final summary
        print('\n' + '═' * 60)
        print(' MIGRATION COMPLETE — Final counts:')
        print('═' * 60)
        for label, model in [
            ('Visa Categories', VisaCategory), ('Embassies', Embassy),
            ('Cities', City), ('Medical Centers', MedicalCenter),
            ('Contacts', Contact), ('Agents', Agent),
            ('Agent Cash Entries', AgentCash),
            ('Clients (Sponsors)', Client), ('Demands (Visa Files)', Demand),
            ('Job Categories', JobCategory), ('Candidates', Candidate),
            ('Candidate Assignments', CandidateAssignment),
            ('Users (total)', User),
        ]:
            print(f'  {label:<25s} {db.query(model).count():>6d}')
        print('═' * 60)
    finally:
        db.close()


if __name__ == '__main__':
    main()
