"""DtcBot Compute Agent — deterministic, offline-capable action engine.

The user wants DtcBot to be a *trained compute agent that performs actions*,
not merely a guide — and to keep working even when the external LLM key is
unavailable.

This module parses natural-language commands and data-entry blocks, maps the
human phrasing to real ``Candidate`` columns, coerces values to the correct
Python types, and then performs **real database writes** with full audit
logging.  It is the offline brain that actually:
    * creates candidates ("create candidate ...")
    * updates/sets fields ("set passport no of ALI to AB1234567")
    * does bulk data-entry from a "Field: value" block (e.g. pasted from a
      passport OCR scan)
    * assigns a candidate to a trade

`try_execute(db, message, user)` is the single entry point used by
``dtcbot.answer()``.  It returns a chat-ready dict when it recognised and
performed an action, or ``None`` when the message is not an action (so the
caller can fall through to LLM / rule-based help & search).
"""
from __future__ import annotations

import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Candidate, JobCategory
from app.services.audit import log_event
from app.core.workflow import AuditEntity, AuditAction


# ---------------------------------------------------------------------------
# Field mapping — human phrases -> Candidate column names
# ---------------------------------------------------------------------------
FIELD_ALIASES: Dict[str, str] = {
    # Personal
    "name": "full_name", "full name": "full_name", "fullname": "full_name",
    "candidate name": "full_name", "worker name": "full_name",
    "name arabic": "name_arabic", "arabic name": "name_arabic",
    "mother": "mother_name", "mother name": "mother_name",
    "father": "father_name", "father name": "father_name", "fathers name": "father_name",
    "father name arabic": "father_name_arabic",
    "gender": "gender", "sex": "gender",
    "marital status": "marital_status", "marital": "marital_status",
    "religion": "religion",
    "dob": "date_of_birth", "date of birth": "date_of_birth", "birth date": "date_of_birth",
    "birthdate": "date_of_birth",
    "place of birth": "place_of_birth", "pob": "place_of_birth", "birth place": "place_of_birth",
    "nationality": "nationality",
    "address": "address",
    "phone": "phone", "mobile": "phone", "contact": "phone", "phone no": "phone",
    "tehsil": "tehsil",
    "district": "district",
    "province": "province",
    "photo": "photo",
    # Identification
    "passport": "passport_no", "passport no": "passport_no", "passport number": "passport_no",
    "passport#": "passport_no",
    "passport issue date": "passport_issue_date", "issue date": "passport_issue_date",
    "passport expiry": "passport_expiry_date", "passport expiry date": "passport_expiry_date",
    "expiry": "passport_expiry_date", "expiry date": "passport_expiry_date",
    "date of expiry": "passport_expiry_date",
    "issuing authority": "issuing_authority", "authority": "issuing_authority",
    "passport issue place": "passport_issue_place", "issue place": "passport_issue_place",
    "cnic": "cnic", "nic": "cnic", "id card": "cnic", "national id": "cnic",
    "nadra token": "nadra_token_no", "nadra token no": "nadra_token_no", "token no": "nadra_token_no",
    # Employment
    "permission no": "permission_no", "permission number": "permission_no", "permission": "permission_no",
    "qualification": "qualification", "education": "qualification",
    "age": "age_employee", "employee age": "age_employee",
    "profession": "profession", "job": "profession", "trade": "profession", "occupation": "profession",
    "salary": "salary", "wage": "salary",
    # Next of kin
    "next of kin": "next_of_kin_name", "next of kin name": "next_of_kin_name", "kin": "next_of_kin_name",
    "next of kin nic": "next_of_kin_nic", "kin nic": "next_of_kin_nic",
    "next of kin relation": "next_of_kin_relation", "relation": "next_of_kin_relation",
    # Common
    "email": "email", "e-mail": "email",
    "status": "status",
    "notes": "notes", "note": "notes", "remarks": "notes",
}

