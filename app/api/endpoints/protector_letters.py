"""Protector Letter endpoints.

Powers the **Protector Letter** workflow page (`/protector-letter`):

* **GET /lookup/{passport_no}** — find one candidate by passport, return
  the row data the UI needs (name, father name, permission, plus the
  active demand for sponsor/permission auto-fill).
* **POST /print/packet** — accept a list of candidate ids + optional
  letter overrides → returns the combined 3-document HTML packet
  (Main Letter + Undertaking-B + Undertaking) ready to auto-print.
* **GET /print/main / /print/undertaking-b / /print/undertaking** —
  individual letters (same data, single document).
* **GET /e-barcode/{candidate_id}** — auto-printable E-Barcode sheet
  for the candidate.  Used by the *Documents* tab on the candidate
  drawer and exposable as a stand-alone document type.

All HTML responses are full standalone pages that the browser can
auto-print. They render directly on the existing
``dogar_letterhead.jpg`` background — see
``app/services/letterhead_renderer.py`` for the templates themselves.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.endpoints.demands import _get_prefix, display_file_number
from app.core.deps import get_current_user
from app.core.tenancy import get_tenant_db as get_db  # tenant-scoped session (falls back to control DB when no tenant)
from app.models import (
    Candidate,
    CandidateAssignment,
    Client,
    Demand,
    JobCategory,
    User,
)
from app.services import letterhead_renderer as letters

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _active_demand_for(db: Session, candidate_id: int) -> Optional[Demand]:
    """Most-recent assignment → JobCategory → Demand. None if unassigned."""
    row = (
        db.query(Demand)
        .join(JobCategory, JobCategory.demand_id == Demand.id)
        .join(CandidateAssignment, CandidateAssignment.job_category_id == JobCategory.id)
        .filter(CandidateAssignment.candidate_id == candidate_id)
        .order_by(CandidateAssignment.assigned_at.desc(), CandidateAssignment.id.desc())
        .first()
    )
    return row


def _candidate_by_passport(db: Session, passport_no: str) -> Optional[Candidate]:
    """Lookup is case-insensitive and trims whitespace — passport numbers
    are often pasted with surrounding spaces."""
    pn = (passport_no or "").strip()
    if not pn:
        return None
    # Try exact match first, then case-insensitive fallback.
    cand = db.query(Candidate).filter(Candidate.passport_no == pn).first()
    if cand:
        return cand
    return (
        db.query(Candidate)
        .filter(Candidate.passport_no.ilike(pn))
        .first()
    )


def _row_for_ui(cand: Candidate, dem: Optional[Demand], db: Session) -> dict:
    """Shape a candidate + demand pair into the JSON row the UI table
    consumes. Mirrors the column layout in the screenshot:
    Passport Number | Name | Father Name | Permission.
    """
    name = (cand.full_name or "").strip()
    father = (cand.father_name or "").strip()
    if not father and name:
        # If the legacy record concatenated name + father_name into full_name
        # surface the second token as the father name so the row never shows
        # a blank cell.
        parts = name.split(None, 1)
        if len(parts) == 2:
            name, father = parts[0], parts[1]

    permission = ""
    permission_date = ""
    if dem is not None:
        permission = (dem.permission_no or "").strip()
        if dem.permission_date:
            permission_date = dem.permission_date.strftime("%d/%m/%Y")
    # Candidate-level fields override demand if filled
    permission = (cand.permission_no or permission).strip()
    if cand.permission_date:
        permission_date = cand.permission_date.strftime("%d/%m/%Y")

    file_no = ""
    if dem is not None:
        file_no = display_file_number(dem.file_number or "", _get_prefix(db))

    return {
        "id": cand.id,
        "passport_no": (cand.passport_no or "").strip(),
        "full_name": name,
        "father_name": father,
        "e_number": (cand.e_number or "").strip(),
        "profession": (cand.profession or "").strip(),
        "permission_no": permission,
        "permission_date": permission_date,
        "permission_display": (f"{permission} / Dated {permission_date}".strip(" /")
                               if (permission or permission_date) else ""),
        "demand_id": dem.id if dem else None,
        "demand_file_no": file_no,
        "sponsor_name": (dem.sponsor_name if dem else "") or "",
        "country": (dem.country if dem else "") or "",
        "embassy": (dem.embassy if dem else "") or "",
    }


# ---------------------------------------------------------------------------
# JSON: lookup by passport (used by the "Get Data" button)
# ---------------------------------------------------------------------------
@router.get("/lookup/{passport_no}")
def lookup_by_passport(
    passport_no: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the candidate row matching ``passport_no`` (case-insensitive),
    or 404 if no match.
    """
    cand = _candidate_by_passport(db, passport_no)
    if not cand:
        raise HTTPException(404, f"No candidate found with passport '{passport_no}'")
    dem = _active_demand_for(db, cand.id)
    return _row_for_ui(cand, dem, db)


