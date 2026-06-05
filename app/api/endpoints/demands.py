"""Demand File endpoints + Trade (JobCategory) management + Assignment workflow.

The Trades tab returns rich candidate cards (photo, name, S/O father, status pill)
ready for the DEMO OEP grid layout. Unassignment performs *full cleanup*:
- delete the CandidateAssignment row,
- clear derived workflow stage on the candidate (back to NEW or DOCS_PENDING),
- clear any cached document_merge state on GeneratedDocument rows tied to this
  assignment context,
- emit an AuditLog row capturing the snapshot.
"""
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.core.deps import get_current_user
from app.core.permissions import require_permission
from app.core.workflow import (
    CandidateStage, DemandStatus,
    normalize_stage, normalize_demand_status,
    stage_label, stage_pill_classes,
    AuditEntity, AuditAction,
)
from app.models import (
    Demand, JobCategory, User, Client, CandidateAssignment, Candidate,
    GeneratedDocument,
)
from app.models.lookups import CompanySettings, ClientStatement
from app.services import audit as audit_svc

router = APIRouter()


def _to_dict(row):
    out = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[col.name] = v
    return out


# ============================================================================
# File-number convention (REAL DATA DRIVEN - verified against legacy MySQL
# backup in /home/user/uploaded_files/ and 2,249 migrated rows in the live DB)
#
#   STORAGE  : `demands.file_number` is the raw sequential integer as a string
#              ("5983", "6022", "8185"). This preserves byte-for-byte
#              compatibility with the legacy MySQL `receipts` table whose
#              file numbers were also raw integers (see cash_book row 24:
#              "...balance file no.5819" and ref_id columns).
#
#   DISPLAY  : Always render as `{file_prefix}{file_number}` where
#              `file_prefix` comes from CompanySettings (default "DTC/786/").
#              Result: "DTC/786/8185", "DTC/786/8186" etc.
#
#   AUTO-GEN : `_next_file_number()` finds the highest integer across the
#              demands table (handling both raw and prefixed values for
#              safety) and returns the next integer as a string.
#
#   USER OVERRIDE: If the create-demand payload supplies a file_number, we
#              strip any leading prefix before storage so storage stays
#              consistent. So a user typing "DTC/786/9000" or just "9000"
#              both end up stored as "9000".
# ============================================================================
import re

_PREFIX_DEFAULT = "DTC/786/"


def _get_prefix(db: Session) -> str:
    settings = db.query(CompanySettings).first()
    if settings and settings.file_prefix:
        p = settings.file_prefix.strip()
        if not p:
            return _PREFIX_DEFAULT
        # Ensure trailing separator
        if p.endswith("/") or p.endswith("-") or p.endswith("_"):
            return p
        return p + "/"
    return _PREFIX_DEFAULT


def _strip_prefix(file_number, prefix=None):
    """Extract the trailing integer portion of a file_number, whatever format."""
    if not file_number:
        return ""
    s = str(file_number).strip()
    if prefix and s.startswith(prefix):
        s = s[len(prefix):].strip()
    m = re.search(r"(\d+)\s*$", s)
    return m.group(1) if m else s


def display_file_number(file_number, prefix=None):
    """Render a file_number for display. Always returns prefixed form.

    Examples (with prefix='DTC/786/'):
        '8185'         -> 'DTC/786/8185'
        'DTC/786/8185' -> 'DTC/786/8185'  (idempotent)
        '6022'         -> 'DTC/786/6022'
        ''             -> ''
    """
    if not file_number:
        return ""
    s = str(file_number).strip()
    p = prefix or _PREFIX_DEFAULT
    if s.startswith(p):
        return s
    # If it's already a formatted non-numeric string, leave alone
    if not s.isdigit() and "/" in s:
        return s
    return f"{p}{_strip_prefix(s, p)}"


def _next_file_number(db: Session) -> str:
    """Return the next file_number AS A RAW INTEGER STRING (not prefixed).

    Scans every existing row, extracts the trailing integer part, finds the
    max, and returns max+1 as a plain string. Falls back to
    settings.starting_point if no rows exist or all rows are malformed.
    """
    settings = db.query(CompanySettings).first()
    start = (settings.starting_point if settings else 0) or 0
    prefix = _get_prefix(db)

    rows = db.query(Demand.file_number).all()
    max_n = start
    for (fn,) in rows:
        tail = _strip_prefix(fn, prefix)
        if not tail:
            continue
        try:
            n = int(tail)
            if n > max_n:
                max_n = n
        except (ValueError, TypeError):
            continue
    return str(max_n + 1)


def _normalize_user_file_number(file_number, prefix=None):
    """Convert a user-supplied file_number to canonical storage form.

    Users may type 'DTC/786/9000' or just '9000' - we always store as '9000'
    so the storage convention stays clean and queries remain consistent.
    """
    if not file_number:
        return ""
    return _strip_prefix(str(file_number).strip(), prefix or _PREFIX_DEFAULT) or str(file_number).strip()


# ============================================================================
# Demand CRUD
# ============================================================================
@router.get("/config/next-file-number")
def get_next_file_number(db: Session = Depends(get_db)):
    prefix = _get_prefix(db)
    nxt = _next_file_number(db)
    return {"prefix": prefix, "next_seq": nxt, "display": display_file_number(nxt, prefix)}

