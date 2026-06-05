from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class Agent(Base):
    """Sub-agent / Travel agency.
    Migrated from legacy `agents` table.
    """
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    company_name = Column(String(255), default="")
    phone = Column(String(50), default="")
    mobile = Column(String(50), default="")
    email = Column(String(150), default="")
    address = Column(Text, default="")
    city = Column(String(100), default="")
    status = Column(String(30), default="active")
    notes = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    cash_entries = relationship("AgentCash", back_populates="agent", cascade="all, delete-orphan")


class AgentCash(Base):
    """Agent cash ledger - migrated from legacy `agents_cash` table."""
    __tablename__ = "agent_cash"

    id = Column(Integer, primary_key=True, index=True)
    datetime = Column(DateTime, server_default=func.now())
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    details = Column(String(500), default="")
    debit = Column(Float, default=0)
    credit = Column(Float, default=0)
    method = Column(String(30), default="cash")  # cash, cheque, transfer
    ref_id = Column(String(100), default="")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    agent = relationship("Agent", back_populates="cash_entries")
