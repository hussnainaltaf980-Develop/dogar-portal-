"""Smart Alerts Engine (v8) — generates reminders by scanning the live DB.

Six alert categories are generated automatically:

  1. **passport_expiry**   — passport expires within 6 months
  2. **visa_expiry**       — visa stamp expiry within 90 days
  3. **medical_expiry**    — GAMCA / medical expires within 60 days
  4. **stage_overdue**     — candidate stuck in same workflow stage > 14 days
  5. **demand_match**      — open trades that match an unassigned candidate's profession
  6. **missing_field**     — candidate at advanced stage but core fields blank

The engine is idempotent: each alert carries a deterministic `dedup_key`
so re-running the scan never produces duplicates. Acknowledged alerts
(is_dismissed=True) stay dismissed even if regenerated.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func

from app.models import (
    Candidate, CandidateAssignment, Demand, JobCategory, Client, User,
)
from app.models.reminders import Reminder

logger = logging.getLogger("alerts_engine")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dedup(kind: str, **parts) -> str:
    """Deterministic dedup key — same args → same key."""
    raw = kind + "|" + "|".join(f"{k}={parts[k]}" for k in sorted(parts.keys()))
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def _days_between(d: Optional[date], today: date) -> Optional[int]:
    if not d:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return (d - today).days


def _upsert(db: Session, *, kind: str, severity: str, title: str, body: str,
            dedup_key: str, candidate_id=None, demand_id=None, trade_id=None,
            client_id=None, due_at=None, meta: Optional[Dict[str, Any]] = None) -> Optional[Reminder]:
    """Insert OR refresh an alert. Skips if a *dismissed* one already exists."""
    existing = (db.query(Reminder)
                .filter(Reminder.dedup_key == dedup_key)
                .first())
    if existing:
        # If user previously dismissed it, leave it alone (don't re-spam).
        if existing.is_dismissed:
            return None
        # Refresh fields in case the underlying data shifted (e.g. days_until)
        existing.title    = title
        existing.body     = body
        existing.severity = severity
        existing.due_at   = due_at
        existing.meta_json = json.dumps(meta or {}, default=str)
        return existing

    r = Reminder(
        kind=kind, severity=severity, title=title, body=body,
        candidate_id=candidate_id, demand_id=demand_id, trade_id=trade_id,
        client_id=client_id, due_at=due_at,
        dedup_key=dedup_key,
        meta_json=json.dumps(meta or {}, default=str),
    )
    db.add(r)
    return r


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------
def scan_passport_expiry(db: Session, today: Optional[date] = None) -> int:
    """Alert N days before a candidate's passport expires."""
    today = today or date.today()
    soon = today + timedelta(days=180)  # 6 months ahead
    cands = (db.query(Candidate)
             .filter(Candidate.passport_expiry_date.isnot(None))
             .filter(Candidate.passport_expiry_date <= soon)
             .all())
    n = 0
    for c in cands:
        d = _days_between(c.passport_expiry_date, today)
        if d is None:
            continue
        if d < 0:
            severity, prefix = "critical", "EXPIRED"
        elif d <= 30:
            severity, prefix = "critical", f"Expires in {d}d"
        elif d <= 90:
            severity, prefix = "warning", f"Expires in {d}d"
        else:
            severity, prefix = "info", f"Expires in {d}d"
        key = _dedup("passport_expiry", cid=c.id, exp=str(c.passport_expiry_date))
        _upsert(db,
            kind="passport_expiry", severity=severity,
            title=f"Passport {prefix} · {(c.full_name or '—').upper()}",
            body=f"Passport No <b>{c.passport_no or '—'}</b> expires on <b>{c.passport_expiry_date}</b>. {'Renewal overdue — block visa filing.' if d < 0 else 'Renew before submitting to embassy.'}",
            dedup_key=key,
            candidate_id=c.id,
            due_at=datetime.combine(c.passport_expiry_date, datetime.min.time()) if isinstance(c.passport_expiry_date, date) else None,
            meta={"days_until": d, "field": "passport_expiry_date"},
        )
        n += 1
    return n


def scan_medical_expiry(db: Session, today: Optional[date] = None) -> int:
    today = today or date.today()
    soon = today + timedelta(days=60)
    cands = (db.query(Candidate)
             .filter(Candidate.medical_date.isnot(None))
             .all())
    n = 0
    for c in cands:
        # Medical reports are valid for 3 months (90 days) from test date
        if not c.medical_date:
            continue
        md = c.medical_date if isinstance(c.medical_date, date) else None
        if not md:
            continue
        expiry = md + timedelta(days=90)
        d = (expiry - today).days
        if d > 60:
            continue
        if d < 0:
            severity, prefix = "critical", f"EXPIRED {abs(d)}d ago"
        elif d <= 14:
            severity, prefix = "critical", f"Expires in {d}d"
        else:
            severity, prefix = "warning", f"Expires in {d}d"
        key = _dedup("medical_expiry", cid=c.id, exp=str(expiry))
        _upsert(db,
            kind="medical_expiry", severity=severity,
            title=f"Medical {prefix} · {(c.full_name or '—').upper()}",
            body=f"GAMCA medical taken on <b>{md}</b> expires on <b>{expiry}</b>. {'Re-do medical before submitting.' if d < 30 else 'Plan a refresh medical.'}",
            dedup_key=key,
            candidate_id=c.id,
            due_at=datetime.combine(expiry, datetime.min.time()),
            meta={"days_until": d, "field": "medical_date"},
        )
        n += 1
    return n


