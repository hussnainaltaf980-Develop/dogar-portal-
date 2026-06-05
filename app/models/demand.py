from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Date, Numeric, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class Demand(Base):
    """Demand File \u2014 mirrors DEMO OEP \"Demand File DEMO-01001\" detail page."""
    __tablename__ = "demands"

    id = Column(Integer, primary_key=True, index=True)
    # File number (display): DEMO-01001 etc. Stored as full string.
    file_number = Column(String(50), unique=True, index=True, nullable=False)
    # Legacy field kept for migration compatibility (alias of file_number)
    demand_code = Column(String(50), index=True, default="")
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)

    # Basic Information
    receiving_date = Column(Date, nullable=True)
    permission_no = Column(String(100), default="")
    permission_date = Column(Date, nullable=True)
    reference = Column(String(150), default="")

    # Sponsor Information
    sponsor_name = Column(String(255), default="")
    sponsor_name_arabic = Column(String(255), default="")
    sponsor_address = Column(Text, default="")
    sponsor_address_arabic = Column(Text, default="")
    sponsor_phone = Column(String(50), default="")
    sponsor_alt_phone = Column(String(50), default="")

    # Visa Information
    visa_number = Column(String(100), default="")
    bataka_number = Column(String(100), default="")
    visa_issue_date = Column(Date, nullable=True)
    visa_issue_date_hijri = Column(String(50), default="")
    country = Column(String(100), default="Saudi Arabia")
    embassy = Column(String(150), default="")
    document_count = Column(Integer, default=0)

    # Legacy / unused fields kept for backward compat
    expiry_date = Column(DateTime, nullable=True)
    visa_quota = Column(Integer, default=0)
    benefits = Column(Text, default="")

    # Tracking fields
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    status = Column(String(30), default="active")  # active, processing, filled, expired, cancelled
    notes = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    client = relationship("Client", back_populates="demands")
    job_categories = relationship("JobCategory", back_populates="demand", cascade="all, delete-orphan")
    created_by = relationship("User", foreign_keys=[created_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


class JobCategory(Base):
    """Trade / Profession entries within a demand (the \"Trades\" tab)."""
    __tablename__ = "job_categories"

    id = Column(Integer, primary_key=True, index=True)
    demand_id = Column(Integer, ForeignKey("demands.id"), nullable=False)
    trade = Column(String(150), nullable=False)
    quantity = Column(Integer, default=1)
    salary = Column(Float, default=0)
    salary_currency = Column(String(10), default="SAR")
    contract_years = Column(Integer, default=2)
    notes = Column(Text, default="")
    # Custom key-value pairs used by the data-overlay engine to merge into
    # document templates that need extra fields beyond the standard schema.
    custom_fields = Column(JSON, default=dict)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    demand = relationship("Demand", back_populates="job_categories")
    assignments = relationship("CandidateAssignment", back_populates="job_category", cascade="all, delete-orphan")
    created_by = relationship("User", foreign_keys=[created_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])