# Also accept passport_no as a query parameter (the UI uses this when the
# user types and presses Enter — avoids URL-encoding issues with slashes).
@router.get("/lookup")
def lookup_by_passport_query(
    passport_no: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return lookup_by_passport(passport_no, db, user)


# ---------------------------------------------------------------------------
# E-Barcode (single candidate) — rendered HTML
# ---------------------------------------------------------------------------
@router.get("/e-barcode/{candidate_id}", response_class=HTMLResponse)
def render_e_barcode(
    candidate_id: int,
    request: Request,
    auto_print: bool = Query(True),
    blank: bool = Query(True, description="Use clean blank-sheet layout (v8 default — barcodes only, no letterhead, matches dogars.com reference). Pass blank=false to force letterhead branding."),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not cand:
        raise HTTPException(404, "Candidate not found")
    tenant = getattr(request.state, "tenant", None)
    html = letters.render_e_barcode(cand, auto_print=auto_print, blank=blank, tenant=tenant)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# NOC Verification by Agency — printed on letterhead (overlay pattern)
# ---------------------------------------------------------------------------
@router.get("/noc-verification/{candidate_id}", response_class=HTMLResponse)
def render_noc_verification(
    candidate_id: int,
    request: Request,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render the NOC Verification letter on the company letterhead.

    The candidate's linked demand (if any) is auto-loaded so the embassy
    salutation (Royal Saudi Consulate Islamabad) and visa number are filled
    in automatically — no manual data entry.

    **v8 restriction**: NOC is ISB-only. If the candidate is linked to a
    Karachi / Lahore / other embassy demand, this endpoint returns HTTP 400
    so URL hacking can't bypass the frontend gate.
    """
    cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not cand:
        raise HTTPException(404, "Candidate not found")

    # Find the linked demand (via assignment → job_category → demand) to pick
    # up the embassy + visa number for the letter body.
    demand = None
    try:
        a = (db.query(CandidateAssignment)
             .filter(CandidateAssignment.candidate_id == candidate_id)
             .order_by(CandidateAssignment.id.desc())
             .first())
        if a:
            # CandidateAssignment links via job_category_id → JobCategory → demand_id
            jc = db.query(JobCategory).filter(JobCategory.id == a.job_category_id).first()
            if jc and jc.demand_id:
                demand = db.query(Demand).filter(Demand.id == jc.demand_id).first()
    except Exception:
        demand = None

    # Determine embassy — try demand first, fall back to candidate row
    embassy = ""
    if demand is not None:
        embassy = (getattr(demand, "embassy", "") or "").strip()
    if not embassy:
        embassy = (getattr(cand, "embassy", "") or "").strip()
    embassy_low = embassy.lower()

    # ISB-only gate
    if "islamabad" not in embassy_low:
        raise HTTPException(
            400,
            "NOC Verification is only available for candidates linked to a "
            "Saudi-Arabia (Islamabad) embassy demand file. Current embassy: "
            f"'{embassy or 'none'}'."
        )

    tenant = getattr(request.state, "tenant", None)
    html = letters.render_noc_verification(
        cand, demand=demand, auto_print=auto_print, tenant=tenant
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Packet (Main + Undertaking-B + Undertaking) — accepts a list of ids
# ---------------------------------------------------------------------------
class PrintPacketPayload(BaseModel):
    candidate_ids: List[int]
    # Optional letter overrides — the user can pre-fill them on the Print
    # Letters dialog (or just leave blank → renderer falls back to demand
    # data / dashes).
    demand_id: Optional[int] = None
    file_number: Optional[str] = None
    letter_date: Optional[str] = None        # YYYY-MM-DD
    embassy_entry_airport: Optional[str] = None
    registration_fee: Optional[str] = None
    challan_no: Optional[str] = None
    challan_date: Optional[str] = None
    permission_granted: Optional[str] = None
    fsa_submitted: Optional[str] = None
    balance: Optional[str] = None
    include: Optional[List[str]] = None       # subset of [main, undertaking_b, undertaking]


def _load_packet_context(db: Session, payload: PrintPacketPayload):
    """Resolve candidate rows + demand for a packet request."""
    if not payload.candidate_ids:
        raise HTTPException(400, "candidate_ids must not be empty")
    cands = (
        db.query(Candidate)
        .filter(Candidate.id.in_(payload.candidate_ids))
        .all()
    )
    if not cands:
        raise HTTPException(404, "No candidates found for the supplied ids")
    # Preserve the order the UI submitted (the user typed them in this order).
    by_id = {c.id: c for c in cands}
    cands = [by_id[i] for i in payload.candidate_ids if i in by_id]

    # Demand — explicit override wins, otherwise first candidate's active demand.
    dem: Optional[Demand] = None
    if payload.demand_id:
        dem = db.query(Demand).filter(Demand.id == payload.demand_id).first()
    if dem is None and cands:
        dem = _active_demand_for(db, cands[0].id)

    letter_date = None
    if payload.letter_date:
        try:
            letter_date = datetime.strptime(payload.letter_date, "%Y-%m-%d").date()
        except ValueError:
            letter_date = None

    include = tuple(payload.include) if payload.include else ("main", "undertaking_b", "undertaking")

    return cands, dem, letter_date, include


@router.post("/print/packet", response_class=HTMLResponse)
def print_packet(
    payload: PrintPacketPayload,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cands, dem, letter_date, include = _load_packet_context(db, payload)
    html = letters.render_protector_packet(
        cands, dem,
        file_number=payload.file_number or "",
        letter_date=letter_date,
        embassy_entry_airport=payload.embassy_entry_airport or "",
        registration_fee=payload.registration_fee or "",
        challan_no=payload.challan_no or "",
        challan_date=payload.challan_date or "",
        permission_granted=payload.permission_granted or "",
        fsa_submitted=payload.fsa_submitted or "",
        balance=payload.balance or "",
        include=include,
        auto_print=auto_print,
    )
    return HTMLResponse(html)


@router.post("/print/main", response_class=HTMLResponse)
def print_main(
    payload: PrintPacketPayload,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cands, dem, letter_date, _ = _load_packet_context(db, payload)
    html = letters.render_main_letter(
        cands, dem,
        file_number=payload.file_number or "",
        letter_date=letter_date,
        embassy_entry_airport=payload.embassy_entry_airport or "",
        registration_fee=payload.registration_fee or "",
        challan_no=payload.challan_no or "",
        challan_date=payload.challan_date or "",
        permission_granted=payload.permission_granted or "",
        fsa_submitted=payload.fsa_submitted or "",
        balance=payload.balance or "",
        auto_print=auto_print,
    )
    return HTMLResponse(html)


@router.post("/print/undertaking-b", response_class=HTMLResponse)
def print_undertaking_b(
    payload: PrintPacketPayload,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cands, dem, letter_date, _ = _load_packet_context(db, payload)
    html = letters.render_undertaking_b(cands, dem, letter_date=letter_date, auto_print=auto_print)
    return HTMLResponse(html)


@router.post("/print/undertaking", response_class=HTMLResponse)
def print_undertaking(
    payload: PrintPacketPayload,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cands, dem, letter_date, _ = _load_packet_context(db, payload)
    html = letters.render_undertaking(cands, dem, letter_date=letter_date, auto_print=auto_print)
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Bulk passport lookup — accepts a `\n`-separated list of passport numbers
# (used by the "Upload txt File" path in the UI screenshot).
# ---------------------------------------------------------------------------
class BulkLookupPayload(BaseModel):
    passport_numbers: List[str]


@router.post("/lookup/bulk")
def lookup_bulk(
    payload: BulkLookupPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resolve a list of passport numbers in one round-trip.
    Returns ``{"found": [...rows...], "missing": ["BB12345", ...]}``.
    """
    found = []
    missing = []
    seen_ids: set[int] = set()
    for pn in payload.passport_numbers:
        cand = _candidate_by_passport(db, pn)
        if cand is None:
            missing.append(pn.strip())
            continue
        if cand.id in seen_ids:
            continue
        seen_ids.add(cand.id)
        dem = _active_demand_for(db, cand.id)
        found.append(_row_for_ui(cand, dem, db))
    return {"found": found, "missing": missing}


# ===========================================================================
# DEMAND LETTERHEAD DOCUMENTS — 5 docs rendered on letterhead from a demand
# ===========================================================================
def _load_demand_or_404(db: Session, demand_id: int) -> Demand:
    dem = db.query(Demand).filter(Demand.id == demand_id).first()
    if dem is None:
        raise HTTPException(404, f"Demand #{demand_id} not found")
    return dem


@router.get("/demand/{demand_id}/demand-letter", response_class=HTMLResponse)
def demand_letter(
    demand_id: int,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    return HTMLResponse(letters.render_demand_letter(dem, auto_print=auto_print))


@router.get("/demand/{demand_id}/undertaking", response_class=HTMLResponse)
def demand_undertaking(
    demand_id: int,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    return HTMLResponse(letters.render_demand_undertaking(dem, auto_print=auto_print))


@router.get("/demand/{demand_id}/visa-undertaking", response_class=HTMLResponse)
def demand_visa_undertaking(
    demand_id: int,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    return HTMLResponse(letters.render_demand_visa_undertaking(dem, auto_print=auto_print))


@router.get("/demand/{demand_id}/roman-undertaking", response_class=HTMLResponse)
def demand_roman_undertaking(
    demand_id: int,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    return HTMLResponse(letters.render_demand_roman_undertaking(dem, auto_print=auto_print))


@router.get("/demand/{demand_id}/permission-request", response_class=HTMLResponse)
def demand_permission_request(
    demand_id: int,
    auto_print: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    return HTMLResponse(letters.render_demand_permission_request(dem, auto_print=auto_print))


@router.get("/demand/{demand_id}/packet", response_class=HTMLResponse)
def demand_packet(
    demand_id: int,
    auto_print: bool = Query(True),
    include: Optional[str] = Query(None, description="Comma-separated subset of: demand_letter,demand_undertaking,visa_undertaking,roman_undertaking,permission_request"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dem = _load_demand_or_404(db, demand_id)
    inc: tuple[str, ...]
    if include:
        inc = tuple([s.strip() for s in include.split(",") if s.strip()])
    else:
        inc = ("demand_letter", "demand_undertaking", "visa_undertaking",
               "roman_undertaking", "permission_request")
    return HTMLResponse(letters.render_demand_packet(dem, include=inc, auto_print=auto_print))


@router.get("/demand/{demand_id}/index")
def demand_documents_index(
    demand_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the 5 letterhead documents available for a demand, with their
    print URLs — used by the Demand detail page to render the buttons."""
    dem = _load_demand_or_404(db, demand_id)
    base = f"/api/protector-letters/demand/{demand_id}"
    return {
        "demand_id": demand_id,
        "file_number": dem.file_number,
        "sponsor_name": dem.sponsor_name,
        "documents": [
            {"key": "demand_letter",       "name": "Demand Letter",
             "description": "M/s. sponsor authorisation — trades table & period of contract",
             "url": f"{base}/demand-letter"},
            {"key": "demand_undertaking",  "name": "Undertaking (Demand)",
             "description": "OEPL undertaking with categories & fringe benefits",
             "url": f"{base}/undertaking"},
            {"key": "visa_undertaking",    "name": "Undertaking (Visa Genuine)",
             "description": "Visa-genuine undertaking, four numbered clauses",
             "url": f"{base}/visa-undertaking"},
            {"key": "roman_undertaking",   "name": "Undertaking (Clauses I-IV)",
             "description": "Roman-numeral undertaking clauses",
             "url": f"{base}/roman-undertaking"},
            {"key": "permission_request",  "name": "Permission Application",
             "description": "Cover letter to The Protector of Emigrants",
             "url": f"{base}/permission-request"},
        ],
        "packet_url": f"{base}/packet",
    }