DATE_FIELDS = {
    "date_of_birth", "passport_issue_date", "passport_expiry_date",
    "permission_date", "protector_date", "medical_date", "medical_send_date",
    "date_of_departure", "visa_stamp_date",
}
INT_FIELDS = {"age_employee"}
NUM_FIELDS = {"salary", "price"}

VALID_COLUMNS = {c.name for c in Candidate.__table__.columns}


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    fmts = (
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y",
        "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
        "%d-%b-%Y", "%d-%B-%Y", "%y%m%d",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # Loose digit grouping e.g. "1990 05 12"
    m = re.match(r"^(\d{4})\D+(\d{1,2})\D+(\d{1,2})$", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\D+(\d{1,2})\D+(\d{4})$", value)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _coerce(field: str, value: Any) -> Any:
    """Coerce a raw string value to the Python type expected by ``field``."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().strip(".,;")
    if value == "":
        return None

    if field in DATE_FIELDS:
        return _parse_date(str(value))

    if field in INT_FIELDS:
        m = re.search(r"-?\d+", str(value))
        return int(m.group(0)) if m else None

    if field in NUM_FIELDS:
        cleaned = re.sub(r"[^\d.\-]", "", str(value))
        try:
            return float(cleaned) if cleaned not in ("", "-", ".") else None
        except ValueError:
            return None

    if field == "gender":
        v = str(value).strip().lower()
        if v in ("m", "male"):
            return "Male"
        if v in ("f", "female"):
            return "Female"
        return str(value).strip().title()

    return str(value).strip()


# ---------------------------------------------------------------------------
# Parsing "Field: value" data-entry blocks
# ---------------------------------------------------------------------------
_KV_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z /#'\-]{1,40}?)\s*[:=\-\u2013]\s*(.+?)\s*$")


def _normalize_key(raw: str) -> Optional[str]:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower()).strip(" .#")
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    # direct column name match (snake_case)
    snake = key.replace(" ", "_")
    if snake in VALID_COLUMNS:
        return snake
    return None


def parse_field_block(text: str) -> Dict[str, Any]:
    """Parse a multi-line or comma-separated "Field: value" block into a dict
    of {candidate_column: coerced_value}.  Unknown keys are ignored."""
    fields: Dict[str, Any] = {}
    if not text:
        return fields

    # Split into candidate lines: real newlines, semicolons, and " · "
    chunks: List[str] = []
    for line in re.split(r"[\n;\u2022]+", text):
        # also allow comma-separated pairs on a single line, but only split on
        # commas that are followed by "word :" (to avoid breaking addresses)
        if _KV_LINE.match(line):
            chunks.append(line)
        else:
            parts = re.split(r",(?=\s*[A-Za-z][A-Za-z /#'\-]{1,40}?\s*[:=])", line)
            chunks.extend(parts)

    for chunk in chunks:
        m = _KV_LINE.match(chunk)
        if not m:
            continue
        col = _normalize_key(m.group(1))
        if not col or col not in VALID_COLUMNS:
            continue
        val = _coerce(col, m.group(2))
        if val is not None:
            fields[col] = val
    return fields


# ---------------------------------------------------------------------------
# Candidate resolution & serialization
# ---------------------------------------------------------------------------
def _find_candidate(db: Session, ref: str) -> Optional[Candidate]:
    ref = (ref or "").strip().strip("'\"")
    if not ref:
        return None
    # by numeric id (#123 or 123)
    m = re.match(r"^#?(\d+)$", ref)
    if m:
        c = db.query(Candidate).get(int(m.group(1)))
        if c:
            return c
    like = f"%{ref}%"
    return (
        db.query(Candidate)
        .filter(
            or_(
                Candidate.passport_no.ilike(ref),
                Candidate.cnic.ilike(ref),
                Candidate.full_name.ilike(ref),
                Candidate.full_name.ilike(like),
                Candidate.passport_no.ilike(like),
                Candidate.cnic.ilike(like),
            )
        )
        .order_by(Candidate.id.desc())
        .first()
    )


def _serialize_candidate(c: Candidate) -> Dict[str, Any]:
    def s(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v
    return {
        "id": c.id,
        "full_name": c.full_name,
        "father_name": c.father_name,
        "passport_no": c.passport_no,
        "cnic": c.cnic,
        "profession": c.profession,
        "date_of_birth": s(c.date_of_birth),
        "nationality": c.nationality,
        "phone": c.phone,
        "status": c.status,
    }


def _audit(db, *, candidate, action, summary, before=None, after=None, user=None):
    try:
        log_event(
            db,
            entity_type=AuditEntity.CANDIDATE.value,
            entity_id=candidate.id if candidate else None,
            action=action,
            actor=user,
            summary=summary,
            before=before,
            after=after,
            commit=False,
        )
    except Exception as exc:   # noqa: BLE001 — audit write can fail in many ways; never break the action
        import logging
        logging.getLogger("dtcbot.audit").warning("audit write failed: %s", exc)


# ---------------------------------------------------------------------------
# Action executors — real DB writes
# ---------------------------------------------------------------------------
def create_candidate(db: Session, fields: Dict[str, Any], user=None) -> Dict[str, Any]:
    """Create a real Candidate row from a mapped fields dict."""
    fields = {k: v for k, v in fields.items() if k in VALID_COLUMNS and v is not None}
    if not fields.get("full_name"):
        return {"ok": False, "error": "A full name is required to create a candidate."}

    c = Candidate(**fields)
    if user is not None:
        try:
            c.created_by_id = user.id
            c.updated_by_id = user.id
        except AttributeError:
            # Legacy Candidate model without audit FKs — non-fatal.
            pass
    db.add(c)
    db.flush()  # assign id

    _audit(
        db, candidate=c, action=AuditAction.CREATE.value,
        summary=f"DtcBot created candidate #{c.id} '{c.full_name}' "
                f"({len(fields)} field(s))",
        after=_serialize_candidate(c), user=user,
    )
    db.commit()
    db.refresh(c)
    return {"ok": True, "candidate": _serialize_candidate(c), "fields_set": sorted(fields.keys())}


def update_candidate(db: Session, candidate_ref: str, fields: Dict[str, Any], user=None) -> Dict[str, Any]:
    """Update an existing candidate's fields."""
    c = _find_candidate(db, candidate_ref)
    if not c:
        return {"ok": False, "error": f"No candidate matched '{candidate_ref}'."}

    fields = {k: v for k, v in fields.items() if k in VALID_COLUMNS}
    if not fields:
        return {"ok": False, "error": "I couldn't identify any valid fields to update."}

    before = _serialize_candidate(c)
    changed: List[str] = []
    for k, v in fields.items():
        if getattr(c, k, None) != v:
            setattr(c, k, v)
            changed.append(k)
    if user is not None:
        try:
            c.updated_by_id = user.id
        except AttributeError:
            # Legacy Candidate model without audit FK — non-fatal.
            pass

    if not changed:
        return {"ok": True, "candidate": _serialize_candidate(c), "fields_set": [],
                "unchanged": True}

    db.flush()
    _audit(
        db, candidate=c, action=AuditAction.UPDATE.value,
        summary=f"DtcBot updated candidate #{c.id} '{c.full_name}': "
                f"{', '.join(changed)}",
        before=before, after=_serialize_candidate(c), user=user,
    )
    db.commit()
    db.refresh(c)
    return {"ok": True, "candidate": _serialize_candidate(c), "fields_set": sorted(changed)}


def assign_candidate(db: Session, candidate_ref: str, trade_id: int, user=None) -> Dict[str, Any]:
    """Assign a candidate to a trade (job_category)."""
    c = _find_candidate(db, candidate_ref)
    if not c:
        return {"ok": False, "error": f"No candidate matched '{candidate_ref}'."}
    jc = db.query(JobCategory).get(int(trade_id))
    if not jc:
        return {"ok": False, "error": f"No trade #{trade_id} found."}

    from app.models import CandidateAssignment
    existing = (
        db.query(CandidateAssignment)
        .filter(CandidateAssignment.candidate_id == c.id,
                CandidateAssignment.job_category_id == jc.id,
                CandidateAssignment.unassigned_at.is_(None))
        .first()
    )
    if existing:
        return {"ok": True, "candidate": _serialize_candidate(c),
                "trade": jc.trade, "already": True}

    a = CandidateAssignment(candidate_id=c.id, job_category_id=jc.id, status="pending")
    if user is not None:
        try:
            a.assigned_by_id = user.id
        except AttributeError:
            # Legacy CandidateAssignment model without audit FK — non-fatal.
            pass
    db.add(a)
    db.flush()
    _audit(
        db, candidate=c, action=AuditAction.ASSIGN.value,
        summary=f"DtcBot assigned '{c.full_name}' to trade '{jc.trade}' (demand #{jc.demand_id})",
        after={"candidate_id": c.id, "trade": jc.trade, "demand_id": jc.demand_id},
        user=user,
    )
    db.commit()
    return {"ok": True, "candidate": _serialize_candidate(c), "trade": jc.trade}


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------
def _wrap_action(res: Dict[str, Any], verb: str) -> Dict[str, Any]:
    if not res.get("ok"):
        return {"type": "text", "text": f"⚠️ {res.get('error', 'Action failed.')}"}

    c = res.get("candidate", {})
    fields_set = res.get("fields_set", [])
    cid = c.get("id")

    if res.get("already"):
        text = f"✅ Candidate **{c.get('full_name')}** is already assigned to **{res.get('trade')}**."
        verb_out = "assigned"
    elif res.get("trade"):
        text = f"✅ Assigned **{c.get('full_name')}** → trade **{res.get('trade')}**. Opening the candidate now…"
        verb_out = "assigned"
    elif res.get("unchanged"):
        text = f"ℹ️ No changes — **{c.get('full_name')}** already had those values."
        verb_out = "noop"
    elif verb == "create":
        text = (f"✅ Created candidate **#{cid} {c.get('full_name')}** "
                f"with {len(fields_set)} field(s) entered. Opening the record now so "
                f"you can review and complete the remaining steps…")
        verb_out = "created"
    else:
        text = (f"✅ Updated **#{cid} {c.get('full_name')}** — "
                f"set {', '.join(fields_set)}. Opening the candidate's edit screen…")
        verb_out = "updated"

    # Build an action response — front-end auto-navigates to the candidate
    # edit screen so the data-entry loop completes end-to-end without
    # requiring an extra click from the operator (Vercel-v0 style agent).
    return {
        "type": "action",
        "verb": verb_out,
        "text": text,
        "candidate": c,
        "candidate_id": cid,
        "fields_set": fields_set,
        "url": f"/candidates?edit={cid}&flyout=0" if cid else "/candidates",
        "navigate": bool(cid) and not res.get("unchanged"),
        "links": [
            {"label": "Open candidate record", "href": f"/candidates?edit={cid}&flyout=0" if cid else "/candidates"},
        ],
    }


# ---------------------------------------------------------------------------
# Natural-language command router
# ---------------------------------------------------------------------------
_CREATE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:create|add|new|register|enter|do\s+data\s*entry\s+for)\s+"
    r"(?:a\s+)?(?:new\s+)?(?:candidate|worker|employee|applicant)\b",
    re.IGNORECASE,
)
_SET_RE = re.compile(
    r"^\s*(?:please\s+)?(?:set|update|change|edit)\s+(?:the\s+)?(.+?)\s+"
    r"(?:of|for)\s+(.+?)\s+(?:to|=|as)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_ASSIGN_RE = re.compile(
    r"^\s*(?:please\s+)?assign\s+(.+?)\s+to\s+(?:trade\s*)?#?(\d+)\s*$",
    re.IGNORECASE,
)


