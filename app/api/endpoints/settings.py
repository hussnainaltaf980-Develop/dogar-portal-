from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.deps import get_current_user, require_admin
from app.models.lookups import CompanySettings
from app.models import User

router = APIRouter()


def _to_dict(s: CompanySettings):
    return {col.name: (getattr(s, col.name).isoformat() if hasattr(getattr(s, col.name), "isoformat") else getattr(s, col.name))
            for col in s.__table__.columns}


@router.get("/general")
def get_general(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.query(CompanySettings).first()
    if not s:
        s = CompanySettings()
        db.add(s); db.commit(); db.refresh(s)
    return _to_dict(s)


@router.put("/general")
def update_general(payload: dict, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    s = db.query(CompanySettings).first()
    if not s:
        s = CompanySettings()
        db.add(s); db.flush()
    allowed = {c.name for c in CompanySettings.__table__.columns} - {"id", "updated_at"}
    for k, v in payload.items():
        if k in allowed:
            setattr(s, k, v)
    db.commit(); db.refresh(s)
    return _to_dict(s)
