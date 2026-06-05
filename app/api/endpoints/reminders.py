"""Reminders & Alerts API (v8 smart features).

Endpoints:
  GET    /api/reminders              — list (filterable by kind/severity/state)
  GET    /api/reminders/summary      — counts by kind + critical badge
  POST   /api/reminders              — create manual reminder
  POST   /api/reminders/scan         — kick off the smart scan engine
  POST   /api/reminders/{id}/read    — mark as read
  POST   /api/reminders/{id}/dismiss — dismiss (won't regenerate)
  POST   /api/reminders/dismiss-all  — bulk dismiss by kind
  DELETE /api/reminders/{id}         — hard delete (admin only)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from app.core.deps import get_current_user
from app.core.tenancy import get_tenant_db as get_db  # tenant-scoped session (falls back to control DB when no tenant)
from app.models import User
from app.models.reminders import Reminder
from app.services import alerts_engine

router = APIRouter()


@router.get("")
def list_reminders(
    kind: Optional[str] = Query(None, description="Filter by kind (passport_expiry, ...)"),
    severity: Optional[str] = Query(None, description="info | warning | critical"),
    state: str = Query("active", description="active | read | dismissed | all"),
    candidate_id: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(Reminder)
    if kind:         q = q.filter(Reminder.kind == kind)
    if severity:     q = q.filter(Reminder.severity == severity)
    if candidate_id: q = q.filter(Reminder.candidate_id == candidate_id)
    if state == "active":
        q = q.filter(Reminder.is_dismissed == False)  # noqa: E712
    elif state == "read":
        q = q.filter(Reminder.is_read == True, Reminder.is_dismissed == False)  # noqa: E712
    elif state == "dismissed":
        q = q.filter(Reminder.is_dismissed == True)  # noqa: E712
    # severity sort: critical > warning > info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    rows = q.order_by(desc(Reminder.created_at)).limit(limit).all()
    rows.sort(key=lambda r: (severity_order.get(r.severity, 3), -(r.id or 0)))
    return {"items": [r.to_dict() for r in rows], "total": len(rows)}


@router.get("/summary")
def reminder_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Counts by kind + severity for dashboard bell."""
    rows = (db.query(Reminder.kind, Reminder.severity)
            .filter(Reminder.is_dismissed == False)  # noqa: E712
            .all())
    by_kind = {}
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    for kind, sev in rows:
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if sev in by_severity:
            by_severity[sev] += 1
    unread = (db.query(Reminder)
              .filter(Reminder.is_dismissed == False, Reminder.is_read == False)  # noqa: E712
              .count())
    return {
        "total_active": sum(by_kind.values()),
        "unread":       unread,
        "by_kind":      by_kind,
        "by_severity":  by_severity,
    }


class ManualReminderPayload(BaseModel):
    title: str
    body: Optional[str] = ""
    severity: Optional[str] = "info"
    candidate_id: Optional[int] = None
    demand_id: Optional[int] = None
    due_at: Optional[datetime] = None


@router.post("")
def create_manual(payload: ManualReminderPayload,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = Reminder(
        kind="manual",
        severity=payload.severity or "info",
        title=payload.title.strip()[:255],
        body=payload.body or "",
        candidate_id=payload.candidate_id,
        demand_id=payload.demand_id,
        due_at=payload.due_at,
        assigned_to_id=user.id,
    )
    db.add(r); db.commit(); db.refresh(r)
    return r.to_dict()


@router.post("/scan")
def trigger_scan(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Run the smart alerts engine — generates / refreshes all auto alerts."""
    results = alerts_engine.run_full_scan(db)
    return {"ok": True, "results": results}


@router.post("/{rid}/read")
def mark_read(rid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(Reminder).filter(Reminder.id == rid).first()
    if not r:
        raise HTTPException(404, "Reminder not found")
    r.is_read = True
    r.read_at = datetime.now(timezone.utc)
    db.commit()
    return r.to_dict()


@router.post("/{rid}/dismiss")
def dismiss(rid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(Reminder).filter(Reminder.id == rid).first()
    if not r:
        raise HTTPException(404, "Reminder not found")
    r.is_dismissed = True
    r.dismissed_at = datetime.now(timezone.utc)
    if not r.is_read:
        r.is_read = True
        r.read_at = r.dismissed_at
    db.commit()
    return r.to_dict()


@router.post("/read-all")
def mark_all_read(kind: Optional[str] = None,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(Reminder).filter(Reminder.is_dismissed == False, Reminder.is_read == False)  # noqa: E712
    if kind:
        q = q.filter(Reminder.kind == kind)
    now = datetime.now(timezone.utc)
    count = 0
    for r in q.all():
        r.is_read = True
        r.read_at = now
        count += 1
    db.commit()
    return {"ok": True, "updated": count}


@router.post("/dismiss-all")
def dismiss_all(kind: Optional[str] = None,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(Reminder).filter(Reminder.is_dismissed == False)  # noqa: E712
    if kind:
        q = q.filter(Reminder.kind == kind)
    now = datetime.now(timezone.utc)
    count = 0
    for r in q.all():
        r.is_dismissed = True
        r.dismissed_at = now
        if not r.is_read:
            r.is_read = True
            r.read_at = now
        count += 1
    db.commit()
    return {"ok": True, "dismissed": count}


@router.delete("/{rid}")
def delete_reminder(rid: int,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if (user.role or "").lower() not in ("admin", "superadmin"):
        raise HTTPException(403, "Admin only")
    r = db.query(Reminder).filter(Reminder.id == rid).first()
    if not r:
        raise HTTPException(404, "Reminder not found")
    db.delete(r)
    db.commit()
    return {"ok": True}
