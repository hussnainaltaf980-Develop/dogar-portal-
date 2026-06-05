from sqlalchemy import Column, Integer, String, Text, DateTime, Date, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


class Client(Base):
    """Foreign sponsor / client company (full DEMO OEP profile fields)."""
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    # Basic
    company_name = Column(String(255), nullable=False, index=True)
    client_type = Column(String(30), default="Company")   # Company | Individual
    status = Column(String(30), default="active")          # active | inactive

    license_no = Column(String(100), default="")
    email = Column(String(150), default="")
    phone = Column(String(50), default="")

    # Sponsor (for demand-file pre-fill)
    sponsor_name = Column(String(255), default="")
    sponsor_name_arabic = Column(String(255), default="")
    sponsor_address = Column(Text, default="")
    sponsor_address_arabic = Column(Text, default="")
    sponsor_phone = Column(String(50), default="")
    sponsor_alt_phone = Column(String(50), default="")
    sponsor_id = Column(String(100), default="")

    # Address
    street = Column(Text, default="")
    city = Column(String(100), default="")
    state = Column(String(100), default="")
    country = Column(String(100), default="Pakistan")
    address = Column(Text, default="")     # legacy combined
    contact_person = Column(String(150), default="")

    # Opening balance
    opening_balance = Column(Numeric(14, 2), default=0)
    opening_balance_direction = Column(String(20), default="Owed to us")   # "Owed to us" | "We owe"
    opening_balance_date = Column(Date, nullable=True)

    notes = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    demands = relationship("Demand", back_populates="client", cascade="all, delete-orphan")
