import os
import uuid
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional

from app.db.session import get_db
from app.core.deps import get_current_user
from app.core.config import settings
from app.models import DocumentTemplate, DocumentField, GeneratedDocument, User
from app.models import Candidate, Demand, Client, Agent
from app.schemas.schemas import (
    DocumentTemplateCreate, DocumentTemplateUpdate, DocumentTemplateOut,
    DocumentFieldCreate, DocumentFieldOut,
)
from app.services.pdf_engine import generate_pdf, get_available_fields

router = APIRouter()


# ============================================================================
# DATA OVERLAY ENGINE
# ============================================================================
# Maps record column values to template field coordinates so the frontend can
# render an exact-pixel preview of the printed document (background image +
# data text positioned at the configured coordinates). The same data is also
# used by the PDF engine for offline export.

def _resolve_field_value(record, field_key: str) -> str:
    """Resolve a single field key against an ORM record.
    Supports system tokens (__today__, __now__) and dot-access for related
    fields (e.g. demand.sponsor_name).
    """
    if field_key == "__today__":
        return datetime.now().strftime("%d-%m-%Y")
    if field_key == "__now__":
        return datetime.now().strftime("%d-%m-%Y %H:%M")
    if record is None:
        return ""
    val = record
    for part in field_key.split("."):
        if val is None:
            return ""
        val = getattr(val, part, None)
    if val is None:
        return ""
    if isinstance(val, (datetime, date)):
        return val.strftime("%d-%m-%Y")
    return str(val)


def _load_record(db: Session, data_source: str, record_id: Optional[int]):
    if not record_id:
        return None
    mapping = {
        "candidate": Candidate,
        "demand": Demand,
        "client": Client,
        "agent": Agent,
    }
    model = mapping.get(data_source)
    if not model:
        return None
    return db.query(model).filter(model.id == record_id).first()


def _build_overlay_payload(tpl: DocumentTemplate, record, db: Optional[Session] = None) -> dict:
    """Build the JSON payload the frontend needs to render the data overlay
    on top of the background image. Coordinates are returned as stored
    (PDF-points, origin = bottom-left). The frontend converts to CSS
    coordinates (origin = top-left).
    """
    from app.services.overlay_engine import resolve_all_related_data_for_record, validate_overlay_mappings
    
    resolved_data = {}
    if db and record:
        resolved_data = resolve_all_related_data_for_record(db, tpl.data_source, record.id)

    fields_out = []
    for f in tpl.fields:
        key = f.field_key
        if key in resolved_data:
            val = resolved_data[key]
        else:
            val = _resolve_field_value(record, key)

        meta = f.meta or {}
        if isinstance(meta, str):
            try:
                import json as _json
                meta = _json.loads(meta)
            except Exception:
                meta = {}

        if f.field_type == "static":
            value = f.static_value or val
        elif f.field_type == "checkbox":
            value = "X" if val else ""
        elif f.field_type == "barcode":
            # Resolve the encoded value from the barcode-content merge spec
            # (matches the PDF engine) so the on-screen preview shows the
            # exact same number that prints under the bars.
            from app.services.pdf_engine import _resolve_merge_text
            spec = (meta.get("barcode_content") or meta.get("content")
                    or f.static_value or (("{{" + key + "}}") if key and not key.startswith("__") else ""))
            value = _resolve_merge_text(spec, resolved_data, record) if spec else (val or "")
        else:
            value = val

        # For trade_table fields, resolve the actual rows so the print HTML
        # can render the same table the PDF engine draws (audit Fix 4).
        trade_rows: list = []
        if f.field_type == "trade_table" and db is not None:
            from app.services.overlay_engine import resolve_trade_table_rows
            # Resolve the demand_id from the record (could be the demand
            # itself, or a candidate's assigned demand).
            demand_id = None
            if record is not None:
                demand_id = getattr(record, "demand_id", None) or getattr(record, "id", None) if tpl.data_source == "demand" else getattr(record, "demand_id", None)
            trade_rows = resolve_trade_table_rows(db, demand_id)

        # For barcodes, pre-render a crisp PNG (data-URI) the browser can show.
        barcode_img = ""
        if f.field_type == "barcode":
            try:
                from app.services.pdf_engine import barcode_to_data_uri
                barcode_img = barcode_to_data_uri(
                    value,
                    symbology=(meta.get("symbology") or meta.get("format") or "code128"),
                    bar_height=meta.get("bar_height"),
                    bar_width=meta.get("bar_width"),
                    show_text=meta.get("show_text", meta.get("human_readable", True)),
                    caption_font_size=float(meta.get("caption_font_size")
                                            or meta.get("caption_font") or 8.0),
                )
            except Exception as _e:
                print(f"[documents] barcode preview render failed: {_e}")

        fields_out.append({
            "id": f.id,
            "label": f.label,
            "field_key": key,
            "field_type": f.field_type,
            "static_value": f.static_value or "",
            "value": value,
            "barcode_img": barcode_img,
            "trade_rows": trade_rows,
            "x": float(f.x or 0),
            "y": float(f.y or 0),
            "width": float(f.width or 200),
            "height": float(f.height or 20),
            "font_size": float(f.font_size or 11),
            "font_bold": bool(f.font_bold),
            "font_italic": bool(f.font_italic),
            "color": f.color or "#000000",
            "align": f.align or "left",
            "page": int(f.page or 1),
            "meta": meta,
        })
    # background_image can be stored either as a bare filename (e.g.
    # "allied_bank_form7.jpg") or as an already-rooted URL path
    # ("/static/pdf_backgrounds/allied_bank_form7.jpg"). Normalize both.
    bg_raw = (tpl.background_image or "").strip()
    if not bg_raw:
        bg_url = ""
    elif bg_raw.startswith("/") or bg_raw.startswith("http"):
        bg_url = bg_raw
    else:
        bg_url = f"/static/pdf_backgrounds/{bg_raw}"

    missing_required = []
    if resolved_data:
        missing_required = validate_overlay_mappings(tpl.fields, resolved_data)

    return {
        "template": {
            "id": tpl.id,
            "name": tpl.name,
            "data_source": tpl.data_source,
            "background_image": bg_raw,
            "background_url": bg_url,
            "page_width": float(tpl.page_width or 595),
            "page_height": float(tpl.page_height or 842),
        },
        "fields": fields_out,
        "missing_required_fields": missing_required,
    }