@router.get("/")
def list_demands(db: Session = Depends(get_db), user: User = Depends(get_current_user),
                 q: Optional[str] = None, skip: int = 0, limit: int = 200):
    query = db.query(Demand).options(joinedload(Demand.job_categories), joinedload(Demand.client))
    
    if user.role == "agent":
        query = query.filter(Demand.created_by_id == user.id)
        
    if q:
        query = query.filter(
            (Demand.file_number.ilike(f"%{q}%")) |
            (Demand.permission_no.ilike(f"%{q}%")) |
            (Demand.sponsor_name.ilike(f"%{q}%"))
        )
    total = query.count()
    rows = query.order_by(Demand.id.desc()).offset(skip).limit(limit).all()
    prefix = _get_prefix(db)
    # Pre-compute assigned counts per job_category for the visible page (one query)
    jc_ids = [jc.id for d in rows for jc in d.job_categories]
    assigned_by_jc = {}
    if jc_ids:
        for jc_id, cnt in (
            db.query(CandidateAssignment.job_category_id, func.count(CandidateAssignment.id))
            .filter(CandidateAssignment.job_category_id.in_(jc_ids))
            .group_by(CandidateAssignment.job_category_id)
            .all()
        ):
            assigned_by_jc[jc_id] = cnt
    items = []
    for d in rows:
        total_slots = sum(jc.quantity for jc in d.job_categories)
        assigned_count = sum(assigned_by_jc.get(jc.id, 0) for jc in d.job_categories)
        items.append({
            **_to_dict(d),
            "file_number_display": display_file_number(d.file_number, prefix),
            "client_name": d.client.company_name if d.client else "",
            "trades_count": len(d.job_categories),
            "total_slots": total_slots,
            "assigned_count": assigned_count,
            "status": normalize_demand_status(d.status),
        })
    return {"total": total, "items": items}


