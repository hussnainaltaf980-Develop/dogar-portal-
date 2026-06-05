"""Candidate endpoints + 5-step Wizard State persistence.

The wizard supports resume/edit/reopen for partially-completed candidate
records. Every step PATCH is persisted as a JSON blob keyed by step number
in CandidateWizardState.step_data, and the user can close the drawer mid-way
and pick up exactly where they left off.
"""
import html as html_lib
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.deps import get_current_user
from app.core.permissions import require_permission
from app.core.config import settings as app_settings
from app.core.workflow import (
    CandidateStage, normalize_stage, WIZARD_STEPS, TOTAL_WIZARD_STEPS,
    AuditEntity, AuditAction, stage_label, stage_pill_classes, all_stages,
)
from app.models import Candidate, User, CandidateWizardState, CandidateAssignment, JobCategory, Demand, Client
from app.services import audit as audit_svc
from app.services.letterhead_renderer import LETTERHEAD_URL
from app.api.endpoints.demands import display_file_number, _get_prefix

router = APIRouter()


DATE_FIELDS = {
    "date_of_birth", "passport_issue_date", "passport_expiry_date",
    "permission_date", "protector_date", "medical_date",
    "medical_send_date", "date_of_departure", "visa_stamp_date",
}


def _to_dict(c: Candidate, *, db: Optional[Session] = None, with_context: bool = False):
    """Serialize a candidate to a dict.

    If ``with_context=True`` and ``db`` is provided, also enrich with the
    candidate's *active* assignment context (most recent): client_name,
    demand_file_no, demand_id, embassy, country, sponsor_name. This is what
    the candidate list / detail panel needs to render the file & embassy
    columns in the DEMO OEP layout.
    """
    out = {}
    for col in c.__table__.columns:
        v = getattr(c, col.name)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif v is not None and hasattr(v, "__float__") and col.name in ("salary", "price"):
            v = float(v)
        out[col.name] = v
    # Enrich with canonical workflow display info
    out["status"] = normalize_stage(out.get("status"))
    out["status_label"] = stage_label(out["status"])
    out["status_classes"] = stage_pill_classes(out["status"])

    # Initialise context fields so the JSON shape is always stable
    out.setdefault("client_name", "")
    out.setdefault("demand_file_no", "")
    out.setdefault("demand_id", None)
    out.setdefault("embassy", "")
    out.setdefault("country", "")
    out.setdefault("sponsor_name", "")
    out.setdefault("trade", out.get("profession", "") or "")

    if with_context and db is not None:
        # Most recent assignment → job_category → demand → client
        row = (
            db.query(CandidateAssignment, JobCategory, Demand, Client)
            .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
            .join(Demand, Demand.id == JobCategory.demand_id)
            .outerjoin(Client, Client.id == Demand.client_id)
            .filter(CandidateAssignment.candidate_id == c.id)
            .order_by(CandidateAssignment.assigned_at.desc(), CandidateAssignment.id.desc())
            .first()
        )
        if row:
            (_a, jc, dm, cli) = row
            out["client_name"] = (cli.company_name if cli else "") or ""
            out["demand_file_no"] = dm.file_number or ""
            out["demand_file_no_display"] = display_file_number(dm.file_number, _get_prefix(db))
            out["demand_id"] = dm.id
            out["embassy"] = dm.embassy or ""
            out["country"] = dm.country or ""
            out["sponsor_name"] = dm.sponsor_name or ""
            out["trade"] = jc.trade or out.get("profession", "")

    return out