@router.get("/templates", response_model=List[DocumentTemplateOut])
def list_templates(
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    category: Optional[str] = Query(None, description="Filter by category: permission | protector | visa_process"),
    data_source: Optional[str] = Query(None, description="Filter by data_source: demand | candidate | client | agent"),
):
    """List all PDF data-overlay templates. The portal does NOT generate
    documents from scratch — every template uses a real-image background
    (visa form, bank slip, OEP form, etc.) and overlays coordinate-mapped
    fields from the linked record (Candidate / Demand / Client).
    """
    q = db.query(DocumentTemplate).options(joinedload(DocumentTemplate.fields))
    if category:
        q = q.filter(DocumentTemplate.category == category)
    if data_source:
        q = q.filter(DocumentTemplate.data_source == data_source)
    return q.order_by(DocumentTemplate.name.asc()).all()


@router.post("/templates", response_model=DocumentTemplateOut)
def create_template(payload: DocumentTemplateCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = DocumentTemplate(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/templates/{tpl_id}", response_model=DocumentTemplateOut)
def get_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentTemplate).options(joinedload(DocumentTemplate.fields)).filter(DocumentTemplate.id == tpl_id).first()
    if not obj:
        raise HTTPException(404, "Template not found")
    return obj


def _normalize_bg_filename(val: str) -> str:
    """Reduce any background_image value to a bare filename so the canvas
    renderer can prepend /static/pdf_backgrounds/ exactly once. Handles
    legacy rows where the UI saved the full URL path."""
    if not val:
        return val
    v = val.strip()
    # Strip any /static/pdf_backgrounds/ prefix (with or without leading slash)
    for prefix in ("/static/pdf_backgrounds/", "static/pdf_backgrounds/"):
        if v.startswith(prefix):
            v = v[len(prefix):]
            break
    # Strip any leading slashes left over
    return v.lstrip("/")


@router.put("/templates/{tpl_id}", response_model=DocumentTemplateOut)
def update_template(tpl_id: int, payload: DocumentTemplateUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not obj:
        raise HTTPException(404, "Template not found")
    data = payload.model_dump(exclude_unset=True)
    if "background_image" in data and data["background_image"]:
        data["background_image"] = _normalize_bg_filename(data["background_image"])
    for k, v in data.items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/templates/{tpl_id}")
def delete_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not obj:
        raise HTTPException(404, "Template not found")
    db.delete(obj)
    db.commit()
    return {"ok": True}


@router.post("/templates/{tpl_id}/background")
async def upload_background(tpl_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not obj:
        raise HTTPException(404, "Template not found")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".png", ".jpg", ".jpeg"]:
        raise HTTPException(400, "Only PNG/JPG images allowed")
    fname = f"tpl_{tpl_id}_{uuid.uuid4().hex[:8]}{ext}"
    fpath = os.path.join(settings.PDF_BG_DIR, fname)
    with open(fpath, "wb") as f:
        content = await file.read()
        f.write(content)

    # Delete old bg
    if obj.background_image:
        old_path = os.path.join(settings.PDF_BG_DIR, obj.background_image)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError as exc:
                # Non-fatal — orphaned file on disk is preferable to a 500.
                # Log so ops can clean up.
                import logging
                logging.getLogger("dtc.documents").warning(
                    "Could not delete stale PDF background %r: %s", old_path, exc
                )

    obj.background_image = fname
    db.commit()
    return {"ok": True, "background_image": fname, "url": f"/static/pdf_backgrounds/{fname}"}


# ===== Fields =====
@router.get("/templates/{tpl_id}/fields", response_model=List[DocumentFieldOut])
def list_fields(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(DocumentField).filter(DocumentField.template_id == tpl_id).order_by(DocumentField.id).all()


@router.post("/templates/{tpl_id}/fields", response_model=DocumentFieldOut)
def create_field(tpl_id: int, payload: DocumentFieldCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404, "Template not found")
    obj = DocumentField(template_id=tpl_id, **payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/fields/{field_id}", response_model=DocumentFieldOut)
def update_field(field_id: int, payload: DocumentFieldCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentField).filter(DocumentField.id == field_id).first()
    if not obj:
        raise HTTPException(404, "Field not found")
    for k, v in payload.model_dump().items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/fields/{field_id}")
def delete_field(field_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    obj = db.query(DocumentField).filter(DocumentField.id == field_id).first()
    if not obj:
        raise HTTPException(404, "Field not found")
    db.delete(obj)
    db.commit()
    return {"ok": True}


@router.get("/available-fields")
def available_fields(data_source: str = "candidate", user: User = Depends(get_current_user)):
    """Flat list of available fields for the given data_source.
    Kept for backwards compatibility — the designer should use
    /designer/palette for the grouped + searchable structure.
    """
    return get_available_fields(data_source)


# ============================================================================
# TEMPLATE DESIGNER — UI helper endpoints
# ============================================================================
# These endpoints power the drag-and-drop WYSIWYG designer at
# /documents/customize/{tpl_id}. They expose the same data the manual
# /templates/{id}/fields endpoints handle, but in a shape that maps 1-to-1
# to the designer's left palette, canvas, and properties panel.

@router.get("/designer/palette")
def designer_palette(
    data_source: str = "candidate",
    user: User = Depends(get_current_user),
):
    """Return the full field palette for the Template Designer, grouped
    exactly the way demo.oep.com.pk shows it: Customer / Category /
    Candidate / Passport / Selected Trade / Visa File / Depositor /
    System. Each group entry includes the field's default render type so
    the designer can pre-seed the right input (text vs char_cells vs
    arabic vs photo etc.).
    """
    # Field-key → default field_type. Anything not listed defaults to "text".
    DEFAULT_TYPES = {
        "photo": "photo",
        "candidate_photo": "photo",
        "name_arabic": "arabic",
        "father_name_arabic": "arabic",
        "sponsor_name_arabic": "arabic",
        "sponsor_address_arabic": "arabic",
        "issuing_authority_arabic": "arabic",
        "company_name_arabic": "arabic",
        "place_of_birth_arabic": "arabic",
        "address_arabic": "arabic",
        "cnic": "char_cells",
        "nadra_token_no": "char_cells",
        "passport_no": "char_cells",
        "next_of_kin_nic": "char_cells",
        "phone": "char_cells",
        "mobile": "char_cells",
        "e_number": "char_cells",
        "permission_no": "char_cells",
        "date_of_birth": "char_cells",
        "passport_issue_date": "char_cells",
        "passport_expiry_date": "char_cells",
        "permission_date": "char_cells",
        "visa_issue_date": "char_cells",
        "gender_male": "checkbox",
        "gender_female": "checkbox",
        "marital_single": "checkbox",
        "marital_married": "checkbox",
        "religion_islam": "checkbox",
        "religion_christian": "checkbox",
        "ticket_included": "checkbox",
        "accommodation": "checkbox",
        "food_allowance": "checkbox",
    }
    # Field icons (Font Awesome) per category for nicer palette
    GROUP_ORDER = [
        "Customer", "Category", "Candidate", "Passport",
        "Selected Trade", "Visa File", "Depositor", "Profession",
        "Contact", "Identification", "Next of Kin", "Workflow",
        "Media", "Sponsor", "Visa", "Demand File", "Client",
        "Agent", "System",
    ]

    # ------------------------------------------------------------------
    # Build the base palette from pdf_engine.get_available_fields, then
    # enrich it with the screenshot-style groups that aren't present in
    # the flat schema (Customer = client/OEP, Category = document
    # category, Selected Trade = job-category rows, Depositor = depositor
    # info). For data_source=candidate we always also include the
    # surrounding "Visa File / Sponsor" group so the designer doesn't
    # have to switch sources to drop a sponsor field onto a candidate
    # document.
    # ------------------------------------------------------------------
    items = []
    seen = set()

    def _add(group, key, label, ftype=None, arabic=False):
        if key in seen:
            return
        seen.add(key)
        items.append({
            "group": group,
            "key": key,
            "label": label,
            "field_type": ftype or DEFAULT_TYPES.get(key, "arabic" if arabic else "text"),
            "arabic": arabic,
        })

    # --- Customer group (the OEP itself / the tenant company) ---
    for k, l in [
        ("customer_name", "Name"),
        ("customer_name_arabic", "Name (Arabic)"),
        ("customer_owner_name", "Owner Name"),
        ("customer_oep_license", "OEP License"),
        ("customer_emigrant_office", "Emigrant Office"),
        ("customer_office_name", "Office Name"),
        ("customer_office_city", "Office City"),
        ("customer_address", "Address"),
        ("customer_address_arabic", "Address (Arabic)"),
        ("customer_phone", "Phone"),
        ("customer_mobile", "Mobile"),
        ("customer_fax", "Fax"),
        ("customer_email", "Email"),
        ("customer_subdomain", "Subdomain"),
        ("customer_slug", "Slug"),
        ("customer_file_prefix", "File Prefix"),
        ("customer_starting_point", "Starting Point"),
        ("customer_status", "Status"),
        ("customer_plan", "Plan"),
        ("customer_expiry_date", "Expiry Date"),
    ]:
        _add("Customer", k, l, arabic=k.endswith("_arabic"))

    # --- Category group ---
    _add("Category", "document_category", "Document Category")

    # --- Candidate group ---
    candidate_fields = [
        ("photo",                "Photo", "photo"),
        ("full_name",            "Name", None),
        ("name_arabic",          "Name (Arabic)", "arabic"),
        ("father_name",          "Father Name", None),
        ("father_name_arabic",   "Father Name (Arabic)", "arabic"),
        ("mother_name",          "Mother Name", None),
        ("cnic",                 "CNIC", "char_cells"),
        ("nadra_token_no",       "NADRA Token", "char_cells"),
        ("date_of_birth",        "Date of Birth", None),
        ("nationality",          "Nationality", None),
        ("address",              "Address", None),
        ("phone",                "Phone", None),
        ("gender",               "Gender", None),
        ("gender_male",          "Gender — Male (checkbox)", "checkbox"),
        ("gender_female",        "Gender — Female (checkbox)", "checkbox"),
        ("marital_status",       "Marital Status", None),
        ("marital_single",       "Marital — Single (checkbox)", "checkbox"),
        ("marital_married",      "Marital — Married (checkbox)", "checkbox"),
        ("religion",             "Religion", None),
        ("religion_islam",       "Religion — Islam (checkbox)", "checkbox"),
        ("religion_christian",   "Religion — Christian (checkbox)", "checkbox"),
        ("place_of_birth",       "Place of Birth", None),
        ("place_of_birth_arabic","Place of Birth (Arabic)", "arabic"),
        ("tehsil",               "Tehsil", None),
        ("district",             "District", None),
        ("province",             "Province", None),
        ("qualification",        "Qualification", None),
        ("profession",           "Occupation / Profession", None),
        ("profession_arabic",    "Occupation (Arabic)", "arabic"),
        ("age_employee",         "Age / Employee", None),
        ("permission_no",        "Permission No", None),
        ("permission_date",      "Permission Date", None),
        ("salary",               "Salary", None),
        ("price",                "Price", None),
        ("ticket_included",      "Ticket Included", "checkbox"),
        ("accommodation",        "Accommodation", "checkbox"),
        ("food_allowance",       "Food Allowance", "checkbox"),
        ("slot_notes",           "Slot Notes", None),
        ("status",               "Status", None),
        ("protector_no",         "Protector No", None),
        ("protector_date",       "Protector Date", None),
        ("next_of_kin_name",     "Next of Kin Name", None),
        ("next_of_kin_nic",      "Next of Kin CNIC", "char_cells"),
        ("next_of_kin_relation", "Next of Kin Relation", None),
        ("medical_center",       "Medical Center", None),
        ("gamca_number",         "GAMCA Number", None),
        ("medical_date",         "Medical Date", None),
        ("e_number",             "E-Number", None),
        ("date_of_departure",    "Departure Date", None),
        ("flight_no",            "Flight Number", None),
        ("destination",          "Destination", None),
        ("ticket_no",            "Ticket Number", None),
        ("visa_stamp_date",      "Visa Stamp Date", None),
    ]
    for k, l, t in candidate_fields:
        _add("Candidate", k, l, ftype=t, arabic=(t == "arabic"))

    # --- Passport group ---
    for k, l, t in [
        ("passport_no",          "Number", "char_cells"),
        ("passport_issue_date",  "Issue Date", None),
        ("passport_expiry_date", "Expiry Date", None),
        ("issuing_authority",    "Authority", None),
        ("issuing_authority_arabic", "Authority (Arabic)", "arabic"),
        ("passport_issue_place", "Issue Place", None),
        ("passport_receive_date", "Receive Date", None),
        ("passport_send_date",   "Send Date", None),
        ("passport_consignment", "Consignment", None),
        ("passport_courier",     "Courier", None),
    ]:
        _add("Passport", k, l, ftype=t, arabic=(t == "arabic"))

    # --- Selected Trade group (job category line items) ---
    for k, l in [
        ("trade_sr_no",       "Sr#"),
        ("selected_trade_visa_category", "Visa Category (Selected)"),
        ("trade_visa_category", "Visa Category"),
        ("trade_qty",         "Quantity"),
        ("trade_assigned",    "Assigned"),
        ("trade_available",   "Available"),
        ("trade_salary",      "Salary"),
        ("trade_contract_period", "Contract Period"),
    ]:
        _add("Selected Trade", k, l)
    _add("Selected Trade", "trades_table", "Trades Table", ftype="trade_table")

    # --- Visa File / Demand group ---
    for k, l, t in [
        ("file_number",            "File Number", None),
        ("client_name",            "Client Name", None),
        ("sponsor_name",           "Sponsor Name", None),
        ("sponsor_name_arabic",    "Sponsor Name (Arabic)", "arabic"),
        ("sponsor_address",        "Sponsor Address", None),
        ("sponsor_address_arabic", "Sponsor Address (Arabic)", "arabic"),
        ("sponsor_phone",          "Sponsor Phone", None),
        ("visa_number",            "Visa Number", None),
        ("permission_no",          "Permission Number", None),
        ("visa_issue_date",        "Visa Issue Date", None),
        ("visa_issue_date_hijri",  "Visa Issue Date (Hijri)", None),
        ("receiving_date",         "Receiving Date", None),
        ("country",                "Country", None),
        ("embassy",                "Embassy", None),
        ("embassy_city",           "Embassy City", None),
        ("reference",              "Reference", None),
        ("notes",                  "Notes", None),
    ]:
        _add("Visa File", k, l, ftype=t, arabic=(t == "arabic"))

    # --- Depositor group ---
    for k, l in [
        ("depositor_first_name", "First Name"),
        ("depositor_last_name",  "Last Name"),
        ("depositor_full_name",  "Full Name"),
        ("depositor_mobile",     "Mobile Number"),
        ("depositor_cnic",       "CNIC"),
    ]:
        _add("Depositor", k, l, ftype="char_cells" if k.endswith("cnic") or k.endswith("mobile") else None)

    # --- System group (always available) ---
    _add("System", "__today__", "Today's Date")
    _add("System", "__now__",   "Current Date + Time")
    _add("System", "page_number", "Page Number")

    # --- Other / Insert primitives (these don't bind to a data column;
    # they let the user place free-form widgets that read from
    # field.static_value or always render as a static checkbox/barcode).
    # The designer shows these under an "Insert" toolbar, but we also
    # surface them in the palette for parity with demo.oep.
    for k, l, t in [
        ("__static_text__",     "Insert Text Box",  "static"),
        ("__static_checkbox__", "Insert Checkbox",  "checkbox"),
        ("__static_barcode__",  "Insert Barcode",   "barcode"),
    ]:
        _add("Other", k, l, ftype=t)

    # Sort items so that GROUP_ORDER is honoured but unknown groups land
    # at the end (alphabetically).
    def _grp_rank(g):
        return (GROUP_ORDER.index(g), 0) if g in GROUP_ORDER else (len(GROUP_ORDER), g)
    items.sort(key=lambda it: (_grp_rank(it["group"]), it["label"].lower()))

    # Build the grouped output shape the designer consumes directly.
    grouped = {}
    for it in items:
        grouped.setdefault(it["group"], []).append({
            "key": it["key"],
            "label": it["label"],
            "field_type": it["field_type"],
            "arabic": it["arabic"],
        })
    ordered_groups = [
        {"group": g, "fields": grouped[g]}
        for g in sorted(grouped.keys(), key=_grp_rank)
    ]

    return {
        "data_source": data_source,
        "groups": ordered_groups,
        # Flat list still available for clients that want it.
        "flat": items,
    }


@router.get("/designer/backgrounds")
def designer_backgrounds(user: User = Depends(get_current_user)):
    """List every background image available under /static/pdf_backgrounds/
    so the designer can swap a template's background without leaving the
    page. Returns the bare filenames in sorted order.
    """
    bg_dir = settings.PDF_BG_DIR
    out = []
    try:
        for name in sorted(os.listdir(bg_dir)):
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                full = os.path.join(bg_dir, name)
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                out.append({
                    "filename": name,
                    "url": f"/static/pdf_backgrounds/{name}",
                    "size": size,
                })
    except FileNotFoundError:
        pass
    return out


@router.post("/templates/{tpl_id}/fields/bulk")
def bulk_save_fields(
    tpl_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Replace the entire field set for a template in one shot. This is
    what the designer's "Save" button calls — it sends the full canvas
    state and we sync the document_fields rows to match.

    Body shape::

        {
          "fields": [
            { "id": 12, "label": "Name", "field_key": "full_name",
              "field_type": "text", "x": 100, "y": 700, "width": 200,
              "height": 18, "font_size": 12, "font_bold": true,
              "font_italic": false, "color": "#000", "align": "left",
              "page": 1, "static_value": "", "meta": {} },
            ...   # rows WITHOUT id are inserted; rows WITH id are updated
          ],
          "deleted_ids": [4, 5]   # rows to delete (optional)
        }

    The endpoint is idempotent and atomic — if any row fails validation
    nothing is persisted.
    """
    tpl = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404, "Template not found")

    incoming = payload.get("fields") or []
    deleted_ids = payload.get("deleted_ids") or []

    if not isinstance(incoming, list):
        raise HTTPException(400, "`fields` must be a list")

    # Load existing field rows keyed by id for fast lookup.
    existing = {f.id: f for f in db.query(DocumentField).filter(DocumentField.template_id == tpl_id).all()}

    # 1) Apply explicit deletions first.
    for fid in deleted_ids:
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        if fid in existing:
            db.delete(existing.pop(fid))

    # 2) Upsert each incoming field.
    #
    # CRITICAL BUG FIX (2026-06-01):  earlier versions trusted the
    # client to always send `id` for existing rows. If the client lost
    # the id (e.g. after an error, or before the post-save reconcile
    # finishes) every Save would create a fresh copy → over time a
    # single template accumulated thousands of duplicate rows.
    #
    # The new logic is "REPLACE ALL": we apply the incoming list as
    # the FULL canvas state. Any existing row whose id is NOT in the
    # incoming list is deleted at the end. We also dedupe by
    # (field_key, page, round(x), round(y)) inside the request so the
    # client cannot accidentally insert two identical fields.
    out_ids: list[int] = []
    kept_ids: set[int] = set()
    pos_seen: set[tuple] = set()

    for raw in incoming:
        if not isinstance(raw, dict):
            continue

        # Coerce + validate the bits we care about.
        try:
            row = {
                "label":       str(raw.get("label") or raw.get("field_key") or "Field"),
                "field_key":   str(raw.get("field_key") or "").strip(),
                "field_type":  str(raw.get("field_type") or "text"),
                "static_value": raw.get("static_value") or "",
                "x":           float(raw.get("x") or 0),
                "y":           float(raw.get("y") or 0),
                "width":       float(raw.get("width") or 200),
                "height":      float(raw.get("height") or 20),
                "font_size":   float(raw.get("font_size") or 11),
                "font_bold":   bool(raw.get("font_bold")),
                "font_italic": bool(raw.get("font_italic")),
                "color":       str(raw.get("color") or "#000000"),
                "align":       str(raw.get("align") or "left"),
                "page":        int(raw.get("page") or 1),
                "meta":        raw.get("meta") or {},
            }
        except (TypeError, ValueError) as exc:
            db.rollback()
            raise HTTPException(400, f"Invalid field payload: {exc}") from exc

        if not row["field_key"]:
            # Skip empty key rows silently — the designer occasionally emits
            # placeholder rows for newly-dragged elements that the user
            # cancelled before naming.
            continue

        # Per-request dedupe — drop exact-position duplicates.
        pos_key = (row["field_key"], row["page"], round(row["x"]), round(row["y"]))
        if pos_key in pos_seen:
            continue
        pos_seen.add(pos_key)

        fid = raw.get("id")
        try:
            fid_int = int(fid) if fid not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            fid_int = None

        obj = existing.get(fid_int) if fid_int else None

        # Second-chance match: if the client lost the id, try to match an
        # existing row by (field_key, page, ~x, ~y). Coordinates are
        # rounded to the nearest point because canvas drag-snap can
        # produce sub-pixel jitter (e.g. 122.0001 vs 122.0).
        if obj is None:
            for ex_id, ex_obj in existing.items():
                if ex_id in kept_ids:
                    continue
                if (ex_obj.field_key == row["field_key"]
                        and ex_obj.page == row["page"]
                        and round(ex_obj.x) == round(row["x"])
                        and round(ex_obj.y) == round(row["y"])):
                    obj = ex_obj
                    break

        if obj is None:
            obj = DocumentField(template_id=tpl_id, **row)
            db.add(obj)
        else:
            for k, v in row.items():
                setattr(obj, k, v)
        db.flush()
        out_ids.append(obj.id)
        kept_ids.add(obj.id)

    # 3) REPLACE-ALL semantics — any existing row NOT touched above is
    # dropped. This is what guarantees the canvas state == the DB state
    # and eliminates the duplicate-on-save bug.
    for ex_id, ex_obj in list(existing.items()):
        if ex_id not in kept_ids:
            db.delete(ex_obj)

    db.commit()
    # Re-fetch ordered list for the client.
    fields = (
        db.query(DocumentField)
        .filter(DocumentField.template_id == tpl_id)
        .order_by(DocumentField.id)
        .all()
    )
    return {
        "ok": True,
        "saved_ids": out_ids,
        "deleted_ids": deleted_ids,
        "total_fields": len(fields),
        "fields": [
            {
                "id": f.id, "label": f.label, "field_key": f.field_key,
                "field_type": f.field_type, "static_value": f.static_value or "",
                "x": f.x, "y": f.y, "width": f.width, "height": f.height,
                "font_size": f.font_size, "font_bold": f.font_bold,
                "font_italic": f.font_italic, "color": f.color, "align": f.align,
                "page": f.page, "meta": f.meta or {},
            }
            for f in fields
        ],
    }


@router.post("/templates/{tpl_id}/fields/dedupe")
def dedupe_fields(
    tpl_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One-shot cleanup: collapse duplicate fields on a template.

    A field is considered a duplicate of another when both share the
    same (field_key, page, round(x), round(y)). The OLDEST row (lowest
    id) wins so any record bound to that id keeps working; all newer
    copies are deleted.
    """
    tpl = db.query(DocumentTemplate).filter(DocumentTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404, "Template not found")

    rows = (
        db.query(DocumentField)
        .filter(DocumentField.template_id == tpl_id)
        .order_by(DocumentField.id.asc())
        .all()
    )
    before = len(rows)
    seen: dict[tuple, int] = {}
    deleted = 0
    for f in rows:
        key = (f.field_key or "", int(f.page or 1), round(f.x or 0), round(f.y or 0))
        if key in seen:
            db.delete(f)
            deleted += 1
        else:
            seen[key] = f.id
    db.commit()
    return {"ok": True, "before": before, "after": before - deleted, "deleted": deleted}


@router.post("/admin/dedupe-all-fields")
def dedupe_all_fields(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin housekeeping — run the dedupe pass across EVERY template.

    Returns a per-template summary. Safe to invoke multiple times: it
    is idempotent (the second call always reports 0 deletions because
    no duplicates remain).
    """
    summary = []
    tpls = db.query(DocumentTemplate.id, DocumentTemplate.name).all()
    for tpl in tpls:
        rows = (
            db.query(DocumentField)
            .filter(DocumentField.template_id == tpl.id)
            .order_by(DocumentField.id.asc())
            .all()
        )
        before = len(rows)
        seen: dict[tuple, int] = {}
        deleted = 0
        for f in rows:
            key = (f.field_key or "", int(f.page or 1),
                   round(f.x or 0), round(f.y or 0))
            if key in seen:
                db.delete(f)
                deleted += 1
            else:
                seen[key] = f.id
        if deleted:
            summary.append({
                "template_id": tpl.id, "name": tpl.name,
                "before": before, "after": before - deleted,
                "deleted": deleted,
            })
    db.commit()
    return {"ok": True, "templates_touched": len(summary), "details": summary}


def _parse_record_id(record_id):
    """Tolerate empty-string ?record_id= (the frontend may pass it blank when
    no record is selected). Returns int or None."""
    if record_id is None or record_id == "" or str(record_id).lower() in ("null", "none", "undefined"):
        return None
    try:
        return int(record_id)
    except (TypeError, ValueError):
        return None


# ===== Generate PDF =====
@router.get("/templates/{tpl_id}/generate")
def generate(tpl_id: int, record_id: Optional[str] = Query(None), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record_id = _parse_record_id(record_id)
    tpl = db.query(DocumentTemplate).options(joinedload(DocumentTemplate.fields)).filter(DocumentTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404, "Template not found")
    try:
        path = generate_pdf(db, tpl, record_id=record_id)
    except Exception as e:
        raise HTTPException(500, f"PDF generation failed: {e}")

    # Log
    log = GeneratedDocument(template_id=tpl_id, record_id=record_id, file_path=path, generated_by=user.id)
    db.add(log)
    db.commit()

    return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path))


# ============================================================================
# REAL-TIME DATA OVERLAY — Live preview JSON
# ============================================================================
@router.get("/templates/{tpl_id}/preview-data")
def preview_data(
    tpl_id: int,
    record_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record_id = _parse_record_id(record_id)
    """Return the merged field+value payload for the document customize page
    so the frontend can render a live overlay on the background image.
    """
    tpl = (
        db.query(DocumentTemplate)
        .options(joinedload(DocumentTemplate.fields))
        .filter(DocumentTemplate.id == tpl_id)
        .first()
    )
    if not tpl:
        raise HTTPException(404, "Template not found")
    record = _load_record(db, tpl.data_source, record_id)
    payload = _build_overlay_payload(tpl, record, db=db)
    payload["record_id"] = record_id
    payload["record_loaded"] = record is not None
    return payload


# ============================================================================
# IN-BROWSER PRINT VIEW — renders HTML page with image + data overlay and
# auto-triggers the browser's native print dialog. NOT a PDF download.
# ============================================================================
@router.get("/templates/{tpl_id}/print", response_class=HTMLResponse)
def print_document(
    tpl_id: int,
    record_id: Optional[str] = Query(None),
    auto: int = Query(1, description="Auto-trigger window.print() after image loads"),
    fit: int = Query(0, description="Scale A4 page to fit viewport width (for in-app modal preview)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record_id = _parse_record_id(record_id)
    """Render an HTML page that overlays the candidate/demand data on top of
    the document's background image at the exact field coordinates, and
    immediately invokes the browser's print dialog. This is the user-visible
    'Print' behaviour from the Documents tab — NO file is downloaded, no new
    tab is opened with a blank PDF; instead the standard browser print panel
    appears with the live data already overlaid.
    """
    tpl = (
        db.query(DocumentTemplate)
        .options(joinedload(DocumentTemplate.fields))
        .filter(DocumentTemplate.id == tpl_id)
        .first()
    )
    if not tpl:
        raise HTTPException(404, "Template not found")

    record = _load_record(db, tpl.data_source, record_id)
    payload = _build_overlay_payload(tpl, record, db=db)

    # Parse the designer meta-fence from the description so Content-tab notes
    # actually render on the printed page (audit Fix 3 — previously the notes
    # were saved via /templates PUT but never appeared anywhere on output).
    from app.services.overlay_engine import parse_designer_meta
    designer_meta = parse_designer_meta(tpl.description or "")
    content_notes = (designer_meta.get("notes") or "").strip()

    # Log the print action
    log = GeneratedDocument(
        template_id=tpl_id, record_id=record_id,
        file_path=f"print:{tpl.name}", generated_by=user.id,
    )
    db.add(log); db.commit()

    page_w = float(tpl.page_width or 595)   # PDF points (1pt = 1/72 inch)
    page_h = float(tpl.page_height or 842)
    # Convert PDF points to mm for CSS (1pt = 0.3528 mm)
    PT_TO_MM = 25.4 / 72.0
    page_w_mm = page_w * PT_TO_MM
    page_h_mm = page_h * PT_TO_MM

    # Group fields by page
    pages: dict = {}
    for f in payload["fields"]:
        pages.setdefault(f["page"], []).append(f)
    if not pages:
        pages[1] = []
    page_numbers = sorted(pages.keys())

    bg_url = payload["template"]["background_url"] or ""

    # --- Robust background for mobile printing -------------------------------
    # Android/Chrome print frequently drops a network-loaded <img> background
    # (timing/auth/"Background graphics" off), which is exactly why a printed
    # page can come out almost blank with only the top text + barcodes (the
    # overlay) visible. To make the background bullet-proof we inline it as a
    # base64 data: URI so it is part of the HTML document itself and cannot
    # fail to load, and below we force print-color-adjust:exact so the browser
    # is not allowed to strip it on print.
    if bg_url.startswith("/static/"):
        import base64 as _b64
        import mimetypes as _mt
        _fs_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "static",
            bg_url[len("/static/"):],
        )
        try:
            with open(_fs_path, "rb") as _bf:
                _raw = _bf.read()
            _mime = _mt.guess_type(_fs_path)[0] or "image/jpeg"
            bg_url = f"data:{_mime};base64," + _b64.b64encode(_raw).decode("ascii")
        except OSError as _be:
            # File missing/unreadable — keep the network URL as a fallback.
            print(f"[documents] could not inline background {_fs_path}: {_be}")

    # Build HTML
    field_html_per_page = []
    for pn in page_numbers:
        items_html = []
        for f in pages[pn]:
            # PDF coord origin = bottom-left → CSS origin = top-left.
            #
            # COORDINATE FIX (Problem 6 — "documents coordinate wrong when
            # printing"): the designer canvas places each field as a BOX whose
            # top = (page_h - y - height) and whose text sits at the BOTTOM of
            # that box (because the stored y is the PDF text baseline / box
            # bottom). The old print code instead used (font_size * 1.2) for
            # the vertical offset, so whenever a field's height differed from
            # its font size (almost always) the printed text drifted away from
            # where it was placed in the designer. We now mirror the designer's
            # exact box model — top = page_h - y - height, render the text
            # bottom-anchored inside a box of the field's real height — so the
            # printed output lands pixel-for-pixel where it was designed.
            fld_h = float(f["height"] or max(f["font_size"] * 1.25, 14))
            css_left_mm = f["x"] * PT_TO_MM
            css_top_mm = (page_h - f["y"] - fld_h) * PT_TO_MM
            css_width_mm = f["width"] * PT_TO_MM
            css_height_mm = fld_h * PT_TO_MM
            # Use pt directly for font-size (most accurate for print).
            # display:flex + align-items:flex-end bottom-anchors the text to the
            # PDF baseline, exactly matching the reportlab drawString() output.
            style = (
                f"left:{css_left_mm:.2f}mm;"
                f"top:{css_top_mm:.2f}mm;"
                f"width:{css_width_mm:.2f}mm;"
                f"height:{css_height_mm:.2f}mm;"
                f"font-size:{f['font_size']:.1f}pt;"
                f"color:{f['color']};"
                f"text-align:{f['align']};"
                f"font-weight:{'bold' if f['font_bold'] else 'normal'};"
                f"font-style:{'italic' if f['font_italic'] else 'normal'};"
            )
            if f.get("field_type") == "barcode" and f.get("barcode_img"):
                # Render the actual barcode image, sized to the field box.
                bc_h_mm = max(float(f["height"] or 32), 16) * PT_TO_MM
                bc_style = (
                    f"left:{css_left_mm:.2f}mm;"
                    f"top:{(page_h - f['y'] - (f['height'] or 32)) * PT_TO_MM:.2f}mm;"
                    f"width:{css_width_mm:.2f}mm;"
                    f"height:{bc_h_mm:.2f}mm;"
                    f"text-align:{f['align']};"
                )
                items_html.append(
                    f'<div class="ov-field ov-barcode" style="{bc_style}" '
                    f'data-field-key="{f["field_key"]}" data-label="{f["label"]}">'
                    f'<img src="{f["barcode_img"]}" alt="barcode" '
                    f'style="height:100%;max-width:100%;object-fit:contain;'
                    f'object-position:{f["align"]} bottom;"></div>'
                )
                continue
            if f.get("field_type") == "trade_table":
                # Render the trade table as an HTML <table> matching the PDF
                # _draw_trade_table output (audit Fix 4). Column spec follows
                # the same convention: meta.columns is a CSV of keys, plus
                # built-in _sr / _assigned / _available.
                import html as _html_esc
                tt_meta = f.get("meta") or {}
                default_cols = [
                    ("_sr", "Sr#", "center"),
                    ("trade", "Trade / Visa Category", "left"),
                    ("quantity", "Qty", "center"),
                    ("_assigned", "Assigned", "center"),
                    ("_available", "Available", "center"),
                    ("salary", "Salary", "right"),
                    ("contract_period", "Contract", "center"),
                ]
                col_defs = tt_meta.get("columns")
                cols: list = []
                if isinstance(col_defs, list) and col_defs:
                    for spec in col_defs:
                        if isinstance(spec, dict):
                            cols.append((str(spec.get("key") or ""),
                                         str(spec.get("label") or spec.get("key") or ""),
                                         str(spec.get("align") or "left").lower()))
                elif isinstance(col_defs, str) and col_defs.strip():
                    defaults = {k: (lbl, al) for (k, lbl, al) in default_cols}
                    for k in [c.strip() for c in col_defs.split(",") if c.strip()]:
                        lbl, al = defaults.get(k, (k.replace("_", " ").title(), "left"))
                        cols.append((k, lbl, al))
                if not cols:
                    cols = default_cols
                show_header = bool(tt_meta.get("show_header", True))
                show_border = bool(tt_meta.get("border", True))
                header_fill = str(tt_meta.get("header_fill", "#e5e7eb"))
                zebra       = bool(tt_meta.get("zebra", False))
                rows_data = f.get("trade_rows") or []
                # Build the <table>
                tr_lines = []
                if show_header:
                    th_cells = "".join(
                        f'<th style="text-align:{al};background:{header_fill};'
                        f'border:{"1px solid #94a3b8" if show_border else "none"};'
                        f'padding:2px 4px;font-weight:700;">{_html_esc.escape(lbl)}</th>'
                        for (_k, lbl, al) in cols)
                    tr_lines.append(f"<tr>{th_cells}</tr>")
                for ri, row in enumerate(rows_data):
                    bg = "#f8fafc" if (zebra and ri % 2 == 1) else "transparent"
                    td_cells = []
                    for (k, _lbl, al) in cols:
                        v = row.get(k, "")
                        if k == "salary" and v not in (None, ""):
                            try:
                                v = f"{float(v):,.0f}"
                            except (TypeError, ValueError):
                                pass
                        td_cells.append(
                            f'<td style="text-align:{al};background:{bg};'
                            f'border:{"1px solid #94a3b8" if show_border else "none"};'
                            f'padding:2px 4px;">{_html_esc.escape("" if v is None else str(v))}</td>')
                    tr_lines.append(f"<tr>{''.join(td_cells)}</tr>")
                tt_style = (
                    f"left:{css_left_mm:.2f}mm;"
                    f"top:{(page_h - f['y'] - fld_h) * PT_TO_MM:.2f}mm;"
                    f"width:{css_width_mm:.2f}mm;"
                    f"font-size:{f['font_size']:.1f}pt;"
                    f"color:{f['color']};"
                )
                items_html.append(
                    f'<div class="ov-field ov-trade-table" style="position:absolute;{tt_style}" '
                    f'data-field-key="{f["field_key"]}" data-label="{f["label"]}">'
                    f'<table style="width:100%;border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;">'
                    f'{"".join(tr_lines)}</table></div>'
                )
                continue
            text = (f["value"] or "").replace("<", "&lt;").replace(">", "&gt;")
            align_cls = "ov-" + (f["align"] if f["align"] in ("left", "center", "right") else "left")
            items_html.append(
                f'<div class="ov-field {align_cls}" style="{style}" data-field-key="{f["field_key"]}" '
                f'data-label="{f["label"]}">{text}</div>'
            )
        field_html_per_page.append((pn, "\n".join(items_html)))

    # Build the Content-tab notes block (audit Fix 3). The notes are stored
    # in tpl.description inside the designer meta-fence and must appear above
    # the page footer on every printed page, exactly matching the PDF
    # _draw_content_notes() implementation.
    notes_block_html = ""
    if content_notes:
        import html as _html
        notes_block_html = (
            '<div class="doc-notes">'
            + _html.escape(content_notes).replace("\n", "<br>")
            + '</div>'
        )

    page_blocks = []
    for pn, items_html in field_html_per_page:
        page_blocks.append(f'''
        <section class="doc-page" data-page="{pn}">
            {('<img class="doc-bg" src="' + bg_url + '" alt="background" crossorigin="anonymous">') if bg_url else '<div class="no-bg">No background image set for this template.</div>'}
            <div class="doc-overlay">
                {items_html}
            </div>
            {notes_block_html}
        </section>
        ''')

    record_label = ""
    if record:
        record_label = getattr(record, "full_name", None) or getattr(record, "file_number", None) or getattr(record, "company_name", None) or f"#{record_id}"

    auto_print_script = ""
    if auto:
        auto_print_script = """
        let _printed = false;
        function tryPrint(){
            if (_printed) return;
            _printed = true;
            // Slight delay so all images decode before print panel opens
            setTimeout(() => { window.focus(); window.print(); }, 250);
        }
        const imgs = document.querySelectorAll('img.doc-bg');
        if (imgs.length === 0) {
            tryPrint();
        } else {
            let loaded = 0;
            imgs.forEach(im => {
                if (im.complete && im.naturalWidth > 0) { loaded++; }
                else {
                    im.addEventListener('load', () => { if (++loaded === imgs.length) tryPrint(); });
                    im.addEventListener('error', () => { if (++loaded === imgs.length) tryPrint(); });
                }
            });
            if (loaded === imgs.length) tryPrint();
            // Safety fallback in case images stall
            setTimeout(tryPrint, 3500);
        }
        """

    body_class = "fit-mode" if fit else ""
    # Fit-to-viewport scaler: when rendered inside the modal iframe, scale the
    # A4 page so it fills the iframe width. Print-mode CSS overrides this back
    # to true 1:1 A4 size, so the actual print output is unaffected — the user
    # sees a properly-sized preview that matches what will physically print.
    fit_script = """
        (function(){
            var page = document.querySelector('.doc-page');
            if (!page) return;
            function apply(){
                var avail = document.documentElement.clientWidth - 16; // gutters
                var pageW = page.getBoundingClientRect().width;
                if (pageW < 10) return;
                // page may already be scaled; un-scale first for accurate measurement
                document.body.style.setProperty('--fit-scale', '1');
                pageW = page.getBoundingClientRect().width;
                var scale = Math.min(1.6, Math.max(0.5, avail / pageW));
                document.body.style.setProperty('--fit-scale', String(scale));
            }
            apply();
            window.addEventListener('resize', apply);
            // Re-apply after fonts/images settle (background image especially)
            setTimeout(apply, 100);
            setTimeout(apply, 600);
        })();
    """ if fit else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Print · {tpl.name} · {record_label}</title>
    <style>
        @page {{
            size: {page_w_mm:.2f}mm {page_h_mm:.2f}mm;
            margin: 0;
        }}
        * {{ box-sizing: border-box; }}
        html, body {{
            margin: 0; padding: 0; background: #525659;
            font-family: Arial, Helvetica, sans-serif;
            -webkit-print-color-adjust: exact !important;
            print-color-adjust: exact !important;
            color-adjust: exact !important;
        }}
        .toolbar {{
            position: sticky; top: 0; z-index: 1000;
            background: #323639; color: #fff; padding: 10px 16px;
            display: flex; align-items: center; justify-content: space-between;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }}
        .toolbar h1 {{ font-size: 14px; margin: 0; font-weight: 600; }}
        .toolbar .sub {{ font-size: 11px; color: #ccc; margin-top: 2px; }}
        .toolbar button {{
            background: #4285f4; color: #fff; border: 0;
            padding: 8px 18px; border-radius: 4px; cursor: pointer;
            font-size: 13px; font-weight: 600;
            display: inline-flex; align-items: center; gap: 6px;
        }}
        .toolbar button:hover {{ background: #3367d6; }}
        .toolbar button.secondary {{ background: #555; }}
        .toolbar button.secondary:hover {{ background: #777; }}
        .doc-page {{
            position: relative;
            width: {page_w_mm:.2f}mm;
            height: {page_h_mm:.2f}mm;
            background: #fff;
            margin: 20px auto;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4);
            overflow: hidden;
            page-break-after: always;
        }}
        /* === fit-to-viewport mode (used by in-app print-preview modal) ===
           The A4 page is exactly 209.9mm wide = ~793px at 96 DPI. Inside the
           modal iframe (which is ~1000px wide) the page would otherwise look
           small, floating, and "cropped" because of the gray modal margins.
           When fit=1 is requested, we use a CSS variable + transform: scale()
           to fluidly scale the page to ~96% of the iframe width while keeping
           the printed output (.doc-page in @media print below) at true 1:1 A4. */
        body.fit-mode {{
            background: #525659;
            min-height: 100vh;
            padding: 18px 0;
        }}
        body.fit-mode .toolbar {{ display: none !important; }}
        body.fit-mode .doc-page {{
            margin: 18px auto;
            /* JS sets --fit-scale = (viewport-width * 0.96) / page-pixel-width.
               Until JS runs, default to 1 so the layout never collapses. */
            transform: scale(var(--fit-scale, 1));
            transform-origin: top center;
        }}
        /* The transform shrinks the visual box but the layout box stays at full
           A4 size, so add a height correction wrapper-spacer via margin-bottom. */
        body.fit-mode .doc-page + .doc-page {{
            margin-top: calc(18px - ({page_h_mm:.2f}mm * (1 - var(--fit-scale, 1))));
        }}
        .doc-bg {{
            position: absolute; top: 0; left: 0;
            width: 100%; height: 100%;
            object-fit: fill;
            user-select: none;
            pointer-events: none;
            -webkit-print-color-adjust: exact !important;
            print-color-adjust: exact !important;
        }}
        .no-bg {{
            padding: 40px; text-align: center; color: #999;
            font-size: 14px;
        }}
        /* When printing on physical pre-printed letterhead, hide the
           "no background" placeholder so only field overlays print
           and align over the real letterhead paper. */
        @media print {{
            .no-bg {{ display: none !important; }}
        }}
        .doc-overlay {{
            position: absolute; top: 0; left: 0;
            width: 100%; height: 100%;
        }}
        .ov-field {{
            position: absolute;
            font-family: Arial, Helvetica, sans-serif;
            line-height: 1.05;
            white-space: nowrap;
            overflow: visible;
            /* TOP-anchor the text inside the box — identical to the designer
               canvas (.field-marker, line-height 1.05, text flows from the
               top of the box). The designer is the single source of truth the
               user positions against, so the print MUST mirror it pixel-for-
               pixel: box top = (page_h - y - height), text at top of box. */
            display: flex;
            align-items: flex-start;
            box-sizing: border-box;
        }}
        .ov-field.ov-left   {{ justify-content: flex-start; }}
        .ov-field.ov-center {{ justify-content: center; }}
        .ov-field.ov-right  {{ justify-content: flex-end; }}
        /* Content-tab notes — printed at the bottom of every page above the
           browser footer, mirroring pdf_engine._draw_content_notes. */
        .doc-notes {{
            position: absolute;
            left: 30px;
            right: 30px;
            bottom: 30px;
            font-family: Arial, Helvetica, sans-serif;
            font-style: italic;
            font-size: 8pt;
            color: #475569;
            line-height: 1.35;
            white-space: pre-wrap;
        }}
        @media print {{
            html, body {{
                background: #fff !important;
                padding: 0 !important;
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
                color-adjust: exact !important;
            }}
            .toolbar {{ display: none !important; }}
            /* CRITICAL: reset the fit-mode transform so printed output is true
               1:1 A4 size — exactly matching the designer canvas, no scaling. */
            body.fit-mode .doc-page,
            .doc-page {{
                margin: 0 !important;
                box-shadow: none !important;
                transform: none !important;
                page-break-after: always;
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }}
            .doc-bg {{
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }}
            .doc-page:last-child {{ page-break-after: auto; }}
        }}
    </style>
</head>
<body class="{body_class}">
    <div class="toolbar">
        <div>
            <h1>{tpl.name}</h1>
            <div class="sub">Record: {record_label or '— blank preview —'} · {len(payload["fields"])} mapped fields · {len(page_numbers)} page(s)</div>
        </div>
        <div style="display:flex; gap:8px;">
            <button class="secondary" onclick="window.close()">Close</button>
            <button onclick="window.print()">🖨️ Print Now</button>
        </div>
    </div>

    {''.join(page_blocks)}

    <script>
        {auto_print_script}
        {fit_script}
    </script>
</body>
</html>
"""
    return HTMLResponse(html)