def _looks_like_block(text: str) -> bool:
    """Heuristic: at least 2 recognisable 'Field: value' lines."""
    hits = 0
    for line in re.split(r"[\n;\u2022]+", text):
        m = _KV_LINE.match(line)
        if m and _normalize_key(m.group(1)):
            hits += 1
        if hits >= 2:
            return True
    return False


def try_execute(db: Session, message: str, user=None) -> Optional[Dict[str, Any]]:
    """Detect an actionable command in ``message`` and execute it.

    Returns a chat-ready response dict on success, or ``None`` if the message
    is not an action (caller should fall through to LLM / rules / search)."""
    if not message:
        return None
    text = message.strip()

    # 1) ASSIGN: "assign ALI to trade #12"
    m = _ASSIGN_RE.match(text)
    if m:
        res = assign_candidate(db, m.group(1).strip(), int(m.group(2)), user=user)
        return _wrap_action(res, "assign")

    # 2) SET/UPDATE: "set passport no of ALI HASSAN to AB1234567"
    m = _SET_RE.match(text)
    if m:
        col = _normalize_key(m.group(1))
        if col:
            ref = m.group(2).strip().strip("'\"")
            val = _coerce(col, m.group(3))
            res = update_candidate(db, ref, {col: val}, user=user)
            return _wrap_action(res, "update")

    # 3) CREATE candidate (with optional inline / following field block)
    if _CREATE_RE.match(text):
        # everything after the create phrase may contain field block
        tail = _CREATE_RE.sub("", text, count=1).strip(" :-\u2013")
        fields = parse_field_block(tail) if tail else {}
        # also allow "named X" / "name X" / "father Y" shorthand
        src = tail or text
        if "full_name" not in fields:
            nm = re.search(r"\bnamed?\s+([A-Za-z][A-Za-z .'\-]{2,60}?)(?=\s*,|\s+father\b|$)",
                           src, re.IGNORECASE)
            if nm:
                fields["full_name"] = _coerce("full_name", nm.group(1))
        if "father_name" not in fields:
            fa = re.search(r"\bfather(?:'?s)?(?:\s+name)?\s+([A-Za-z][A-Za-z .'\-]{2,60}?)(?=\s*,|$)",
                           src, re.IGNORECASE)
            if fa:
                fields["father_name"] = _coerce("father_name", fa.group(1))
        if not fields.get("full_name"):
            return {
                "type": "text",
                "text": ("I can create the candidate right away — just give me at least the "
                         "**Full Name**. You can also paste a data block, e.g.:\n\n"
                         "`create candidate`\n`Full Name: ALI HASSAN`\n`Father Name: HASSAN ALI`\n"
                         "`Passport No: AB1234567`\n`CNIC: 35202-1234567-1`\n`Profession: Driver`"),
            }
        res = create_candidate(db, fields, user=user)
        return _wrap_action(res, "create")

    # 4) Bare data-entry block (e.g. pasted OCR result) → create candidate
    if _looks_like_block(text):
        fields = parse_field_block(text)
        if fields.get("full_name"):
            res = create_candidate(db, fields, user=user)
            return _wrap_action(res, "create")
        # block without a name but with a passport/cnic → try update by that ref
        ref = fields.get("passport_no") or fields.get("cnic")
        if ref:
            res = update_candidate(db, str(ref), fields, user=user)
            if res.get("ok"):
                return _wrap_action(res, "update")
        return {
            "type": "text",
            "text": ("I parsed a data block but couldn't find a **Full Name** or a matching "
                     "existing candidate (by passport/CNIC). Add a `Full Name:` line to create "
                     "a new record."),
        }

    return None
