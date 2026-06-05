from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List

from app.db.session import get_db
from app.core.deps import get_current_user
from app.models import Agent, AgentCash, User
from app.schemas.schemas import (
    AgentCreate, AgentUpdate, AgentOut,
    AgentCashCreate, AgentCashOut,
)

router = APIRouter()


def _compute_balance(db: Session, agent_id: int) -> float:
    """Balance = SUM(debit) - SUM(credit). 
    In legacy data semantics: debit = amount owed by agent (ticket booked),
    credit = amount paid in. Balance > 0 means agent owes us.
    """
    total_debit = db.query(func.coalesce(func.sum(AgentCash.debit), 0)).filter(AgentCash.agent_id == agent_id).scalar() or 0
    total_credit = db.query(func.coalesce(func.sum(AgentCash.credit), 0)).filter(AgentCash.agent_id == agent_id).scalar() or 0
    return float(total_debit) - float(total_credit)


@router.get("/", response_model=List[AgentOut])
def list_agents(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    agents = db.query(Agent).order_by(Agent.id.desc()).all()
    result = []
    for a in agents:
        out = AgentOut.model_validate(a)
        out.balance = _compute_balance(db, a.id)
        result.append(out)
    return result


@router.post("/", response_model=AgentOut)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = Agent(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    out = AgentOut.model_validate(obj)
    out.balance = 0
    return out


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Agent).filter(Agent.id == agent_id).first()
    if not obj:
        raise HTTPException(404, "Agent not found")
    out = AgentOut.model_validate(obj)
    out.balance = _compute_balance(db, agent_id)
    return out


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: int, payload: AgentUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Agent).filter(Agent.id == agent_id).first()
    if not obj:
        raise HTTPException(404, "Agent not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    out = AgentOut.model_validate(obj)
    out.balance = _compute_balance(db, agent_id)
    return out


@router.delete("/{agent_id}")
def delete_agent(agent_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(Agent).filter(Agent.id == agent_id).first()
    if not obj:
        raise HTTPException(404, "Agent not found")
    db.delete(obj)
    db.commit()
    return {"ok": True}


# Cash book endpoints
@router.get("/{agent_id}/cashbook", response_model=List[AgentCashOut])
def agent_cashbook(agent_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(AgentCash).filter(AgentCash.agent_id == agent_id).order_by(AgentCash.datetime.desc(), AgentCash.id.desc()).all()


@router.post("/cashbook", response_model=AgentCashOut)
def add_cash_entry(payload: AgentCashCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = payload.model_dump()
    obj = AgentCash(**data, user_id=user.id)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/cashbook/{entry_id}")
def delete_cash_entry(entry_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(AgentCash).filter(AgentCash.id == entry_id).first()
    if not obj:
        raise HTTPException(404, "Entry not found")
    db.delete(obj)
    db.commit()
    return {"ok": True}
