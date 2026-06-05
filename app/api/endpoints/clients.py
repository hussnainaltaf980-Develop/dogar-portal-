from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from typing import List, Optional
from datetime import date, datetime

from app.db.session import get_db
from app.core.deps import get_current_user
from app.core.permissions import require_permission
from app.models import Client, User, Demand, CandidateAssignment
from app.models.demand import JobCategory
from app.models.lookups import ClientContact, ClientStatement, CompanySettings
from app.schemas.schemas import ClientCreate, ClientUpdate, ClientOut

# Import the canonical display helper so all responses use the same format.
from app.api.endpoints.demands import display_file_number, _get_prefix

router = APIRouter()


def _client_to_dict(c: Client):
    return {
        col.name: (getattr(c, col.name).isoformat() if hasattr(getattr(c, col.name), "isoformat") else
                   (float(getattr(c, col.name)) if col.name in ("opening_balance",) and getattr(c, col.name) is not None else
                    getattr(c, col.name)))
        for col in c.__table__.columns
    }


@router.get("/")
def list_clients(db: Session = Depends(get_db), user: User = Depends(get_current_user),
                 q: Optional[str] = None, skip: int = 0, limit: int = 200):
    query = db.query(Client)
    if q:
        query = query.filter(Client.company_name.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(Client.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [_client_to_dict(c) for c in rows]}


@router.post("/")
def create_client(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not payload.get("company_name"):
        raise HTTPException(400, "Company name is required")
    # Accept any field defined on Client
    allowed = {c.name for c in Client.__table__.columns} - {"id", "created_at", "updated_at"}
    data = {k: v for k, v in payload.items() if k in allowed}
    obj = Client(**data)
    db.add(obj); db.commit(); db.refresh(obj)
    return _client_to_dict(obj)


@router.get("/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Client).filter(Client.id == client_id).first()
    if not obj:
        raise HTTPException(404, "Client not found")
    return _client_to_dict(obj)


@router.put("/{client_id}")
def update_client(client_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Client).filter(Client.id == client_id).first()
    if not obj:
        raise HTTPException(404, "Client not found")
    allowed = {c.name for c in Client.__table__.columns} - {"id", "created_at", "updated_at"}
    for k, v in payload.items():
        if k in allowed:
            setattr(obj, k, v)
    db.commit(); db.refresh(obj)
    return _client_to_dict(obj)


@router.delete("/{client_id}")
def delete_client(client_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("clients:delete"))):
    obj = db.query(Client).filter(Client.id == client_id).first()
    if not obj:
        raise HTTPException(404, "Client not found")
    db.delete(obj); db.commit()
    return {"ok": True}


# ===== Summary (stats cards on Profile tab) =====
@router.get("/{client_id}/summary")
def client_summary(client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")

    # Demand files count
    demand_count = db.query(func.count(Demand.id)).filter(Demand.client_id == client_id).scalar() or 0

    # Statement totals
    txn_row = db.query(
        func.count(ClientStatement.id).label("cnt"),
        func.coalesce(func.sum(ClientStatement.debit), 0).label("debit"),
        func.coalesce(func.sum(ClientStatement.credit), 0).label("credit"),
        func.max(ClientStatement.entry_date).label("last_date"),
    ).filter(ClientStatement.client_id == client_id).first()

    debit = float(txn_row.debit or 0)
    credit = float(txn_row.credit or 0)
    opening = float(client.opening_balance or 0)
    # Balance = opening + debit - credit  (debit = owed by client, credit = client paid)
    balance = opening + debit - credit

    contacts_count = db.query(func.count(ClientContact.id)).filter(ClientContact.client_id == client_id).scalar() or 0

    return {
        "transactions": int(txn_row.cnt or 0),
        "total_debit": debit,
        "total_credit": credit,
        "balance": balance,
        "contacts": int(contacts_count),
        "demand_files": int(demand_count),
        "last_transaction": txn_row.last_date.isoformat() if txn_row.last_date else None,
    }


# ===== Contacts (sub-resource) =====
@router.get("/{client_id}/contacts")
def list_contacts(client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(ClientContact).filter(ClientContact.client_id == client_id).order_by(ClientContact.id.desc()).all()
    items = [
        {
            "id": r.id, "name": r.name, "designation": r.designation,
            "email": r.email, "phone": r.phone, "is_primary": r.is_primary,
        } for r in rows
    ]
    # Return shaped envelope {items, total} for consistency with client_detail.html and other list endpoints
    return {"items": items, "total": len(items)}


@router.post("/{client_id}/contacts")
def create_contact(client_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not payload.get("name"):
        raise HTTPException(400, "Name is required")
    obj = ClientContact(
        client_id=client_id,
        name=payload["name"],
        designation=payload.get("designation", ""),
        email=payload.get("email", ""),
        phone=payload.get("phone", ""),
        is_primary=bool(payload.get("is_primary", False)),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return {"id": obj.id}


@router.delete("/{client_id}/contacts/{cid}")
def delete_contact(client_id: int, cid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(ClientContact).filter(ClientContact.id == cid, ClientContact.client_id == client_id).first()
    if not obj: raise HTTPException(404, "Not found")
    db.delete(obj); db.commit(); return {"ok": True}


# ===== Statement (sub-resource) =====
@router.get("/{client_id}/statement")
def list_statement(
    client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
    entry_type: Optional[str] = Query(None),
    date_from: Optional[date] = None, date_to: Optional[date] = None,
):
    query = db.query(ClientStatement).filter(ClientStatement.client_id == client_id)
    if entry_type and entry_type.lower() not in ("all", ""):
        query = query.filter(ClientStatement.entry_type == entry_type)
    if date_from: query = query.filter(ClientStatement.entry_date >= date_from)
    if date_to:   query = query.filter(ClientStatement.entry_date <= date_to)
    rows = query.order_by(ClientStatement.entry_date.desc(), ClientStatement.id.desc()).all()

    running = 0.0
    items = []
    # compute running balance bottom-up (oldest first) then reverse
    for r in reversed(rows):
        running += float(r.debit or 0) - float(r.credit or 0)
        items.append({
            "id": r.id,
            "entry_type": r.entry_type,
            "reference": r.reference,
            "description": r.description,
            "debit": float(r.debit or 0),
            "credit": float(r.credit or 0),
            "balance": running,
            "entry_date": r.entry_date.isoformat() if r.entry_date else None,
        })
    items.reverse()
    return {"items": items}


@router.post("/{client_id}/statement")
def create_statement(client_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    entry_type = (payload.get("entry_type") or "INVOICE").upper()
    if entry_type not in ("INVOICE", "PAYMENT"):
        raise HTTPException(400, "entry_type must be INVOICE or PAYMENT")
    obj = ClientStatement(
        client_id=client_id,
        entry_type=entry_type,
        reference=payload.get("reference", ""),
        description=payload.get("description", ""),
        debit=float(payload.get("debit", 0) or 0),
        credit=float(payload.get("credit", 0) or 0),
        entry_date=payload.get("entry_date") or date.today(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return {"id": obj.id}


@router.delete("/{client_id}/statement/{sid}")
def delete_statement(client_id: int, sid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(ClientStatement).filter(ClientStatement.id == sid, ClientStatement.client_id == client_id).first()
    if not obj: raise HTTPException(404, "Not found")
    db.delete(obj); db.commit(); return {"ok": True}


# ===== Demand files for this client =====
@router.get("/{client_id}/demands")
def list_client_demands(client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """
    Returns the demand-files list for a client, shaped for the
    `Demand Files` tab on the client detail page.

    Columns shown in the reference UI: File No, Date, Sponsor, Visa No, Visas, Status.
    We therefore include `receiving_date`, `sponsor_name`, `visa_number`, and the
    aggregated **visa count** (sum of trade quantities), plus envelope `{items,total}`
    for consistency with other list endpoints.
    """
    rows = db.query(Demand).filter(Demand.client_id == client_id).order_by(Demand.id.desc()).all()
    demand_ids = [d.id for d in rows]

    # Batch-load trade slot totals to avoid N+1 queries.
    slot_map: dict[int, int] = {}
    trades_count_map: dict[int, int] = {}
    if demand_ids:
        for did, total, trades_count in (
            db.query(
                JobCategory.demand_id,
                func.coalesce(func.sum(JobCategory.quantity), 0),
                func.count(JobCategory.id),
            )
            .filter(JobCategory.demand_id.in_(demand_ids))
            .group_by(JobCategory.demand_id)
            .all()
        ):
            slot_map[int(did)] = int(total or 0)
            trades_count_map[int(did)] = int(trades_count or 0)

    # Batch-load assigned candidate counts per demand via JobCategory join.
    assigned_map: dict[int, int] = {}
    if demand_ids:
        for did, cnt in (
            db.query(
                JobCategory.demand_id,
                func.count(CandidateAssignment.id),
            )
            .join(CandidateAssignment, CandidateAssignment.job_category_id == JobCategory.id)
            .filter(JobCategory.demand_id.in_(demand_ids))
            .group_by(JobCategory.demand_id)
            .all()
        ):
            assigned_map[int(did)] = int(cnt or 0)

    prefix = _get_prefix(db)
    items = []
    for d in rows:
        # Prefer receiving_date for the "Date" column (legacy backfilled from created_at).
        date_val = d.receiving_date.isoformat() if d.receiving_date else (
            d.created_at.date().isoformat() if d.created_at else None
        )
        total_slots = slot_map.get(d.id, 0)
        assigned = assigned_map.get(d.id, 0)
        items.append({
            "id": d.id,
            "file_number": d.file_number,                                   # raw storage form (e.g. "8185")
            "file_number_display": display_file_number(d.file_number, prefix),  # rendered (e.g. "DTC/786/8185")
            "receiving_date": date_val,
            "date": date_val,                    # alias used by some templates
            "sponsor_name": d.sponsor_name or "",
            "visa_number": d.visa_number or "",
            "visas": total_slots,                # aggregated trade quantity (total slots)
            "total_slots": total_slots,
            "assigned": assigned,                # candidates already assigned to this demand
            "available": max(total_slots - assigned, 0),
            "trades_count": trades_count_map.get(d.id, 0),
            "country": d.country or "",
            "embassy": d.embassy or "",
            "permission_no": d.permission_no or "",
            "status": d.status or "active",
            "created_at": d.created_at.isoformat() if d.created_at else None,
            # Deep links for navigation from the client detail Demand Files tab
            "url": f"/demands/{d.id}",
            "candidates_url": f"/candidates?demand_id={d.id}",
        })
    return {"items": items, "total": len(items)}


# ===== Print: Official Client Statement (server-rendered, A4) =====
@router.get("/{client_id}/print-statement", response_class=HTMLResponse)
def print_client_statement(
    client_id: int, request: Request,
    auto: int = Query(1, description="1 = auto-fire window.print() on load"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """v7 — Render an official Client Account Statement as a clean A4
    HTML document with the company letterhead, summary cards, ledger
    table, demand-files block, QR verification row and authorised
    signature line.  Replaces the legacy `window.print()`-the-UI flow
    (which screenshotted the on-screen panel and clipped at the side
    drawer borders)."""
    from app.services.receipts_renderer import render_client_statement

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    company = db.query(CompanySettings).first()

    # ---- Summary ----
    txn_row = db.query(
        func.count(ClientStatement.id).label("cnt"),
        func.coalesce(func.sum(ClientStatement.debit), 0).label("debit"),
        func.coalesce(func.sum(ClientStatement.credit), 0).label("credit"),
    ).filter(ClientStatement.client_id == client_id).first()
    opening = float(client.opening_balance or 0)
    debit = float(txn_row.debit or 0)
    credit = float(txn_row.credit or 0)
    summary = {
        "txn_count": int(txn_row.cnt or 0),
        "total_debit": debit,
        "total_credit": credit,
        "balance": opening + debit - credit,
    }

    # ---- Statement entries (running balance, oldest → newest) ----
    rows = db.query(ClientStatement).filter(
        ClientStatement.client_id == client_id
    ).order_by(ClientStatement.entry_date.asc(), ClientStatement.id.asc()).all()
    running = opening
    entries = []
    for r in rows:
        running += float(r.debit or 0) - float(r.credit or 0)
        entries.append({
            "entry_date": r.entry_date,
            "entry_type": r.entry_type,
            "reference": r.reference,
            "receipt_no": getattr(r, "receipt_no", None),
            "description": r.description,
            "debit": float(r.debit or 0),
            "credit": float(r.credit or 0),
            "balance": running,
        })

    # ---- Demand files ----
    demand_rows = db.query(Demand).filter(
        Demand.client_id == client_id
    ).order_by(Demand.id.desc()).all()
    prefix = _get_prefix(db)
    demand_ids = [d.id for d in demand_rows]
    cand_count_map: dict[int, int] = {}
    if demand_ids:
        for did, cnt in (
            db.query(JobCategory.demand_id, func.count(CandidateAssignment.id))
            .join(CandidateAssignment, CandidateAssignment.job_category_id == JobCategory.id)
            .filter(JobCategory.demand_id.in_(demand_ids))
            .group_by(JobCategory.demand_id)
            .all()
        ):
            cand_count_map[int(did)] = int(cnt or 0)
    demands_list = [{
        "file_number": display_file_number(d.file_number, prefix),
        "country": d.country or "",
        "embassy": d.embassy or "",
        "sponsor_name": d.sponsor_name or "",
        "status": d.status or "active",
        "candidates_count": cand_count_map.get(d.id, 0),
    } for d in demand_rows]

    # ---- Client dict ----
    client_dict = {
        "id": client.id,
        "company_name": client.company_name,
        "type": client.client_type,
        "owner": getattr(client, "contact_person", "") or "",
        "phone": client.phone or "",
        "mobile": getattr(client, "phone", "") or "",
        "email": client.email or "",
        "address": client.address or getattr(client, "street", "") or "",
        "city": client.city or "",
        "country": client.country or "",
        "status": client.status or "active",
        "opening_balance": float(client.opening_balance or 0),
    }

    # Build absolute verify URL using the current host
    base = str(request.base_url).rstrip("/")
    verify_url = f"{base}/clients/{client.id}"

    # Reuse the same company-dict helper as demands.py so the header
    # stays identical across all printed receipts/statements.
    from app.api.endpoints.demands import _company_dict
    company_dict = _company_dict(company)

    html = render_client_statement(
        client=client_dict,
        entries=entries,
        summary=summary,
        demands=demands_list,
        company=company_dict,
        verify_url=verify_url,
        auto_print=(auto != 0),
    )
    return HTMLResponse(html)


# ===== Linked candidates for a specific demand (for client-detail expansion) =====
@router.get("/{client_id}/demands/{demand_id}/candidates")
def list_demand_candidates(
    client_id: int, demand_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Return the real linked candidates for a given (client, demand) pair.

    Used by the client detail page Demand Files tab when a row is expanded.
    Shows each assigned candidate with deep-link back to /candidates?open=...
    """
    # Verify demand belongs to client
    demand = db.query(Demand).filter(
        Demand.id == demand_id, Demand.client_id == client_id
    ).first()
    if not demand:
        raise HTTPException(404, "Demand not found for this client")

    # Import Candidate locally to keep top-level imports tidy
    from app.models import Candidate

    rows = (
        db.query(CandidateAssignment, Candidate, JobCategory)
        .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
        .join(Candidate, Candidate.id == CandidateAssignment.candidate_id)
        .filter(JobCategory.demand_id == demand_id)
        .order_by(CandidateAssignment.assigned_at.desc().nullslast(), CandidateAssignment.id.desc())
        .all()
    )

    items = []
    for (a, c, jc) in rows:
        items.append({
            "assignment_id": a.id,
            "candidate_id": c.id,
            "full_name": c.full_name or "",
            "father_name": c.father_name or "",
            "cnic": c.cnic or "",
            "passport_no": c.passport_no or "",
            "phone": c.phone or "",
            "photo": c.photo or "",
            "profession": c.profession or "",
            "trade": jc.trade or "",
            "trade_id": jc.id,
            "status": a.status or c.status or "new",
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
            # Deep links
            "url": f"/candidates?open={c.id}",
            "edit_url": f"/candidates?edit={c.id}",
        })
    return {
        "demand_id": demand_id,
        "client_id": client_id,
        "file_number": demand.file_number,
        "file_number_display": display_file_number(demand.file_number, _get_prefix(db)),
        "items": items,
        "total": len(items),
    }
