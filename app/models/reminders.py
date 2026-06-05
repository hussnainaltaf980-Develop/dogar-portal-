"""Reminder / Notification / Alert model (v8 smart features).

A single polymorphic table powers:
  - Expiry alerts   (passport, visa, medical, permission)
  - Overdue alerts  (workflow stages stuck > N days)
  - Demand-match suggestions (candidates matching open trades)
  - Missing-field warnings (AI prompts staff about incomplete profiles)
  - Manual reminders (staff can create their own)

Reminders are NOT pushed to email/SMS — they live in the in-app
notification bell. Each user marks them as `read` or `dismissed`.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.session import Base


class Reminder(Base):
    __tablename__ = "reminders"

    id          = Column(Integer, primary_key=True, index=True)
    # Logical category: 'expiry', 'overdue', 'demand_match', 'missing_field', 'manual'
    kind        = Column(String(32), nullable=False, index=True)
    # Severity drives UI color: 'info' | 'warning' | 'critical'
    severity    = Column(String(16), nullable=False, default="info", index=True)
    # Short headline + longer body (markdown safe)
    title       = Column(String(255), nullable=False)
    body        = Column(Text, default="")

    # Optional anchor links — clicking the reminder jumps to the entity
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=True, index=True)
    demand_id    = Column(Integer, ForeignKey("demands.id",    ondelete="CASCADE"), nullable=True, index=True)
    trade_id     = Column(Integer, ForeignKey("job_categories.id", ondelete="CASCADE"), nullable=True, index=True)
    client_id    = Column(Integer, ForeignKey("clients.id",    ondelete="CASCADE"), nullable=True, index=True)

    # Optional staff-assignment (None = anyone can see / handle)
    assigned_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # State
    is_read     = Column(Boolean, default=False, nullable=False, index=True)
    is_dismissed = Column(Boolean, default=False, nullable=False, index=True)

    # When the reminder should *fire* (e.g. passport expires on X — alert N days before)
    due_at      = Column(DateTime, nullable=True, index=True)
    created_at  = Column(DateTime, default=func.now(), nullable=False)
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())
    read_at     = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)

    # Deduplication signature — generated alerts hash on
    # `{kind}:{candidate_id}:{trade_id}:{due_at}` so the rescan job doesn't
    # spam duplicates on every run.
    dedup_key   = Column(String(128), nullable=True, index=True, unique=False)

    # Optional small JSON-encoded metadata payload (eg. {days_until: 3, field: 'passport'})
    meta_json   = Column(Text, default="")

    # Relationships (lazy — only loaded when needed)
    candidate = relationship("Candidate", lazy="joined")
    demand    = relationship("Demand", lazy="joined", foreign_keys=[demand_id])
    trade     = relationship("JobCategory", lazy="joined", foreign_keys=[trade_id])
    client    = relationship("Client", lazy="joined", foreign_keys=[client_id])
    assigned_to = relationship("User", lazy="joined", foreign_keys=[assigned_to_id])

    __table_args__ = (
        Index("ix_reminders_kind_dismissed", "kind", "is_dismissed"),
        Index("ix_reminders_due_dismissed", "due_at", "is_dismissed"),
    )

    def to_dict(self) -> dict:
        return {
            "id":             self.id,
            "kind":           self.kind,
            "severity":       self.severity,
            "title":          self.title,
            "body":           self.body,
            "candidate_id":   self.candidate_id,
            "candidate_name": getattr(self.candidate, "full_name", None) if self.candidate_id else None,
            "demand_id":      self.demand_id,
            "demand_file_no": getattr(self.demand, "file_number", None) if self.demand_id else None,
            "trade_id":       self.trade_id,
            "trade_name":     getattr(self.trade, "trade", None) if self.trade_id else None,
            "client_id":      self.client_id,
            "client_name":    getattr(self.client, "name", None) if self.client_id else None,
            "assigned_to_id": self.assigned_to_id,
            "is_read":        bool(self.is_read),
            "is_dismissed":   bool(self.is_dismissed),
            "due_at":         self.due_at.isoformat() if self.due_at else None,
            "created_at":     self.created_at.isoformat() if self.created_at else None,
            "read_at":        self.read_at.isoformat() if self.read_at else None,
            "dismissed_at":   self.dismissed_at.isoformat() if self.dismissed_at else None,
            "meta_json":      self.meta_json or "",
            "dedup_key":      self.dedup_key,
        }
