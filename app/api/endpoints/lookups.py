"""Generic CRUD endpoints for sidebar lookup tables (Visa Categories, Embassies,
Cities, Medical Centers, Contacts, Depositors, Service Charges).
Each endpoint follows the same simple pattern: list / create / update / delete.
"""
from typing import Optional, Type
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.tenancy import get_tenant_db as get_db  # tenant-scoped session (falls back to control DB when no tenant)
from app.core.deps import get_current_user
from app.models.lookups import (
    VisaCategory, Embassy, City, MedicalCenter, Contact,
    Depositor, ServiceCharge,
)

router = APIRouter()


# ----- helpers -----
def _to_dict(row):
    return {
        c.name: (getattr(row, c.name).isoformat() if hasattr(getattr(row, c.name), "isoformat") else getattr(row, c.name))
        for c in row.__table__.columns
    }


def _crud(model_class: Type, allowed_fields: list, search_field: str = "name"):
    """Build 4 CRUD endpoint functions for a model. Returns dict for inclusion in router."""

    def list_items(
        db: Session = Depends(get_db),
        user=Depends(get_current_user),
        q: Optional[str] = Query(None, description="Search term"),
        skip: int = 0,
        limit: int = 200,
    ):
        query = db.query(model_class)
        if q and hasattr(model_class, search_field):
            query = query.filter(getattr(model_class, search_field).ilike(f"%{q}%"))
        total = query.count()
        rows = query.order_by(model_class.id.desc()).offset(skip).limit(limit).all()
        return {"total": total, "items": [_to_dict(r) for r in rows]}

    def create_item(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
        data = {k: v for k, v in payload.items() if k in allowed_fields}
        if not data.get(search_field):
            raise HTTPException(400, f"{search_field} is required")
        row = model_class(**data)
        db.add(row); db.commit(); db.refresh(row)
        return _to_dict(row)

    def update_item(item_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
        row = db.query(model_class).filter(model_class.id == item_id).first()
        if not row:
            raise HTTPException(404, "Not found")
        for k, v in payload.items():
            if k in allowed_fields:
                setattr(row, k, v)
        db.commit(); db.refresh(row)
        return _to_dict(row)

    def delete_item(item_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
        row = db.query(model_class).filter(model_class.id == item_id).first()
        if not row:
            raise HTTPException(404, "Not found")
        db.delete(row); db.commit()
        return {"ok": True}

    return list_items, create_item, update_item, delete_item


# ===== Build endpoints for each lookup =====
_LOOKUPS = [
    ("visa-categories", VisaCategory, ["name", "code", "description", "is_active"]),
    ("trades",          VisaCategory, ["name", "code", "description", "is_active"]),
    ("embassies",       Embassy,      ["name", "country", "city", "address", "phone"]),
    ("cities",          City,         ["name", "province", "country"]),
    ("medical-centers", MedicalCenter,["name", "city", "address", "phone"]),
    ("contacts",        Contact,      ["name", "company", "email", "phone", "note"]),
    ("service-charges", ServiceCharge,["name", "amount", "description", "is_active"]),
]

for slug, model_class, fields in _LOOKUPS:
    l, c, u, d = _crud(model_class, fields)
    router.add_api_route(f"/{slug}",            l, methods=["GET"],    name=f"list_{slug}")
    router.add_api_route(f"/{slug}",            c, methods=["POST"],   name=f"create_{slug}")
    router.add_api_route(f"/{slug}/{{item_id}}",u, methods=["PUT"],    name=f"update_{slug}")
    router.add_api_route(f"/{slug}/{{item_id}}",d, methods=["DELETE"], name=f"delete_{slug}")


# ===== Depositors (special: first_name is the required search field) =====
def _list_depositors(
    db: Session = Depends(get_db), user=Depends(get_current_user),
    q: Optional[str] = None, skip: int = 0, limit: int = 200,
):
    query = db.query(Depositor)
    if q:
        query = query.filter(
            (Depositor.first_name.ilike(f"%{q}%")) |
            (Depositor.last_name.ilike(f"%{q}%")) |
            (Depositor.cnic.ilike(f"%{q}%"))
        )
    total = query.count()
    rows = query.order_by(Depositor.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [_to_dict(r) for r in rows]}


def _create_depositor(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not payload.get("first_name"):
        raise HTTPException(400, "first_name is required")
    allowed = ["first_name", "last_name", "full_name", "cnic", "mobile", "address"]
    data = {k: v for k, v in payload.items() if k in allowed}
    if not data.get("full_name"):
        data["full_name"] = f"{data.get('first_name','')} {data.get('last_name','')}".strip()
    row = Depositor(**data); db.add(row); db.commit(); db.refresh(row)
    return _to_dict(row)


def _update_depositor(item_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    row = db.query(Depositor).filter(Depositor.id == item_id).first()
    if not row: raise HTTPException(404, "Not found")
    allowed = ["first_name", "last_name", "full_name", "cnic", "mobile", "address"]
    for k, v in payload.items():
        if k in allowed: setattr(row, k, v)
    db.commit(); db.refresh(row); return _to_dict(row)


def _delete_depositor(item_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    row = db.query(Depositor).filter(Depositor.id == item_id).first()
    if not row: raise HTTPException(404, "Not found")
    db.delete(row); db.commit(); return {"ok": True}


router.add_api_route("/depositors",            _list_depositors,   methods=["GET"])
router.add_api_route("/depositors",            _create_depositor,  methods=["POST"])
router.add_api_route("/depositors/{item_id}",  _update_depositor,  methods=["PUT"])
router.add_api_route("/depositors/{item_id}",  _delete_depositor,  methods=["DELETE"])
