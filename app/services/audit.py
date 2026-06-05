"""Audit logging service - emits AuditLog rows for state-changing events."""
from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from fastapi import Request

from app.models import AuditLog, User
from app.core.workflow import AuditEntity, AuditAction


def _json_safe(v: Any) -> Any:
    """Coerce a single value to something the stdlib json module can dump.

    SQLAlchemy's ``JSON`` column calls ``json.dumps`` which by default
    chokes on ``datetime``, ``date``, ``Decimal``, ``bytes`` and ORM
    instances. We normalise those here so audit-log writes never crash
    the originating request (the v5 → v6 regression was a 500 on stage
    update caused by a ``Decimal`` salary).
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        # Preserve fractional precision but stay JSON-friendly.
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return "<binary>"
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, (list, tuple, set)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


def _row_to_dict(obj) -> Dict[str, Any]:
    """Best-effort SQLAlchemy row -> dict snapshot."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    out = {}
    try:
        for col in obj.__table__.columns:
            v = getattr(obj, col.name, None)
            out[col.name] = _json_safe(v)
    except (AttributeError, TypeError) as exc:
        # ORM row missing __table__ or column getter raised — log and
        # return whatever we collected so far. Audit logging must never
        # break the request that triggered it.
        import logging
        logging.getLogger("dtc.audit").exception("audit._row_to_dict failed: %s", exc)
    return out


def log_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    actor: Optional[User] = None,
    summary: str = "",
    before: Any = None,
    after: Any = None,
    request: Optional[Request] = None,
    commit: bool = True,
) -> AuditLog:
    """Write a single audit log row."""
    ip = ""
    ua = ""
    if request is not None:
        try:
            ip = (request.client.host if request.client else "") or ""
            ua = (request.headers.get("user-agent") or "")[:255]
        except (AttributeError, KeyError) as exc:
            # Some test clients have no request.client — keep audit best-effort.
            import logging
            logging.getLogger("dtc.audit").debug("audit: could not read request meta: %s", exc)

    if before is None:
        before_json = None
    elif isinstance(before, (dict, list)):
        before_json = _json_safe(before)
    else:
        before_json = _row_to_dict(before)

    if after is None:
        after_json = None
    elif isinstance(after, (dict, list)):
        after_json = _json_safe(after)
    else:
        after_json = _row_to_dict(after)

    entry = AuditLog(
        entity_type=str(entity_type),
        entity_id=entity_id,
        action=str(action),
        actor_user_id=actor.id if actor else None,
        actor_name=(actor.name if actor else "system")[:150],
        summary=summary[:500],
        before_json=before_json,
        after_json=after_json,
        ip_address=ip[:64],
        user_agent=ua,
    )
    try:
        db.add(entry)
        if commit:
            db.commit()
            db.refresh(entry)
    except Exception as exc:
        # Last line of defence: audit writes MUST NEVER break the
        # originating request. If JSON serialisation or DB constraints
        # blow up, we roll back the audit row and log a warning so the
        # business operation (stage change, update, etc.) can succeed.
        import logging
        logging.getLogger("dtc.audit").exception(
            "audit.log_event swallowed exception: %s", exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 — rollback failures are non-recoverable
            pass
        return entry
    return entry


# ---- Convenience wrappers -------------------------------------------------
def log_assign(db, *, candidate, job_category, actor, request=None):
    return log_event(
        db,
        entity_type=AuditEntity.ASSIGNMENT.value,
        entity_id=candidate.id,
        action=AuditAction.ASSIGN.value,
        actor=actor, request=request,
        summary=f"Assigned candidate '{candidate.full_name}' to trade '{job_category.trade}' (demand #{job_category.demand_id})",
        before=None,
        after={
            "candidate_id": candidate.id,
            "candidate_name": candidate.full_name,
            "job_category_id": job_category.id,
            "trade": job_category.trade,
            "demand_id": job_category.demand_id,
        },
    )


def log_unassign(db, *, candidate, job_category, assignment_snapshot, cleared_fields, actor, request=None):
    return log_event(
        db,
        entity_type=AuditEntity.ASSIGNMENT.value,
        entity_id=candidate.id,
        action=AuditAction.UNASSIGN.value,
        actor=actor, request=request,
        summary=f"Unassigned candidate '{candidate.full_name}' from trade '{job_category.trade if job_category else ''}'; cleared {len(cleared_fields)} derived field(s)",
        before=assignment_snapshot,
        after={
            "candidate_id": candidate.id,
            "candidate_status": candidate.status,
            "cleared_fields": cleared_fields,
        },
    )


def log_stage_change(db, *, candidate, old_stage, new_stage, actor, request=None, reason=""):
    return log_event(
        db,
        entity_type=AuditEntity.CANDIDATE.value,
        entity_id=candidate.id,
        action=AuditAction.STAGE_CHANGE.value,
        actor=actor, request=request,
        summary=f"Stage: {old_stage} -> {new_stage}" + (f" ({reason})" if reason else ""),
        before={"status": old_stage},
        after={"status": new_stage, "reason": reason},
    )


def log_wizard_step(db, *, wizard_state, step, actor, request=None):
    return log_event(
        db,
        entity_type=AuditEntity.WIZARD_STATE.value,
        entity_id=wizard_state.id,
        action=AuditAction.WIZARD_STEP.value,
        actor=actor, request=request,
        summary=f"Saved wizard step {step}/{wizard_state.total_steps}",
        after={"step": step, "current_step": wizard_state.current_step},
    )


def log_wizard_finalize(db, *, wizard_state, candidate, actor, request=None):
    return log_event(
        db,
        entity_type=AuditEntity.WIZARD_STATE.value,
        entity_id=wizard_state.id,
        action=AuditAction.WIZARD_FINALIZE.value,
        actor=actor, request=request,
        summary=f"Finalized wizard -> candidate #{candidate.id} '{candidate.full_name}'",
        after={"candidate_id": candidate.id, "candidate_name": candidate.full_name},
    )


def log_wizard_reopen(db, *, wizard_state, actor, request=None):
    return log_event(
        db,
        entity_type=AuditEntity.WIZARD_STATE.value,
        entity_id=wizard_state.id,
        action=AuditAction.WIZARD_REOPEN.value,
        actor=actor, request=request,
        summary=f"Reopened wizard for candidate #{wizard_state.candidate_id}",
    )
