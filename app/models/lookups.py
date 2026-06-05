"""Lookup tables for the sidebar pages: Visa Categories, Embassies, Cities,
Medical Centers, Contacts, Depositors, Service Charges, Roles, Login History,
Client Contacts, Client Statement, Company Settings.
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Date, Boolean, Numeric, ForeignKey
from sqlalchemy.sql import func
from app.db.session import Base


class VisaCategory(Base):
    __tablename__ = "visa_categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    code = Column(String(50), default="")
    description = Column(Text, default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Embassy(Base):
    __tablename__ = "embassies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    country = Column(String(100), default="")
    city = Column(String(100), default="")
    address = Column(Text, default="")
    phone = Column(String(50), default="")
    created_at = Column(DateTime, server_default=func.now())


class City(Base):
    __tablename__ = "cities"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    province = Column(String(100), default="")
    country = Column(String(100), default="Pakistan")
    created_at = Column(DateTime, server_default=func.now())


class MedicalCenter(Base):
    __tablename__ = "medical_centers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    city = Column(String(100), default="")
    address = Column(Text, default="")
    phone = Column(String(50), default="")
    created_at = Column(DateTime, server_default=func.now())


class Contact(Base):
    """Generic address-book contacts (NOT client-contacts which live on the Client tab)."""
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    company = Column(String(150), default="")
    email = Column(String(150), default="")
    phone = Column(String(50), default="")
    note = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


class ClientContact(Base):
    """Contacts that belong TO a specific client (Client > Contacts tab)."""
    __tablename__ = "client_contacts"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    name = Column(String(150), nullable=False)
    designation = Column(String(100), default="")
    email = Column(String(150), default="")
    phone = Column(String(50), default="")
    is_primary = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class ClientStatement(Base):
    """Client > Statement tab \u2014 each row is an INVOICE or PAYMENT entry.

    A payment can optionally be tied to a Demand File (``demand_id``) so the
    payment shows up in both: a) the client's statement, b) the demand's
    Payments tab. ``payment_method`` and ``receipt_no`` enable receipt-print.
    """
    __tablename__ = "client_statements"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    demand_id = Column(Integer, ForeignKey("demands.id"), nullable=True, index=True)
    entry_type = Column(String(20), default="INVOICE")   # INVOICE | PAYMENT
    reference = Column(String(100), default="")
    description = Column(Text, default="")
    debit = Column(Numeric(14, 2), default=0)
    credit = Column(Numeric(14, 2), default=0)
    payment_method = Column(String(40), default="")  # Cash | Bank Transfer | Cheque | Online
    receipt_no = Column(String(50), default="")
    received_by = Column(String(150), default="")
    entry_date = Column(Date, nullable=False, server_default=func.current_date())
    created_at = Column(DateTime, server_default=func.now())


class Depositor(Base):
    __tablename__ = "depositors"
    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), default="")
    full_name = Column(String(200), default="")
    cnic = Column(String(30), default="")
    mobile = Column(String(50), default="")
    address = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


class ServiceCharge(Base):
    __tablename__ = "service_charges"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    amount = Column(Numeric(12, 2), default=0)
    description = Column(Text, default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Role(Base):
    """Roles & Permissions (Users > Roles tab)."""
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(Text, default="")
    is_system = Column(Boolean, default=False)
    permissions = Column(Text, default="[]")  # JSON list of permission keys
    created_at = Column(DateTime, server_default=func.now())


class LoginHistory(Base):
    """Users > Login history tab \u2014 successful + failed sign-in events."""
    __tablename__ = "login_history"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(150), nullable=False, index=True)
    status = Column(String(20), default="Success")   # Success | Failed
    ip_address = Column(String(64), default="")
    user_agent = Column(Text, default="")
    occurred_at = Column(DateTime, server_default=func.now(), index=True)


class CompanySettings(Base):
    """Settings > General \u2014 single-row company information."""
    __tablename__ = "company_settings"
    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String(200), default="Dogar Trading Corporation")
    company_name_arabic = Column(String(200), default="")
    oep_license_number = Column(String(50), default="1338/SKT")
    owner_name = Column(String(150), default="")
    address = Column(Text, default="")
    address_arabic = Column(Text, default="")
    phone = Column(String(50), default="")
    mobile = Column(String(50), default="")
    fax = Column(String(50), default="")
    email = Column(String(150), default="")
    website = Column(String(150), default="")
    subdomain = Column(String(50), default="dogar")
    slug = Column(String(50), default="dogar")
    # File number prefix for demand files.
    # Convention: file_prefix + sequential_integer = "DTC/786/8186".
    # The legacy MySQL DB stored raw integers ("5983", "6022", "8185"); we
    # preserve that storage format in the demands table and prefix only on
    # display. `starting_point` lets a tenant skip ahead (e.g. start at 8001)
    # but is ignored if existing rows already exceed it.
    file_prefix = Column(String(20), default="DTC/786/")
    starting_point = Column(Integer, default=0)
    status = Column(String(20), default="active")
    plan = Column(String(100), default="OEP Yearly")
    expiry_date = Column(Date, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PasswordResetToken(Base):
    """One-time password reset token. Expires after `expires_at`."""
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(150), nullable=False, index=True)
    token = Column(String(80), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class SupportMessage(Base):
    """Submission from Contact Support page."""
    __tablename__ = "support_messages"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), default="")
    email = Column(String(150), default="")
    phone = Column(String(50), default="")
    subject = Column(String(255), default="")
    message = Column(Text, default="")
    status = Column(String(20), default="open")  # open / resolved
    created_at = Column(DateTime, server_default=func.now())
