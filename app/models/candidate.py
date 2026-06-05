from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Date, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class Candidate(Base):
    """A worker being recruited \u2014 fields match the DEMO OEP Candidate slide-in panel exactly."""
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, index=True)

    # ===== Personal Info (Step 1) =====
    full_name = Column(String(255), nullable=False, index=True)
    name_arabic = Column(String(255), default="")
    mother_name = Column(String(255), default="")
    father_name = Column(String(255), default="")
    father_name_arabic = Column(String(255), default="")
    gender = Column(String(10), default="Male")
    marital_status = Column(String(20), default="Single")
    religion = Column(String(30), default="Islam")
    date_of_birth = Column(Date, nullable=True)
    place_of_birth = Column(String(100), default="")
    place_of_birth_arabic = Column(String(100), default="")
    nationality = Column(String(50), default="PAKISTANI")
    address = Column(Text, default="")
    phone = Column(String(30), default="")
    tehsil = Column(String(100), default="")
    district = Column(String(100), default="")
    province = Column(String(100), default="")
    photo = Column(String(255), default="")

    # ===== Identification (Step 2) =====
    passport_no = Column(String(50), index=True, default="")
    passport_issue_date = Column(Date, nullable=True)
    passport_expiry_date = Column(Date, nullable=True)
    issuing_authority = Column(String(100), default="PAKISTAN")
    issuing_authority_arabic = Column(String(100), default="")
    passport_issue_place = Column(String(100), default="")
    cnic = Column(String(30), index=True, default="")
    nadra_token_no = Column(String(50), default="")

    # ===== Employment (Step 3) =====
    permission_no = Column(String(100), default="")
    permission_date = Column(Date, nullable=True)
    qualification = Column(String(150), default="")
    age_employee = Column(Integer, nullable=True)
    profession = Column(String(100), default="")  # job/profession
    salary = Column(Numeric(12, 2), default=0)

    # ===== Next of Kin (Step 4) =====
    next_of_kin_name = Column(String(255), default="")
    next_of_kin_nic = Column(String(30), default="")
    next_of_kin_relation = Column(String(50), default="")

    # ===== Protector / Medical / Travel (post-process) =====
    protector_no = Column(String(50), default="")
    protector_date = Column(Date, nullable=True)
    medical_center = Column(String(150), default="")
    gamca_number = Column(String(50), default="")
    medical_date = Column(Date, nullable=True)
    medical_consignment_no = Column(String(50), default="")
    medical_send_date = Column(Date, nullable=True)
    medical_courier_name = Column(String(100), default="")
    e_number = Column(String(50), default="")
    date_of_departure = Column(Date, nullable=True)
    flight_no = Column(String(50), default="")
    destination = Column(String(100), default="")
    ticket_no = Column(String(50), default="")
    visa_stamp_date = Column(Date, nullable=True)
    ticket_included = Column(String(10), default="No")
    accommodation_allowance = Column(String(50), default="")
    food_allowance = Column(String(50), default="")
    slot_notes = Column(Text, default="")
    price = Column(Numeric(12, 2), default=0)

    # ===== Common =====
    email = Column(String(150), default="")
    status = Column(String(30), default="pending")  # pending, documents_pending, processing, issued, deployed
    notes = Column(Text, default="")
    
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    assignments = relationship("CandidateAssignment", back_populates="candidate", cascade="all, delete-orphan")
    created_by = relationship("User", foreign_keys=[created_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


class CandidateAssignment(Base):
    """Link candidate to a specific trade (visa category) within a demand."""
    __tablename__ = "candidate_assignments"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    job_category_id = Column(Integer, ForeignKey("job_categories.id"), nullable=False)
    status = Column(String(30), default="pending")  # pending, documents_pending, processing, visa_issued, deployed
    embassy = Column(String(150), default="") # To persist the selected embassy choice for this assignment
    
    assigned_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_at = Column(DateTime, server_default=func.now())
    unassigned_at = Column(DateTime, nullable=True)
    notes = Column(Text, default="")

    candidate = relationship("Candidate", back_populates="assignments")
    job_category = relationship("JobCategory", back_populates="assignments")
    assigned_by = relationship("User", foreign_keys=[assigned_by_id])
