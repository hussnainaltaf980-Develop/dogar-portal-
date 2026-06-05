"""Audit log + Candidate wizard state persistence."""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class AuditLog(Base):
    """Immutable record of state-changing events.

    Every assign / unassign / stage-change / wizard-finalize / delete writes
    a row here so we have a permanent trail of *who did what when*.
    """
    __tablename__ = "audit_logs"

    id            = Column(Integer, primary_key=True, index=True)
    entity_type   = Column(String(40), nullable=False, index=True)  # AuditEntity
    entity_id     = Column(Integer, nullable=True, index=True)
    action        = Column(String(40), nullable=False, index=True)  # AuditAction
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_name    = Column(String(150), default="")
    summary       = Column(String(500), default="")
    before_json   = Column(JSON, nullable=True)
    after_json    = Column(JSON, nullable=True)
    ip_address    = Column(String(64), default="")
    user_agent    = Column(String(255), default="")
    occurred_at   = Column(DateTime, server_default=func.now(), index=True)

    actor = relationship("User", foreign_keys=[actor_user_id])

    __table_args__ = (
        Index("ix_audit_entity_time", "entity_type", "entity_id", "occurred_at"),
    )


class CandidateWizardState(Base):
    """Persistent draft state for the 5-step candidate wizard.

    Created the first time a user opens the wizard (POST /api/candidates/wizard).
    Each PATCH /api/candidates/wizard/{id}/step/{n} writes the partial payload
    into step_data[n] and bumps current_step so the user can close the
    drawer and resume later.

    When the wizard is finalised, candidate_id is populated and
    is_finalized = 1. Reopening a finalised wizard clears
    is_finalized and lets the user edit any step.
    """
    __tablename__ = "candidate_wizard_states"

    id              = Column(Integer, primary_key=True, index=True)
    candidate_id    = Column(Integer, ForeignKey("candidates.id"), nullable=True, index=True)
    job_category_id = Column(Integer, ForeignKey("job_categories.id"), nullable=True)

    current_step    = Column(Integer, default=1)
    total_steps     = Column(Integer, default=5)
    is_finalized    = Column(Integer, default=0)

    step_data       = Column(JSON, default=dict)

    created_by_id   = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())
    finalized_at    = Column(DateTime, nullable=True)

    candidate     = relationship("Candidate", foreign_keys=[candidate_id])
    job_category  = relationship("JobCategory", foreign_keys=[job_category_id])
    created_by    = relationship("User", foreign_keys=[created_by_id])


class CandidateDocumentChecklist(Base):
    """Per-candidate document checklist state."""
    __tablename__ = "candidate_document_checklist"

    id            = Column(Integer, primary_key=True, index=True)
    candidate_id  = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)
    document_key  = Column(String(50), nullable=False)
    received      = Column(Integer, default=0)
    received_at   = Column(DateTime, nullable=True)
    received_by   = Column(String(150), default="")
    notes         = Column(Text, default="")

    candidate = relationship("Candidate")

    __table_args__ = (
        Index("ix_doc_checklist_cand_key", "candidate_id", "document_key", unique=True),
    )
