"""Multi-tenant Copy Portal model.

Each Tenant is an independent portal instance for another business partner /
office / client. Per-tenant data isolation is achieved by giving each tenant
its own SQLite database file (`db_path`). Per-tenant feature flags are stored
as a JSON column so the admin can switch features (AI, document overlay,
role creation, settings, etc.) on/off without code changes.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON
from app.db.session import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)

    # Branding
    slug = Column(String(50), unique=True, nullable=False, index=True)   # e.g. "demo-oep"
    company_name = Column(String(200), nullable=False)                   # "DEMO OEP Pvt Ltd"
    short_name = Column(String(50), nullable=False)                      # "DEMO OEP"
    subtitle = Column(String(150), default="Overseas Employment Promoters")
    primary_color = Column(String(20), default="#2563eb")                # corporate blue
    logo_filename = Column(String(255), nullable=True)
    # Legacy column kept for older code paths that still reference logo_path
    logo_path = Column(String(255), nullable=True)

    # Enterprise branding (per-tenant office identity)
    office_name = Column(String(200), default="", nullable=True)         # e.g. "Lahore Office"
    letterhead_path = Column(String(255), default="", nullable=True)     # filename under data/tenants/<slug>/
    receipt_template = Column(Text, default="", nullable=True)           # custom receipt HTML / boilerplate
    demand_format = Column(String(50), default="", nullable=True)        # demand file number format e.g. "DEM-{YYYY}-{####}"

    # Data isolation — each tenant has its OWN SQLite database file
    db_path = Column(String(500), nullable=False)                        # ./data/tenant_<slug>.db

    # Per-tenant RBAC feature flags (admin can hide capabilities)
    # Example: {"ai_chatbot": false, "document_overlay": true, "settings": true,
    #           "role_creation": false, "agents": true, "reports": true, "documents": true}
    features = Column(JSON, default=dict, nullable=False)

    # Owner / partner info
    contact_name = Column(String(150))
    contact_email = Column(String(150))
    contact_phone = Column(String(50))

    # Status
    status = Column(String(20), default="active")          # active|suspended|provisioning|archived
    plan = Column(String(30), default="standard")          # standard|pro|enterprise

    # Default admin credentials for the new tenant (one-time seeded)
    admin_email = Column(String(150))
    admin_password_set = Column(Boolean, default=False)

    notes = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Feature defaults — used when features={} (newly provisioned)
    @staticmethod
    def default_features() -> dict:
        return {
            # Core (cannot be turned off via UI, but listed for completeness)
            "dashboard": True,
            "clients": True,
            "demands": True,
            "candidates": True,
            # Toggleable
            "documents": True,           # Document templates page
            "document_overlay": True,    # Document Coordinate engine (canvas designer)
            "ai_chatbot": True,          # DtcBot floating widget
            "ai_ocr": True,              # Passport OCR
            "agents": True,              # Sub-Agents legacy page
            "reports": True,             # Reports page
            "settings": True,            # Settings menu (in addition to admin-only RBAC)
            "role_creation": True,       # Roles management page
            "login_history": True,       # Login history page
            "depositors": True,          # Depositors page
            "service_charges": True,     # Service charges page
        }
