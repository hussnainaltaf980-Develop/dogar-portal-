from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.tenancy import get_tenant_db as get_db  # tenant-scoped session (falls back to control DB when no tenant)
from app.core.deps import get_current_user
from app.models import Client, Demand, Candidate, Agent, AgentCash, JobCategory, User

router = APIRouter()


@router.get("/stats")
def dashboard_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    total_clients = db.query(func.count(Client.id)).scalar() or 0
    total_demands = db.query(func.count(Demand.id)).scalar() or 0
    total_candidates = db.query(func.count(Candidate.id)).scalar() or 0
    total_agents = db.query(func.count(Agent.id)).scalar() or 0

    total_visa_quota = db.query(func.coalesce(func.sum(JobCategory.quantity), 0)).scalar() or 0

    # Count candidates by canonical stages (includes aliases: new/pending both count as pending)
    from sqlalchemy import or_
    pending = db.query(func.count(Candidate.id)).filter(
        or_(Candidate.status == "pending", Candidate.status == "new", Candidate.status == "docs_pending")
    ).scalar() or 0
    processing = db.query(func.count(Candidate.id)).filter(
        or_(Candidate.status == "processing", Candidate.status == "medical", Candidate.status == "interviewed", Candidate.status == "protector")
    ).scalar() or 0
    issued = db.query(func.count(Candidate.id)).filter(
        or_(Candidate.status == "issued", Candidate.status == "visa_issued", Candidate.status == "stamped")
    ).scalar() or 0
    deployed = db.query(func.count(Candidate.id)).filter(
        or_(Candidate.status == "deployed", Candidate.status == "completed")
    ).scalar() or 0

    total_debit = db.query(func.coalesce(func.sum(AgentCash.debit), 0)).scalar() or 0
    total_credit = db.query(func.coalesce(func.sum(AgentCash.credit), 0)).scalar() or 0

    # Top trades
    top_trades_rows = (
        db.query(JobCategory.trade, func.sum(JobCategory.quantity).label("qty"))
        .group_by(JobCategory.trade)
        .order_by(func.sum(JobCategory.quantity).desc())
        .limit(7)
        .all()
    )
    top_trades = [{"trade": t, "qty": int(q or 0)} for t, q in top_trades_rows]

    # Demands by status
    demands_status_rows = (
        db.query(Demand.status, func.count(Demand.id))
        .group_by(Demand.status)
        .all()
    )
    demands_status = {s or "unknown": int(c) for s, c in demands_status_rows}

    return {
        "totals": {
            "clients": total_clients,
            "demands": total_demands,
            "candidates": total_candidates,
            "agents": total_agents,
            "visa_quota": int(total_visa_quota),
        },
        "candidates_status": {
            "pending": pending,
            "processing": processing,
            "issued": issued,
            "deployed": deployed,
        },
        "finance": {
            "total_debit": float(total_debit),
            "total_credit": float(total_credit),
            "net": float(total_debit) - float(total_credit),
        },
        "top_trades": top_trades,
        "demands_status": demands_status,
    }