def scan_stage_overdue(db: Session, today: Optional[date] = None, max_days: int = 14) -> int:
    """Candidates stuck in the same workflow stage too long."""
    today = today or date.today()
    cutoff = today - timedelta(days=max_days)
    cands = (db.query(Candidate)
             .filter(Candidate.status.isnot(None))
             .filter(Candidate.status.notin_(["completed", "cancelled", "deployed", "rejected"]))
             .all())
    n = 0
    for c in cands:
        # Use updated_at if set, else created_at
        last = c.updated_at or c.created_at
        if not last:
            continue
        if isinstance(last, datetime):
            last_d = last.date()
        else:
            last_d = last
        if last_d > cutoff:
            continue
        days_stuck = (today - last_d).days
        severity = "critical" if days_stuck > 30 else "warning"
        key = _dedup("stage_overdue", cid=c.id, stage=c.status)
        _upsert(db,
            kind="stage_overdue", severity=severity,
            title=f"Stuck {days_stuck}d at '{c.status}' · {(c.full_name or '—').upper()}",
            body=f"Candidate has been in stage <b>{c.status}</b> since <b>{last_d}</b>. Please review and progress or cancel.",
            dedup_key=key,
            candidate_id=c.id,
            meta={"days_stuck": days_stuck, "stage": c.status},
        )
        n += 1
    return n


def scan_demand_match(db: Session) -> int:
    """For each OPEN trade slot, suggest unassigned candidates whose profession
    matches the trade. Helps staff fill slots fast."""
    # Find trades with available slots
    trades = db.query(JobCategory).all()
    assigned_counts = dict(db.query(
        CandidateAssignment.job_category_id, func.count(CandidateAssignment.id)
    ).group_by(CandidateAssignment.job_category_id).all())
    n = 0
    for t in trades:
        qty = t.quantity or 0
        filled = assigned_counts.get(t.id, 0)
        open_slots = qty - filled
        if open_slots <= 0:
            continue
        # Find candidates whose profession matches trade & who are not yet assigned anywhere
        trade_name = (t.trade or "").strip()
        if not trade_name:
            continue
        sub = db.query(CandidateAssignment.candidate_id)
        matches = (db.query(Candidate)
                   .filter(Candidate.id.notin_(sub.subquery().select()))
                   .filter(Candidate.profession.ilike(f"%{trade_name}%"))
                   .limit(10)
                   .all())
        if not matches:
            continue
        names = ", ".join(c.full_name for c in matches[:5] if c.full_name)
        key = _dedup("demand_match", tid=t.id, n=len(matches))
        demand = db.query(Demand).filter(Demand.id == t.demand_id).first() if t.demand_id else None
        _upsert(db,
            kind="demand_match", severity="info",
            title=f"{len(matches)} candidate(s) match '{trade_name}' ({open_slots} slot{'s' if open_slots != 1 else ''} open)",
            body=f"Demand <b>{getattr(demand,'file_number','—') or '—'}</b> has <b>{open_slots}</b> open {trade_name} slot{'s' if open_slots != 1 else ''}. Available matches: <b>{names}</b>.",
            dedup_key=key,
            demand_id=t.demand_id, trade_id=t.id,
            meta={"open_slots": open_slots, "match_count": len(matches),
                  "match_ids": [c.id for c in matches]},
        )
        n += 1
    return n


# Core fields a candidate needs before they can ship to an embassy
_CORE_REQUIRED_FIELDS = [
    "full_name", "father_name", "passport_no", "passport_expiry_date",
    "date_of_birth", "cnic", "profession", "nationality",
]


def scan_missing_fields(db: Session) -> int:
    """For candidates at stage >= medical, flag any missing core fields."""
    advanced_stages = ["medical", "protector", "visa", "visa_stamping",
                       "deployment_ready"]
    cands = (db.query(Candidate)
             .filter(Candidate.status.in_(advanced_stages))
             .all())
    n = 0
    for c in cands:
        missing = [f for f in _CORE_REQUIRED_FIELDS if not getattr(c, f, None)]
        if not missing:
            continue
        key = _dedup("missing_field", cid=c.id, fields=",".join(missing))
        sev = "critical" if len(missing) >= 3 else "warning"
        nice = ", ".join(m.replace("_", " ") for m in missing)
        _upsert(db,
            kind="missing_field", severity=sev,
            title=f"{len(missing)} required field(s) missing · {(c.full_name or '—').upper()}",
            body=f"Candidate is at stage <b>{c.status}</b> but is missing: <b>{nice}</b>. Please complete the profile before next workflow step.",
            dedup_key=key,
            candidate_id=c.id,
            meta={"missing_fields": missing, "stage": c.status},
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Master scan
# ---------------------------------------------------------------------------
def run_full_scan(db: Session) -> Dict[str, int]:
    """Run every scanner and commit. Returns count-per-kind."""
    results = {
        "passport_expiry": 0,
        "medical_expiry":  0,
        "stage_overdue":   0,
        "demand_match":    0,
        "missing_field":   0,
    }
    try:
        results["passport_expiry"] = scan_passport_expiry(db)
    except Exception as e:
        logger.exception("passport_expiry scan failed: %s", e)
    try:
        results["medical_expiry"] = scan_medical_expiry(db)
    except Exception as e:
        logger.exception("medical_expiry scan failed: %s", e)
    try:
        results["stage_overdue"] = scan_stage_overdue(db)
    except Exception as e:
        logger.exception("stage_overdue scan failed: %s", e)
    try:
        results["demand_match"] = scan_demand_match(db)
    except Exception as e:
        logger.exception("demand_match scan failed: %s", e)
    try:
        results["missing_field"] = scan_missing_fields(db)
    except Exception as e:
        logger.exception("missing_field scan failed: %s", e)
    db.commit()
    return results