def _coerce(payload: dict) -> dict:
    """Convert empty strings to None for date fields and numeric fields."""
    allowed = {c.name for c in Candidate.__table__.columns} - {"id", "created_at", "updated_at"}
    data = {}
    for k, v in payload.items():
        if k not in allowed:
            continue
        if k in DATE_FIELDS:
            if v in (None, "", "—"):
                data[k] = None
                continue
            try:
                if isinstance(v, str):
                    try:
                        data[k] = datetime.fromisoformat(v).date()
                    except ValueError:
                        data[k] = datetime.strptime(v, "%d/%m/%Y").date()
                else:
                    data[k] = v
            except (ValueError, TypeError):
                # Unparseable date — store NULL instead of failing the request.
                data[k] = None
        elif k == "age_employee":
            try:
                data[k] = int(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                data[k] = None
        elif k in ("salary", "price"):
            try:
                data[k] = float(v) if v not in (None, "") else 0
            except (ValueError, TypeError):
                data[k] = 0
        elif k == "status":
            data[k] = normalize_stage(v)
        else:
            data[k] = v if v is not None else ""
    return data


# ============================================================================
# Standard CRUD
# ============================================================================
@router.get("/stages")
def list_stages(user: User = Depends(get_current_user)):
    """Canonical list of workflow stages for dropdowns / filters."""
    return {"stages": all_stages(), "total_wizard_steps": TOTAL_WIZARD_STEPS, "steps": WIZARD_STEPS}


# ============================================================================
# Photo upload (used by wizard Step 1)
# ============================================================================
@router.post("/upload-photo")
async def upload_candidate_photo(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Save a candidate photo and return the stored filename.

    Files live under ``app/static/uploads/photos/`` and are served at
    ``/static/uploads/photos/<filename>``.
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are accepted (JPG, PNG, WEBP)")
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 8 MB)")
    if len(content) < 256:
        raise HTTPException(400, "Image too small or empty")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    fname = f"cand_{uuid.uuid4().hex[:12]}{ext}"

    photos_dir = os.path.join(app_settings.UPLOAD_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    fpath = os.path.join(photos_dir, fname)
    with open(fpath, "wb") as f:
        f.write(content)

    return {
        "ok": True,
        "filename": fname,
        "url": f"/static/uploads/photos/{fname}",
        "size": len(content),
    }


@router.get("/")
def list_candidates(
    q: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    client_id: Optional[int] = Query(None),
    demand_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None,
        description="Filter by trade/category (matches JobCategory.trade ilike)"),
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Returns a paginated list of candidates with rich context (client_name,
    demand_file_no, trade, embassy) injected per row via a single batched
    JOIN — no N+1.

    Filters (all optional, combinable):
      - `q`            : free-text on name / passport / CNIC / father
      - `status`       : workflow stage (`new`, `interviewed`, `medical`, ...)
      - `client_id`    : restrict to candidates linked to demands of this client
      - `demand_id`    : restrict to candidates assigned to this demand file
      - `category`     : trade/profession substring (e.g. "Welder")
    """
    query = db.query(Candidate)

    if user.role == "agent":
        query = query.filter(Candidate.created_by_id == user.id)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Candidate.full_name.ilike(like),
            Candidate.passport_no.ilike(like),
            Candidate.cnic.ilike(like),
            Candidate.father_name.ilike(like),
        ))
    if status_filter:
        query = query.filter(Candidate.status == normalize_stage(status_filter))

    # Assignment-context filters require a join via CandidateAssignment → JobCategory → Demand.
    if client_id or demand_id or category:
        sub = (
            db.query(CandidateAssignment.candidate_id)
              .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
              .join(Demand, Demand.id == JobCategory.demand_id)
        )
        if client_id:
            sub = sub.filter(Demand.client_id == client_id)
        if demand_id:
            sub = sub.filter(Demand.id == demand_id)
        if category:
            sub = sub.filter(JobCategory.trade.ilike(f"%{category}%"))
        query = query.filter(Candidate.id.in_(sub.subquery().select()))

    total = query.count()
    # Sort candidates putting those mapped to newest demand files first
    rows = query.outerjoin(
        CandidateAssignment, CandidateAssignment.candidate_id == Candidate.id
    ).outerjoin(
        JobCategory, JobCategory.id == CandidateAssignment.job_category_id
    ).outerjoin(
        Demand, Demand.id == JobCategory.demand_id
    ).order_by(
        Demand.id.desc().nullslast(),
        Candidate.id.desc(),
    ).offset(skip).limit(limit).all()

    # ---- Batch-fetch context in 1 query to avoid N+1 ----
    # Get the most recent assignment per candidate via window-function-equivalent in Python.
    cand_ids = [c.id for c in rows]
    ctx_map: dict = {}
    if cand_ids:
        ctx_rows = (
            db.query(
                CandidateAssignment.candidate_id,
                JobCategory.trade,
                Demand.id.label("demand_id"),
                Demand.file_number,
                Demand.embassy,
                Demand.country,
                Demand.sponsor_name,
                Client.company_name,
                CandidateAssignment.assigned_at,
                CandidateAssignment.id.label("assignment_id"),
            )
            .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
            .join(Demand, Demand.id == JobCategory.demand_id)
            .outerjoin(Client, Client.id == Demand.client_id)
            .filter(CandidateAssignment.candidate_id.in_(cand_ids))
            .order_by(CandidateAssignment.candidate_id.asc(),
                      CandidateAssignment.assigned_at.desc().nullslast(),
                      CandidateAssignment.id.desc())
            .all()
        )
        # First row per candidate_id wins (newest assignment first due to order_by)
        prefix = _get_prefix(db)
        for r in ctx_rows:
            cid = r.candidate_id
            if cid not in ctx_map:
                ctx_map[cid] = {
                    "client_name": r.company_name or "",
                    "demand_file_no": r.file_number or "",
                    "demand_file_no_display": display_file_number(r.file_number, prefix),
                    "demand_id": r.demand_id,
                    "embassy": r.embassy or "",
                    "country": r.country or "",
                    "sponsor_name": r.sponsor_name or "",
                    "trade": r.trade or "",
                }

    items = []
    for c in rows:
        d = _to_dict(c)  # cheap — no DB calls
        ctx = ctx_map.get(c.id)
        if ctx:
            d.update(ctx)
            if not d.get("trade"):
                d["trade"] = d.get("profession", "") or ""
        items.append(d)

    return {"total": total, "items": items}


@router.post("/")
def create_candidate(payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not payload.get("full_name"):
        raise HTTPException(400, "Full name is required")
    data = _coerce(payload)
    if not data.get("status"):
        data["status"] = CandidateStage.NEW.value
    obj = Candidate(**data)
    db.add(obj); db.commit(); db.refresh(obj)
    audit_svc.log_event(
        db, entity_type=AuditEntity.CANDIDATE.value, entity_id=obj.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Created candidate '{obj.full_name}' (CNIC {obj.cnic or '—'})",
        after=obj,
    )
    return _to_dict(obj)


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not obj:
        raise HTTPException(404, "Candidate not found")
    out = _to_dict(obj, db=db, with_context=True)

    # Include active assignment(s) so the drawer can show the file linkage.
    assigns = (
        db.query(CandidateAssignment, JobCategory)
        .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
        .filter(CandidateAssignment.candidate_id == candidate_id).all()
    )
    out["assignments"] = [
        {
            "assignment_id": a.id,
            "job_category_id": jc.id,
            "trade": jc.trade,
            "demand_id": jc.demand_id,
            "status": normalize_stage(a.status),
            "status_label": stage_label(a.status),
            "status_classes": stage_pill_classes(a.status),
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        }
        for (a, jc) in assigns
    ]
    return out


@router.put("/{candidate_id}")
def update_candidate(candidate_id: int, payload: dict, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not obj:
        raise HTTPException(404, "Candidate not found")
    # Snapshot ONLY the columns the payload is changing, so the audit
    # diff is small + always JSON-safe (raw Decimal salaries previously
    # crashed the audit insert with a 500 on /api/candidates/{id} PUT).
    before = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    before_status = obj.status

    data = _coerce(payload)
    for k, v in data.items():
        setattr(obj, k, v)
    db.commit(); db.refresh(obj)

    # If status changed -> emit stage_change audit
    if "status" in data and data["status"] != normalize_stage(before_status):
        audit_svc.log_stage_change(
            db, candidate=obj, old_stage=normalize_stage(before_status),
            new_stage=data["status"], actor=user, request=request, reason="manual edit",
        )
    # The audit service is responsible for JSON-safety + must-not-throw
    # semantics (see app/services/audit.py::_json_safe). We just pass the
    # raw dict — no manual isoformat() coercion is needed.
    audit_svc.log_event(
        db, entity_type=AuditEntity.CANDIDATE.value, entity_id=obj.id,
        action=AuditAction.UPDATE.value, actor=user, request=request,
        summary=f"Updated candidate '{obj.full_name}'",
        before=before,
        after=obj,
    )
    return _to_dict(obj)


@router.delete("/{candidate_id}")
def delete_candidate(candidate_id: int, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_permission("candidates:delete"))):
    obj = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not obj:
        raise HTTPException(404, "Candidate not found")
    audit_svc.log_event(
        db, entity_type=AuditEntity.CANDIDATE.value, entity_id=obj.id,
        action=AuditAction.DELETE.value, actor=user, request=request,
        summary=f"Deleted candidate '{obj.full_name}'",
        before=obj,
    )
    db.delete(obj); db.commit()
    return {"ok": True}


# ============================================================================
# 5-Step Wizard State persistence
# ============================================================================
def _wizard_to_dict(w: CandidateWizardState) -> dict:
    return {
        "id": w.id,
        "candidate_id": w.candidate_id,
        "job_category_id": w.job_category_id,
        "current_step": w.current_step or 1,
        "total_steps": w.total_steps or TOTAL_WIZARD_STEPS,
        "is_finalized": bool(w.is_finalized),
        "step_data": w.step_data or {},
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "updated_at": w.updated_at.isoformat() if w.updated_at else None,
        "finalized_at": w.finalized_at.isoformat() if w.finalized_at else None,
        "steps_meta": WIZARD_STEPS,
    }


@router.post("/wizard")
def create_wizard_draft(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new wizard draft. Optionally pre-link to a job_category.

    Body: { "job_category_id": <optional int>, "step_data": <optional dict> }
    """
    job_category_id = payload.get("job_category_id")
    # ENFORCE: candidates can only be created in the context of a demand file
    # (i.e. by assigning to a specific Trade / job_category). Direct creation
    # from the Candidates page is intentionally disabled so every candidate is
    # always linked to a Client / Demand File / Embassy.
    if not job_category_id:
        raise HTTPException(
            400,
            "Direct candidate creation is disabled. Please open a Demand File, "
            "choose a Trade, and click 'Assign Candidate'.",
        )
    if not db.query(JobCategory).filter(JobCategory.id == job_category_id).first():
        raise HTTPException(400, "Invalid job_category_id")
    w = CandidateWizardState(
        candidate_id=None,
        job_category_id=job_category_id,
        current_step=1,
        total_steps=TOTAL_WIZARD_STEPS,
        is_finalized=0,
        step_data=payload.get("step_data") or {},
        created_by_id=user.id,
    )
    db.add(w); db.commit(); db.refresh(w)
    audit_svc.log_event(
        db, entity_type=AuditEntity.WIZARD_STATE.value, entity_id=w.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Opened new candidate wizard (job_category={job_category_id or '—'})",
        after={"current_step": 1, "job_category_id": job_category_id},
    )
    return _wizard_to_dict(w)


@router.get("/wizard/active")
def get_active_wizards(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List unfinished wizard drafts (for the 'Resume' shelf on the candidates page)."""
    rows = (
        db.query(CandidateWizardState)
        .filter(CandidateWizardState.is_finalized == 0)
        .order_by(CandidateWizardState.updated_at.desc())
        .limit(50).all()
    )
    return [_wizard_to_dict(w) for w in rows]


@router.get("/wizard/{wizard_id}")
def get_wizard(wizard_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    w = db.query(CandidateWizardState).filter(CandidateWizardState.id == wizard_id).first()
    if not w:
        raise HTTPException(404, "Wizard draft not found")
    return _wizard_to_dict(w)


@router.get("/wizard/by-candidate/{candidate_id}")
def get_wizard_by_candidate(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Find any existing wizard draft for a given candidate (so editing reopens it)."""
    w = (
        db.query(CandidateWizardState)
        .filter(CandidateWizardState.candidate_id == candidate_id)
        .order_by(CandidateWizardState.updated_at.desc())
        .first()
    )
    if not w:
        return None
    return _wizard_to_dict(w)


@router.patch("/wizard/{wizard_id}/step/{step}")
def save_wizard_step(
    wizard_id: int, step: int, payload: dict, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Persist a single step's form fields. Idempotent.

    Body: { "data": { ...form fields... }, "advance": true/false }
    """
    if step < 1 or step > TOTAL_WIZARD_STEPS:
        raise HTTPException(400, f"step must be 1..{TOTAL_WIZARD_STEPS}")
    w = db.query(CandidateWizardState).filter(CandidateWizardState.id == wizard_id).first()
    if not w:
        raise HTTPException(404, "Wizard draft not found")

    step_data = dict(w.step_data or {})
    step_data[str(step)] = payload.get("data") or {}
    w.step_data = step_data

    # current_step = furthest step user has *reached* (not just saved)
    if payload.get("advance", True):
        w.current_step = max(w.current_step or 1, min(step + 1, TOTAL_WIZARD_STEPS))
    else:
        w.current_step = max(w.current_step or 1, step)

    db.commit(); db.refresh(w)
    audit_svc.log_wizard_step(db, wizard_state=w, step=step, actor=user, request=request)
    return _wizard_to_dict(w)


@router.post("/wizard/{wizard_id}/finalize")
def finalize_wizard(
    wizard_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Merge all step_data into the canonical Candidate row, create/update the
    candidate, optionally create the assignment, and mark the wizard finalised.
    """
    w = db.query(CandidateWizardState).filter(CandidateWizardState.id == wizard_id).first()
    if not w:
        raise HTTPException(404, "Wizard draft not found")

    # Merge every step_data slice into one big payload
    merged: dict = {}
    for k, v in (w.step_data or {}).items():
        if isinstance(v, dict):
            merged.update(v)

    if not merged.get("full_name"):
        raise HTTPException(400, "Step 1 (Personal Info): Full name is required to finalize")
    if not merged.get("father_name"):
        raise HTTPException(400, "Step 1 (Personal Info): Father name is required to finalize")

    data = _coerce(merged)
    if not data.get("status"):
        data["status"] = CandidateStage.DOCS_PENDING.value if w.job_category_id else CandidateStage.NEW.value

    if w.candidate_id:
        # Update existing
        cand = db.query(Candidate).filter(Candidate.id == w.candidate_id).first()
        if not cand:
            raise HTTPException(404, "Linked candidate not found")
        before_status = cand.status
        for k, v in data.items():
            setattr(cand, k, v)
        db.commit(); db.refresh(cand)
        if data.get("status") and normalize_stage(before_status) != data["status"]:
            audit_svc.log_stage_change(
                db, candidate=cand, old_stage=normalize_stage(before_status),
                new_stage=data["status"], actor=user, request=request, reason="wizard finalize",
            )
    else:
        cand = Candidate(**data)
        db.add(cand); db.commit(); db.refresh(cand)
        w.candidate_id = cand.id

    # Auto-assign to pre-selected trade
    if w.job_category_id:
        existing = db.query(CandidateAssignment).filter(
            CandidateAssignment.candidate_id == cand.id,
            CandidateAssignment.job_category_id == w.job_category_id,
        ).first()
        if not existing:
            jc = db.query(JobCategory).filter(JobCategory.id == w.job_category_id).first()
            if jc:
                assign = CandidateAssignment(
                    candidate_id=cand.id,
                    job_category_id=jc.id,
                    status=CandidateStage.DOCS_PENDING.value,
                )
                db.add(assign); db.commit()
                audit_svc.log_assign(db, candidate=cand, job_category=jc, actor=user, request=request)

    w.is_finalized = 1
    w.finalized_at = datetime.now(timezone.utc)
    w.current_step = TOTAL_WIZARD_STEPS
    db.commit(); db.refresh(w)

    audit_svc.log_wizard_finalize(db, wizard_state=w, candidate=cand, actor=user, request=request)
    return {"wizard": _wizard_to_dict(w), "candidate": _to_dict(cand)}


@router.post("/wizard/{wizard_id}/reopen")
def reopen_wizard(
    wizard_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Reopen a finalised wizard for further edits (clears is_finalized flag)."""
    w = db.query(CandidateWizardState).filter(CandidateWizardState.id == wizard_id).first()
    if not w:
        raise HTTPException(404, "Wizard draft not found")
    w.is_finalized = 0
    w.finalized_at = None
    db.commit(); db.refresh(w)
    audit_svc.log_wizard_reopen(db, wizard_state=w, actor=user, request=request)
    return _wizard_to_dict(w)


@router.post("/wizard/from-candidate/{candidate_id}")
def open_wizard_for_existing(
    candidate_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Open (or create) a wizard pre-populated from an existing candidate."""
    cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not cand:
        raise HTTPException(404, "Candidate not found")

    # Build the canonical 7-slice step_data from the candidate row
    c = _to_dict(cand)
    step_data = {
        "1": {k: c.get(k) for k in [
            "full_name","name_arabic","mother_name","father_name","father_name_arabic",
            "gender","marital_status","religion","date_of_birth","place_of_birth",
            "place_of_birth_arabic","nationality","address","phone","email","tehsil",
            "district","province","photo",
        ]},
        "2": {k: c.get(k) for k in [
            "passport_no","passport_issue_date","passport_expiry_date",
            "issuing_authority","issuing_authority_arabic","passport_issue_place",
            "cnic","nadra_token_no",
        ]},
        "3": {k: c.get(k) for k in [
            "permission_no","permission_date","qualification","age_employee",
            "profession","salary",
        ]},
        "4": {k: c.get(k) for k in [
            "next_of_kin_name","next_of_kin_nic","next_of_kin_relation","notes",
        ]},
        "5": {k: c.get(k) for k in [
            "gamca_number","medical_center","medical_date","medical_consignment_no",
            "medical_send_date","medical_courier_name","protector_no","protector_date",
        ]},
        "6": {k: c.get(k) for k in [
            "visa_stamp_date","e_number","destination","flight_no",
            "date_of_departure","ticket_no",
        ]},
        "7": {k: c.get(k) for k in [
            "price","accommodation_allowance","food_allowance","ticket_included",
            "slot_notes","status",
        ]},
    }

    # Reuse an existing draft if present — but upgrade legacy drafts that were
    # created before the 5→7 step expansion (backfill any missing slices/keys
    # from the candidate row so Steps 6 & 7 hydrate correctly).
    existing = (
        db.query(CandidateWizardState)
        .filter(CandidateWizardState.candidate_id == candidate_id)
        .order_by(CandidateWizardState.updated_at.desc())
        .first()
    )
    if existing:
        changed = False
        ex_data = dict(existing.step_data or {})
        for step_key, fresh_slice in step_data.items():
            slice_existing = dict(ex_data.get(step_key) or {})
            for fk, fv in fresh_slice.items():
                # Only fill keys the draft is missing (preserve user's in-progress edits)
                if fk not in slice_existing:
                    slice_existing[fk] = fv
                    changed = True
            if step_key not in ex_data:
                changed = True
            ex_data[step_key] = slice_existing
        if changed:
            existing.step_data = ex_data
        if (existing.total_steps or 0) != TOTAL_WIZARD_STEPS:
            existing.total_steps = TOTAL_WIZARD_STEPS
            changed = True
        if existing.is_finalized:
            existing.is_finalized = 0
            existing.finalized_at = None
            changed = True
            audit_svc.log_wizard_reopen(db, wizard_state=existing, actor=user, request=request)
        if changed:
            db.commit(); db.refresh(existing)
        return _wizard_to_dict(existing)

    w = CandidateWizardState(
        candidate_id=cand.id,
        current_step=1,
        total_steps=TOTAL_WIZARD_STEPS,
        is_finalized=0,
        step_data=step_data,
        created_by_id=user.id,
    )
    db.add(w); db.commit(); db.refresh(w)
    audit_svc.log_event(
        db, entity_type=AuditEntity.WIZARD_STATE.value, entity_id=w.id,
        action=AuditAction.CREATE.value, actor=user, request=request,
        summary=f"Opened wizard for candidate #{cand.id} '{cand.full_name}'",
        after={"candidate_id": cand.id},
    )
    return _wizard_to_dict(w)


@router.delete("/wizard/{wizard_id}")
def discard_wizard(
    wizard_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Throw away an unfinalised draft."""
    w = db.query(CandidateWizardState).filter(CandidateWizardState.id == wizard_id).first()
    if not w:
        raise HTTPException(404, "Wizard draft not found")
    audit_svc.log_event(
        db, entity_type=AuditEntity.WIZARD_STATE.value, entity_id=w.id,
        action=AuditAction.DELETE.value, actor=user, request=request,
        summary="Discarded wizard draft",
    )
    db.delete(w); db.commit()
    return {"ok": True}


# ============================================================================
# Audit log read endpoint (for the candidate side-drawer "Activity" tab)
# ============================================================================
@router.get("/{candidate_id}/audit")
def get_candidate_audit(
    candidate_id: int, limit: int = 50,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    from app.models import AuditLog
    rows = (
        db.query(AuditLog)
        .filter(
            ((AuditLog.entity_type == AuditEntity.CANDIDATE.value) & (AuditLog.entity_id == candidate_id))
            | ((AuditLog.entity_type == AuditEntity.ASSIGNMENT.value) & (AuditLog.entity_id == candidate_id))
        )
        .order_by(AuditLog.occurred_at.desc())
        .limit(limit).all()
    )
    return [
        {
            "id": r.id,
            "entity_type": r.entity_type,
            "action": r.action,
            "actor_name": r.actor_name,
            "summary": r.summary,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]


# ============================================================================
# Printable candidate profile (CV-style) — used by the side-panel Print btn
# ============================================================================
# Previously the side-panel "Print" button just called `window.print()`,
# which printed a screenshot of the popup. In v6 we replace that with a
# proper server-rendered profile sheet using the tenant's letterhead.
#
# The rendered HTML auto-fires `window.print()` on load so the user
# immediately sees the print dialog — no extra clicks, no new tab.
# ----------------------------------------------------------------------------


def _safe(v) -> str:
    """HTML-escape a value for safe inline rendering."""
    if v is None or v == "":
        return "—"
    if hasattr(v, "isoformat"):
        try:
            v = v.isoformat()
        except (TypeError, ValueError):
            v = str(v)
    return html_lib.escape(str(v))


def _fmt_date(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (date, datetime)):
        return v.strftime("%d %b %Y")
    try:
        return datetime.fromisoformat(str(v)).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return _safe(v)


@router.get("/{candidate_id}/print-profile", response_class=HTMLResponse)
def print_candidate_profile(
    candidate_id: int,
    auto_print: bool = Query(True, description="Auto-fire window.print() on load"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render a printable CV-style candidate profile on the tenant letterhead.

    This is the proper replacement for the broken `window.print()` button
    in the candidate side-panel. The page is fully self-contained
    (letterhead embedded as a base64 data: URL) so it can be printed
    directly without any external resource fetches.
    """
    cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not cand:
        raise HTTPException(404, "Candidate not found")

    # Pull the active assignment context for the demand-file linkage card.
    ctx = _to_dict(cand, db=db, with_context=True)

    # Photo handling — show real photo if uploaded, else initial.
    initials = (cand.full_name or "?")[0].upper()
    photo_block = f'<div class="photo-initials">{html_lib.escape(initials)}</div>'
    if cand.photo:
        # photo column stores a filename relative to /static/uploads/photos
        photo_url = f"/static/uploads/photos/{html_lib.escape(cand.photo)}"
        photo_block = (
            f'<img class="photo-img" src="{photo_url}" '
            f'onerror="this.outerHTML=\'<div class=&quot;photo-initials&quot;>{html_lib.escape(initials)}</div>\'">'
        )

    # Build per-card field grids.
    personal_rows = [
        ("Full Name",       _safe(cand.full_name)),
        ("Name (Arabic)",   _safe(cand.name_arabic)),
        ("Father's Name",   _safe(cand.father_name)),
        ("Mother's Name",   _safe(cand.mother_name)),
        ("Date of Birth",   _fmt_date(cand.date_of_birth)),
        ("Place of Birth",  _safe(cand.place_of_birth)),
        ("Gender",          _safe(cand.gender)),
        ("Marital Status",  _safe(cand.marital_status)),
        ("Nationality",     _safe(cand.nationality)),
        ("Religion",        _safe(cand.religion)),
        ("Qualification",   _safe(cand.qualification)),
    ]
    identity_rows = [
        ("CNIC",                _safe(cand.cnic)),
        ("Passport No.",        _safe(cand.passport_no)),
        ("Passport Issue Date", _fmt_date(cand.passport_issue_date)),
        ("Passport Expiry",     _fmt_date(cand.passport_expiry_date)),
        ("Issue Place",         _safe(cand.passport_issue_place)),
        ("Issuing Authority",   _safe(cand.issuing_authority)),
    ]
    contact_rows = [
        ("Phone",        _safe(cand.phone)),
        ("Email",        _safe(cand.email)),
        ("Tehsil",       _safe(cand.tehsil)),
        ("District",     _safe(cand.district)),
        ("Province",     _safe(cand.province)),
        ("Full Address", _safe(cand.address)),
    ]
    nok_rows = [
        ("Name",         _safe(cand.next_of_kin_name)),
        ("CNIC",         _safe(cand.next_of_kin_nic)),
        ("Relationship", _safe(cand.next_of_kin_relation)),
    ]
    demand_rows = [
        ("File Number", _safe(ctx.get("demand_file_no_display") or ctx.get("demand_file_no"))),
        ("Client",      _safe(ctx.get("client_name"))),
        ("Sponsor",     _safe(ctx.get("sponsor_name"))),
        ("Country",     _safe(ctx.get("country"))),
        ("Embassy",     _safe(ctx.get("embassy"))),
        ("Trade",       _safe(ctx.get("trade") or cand.profession)),
    ]

    def _rows_html(rows):
        return "".join(
            f'<div class="kv"><div class="k">{html_lib.escape(k)}</div>'
            f'<div class="v">{v}</div></div>'
            for (k, v) in rows
        )

    auto_print_script = (
        "<script>window.addEventListener('load', function(){ setTimeout(function(){ window.print(); }, 250); });</script>"
        if auto_print else ""
    )

    cand_id_str = html_lib.escape(str(cand.id))
    e_number = _safe(cand.e_number)
    status_lbl = html_lib.escape(stage_label(cand.status) or "")
    title = f"Candidate Profile — {html_lib.escape(cand.full_name or '')}"

    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Helvetica Neue', Arial, sans-serif;
    margin: 0; padding: 0;
    background: #f3f4f6;
    color: #1f2937;
  }}
  .sheet {{
    width: 210mm; min-height: 297mm;
    margin: 0 auto;
    background: #ffffff url('{LETTERHEAD_URL}') no-repeat top center;
    background-size: 210mm 297mm;
    position: relative;
    padding: 55mm 14mm 22mm 14mm;   /* leave room for the letterhead header + footer */
  }}
  .doc-title {{
    text-align: center;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
    color: #1e3a8a;
    text-transform: uppercase;
    margin: 0 0 4mm 0;
    padding-bottom: 3mm;
    border-bottom: 2px solid #1e3a8a;
  }}
  .hero {{
    display: flex; align-items: center; gap: 8mm;
    margin-bottom: 6mm;
    padding: 4mm;
    background: linear-gradient(135deg, #4f46e5 0%, #6d28d9 100%);
    color: #fff;
    border-radius: 6px;
  }}
  .photo-img, .photo-initials {{
    width: 30mm; height: 36mm;
    border-radius: 4px;
    border: 2px solid rgba(255,255,255,0.55);
    background: rgba(255,255,255,0.15);
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-weight: bold; font-size: 32px;
    object-fit: cover;
    flex-shrink: 0;
  }}
  .hero h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
  .hero .father {{ margin-top: 2mm; opacity: .85; font-size: 12px; }}
  .hero .badges {{ margin-top: 3mm; display: flex; gap: 2mm; flex-wrap: wrap; }}
  .hero .badge {{
    display: inline-block;
    padding: 1.2mm 2.5mm;
    background: rgba(255,255,255,0.2);
    border-radius: 3px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
    font-weight: 600;
  }}
  .hero .badge.amber {{ background: #fbbf24; color: #78350f; }}
  .hero .badge.emerald {{ background: #34d399; color: #064e3b; }}
  section.card {{
    border: 1px solid #e5e7eb;
    border-radius: 5px;
    margin-bottom: 4mm;
    overflow: hidden;
    background: #ffffff;
  }}
  section.card > h2 {{
    margin: 0;
    padding: 2.5mm 4mm;
    background: #f8fafc;
    border-bottom: 1px solid #e5e7eb;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.7px;
    color: #1e3a8a;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2mm 6mm;
    padding: 3mm 4mm;
  }}
  .grid.three {{ grid-template-columns: 1fr 1fr 1fr; }}
  .kv .k {{ font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; color: #6b7280; }}
  .kv .v {{ font-size: 12px; font-weight: 500; color: #1f2937; margin-top: 0.5mm; word-break: break-word; }}
  .footnote {{
    position: absolute; bottom: 8mm; left: 14mm; right: 14mm;
    text-align: center;
    font-size: 9px;
    color: #6b7280;
    padding-top: 2mm;
    border-top: 1px solid #e5e7eb;
  }}
  @media print {{
    body {{ background: #ffffff; }}
    .sheet {{ box-shadow: none; margin: 0; }}
  }}
</style>
</head>
<body>
  <div class="sheet">
    <div class="doc-title">Candidate Profile Sheet</div>
    <div class="hero">
      {photo_block}
      <div>
        <h1>{html_lib.escape(cand.full_name or '—')}</h1>
        <div class="father">{html_lib.escape('S/O ' + (cand.father_name or '—') if cand.father_name else '')}</div>
        <div class="badges">
          <span class="badge"># {cand_id_str}</span>
          {f'<span class="badge amber">E-Number: {e_number}</span>' if cand.e_number else ''}
          {f'<span class="badge emerald">Stage: {status_lbl}</span>' if status_lbl else ''}
        </div>
      </div>
    </div>

    <section class="card">
      <h2>Personal Information</h2>
      <div class="grid">{_rows_html(personal_rows)}</div>
    </section>

    <section class="card">
      <h2>Identity &amp; Passport</h2>
      <div class="grid">{_rows_html(identity_rows)}</div>
    </section>

    <section class="card">
      <h2>Contact &amp; Address</h2>
      <div class="grid">{_rows_html(contact_rows)}</div>
    </section>

    <section class="card">
      <h2>Next of Kin</h2>
      <div class="grid three">{_rows_html(nok_rows)}</div>
    </section>

    <section class="card">
      <h2>Linked Demand File / Visa Category</h2>
      <div class="grid">{_rows_html(demand_rows)}</div>
    </section>

    <div class="footnote">
      Generated by Dogar Trading Corporation Portal · Candidate #{cand_id_str} ·
      {datetime.now().strftime('%d %b %Y · %H:%M')}
    </div>
  </div>
  {auto_print_script}
</body>
</html>""")