@router.post("/")
def create_demand(payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not payload.get("client_id"):
        raise HTTPException(400, "client_id is required")

    allowed = {c.name for c in Demand.__table__.columns} - {"id", "created_at", "updated_at", "file_number"}
    data = {k: v for k, v in payload.items() if k in allowed and v is not None}
    if "status" in data:
        data["status"] = normalize_demand_status(data["status"])

    # Normalize user-supplied file_number (strip any prefix). If blank, auto-generate.
    raw_input = (payload.get("file_number") or "").strip()
    file_number = _normalize_user_file_number(raw_input, _get_prefix(db)) if raw_input else _next_file_number(db)

    # Collision safety: if user typed a duplicate, bump to next available
    while db.query(Demand).filter(Demand.file_number == file_number).first():
        try:
            file_number = str(int(file_number) + 1)
        except ValueError:
            file_number = _next_file_number(db)

    obj = Demand(file_number=file_number, demand_code=file_number, **data)
    db.add(obj); db.commit(); db.refresh(obj)

    audit_svc.log_event(
        db, entity_type=AuditEntity.DEMAND.value, entity_id=obj.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Created Demand File {display_file_number(obj.file_number, _get_prefix(db))} for client #{obj.client_id}",
        after=obj,
    )
    out = _to_dict(obj)
    out["file_number_display"] = display_file_number(obj.file_number, _get_prefix(db))
    return out


@router.get("/{demand_id}")
def get_demand(demand_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Demand).options(joinedload(Demand.job_categories), joinedload(Demand.client)).filter(Demand.id == demand_id).first()
    if not obj:
        raise HTTPException(404, "Demand not found")
    out = _to_dict(obj)
    out["file_number_display"] = display_file_number(obj.file_number, _get_prefix(db))
    out["client_name"] = obj.client.company_name if obj.client else ""
    out["status"] = normalize_demand_status(obj.status)
    out["created_by"] = "Dogar Trading"
    out["job_categories"] = [_to_dict(jc) for jc in obj.job_categories]
    return out


@router.get("/{demand_id}/summary")
def demand_summary(demand_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Demand).options(joinedload(Demand.job_categories)).filter(Demand.id == demand_id).first()
    if not obj:
        raise HTTPException(404, "Demand not found")
    trades = obj.job_categories
    total_slots = sum(jc.quantity for jc in trades)
    assigned = db.query(func.count(CandidateAssignment.id)).filter(
        CandidateAssignment.job_category_id.in_([jc.id for jc in trades] or [0])
    ).scalar() or 0
    return {
        "trades": len(trades),
        "total_slots": total_slots,
        "assigned": assigned,
        "available": max(total_slots - assigned, 0),
        "created_by": "Dogar Trading",
        "created": obj.created_at.date().isoformat() if obj.created_at else None,
        "updated": obj.updated_at.date().isoformat() if obj.updated_at else None,
    }


@router.put("/{demand_id}")
def update_demand(demand_id: int, payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Demand).filter(Demand.id == demand_id).first()
    if not obj:
        raise HTTPException(404, "Demand not found")
    before = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    allowed = {c.name for c in Demand.__table__.columns} - {"id", "created_at", "updated_at"}
    for k, v in payload.items():
        if k in allowed:
            if k == "status":
                v = normalize_demand_status(v)
            setattr(obj, k, v)
    if "file_number" in payload:
        # Normalize user input to canonical raw integer form
        normalized = _normalize_user_file_number(payload["file_number"], _get_prefix(db))
        if normalized:
            obj.file_number = normalized
            obj.demand_code = normalized
    db.commit(); db.refresh(obj)
    audit_svc.log_event(
        db, entity_type=AuditEntity.DEMAND.value, entity_id=obj.id,
        action=AuditAction.UPDATE.value, actor=user, request=request,
        summary=f"Updated Demand File {display_file_number(obj.file_number, _get_prefix(db))}",
        before={k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
        after=obj,
    )
    out = _to_dict(obj)
    out["file_number_display"] = display_file_number(obj.file_number, _get_prefix(db))
    return out


@router.delete("/{demand_id}")
def delete_demand(demand_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("demands:delete"))):
    obj = db.query(Demand).filter(Demand.id == demand_id).first()
    if not obj:
        raise HTTPException(404, "Demand not found")
    audit_svc.log_event(
        db, entity_type=AuditEntity.DEMAND.value, entity_id=obj.id,
        action=AuditAction.DELETE.value, actor=user, request=request,
        summary=f"Deleted Demand File {obj.file_number}",
        before=obj,
    )
    db.delete(obj); db.commit()
    return {"ok": True}


# ============================================================================
# Trades (JobCategory) within a demand
# ============================================================================
@router.get("/{demand_id}/trades")
def list_trades(demand_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Return trades with RICH assigned-candidate cards.

    Each candidate card includes: id, full_name, name_arabic, father_name,
    photo, passport_no, profession, phone, status (canonical), status_label,
    status_classes. This is what the demand_detail Trades tab renders.
    """
    rows = db.query(JobCategory).filter(JobCategory.demand_id == demand_id).all()
    items = []
    for jc in rows:
        assignments = db.query(CandidateAssignment, Candidate).join(
            Candidate, Candidate.id == CandidateAssignment.candidate_id
        ).filter(CandidateAssignment.job_category_id == jc.id).all()
        cand_list = []
        for (a, c) in assignments:
            canonical = normalize_stage(a.status or c.status)
            cand_list.append({
                "assignment_id": a.id,
                "id": c.id,
                "full_name": c.full_name or "",
                "name_arabic": c.name_arabic or "",
                "father_name": c.father_name or "",
                "photo": c.photo or "",
                "passport_no": c.passport_no or "",
                "cnic": c.cnic or "",
                "profession": c.profession or "",
                "phone": c.phone or "",
                "status": canonical,
                "status_label": stage_label(canonical),
                "status_classes": stage_pill_classes(canonical),
                "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
            })
        assigned_cnt = len(cand_list)
        # Show "available" as max(quantity, assigned) - assigned so the bar
        # always makes sense even when legacy migration over-assigned.
        total_slots = max(jc.quantity, assigned_cnt)
        items.append({
            "id": jc.id,
            "trade": jc.trade,
            "quantity": jc.quantity,
            "assigned": assigned_cnt,
            "available": max(total_slots - assigned_cnt, 0),
            "salary": jc.salary,
            "salary_currency": jc.salary_currency,
            "contract_years": jc.contract_years,
            "notes": jc.notes,
            "candidates": cand_list,
        })
    return items


@router.post("/{demand_id}/trades")
def add_trade(demand_id: int, payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    if not demand:
        raise HTTPException(404, "Demand not found")
    if not payload.get("trade"):
        raise HTTPException(400, "trade is required")
    jc = JobCategory(
        demand_id=demand_id,
        trade=payload["trade"],
        quantity=int(payload.get("quantity", 1) or 1),
        salary=float(payload.get("salary", 0) or 0),
        salary_currency=payload.get("salary_currency", "SAR"),
        contract_years=int(payload.get("contract_years", 2) or 2),
        notes=payload.get("notes", ""),
        custom_fields=payload.get("custom_fields") or {},
    )
    db.add(jc); db.commit(); db.refresh(jc)
    audit_svc.log_event(
        db, entity_type=AuditEntity.TRADE.value, entity_id=jc.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Added trade '{jc.trade}' x{jc.quantity} to Demand {demand.file_number}",
        after=jc,
    )
    return _to_dict(jc)


@router.put("/trades/{trade_id}")
def update_trade(trade_id: int, payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    jc = db.query(JobCategory).filter(JobCategory.id == trade_id).first()
    if not jc:
        raise HTTPException(404, "Trade not found")
    before = {c.name: getattr(jc, c.name) for c in jc.__table__.columns}
    for k in ("trade", "quantity", "salary", "salary_currency", "contract_years", "notes", "custom_fields"):
        if k in payload and payload[k] is not None:
            if k in ("quantity", "contract_years"):
                setattr(jc, k, int(payload[k] or 0))
            elif k == "salary":
                setattr(jc, k, float(payload[k] or 0))
            elif k == "custom_fields":
                # Accept dict directly or JSON string
                v = payload[k]
                if isinstance(v, str):
                    import json
                    try:
                        v = json.loads(v)
                    except (ValueError, TypeError):
                        v = {}
                setattr(jc, k, v or {})
            else:
                setattr(jc, k, payload[k])
    db.commit(); db.refresh(jc)
    audit_svc.log_event(
        db, entity_type=AuditEntity.TRADE.value, entity_id=jc.id,
        action=AuditAction.UPDATE.value, actor=user, request=request,
        summary=f"Updated trade '{jc.trade}'",
        before={k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
        after=jc,
    )
    return _to_dict(jc)


@router.delete("/trades/{trade_id}")
def delete_trade(trade_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    jc = db.query(JobCategory).filter(JobCategory.id == trade_id).first()
    if not jc:
        raise HTTPException(404, "Trade not found")

    # Audit each existing assignment as unassigned before cascade-delete kicks in
    for a in list(jc.assignments):
        cand = db.query(Candidate).filter(Candidate.id == a.candidate_id).first()
        if cand:
            audit_svc.log_unassign(
                db, candidate=cand, job_category=jc,
                assignment_snapshot={"assignment_id": a.id, "status": a.status, "trade": jc.trade},
                cleared_fields=["assignment_row (via trade deletion)"],
                actor=user, request=request,
            )

    snapshot = _to_dict(jc)
    db.delete(jc); db.commit()
    audit_svc.log_event(
        db, entity_type=AuditEntity.TRADE.value, entity_id=trade_id,
        action=AuditAction.DELETE.value, actor=user, request=request,
        summary=f"Deleted trade '{snapshot.get('trade')}' from demand #{snapshot.get('demand_id')}",
        before=snapshot,
    )
    return {"ok": True}


# ============================================================================
# Candidate assignment workflow
# ============================================================================

@router.post("/trades/{trade_id}/assign")
def assign_candidate(trade_id: int, payload: dict, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    jc = db.query(JobCategory).filter(JobCategory.id == trade_id).first()
    if not jc:
        raise HTTPException(404, "Trade not found")
    candidate_id = payload.get("candidate_id")
    if not candidate_id:
        raise HTTPException(400, "candidate_id required")
    cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if db.query(CandidateAssignment).filter(
        CandidateAssignment.candidate_id == candidate_id,
        CandidateAssignment.job_category_id == trade_id,
    ).first():
        raise HTTPException(400, "Candidate already assigned to this trade")

    a = CandidateAssignment(
        candidate_id=candidate_id,
        job_category_id=trade_id,
        status=CandidateStage.DOCS_PENDING.value,
    )
    db.add(a)

    # When you assign a fresh candidate (status=new), bump to docs_pending
    if normalize_stage(cand.status) == CandidateStage.NEW.value:
        old = cand.status
        cand.status = CandidateStage.DOCS_PENDING.value
        db.commit()
        audit_svc.log_stage_change(
            db, candidate=cand, old_stage=normalize_stage(old),
            new_stage=cand.status, actor=user, request=request, reason="assigned to trade",
        )
    else:
        db.commit()

    audit_svc.log_assign(db, candidate=cand, job_category=jc, actor=user, request=request)
    return {"ok": True, "assignment_id": a.id, "candidate_id": cand.id, "trade_id": jc.id}


@router.delete("/assignments/{assignment_id}")
def remove_assignment(
    assignment_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Fully unassign a candidate from a trade, clearing every derived ref.

    Cleanup performed:
      1. Capture full assignment snapshot for audit.
      2. Delete the CandidateAssignment row.
      3. If candidate has no remaining assignments and stage is anywhere past
         DOCS_PENDING but before DEPLOYED, reset stage to NEW (back to pool).
      4. Clear derived document-context fields on any GeneratedDocument rows
         that referenced this assignment.
      5. Emit unassign audit log with cleared_fields list.
    """
    a = db.query(CandidateAssignment).filter(CandidateAssignment.id == assignment_id).first()
    if not a:
        raise HTTPException(404, "Assignment not found")

    cand = db.query(Candidate).filter(Candidate.id == a.candidate_id).first()
    jc = db.query(JobCategory).filter(JobCategory.id == a.job_category_id).first()

    # Snapshot BEFORE we mutate anything
    snapshot = {
        "assignment_id": a.id,
        "candidate_id": a.candidate_id,
        "candidate_name": cand.full_name if cand else "",
        "job_category_id": a.job_category_id,
        "trade": jc.trade if jc else "",
        "demand_id": jc.demand_id if jc else None,
        "assignment_status": a.status,
        "candidate_status_before": cand.status if cand else "",
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
    }

    cleared_fields = ["candidate_assignment_row"]

    # 1. Delete the assignment row
    db.delete(a)
    db.flush()

    # 2. Cascade clean derived state on candidate
    if cand:
        remaining = db.query(CandidateAssignment).filter(
            CandidateAssignment.candidate_id == cand.id
        ).count()
        if remaining == 0:
            old_stage = normalize_stage(cand.status)
            # Reset workflow if still in mid-processing stages
            if old_stage not in (CandidateStage.DEPLOYED.value, CandidateStage.CANCELLED.value, CandidateStage.NEW.value):
                cand.status = CandidateStage.NEW.value
                cleared_fields.append("candidate.status -> new")
                audit_svc.log_stage_change(
                    db, candidate=cand, old_stage=old_stage,
                    new_stage=CandidateStage.NEW.value, actor=user, request=request,
                    reason="unassigned from all trades",
                )

    # 3. Clear derived document-context on GeneratedDocument rows for this candidate+demand
    if cand and jc:
        try:
            doc_rows = (
                db.query(GeneratedDocument)
                .filter(GeneratedDocument.candidate_id == cand.id)
                .filter(GeneratedDocument.demand_id == jc.demand_id)
                .all()
            )
            for d in doc_rows:
                # Clear the demand_id link so the merge context isn't stale.
                # Keep the file on disk; just unlink the file context.
                if hasattr(d, "demand_id"):
                    d.demand_id = None
                if hasattr(d, "job_category_id"):
                    d.job_category_id = None
                cleared_fields.append(f"generated_document#{d.id} demand/category link")
        except AttributeError:
            # GeneratedDocument may not have these columns on legacy DBs.
            # This is expected on older deployments; not a real error.
            import logging
            logging.getLogger("dtc.demands").debug(
                "GeneratedDocument lacks demand_id/job_category_id columns; skipping"
            )

    db.commit()

    # 4. Emit unassign audit log
    if cand:
        audit_svc.log_unassign(
            db, candidate=cand, job_category=jc,
            assignment_snapshot=snapshot,
            cleared_fields=cleared_fields,
            actor=user, request=request,
        )

    return {"ok": True, "cleared_fields": cleared_fields, "snapshot": snapshot}


@router.post("/trades/{trade_id}/assign-new")
def assign_new_candidate(
    trade_id: int, payload: dict, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Create a brand-new candidate AND assign to this trade in one transaction.

    This is the workflow opened by the "Assign" button on the Demand File →
    Trades tab (NOT a candidate picker — a full data-entry wizard).

    Expected payload structure (5-step wizard):
        {
          "personal_info":  {full_name, name_arabic, mother_name, father_name,
                             father_name_arabic, gender, marital_status, religion,
                             date_of_birth, place_of_birth, place_of_birth_arabic,
                             nationality, address, phone, tehsil, district, province,
                             photo (filename)},
          "identification": {passport_no, passport_issue_date, passport_expiry_date,
                             issuing_authority, issuing_authority_arabic,
                             passport_issue_place, cnic, nadra_token_no},
          "employment":     {permission_no, permission_date, qualification},
          "next_of_kin":    {next_of_kin_name, next_of_kin_nic, next_of_kin_relation},
          "charge_summary": {salary, price, ticket_included, accommodation_allowance,
                             food_allowance, slot_notes}
        }
    """
    jc = db.query(JobCategory).filter(JobCategory.id == trade_id).first()
    if not jc:
        raise HTTPException(404, "Trade not found")

    # Check availability
    assigned = db.query(CandidateAssignment).filter(
        CandidateAssignment.job_category_id == trade_id
    ).count()
    if assigned >= (jc.quantity or 0):
        raise HTTPException(400, f"Trade '{jc.trade}' is fully assigned ({assigned}/{jc.quantity})")

    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")

    p = payload.get("personal_info") or {}
    if not (p.get("full_name") or "").strip():
        raise HTTPException(400, "full_name is required")

    i = payload.get("identification") or {}
    e = payload.get("employment") or {}
    k = payload.get("next_of_kin") or {}
    cs = payload.get("charge_summary") or {}

    def _date(s):
        try:
            from datetime import datetime
            if not s: return None
            return datetime.fromisoformat(str(s).replace("Z", "")).date()
        except (ValueError, TypeError):
            try:
                from datetime import datetime
                return datetime.strptime(str(s), "%d/%m/%Y").date()
            except (ValueError, TypeError):
                return None

    # 1. Create the candidate
    cand = Candidate(
        # Personal
        full_name=p.get("full_name", "").strip(),
        name_arabic=p.get("name_arabic", "") or "",
        mother_name=p.get("mother_name", "") or "",
        father_name=p.get("father_name", "") or "",
        father_name_arabic=p.get("father_name_arabic", "") or "",
        gender=p.get("gender", "Male") or "Male",
        marital_status=p.get("marital_status", "Single") or "Single",
        religion=p.get("religion", "Islam") or "Islam",
        date_of_birth=_date(p.get("date_of_birth")),
        place_of_birth=p.get("place_of_birth", "") or "",
        place_of_birth_arabic=p.get("place_of_birth_arabic", "") or "",
        nationality=p.get("nationality", "PAKISTANI") or "PAKISTANI",
        address=p.get("address", "") or "",
        phone=p.get("phone", "") or "",
        tehsil=p.get("tehsil", "") or "",
        district=p.get("district", "") or "",
        province=p.get("province", "") or "",
        photo=p.get("photo", "") or "",
        # Identification
        passport_no=i.get("passport_no", "") or "",
        passport_issue_date=_date(i.get("passport_issue_date")),
        passport_expiry_date=_date(i.get("passport_expiry_date")),
        issuing_authority=i.get("issuing_authority", "PAKISTAN") or "PAKISTAN",
        issuing_authority_arabic=i.get("issuing_authority_arabic", "") or "",
        passport_issue_place=i.get("passport_issue_place", "") or "",
        cnic=i.get("cnic", "") or "",
        nadra_token_no=i.get("nadra_token_no", "") or "",
        # Employment
        permission_no=e.get("permission_no", "") or "",
        permission_date=_date(e.get("permission_date")),
        qualification=e.get("qualification", "") or "",
        profession=jc.trade,
        # Charge summary
        salary=float(cs.get("salary") or jc.salary or 0),
        price=float(cs.get("price") or 0),
        ticket_included=cs.get("ticket_included", "No") or "No",
        accommodation_allowance=str(cs.get("accommodation_allowance", "") or ""),
        food_allowance=str(cs.get("food_allowance", "") or ""),
        slot_notes=cs.get("slot_notes", "") or "",
        # Next of Kin
        next_of_kin_name=k.get("next_of_kin_name", "") or "",
        next_of_kin_nic=k.get("next_of_kin_nic", "") or "",
        next_of_kin_relation=k.get("next_of_kin_relation", "") or "",
        # Stage
        status=CandidateStage.DOCS_PENDING.value,
    )
    db.add(cand)
    db.flush()

    # 2. Create the assignment
    a = CandidateAssignment(
        candidate_id=cand.id,
        job_category_id=trade_id,
        status=CandidateStage.DOCS_PENDING.value,
    )
    db.add(a)
    db.commit()
    db.refresh(cand)
    db.refresh(a)

    # 3. Audit
    audit_svc.log_event(
        db, entity_type=AuditEntity.CANDIDATE.value, entity_id=cand.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Created candidate '{cand.full_name}' via Assign-to-Trade wizard ({jc.trade})",
        after=cand,
    )
    audit_svc.log_assign(db, candidate=cand, job_category=jc, actor=user, request=request)

    return {
        "ok": True,
        "candidate_id": cand.id,
        "assignment_id": a.id,
        "trade_id": jc.id,
        "trade": jc.trade,
    }


@router.put("/assignments/{assignment_id}/status")
def update_assignment_status(
    assignment_id: int, payload: dict, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Move an assignment to a new canonical stage."""
    a = db.query(CandidateAssignment).filter(CandidateAssignment.id == assignment_id).first()
    if not a:
        raise HTTPException(404, "Assignment not found")
    new_stage = normalize_stage(payload.get("status"))
    old_stage = normalize_stage(a.status)
    a.status = new_stage
    # Mirror on the candidate row so list views agree
    cand = db.query(Candidate).filter(Candidate.id == a.candidate_id).first()
    if cand:
        cand.status = new_stage
    db.commit()
    if cand:
        audit_svc.log_stage_change(
            db, candidate=cand, old_stage=old_stage, new_stage=new_stage,
            actor=user, request=request, reason="assignment status change",
        )
    return {"ok": True, "status": new_stage}


# Legacy alias kept for backward compat
@router.post("/{demand_id}/categories")
def add_job_category_legacy(demand_id: int, payload: dict, request: Request,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return add_trade(demand_id, payload, request, db, user)


# ============================================================================
# Payments (against this Demand File) — also write to ClientStatement so
# the client's running balance stays in sync.
# ============================================================================
def _next_receipt_no(db: Session) -> str:
    """Auto-incrementing receipt number: RCP-YYYY-NNNN (per-year reset)."""
    year = datetime.now().year
    prefix = f"RCP-{year}-"
    rows = db.query(ClientStatement.receipt_no).filter(
        ClientStatement.receipt_no.like(f"{prefix}%")
    ).all()
    max_n = 0
    for (rn,) in rows:
        try:
            n = int((rn or "").replace(prefix, "").strip())
            if n > max_n:
                max_n = n
        except (ValueError, AttributeError):
            continue
    return f"{prefix}{max_n + 1:04d}"


@router.get("/{demand_id}/payments")
def list_demand_payments(
    demand_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Return every PAYMENT against this Demand File with a running balance."""
    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    if not demand:
        raise HTTPException(404, "Demand not found")

    rows = (
        db.query(ClientStatement)
        .filter(ClientStatement.demand_id == demand_id)
        .order_by(ClientStatement.entry_date.asc(), ClientStatement.id.asc())
        .all()
    )

    items = []
    total_debit = 0.0
    total_credit = 0.0
    for r in rows:
        d = float(r.debit or 0)
        c = float(r.credit or 0)
        total_debit += d
        total_credit += c
        items.append({
            "id": r.id,
            "entry_type": r.entry_type,
            "reference": r.reference,
            "description": r.description,
            "debit": d,
            "credit": c,
            "payment_method": r.payment_method or "",
            "receipt_no": r.receipt_no or "",
            "received_by": r.received_by or "",
            "entry_date": r.entry_date.isoformat() if r.entry_date else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return {
        "demand_id": demand_id,
        "file_number": demand.file_number,
        "file_number_display": display_file_number(demand.file_number, _get_prefix(db)),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balance": total_debit - total_credit,
        "items": items,
    }


@router.post("/{demand_id}/payments")
def create_demand_payment(
    demand_id: int, payload: dict, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Record a payment received against this Demand File.

    Body: {
      entry_type: "PAYMENT" | "INVOICE",
      amount: 50000,
      payment_method: "Cash" | "Bank Transfer" | "Cheque" | "Online",
      reference: optional invoice/cheque no,
      received_by: name of person who received,
      description: free text,
      entry_date: ISO date (default today)
    }
    """
    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    if not demand:
        raise HTTPException(404, "Demand not found")

    entry_type = (payload.get("entry_type") or "PAYMENT").upper()
    if entry_type not in ("PAYMENT", "INVOICE"):
        raise HTTPException(400, "entry_type must be PAYMENT or INVOICE")

    amount = float(payload.get("amount") or 0)
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")

    # PAYMENT = client paid us => credit. INVOICE = we charged client => debit.
    debit = amount if entry_type == "INVOICE" else 0.0
    credit = amount if entry_type == "PAYMENT" else 0.0

    receipt_no = (payload.get("receipt_no") or "").strip() or _next_receipt_no(db)
    entry_date_str = payload.get("entry_date")
    try:
        entry_date_val = datetime.fromisoformat(entry_date_str).date() if entry_date_str else date.today()
    except (ValueError, TypeError):
        entry_date_val = date.today()

    row = ClientStatement(
        client_id=demand.client_id,
        demand_id=demand_id,
        entry_type=entry_type,
        reference=payload.get("reference", "") or "",
        description=payload.get("description", "") or f"{entry_type} against Demand {demand.file_number}",
        debit=debit,
        credit=credit,
        payment_method=payload.get("payment_method", "") or "",
        receipt_no=receipt_no,
        received_by=payload.get("received_by", "") or (user.full_name if hasattr(user, "full_name") else (user.email or "")),
        entry_date=entry_date_val,
    )
    db.add(row); db.commit(); db.refresh(row)

    audit_svc.log_event(
        db, entity_type=AuditEntity.DEMAND.value, entity_id=demand_id,
        action=AuditAction.UPDATE.value, actor=user, request=request,
        summary=f"Recorded {entry_type} {receipt_no} of {amount:,.2f} against Demand {demand.file_number}",
        after={"receipt_no": receipt_no, "amount": amount, "entry_type": entry_type,
               "payment_method": row.payment_method},
    )
    return {
        "ok": True, "id": row.id, "receipt_no": receipt_no,
        "amount": amount, "entry_type": entry_type,
    }


@router.delete("/{demand_id}/payments/{pid}")
def delete_demand_payment(
    demand_id: int, pid: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    row = db.query(ClientStatement).filter(
        ClientStatement.id == pid, ClientStatement.demand_id == demand_id
    ).first()
    if not row:
        raise HTTPException(404, "Payment not found")
    receipt_no = row.receipt_no
    db.delete(row); db.commit()
    audit_svc.log_event(
        db, entity_type=AuditEntity.DEMAND.value, entity_id=demand_id,
        action=AuditAction.DELETE.value, actor=user, request=request,
        summary=f"Deleted payment {receipt_no} on Demand #{demand_id}",
    )
    return {"ok": True}


# ----------------------------------------------------------------------
# Helper: build the dicts the receipts_renderer module expects
# ----------------------------------------------------------------------
def _company_dict(company, request: Optional[Request] = None) -> dict:
    if not company:
        return {
            "name": "Dogar Trading Corporation",
            "tagline": "Overseas Employment Promoters",
            "address": "",
            "phone": "",
            "email": "",
            "license_no": "",
        }
    return {
        "name": getattr(company, "company_name", None) or "Dogar Trading Corporation",
        "tagline": "Overseas Employment Promoters · Licensed Recruiter",
        "address": getattr(company, "address", "") or "",
        "phone": getattr(company, "phone", "") or "",
        "email": getattr(company, "email", "") or "",
        "license_no": getattr(company, "oep_license_number", "") or "",
        "authorised_signatory": getattr(company, "signing_authority_name", None)
                                or "Ghazanfar Manzoor Dogar",
    }


@router.get("/{demand_id}/payments/{pid}/receipt", response_class=HTMLResponse)
def print_payment_receipt(
    demand_id: int, pid: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Official PAYMENT / INVOICE receipt — logo + watermark + QR code.

    Rendered server-side as a single self-contained HTML document; the
    browser auto-prints on load.
    """
    from app.services.receipts_renderer import render_payment_receipt

    row = db.query(ClientStatement).filter(
        ClientStatement.id == pid, ClientStatement.demand_id == demand_id
    ).first()
    if not row:
        raise HTTPException(404, "Payment not found")
    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    client = db.query(Client).filter(Client.id == row.client_id).first() if row.client_id else None
    company = db.query(CompanySettings).first()

    payment_dict = {
        "id": row.id,
        "receipt_no": row.receipt_no,
        "entry_date": row.entry_date,
        "entry_type": row.entry_type,
        "debit": float(row.debit or 0),
        "credit": float(row.credit or 0),
        "payment_method": row.payment_method,
        "reference": row.reference,
        "description": row.description,
        "received_by": row.received_by,
        "currency": "PKR",
    }
    demand_dict = None
    if demand:
        demand_dict = {
            "id": demand.id,
            "file_number": display_file_number(demand.file_number, _get_prefix(db)),
        }
    client_dict = {"company_name": client.company_name} if client else None

    # Build absolute verify URL using the current host
    base = str(request.base_url).rstrip("/")
    verify_url = f"{base}/demands/{demand_id}/payments/{pid}/receipt"

    html = render_payment_receipt(
        payment_dict,
        demand=demand_dict,
        client=client_dict,
        company=_company_dict(company),
        verify_url=verify_url,
        auto_print=request.query_params.get("auto", "1") != "0",
    )
    return HTMLResponse(html)


# ============================================================================
# Demand File Receipt — official document with logo + watermark + QR
# ============================================================================
@router.get("/{demand_id}/file-receipt", response_class=HTMLResponse)
def print_demand_file_receipt(
    demand_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Print-ready Demand File receipt — issued when a new demand file is
    opened.  Carries the company logo, an angled DOGAR TRADING watermark,
    sponsor + visa + recruitment-categories block, and a verification QR.
    """
    from app.services.receipts_renderer import render_demand_file_receipt

    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    if not demand:
        raise HTTPException(404, "Demand not found")
    client = db.query(Client).filter(Client.id == demand.client_id).first() if demand.client_id else None
    company = db.query(CompanySettings).first()

    trades_rows = db.query(JobCategory).filter(
        JobCategory.demand_id == demand_id
    ).all()
    trades_list = [{
        "trade": t.trade,
        "quantity": t.quantity,
        "salary": float(t.salary or 0),
        "salary_currency": t.salary_currency or "SAR",
        "contract_years": t.contract_years or 2,
    } for t in trades_rows]

    demand_dict = {
        "id": demand.id,
        "file_number": display_file_number(demand.file_number, _get_prefix(db)),
        "receiving_date": demand.receiving_date,
        "created_at": demand.created_at,
        "reference": demand.reference,
        "permission_no": demand.permission_no,
        "permission_date": demand.permission_date,
        "sponsor_name": demand.sponsor_name,
        "sponsor_address": demand.sponsor_address,
        "country": demand.country,
        "embassy": demand.embassy,
        "visa_number": demand.visa_number,
        "status": demand.status,
    }
    client_dict = {"company_name": client.company_name} if client else None

    base = str(request.base_url).rstrip("/")
    verify_url = f"{base}/demands/{demand_id}"

    html = render_demand_file_receipt(
        demand_dict,
        client=client_dict,
        company=_company_dict(company),
        trades=trades_list,
        verify_url=verify_url,
        auto_print=request.query_params.get("auto", "1") != "0",
    )
    return HTMLResponse(html)


# ============================================================================
# Visa Receipt
# ============================================================================
@router.get("/{demand_id}/visa-receipt")
def print_visa_receipt(
    demand_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Print-ready visa-process receipt for ALL candidates in a demand."""
    demand = db.query(Demand).filter(Demand.id == demand_id).first()
    if not demand:
        raise HTTPException(404, "Demand not found")
    client = db.query(Client).filter(Client.id == demand.client_id).first()
    company = db.query(CompanySettings).first()
    company_name = (company.company_name if company else "Dogar Trading Corporation") or "Dogar Trading Corporation"
    company_addr = (company.address if company else "") or ""
    company_phone = (company.phone if company else "") or ""
    company_email = (company.email if company else "") or ""
    license_no = (company.oep_license_number if company else "") or ""
    rows = db.query(
        Candidate.full_name, Candidate.passport_no, Candidate.cnic,
        Candidate.visa_stamp_date,
        JobCategory.trade
    ).join(
        CandidateAssignment, CandidateAssignment.candidate_id == Candidate.id
    ).join(
        JobCategory, JobCategory.id == CandidateAssignment.job_category_id
    ).filter(JobCategory.demand_id == demand_id).all()
    cand_rows = ""
    for idx, r in enumerate(rows, 1):
        cand_rows += f"""<tr>
            <td>{idx}</td><td>{r[0] or '—'}</td>
            <td>{r[1] or '—'}</td><td>{r[2] or '—'}</td>
            <td>{r[4] or '—'}</td>
            <td>{r[3].strftime('%d %b %Y') if r[3] else '—'}</td>
        </tr>"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Visa Receipt {demand.file_number or ''}</title>
<style>
  @media print {{ .no-print {{ display:none }} body {{ margin:0 }} @page {{ size: A4; margin: 14mm }} }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color:#1f2937; max-width: 900px; margin: 18px auto; padding:0 18px; }}
  .header {{ border-bottom: 3px double #1e40af; padding-bottom: 12px; margin-bottom: 18px; text-align:center }}
  .header h1 {{ margin: 4px 0; color:#1e40af; font-size: 22px; letter-spacing:.5px }}
  .header .meta {{ font-size: 12px; color:#475569 }}
  .title-row {{ display:flex; align-items:center; justify-content:space-between; margin: 14px 0 18px }}
  .title-row h2 {{ margin:0; font-size:18px; color:#0f172a }}
  table.kv {{ width:100%; border-collapse:collapse; margin: 10px 0 18px; font-size:13.5px }}
  table.kv td {{ padding: 7px 10px; border-bottom: 1px solid #e2e8f0; vertical-align:top }}
  table.kv td:first-child {{ width: 30%; color:#64748b; font-weight:600 }}
  table.cand {{ width:100%; border-collapse:collapse; font-size:12px; margin: 10px 0 }}
  table.cand th {{ background:#f1f5f9; color:#475569; font-weight:700; padding:6px 8px; border:1px solid #cbd5e1; text-align:left }}
  table.cand td {{ padding:5px 8px; border:1px solid #e2e8f0 }}
  .footer {{ margin-top: 28px; padding-top: 8px; border-top: 1px solid #cbd5e1; font-size:11px; color:#64748b; text-align:center }}
  .printbtn {{ background:#1e40af; color:white; border:0; padding:9px 18px; border-radius:6px; cursor:pointer; font-weight:600; font-size:13px }}
</style></head>
<body>
  <div class="no-print" style="text-align:right;margin-bottom:8px">
    <button class="printbtn" onclick="window.print()">🖨 Print Visa Receipt</button>
    <button class="printbtn" style="background:#64748b;margin-left:6px" onclick="window.close()">✕ Close</button>
  </div>
  <div class="header">
    <h1>{company_name}</h1>
    <div class="meta">{company_addr}</div>
    <div class="meta">{('Tel: ' + company_phone) if company_phone else ''} {('· ' + company_email) if company_email else ''}</div>
    <div class="meta">{('OEP License No: ' + license_no) if license_no else ''}</div>
  </div>
  <div class="title-row"><h2>VISA PROCESS RECEIPT</h2></div>
  <table class="kv">
    <tr><td>Demand File No.</td><td><strong>{display_file_number(demand.file_number, _get_prefix(db)) if demand else '—'}</strong></td></tr>
    <tr><td>Client / Company</td><td>{(client.company_name if client else '—')}</td></tr>
    <tr><td>Country</td><td>{demand.country or '—'}</td></tr>
    <tr><td>Embassy</td><td>{demand.embassy or '—'}</td></tr>
    <tr><td>Total Candidates</td><td><strong>{len(rows)}</strong></td></tr>
  </table>
  <table class="cand">
    <tr><th>#</th><th>Name</th><th>Passport</th><th>CNIC</th><th>Trade</th><th>Visa Stamp Date</th></tr>
    {cand_rows or '<tr><td colspan="6" style="text-align:center;color:#94a3b8">No candidates assigned</td></tr>'}
  </table>
  <div class="footer">This is a computer-generated visa receipt. {company_name} · OEP Licensed Recruiter</div>
</body></html>
"""
    return HTMLResponse(html)


def _amount_in_words(amount: float) -> str:
    """Convert a number to English words (Pakistani lakh/crore style)."""
    n = int(round(amount))
    if n == 0:
        return "Rupees Zero Only"
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
            "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
            "Seventeen", "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
    def two(num):
        if num < 20: return ones[num]
        return tens[num // 10] + (" " + ones[num % 10] if num % 10 else "")
    def three(num):
        if num >= 100:
            return ones[num // 100] + " Hundred" + ((" " + two(num % 100)) if num % 100 else "")
        return two(num)
    parts = []
    crore = n // 10000000; n %= 10000000
    lakh  = n // 100000;   n %= 100000
    thou  = n // 1000;     n %= 1000
    rest  = n
    if crore: parts.append(three(crore) + " Crore")
    if lakh:  parts.append(three(lakh)  + " Lakh")
    if thou:  parts.append(three(thou)  + " Thousand")
    if rest:  parts.append(three(rest))
    paisa = int(round((amount - int(amount)) * 100))
    out = "Rupees " + " ".join(parts)
    if paisa:
        out += " and " + two(paisa) + " Paisa"
    out += " Only"
    return out
