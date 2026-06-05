from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="staff")  # admin, manager, staff, agent
    is_active = Column(Boolean, default=True)
    phone = Column(String(30), default="")
    photo = Column(String(255), default="")          # profile picture URL
    designation = Column(String(120), default="")     # e.g. "Recruitment Officer"
    bio = Column(String(500), default="")             # short bio / notes
    theme = Column(String(50), default="corporate-blue") # corporate-blue or forest-green
    # When True the user MUST change their password before they can use the
    # app. Set automatically for the bootstrap admin so the documented
    # default credentials cannot be left untouched in production.
    must_change_password = Column(Boolean, default=False, nullable=False)
    # Permanent super-admin flag (developer account). Users with this flag
    # cannot be deleted, demoted, or deactivated by other admins. Only ONE
    # super-admin is created at bootstrap and the flag is migrated via
    # _safe_add_column() in init_db.py.
    is_super_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
