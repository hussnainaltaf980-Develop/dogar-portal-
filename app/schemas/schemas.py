"""Pydantic schemas for request/response."""
from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field, field_validator


# ===== Auth =====
class LoginRequest(BaseModel):
    username: Optional[str] = None   # login by username (preferred)
    email: Optional[str] = None       # login by email (fallback / backwards-compat)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


# ===== User =====
class UserBase(BaseModel):
    username: Optional[str] = None
    name: str
    email: str
    role: str = "staff"
    phone: str = ""
    is_active: bool = True
    photo: str = ""
    designation: str = ""
    bio: str = ""
    theme: str = "corporate-blue"
    is_super_admin: bool = False
    must_change_password: bool = False


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    photo: Optional[str] = None
    designation: Optional[str] = None
    bio: Optional[str] = None
    theme: Optional[str] = None


# Profile self-update (user editing their own profile — cannot change role)
class ProfileUpdate(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    photo: Optional[str] = None
    designation: Optional[str] = None
    bio: Optional[str] = None
    theme: Optional[str] = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserOut(UserBase):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True


# ===== Client =====
class ClientBase(BaseModel):
    company_name: str
    license_no: str = ""
    sponsor_name: str = ""
    sponsor_id: str = ""
    country: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    contact_person: str = ""
    status: str = "active"
    notes: str = ""


class ClientCreate(ClientBase):
    pass


class ClientUpdate(BaseModel):
    company_name: Optional[str] = None
    license_no: Optional[str] = None
    sponsor_name: Optional[str] = None
    sponsor_id: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    contact_person: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class ClientOut(ClientBase):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True


# ===== Demand =====
class JobCategoryBase(BaseModel):
    trade: str
    quantity: int = 1
    salary: float = 0
    salary_currency: str = "SAR"
    contract_years: int = 2
    notes: str = ""


class JobCategoryCreate(JobCategoryBase):
    pass


class JobCategoryOut(JobCategoryBase):
    id: int
    demand_id: int
    class Config:
        from_attributes = True


class DemandBase(BaseModel):
    demand_code: str
    client_id: int
    visa_quota: int = 0
    status: str = "processing"
    benefits: str = ""
    notes: str = ""


class DemandCreate(DemandBase):
    receiving_date: Optional[datetime] = None
    expiry_date: Optional[datetime] = None


class DemandUpdate(BaseModel):
    demand_code: Optional[str] = None
    client_id: Optional[int] = None
    visa_quota: Optional[int] = None
    status: Optional[str] = None
    benefits: Optional[str] = None
    notes: Optional[str] = None
    receiving_date: Optional[datetime] = None
    expiry_date: Optional[datetime] = None


class DemandOut(DemandBase):
    id: int
    receiving_date: Optional[datetime] = None
    expiry_date: Optional[datetime] = None
    created_at: datetime
    job_categories: List[JobCategoryOut] = []
    class Config:
        from_attributes = True


# ===== Candidate =====
class CandidateBase(BaseModel):
    full_name: str
    name_arabic: str = ""
    father_name: str = ""
    cnic: str = ""
    passport_no: str = ""
    passport_expiry: Optional[date] = None
    date_of_birth: Optional[date] = None
    place_of_birth: str = ""
    gender: str = "Male"
    marital_status: str = "Single"
    religion: str = "Islam"
    education: str = ""
    profession: str = ""
    nationality: str = "Pakistani"
    address: str = ""
    phone: str = ""
    email: str = ""
    status: str = "pending"
    notes: str = ""


class CandidateCreate(CandidateBase):
    pass


class CandidateUpdate(BaseModel):
    full_name: Optional[str] = None
    name_arabic: Optional[str] = None
    father_name: Optional[str] = None
    cnic: Optional[str] = None
    passport_no: Optional[str] = None
    passport_expiry: Optional[date] = None
    date_of_birth: Optional[date] = None
    place_of_birth: Optional[str] = None
    gender: Optional[str] = None
    marital_status: Optional[str] = None
    religion: Optional[str] = None
    education: Optional[str] = None
    profession: Optional[str] = None
    nationality: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class CandidateOut(CandidateBase):
    id: int
    photo: str = ""
    created_at: datetime
    class Config:
        from_attributes = True


# ===== Agent =====
class AgentBase(BaseModel):
    name: str
    company_name: str = ""
    phone: str = ""
    mobile: str = ""
    email: str = ""
    address: str = ""
    city: str = ""
    status: str = "active"
    notes: str = ""


class AgentCreate(AgentBase):
    pass


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    company_name: Optional[str] = None
    phone: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class AgentOut(AgentBase):
    id: int
    balance: float = 0
    class Config:
        from_attributes = True


class AgentCashBase(BaseModel):
    agent_id: int
    details: str = ""
    debit: float = 0
    credit: float = 0
    method: str = "cash"
    ref_id: str = ""


class AgentCashCreate(AgentCashBase):
    datetime: Optional[datetime] = None


class AgentCashOut(AgentCashBase):
    id: int
    datetime: datetime
    class Config:
        from_attributes = True


# ===== Document Templates =====
class DocumentFieldBase(BaseModel):
    label: str
    field_key: str
    field_type: str = "text"
    # `static_value` may be None in DB for fields that bind to a record column;
    # only fields of type "static" / "label" use a literal value, so we accept
    # None and coerce it to empty string for the API consumer.
    static_value: Optional[str] = ""
    x: float
    y: float
    width: float = 200
    height: float = 20
    font_size: float = 11
    font_bold: bool = False
    font_italic: bool = False
    color: Optional[str] = "#000000"
    align: Optional[str] = "left"
    page: int = 1
    # Extra params consumed by the PDF engine for advanced field types
    # (char_cells / photo / barcode / arabic / trade_table). Stored as JSON
    # on the DocumentField row; default to an empty dict so the designer can
    # always treat it as an object.
    meta: Optional[dict] = None

    @field_validator("static_value", "color", "align", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        if v is None:
            return ""
        return v

    @field_validator("meta", mode="before")
    @classmethod
    def _meta_default(cls, v):
        if v is None:
            return {}
        return v


class DocumentFieldCreate(DocumentFieldBase):
    pass


class DocumentFieldOut(DocumentFieldBase):
    id: int
    template_id: int
    class Config:
        from_attributes = True


class DocumentTemplateBase(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    data_source: str = "candidate"
    page_width: float = 595
    page_height: float = 842
    is_active: bool = True


class DocumentTemplateCreate(DocumentTemplateBase):
    pass


class DocumentTemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    data_source: Optional[str] = None
    page_width: Optional[float] = None
    page_height: Optional[float] = None
    is_active: Optional[bool] = None
    # Allow the designer to change the background image via PUT (previously
    # only the dedicated /background upload endpoint could touch this).
    background_image: Optional[str] = None


class DocumentTemplateOut(DocumentTemplateBase):
    id: int
    background_image: str = ""
    fields: List[DocumentFieldOut] = []
    created_at: datetime
    class Config:
        from_attributes = True
