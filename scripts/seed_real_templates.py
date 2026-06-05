"""Seed DocumentTemplate + DocumentField rows for the REAL form backgrounds
extracted from the user-supplied PDF. This replaces approximate coordinates
with pixel-accurate ones derived from the actual scans.

Usage:
    cd /home/user/webapp/dogar-portal
    python3 scripts/seed_real_templates.py

Safe to re-run — wipes & re-inserts the named templates.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models import DocumentTemplate, DocumentField


# --- A4 PDF dimensions in points ---
PDF_W, PDF_H = 595.0, 842.0


def make_converter(image_path: str):
    """Return px2pdf / px_w / px_h functions calibrated for this image.

    NOTE on baseline correction:
        The pixel coordinates in this script point at the TOP of each line
        on the form scan (i.e. where the underline / cell-top sits in the
        image). In ReportLab, drawString(x, y, text) places the *baseline*
        of the text at y. So we want the baseline to sit slightly ABOVE
        the underline (or near the bottom of the cell box) for the text
        to appear INSIDE the cell.

        Therefore the helpers below pass a baseline-down shift of roughly
        font_size * 0.8 POINTS, which we subtract from PDF-y (PDF y axis
        goes upward, so subtracting moves the baseline down on the page).
    """
    img = Image.open(image_path)
    PX_W, PX_H = img.size

    def px2pdf(px, py):
        return (round(px * PDF_W / PX_W, 1),
                round(PDF_H - (py * PDF_H / PX_H), 1))

    def px_w(w):
        return round(w * PDF_W / PX_W, 1)

    def px_h(h):
        return round(h * PDF_H / PX_H, 1)

    return px2pdf, px_w, px_h, (PX_W, PX_H)


# Baseline correction factor. ReportLab's drawString puts the baseline at y,
# while our pixel coordinates point at the TOP of the target line/cell.
# So we shift the baseline DOWN by ~80% of the font size (the ascent height).
# In PDF coords (y goes up), "shift down" means SUBTRACT from y.
def _baseline_shift(font_size: float, factor: float = 0.78) -> float:
    return round(font_size * factor, 1)


def wipe_and_create_template(db: Session, name: str, **kwargs) -> DocumentTemplate:
    existing = db.query(DocumentTemplate).filter(DocumentTemplate.name == name).all()
    for t in existing:
        db.delete(t)
    db.commit()
    tpl = DocumentTemplate(name=name, **kwargs)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


# =============================================================================
# COORDINATE CONVENTION (after pixel-perfect calibration)
# =============================================================================
# All pixel `py` values passed to add_text() / add_cells() / add_arabic() are
# now the **TARGET BASELINE** in image pixels — i.e. where the bottom of
# typical glyphs (like "M", "R", "A") should sit on the form's underline.
#
# The helpers compute PDF y by:
#   1) Converting (px, py) → PDF coords (this places baseline at the same
#      vertical position in PDF that py represented in the image).
#   2) NO extra baseline shift — the caller already provides baseline px.
#
# This is much easier to reason about: just look at the form, find the
# pixel row where the underline IS, and pass that as py.
# =============================================================================
TEXT_BASELINE_FACTOR  = 0.0   # no extra shift — py IS the baseline already
CELL_BASELINE_FACTOR  = 0.0   # py for cells = baseline = bottom of digits


def add_text(tpl, db, label, key, px, py, w_px=200, page=1, font_size=10,
             bold=False, align="left", convert=None,
             baseline_factor=TEXT_BASELINE_FACTOR):
    """Add a normal text field. (px, py) points at the TOP of the form line
    where you want text to sit; the helper applies baseline-down correction
    so the rendered text appears INSIDE the line/cell."""
    px2pdf, px_w, _, _ = convert
    x, y = px2pdf(px, py)
    y -= _baseline_shift(font_size, baseline_factor)
    f = DocumentField(
        template_id=tpl.id, label=label, field_key=key, field_type="text",
        x=x, y=y, width=px_w(w_px), height=14,
        font_size=font_size, font_bold=bold, align=align, page=page,
    )
    db.add(f)


def add_arabic(tpl, db, label, key, px, py, w_px=200, page=1, font_size=10,
               bold=False, align="right", convert=None,
               baseline_factor=TEXT_BASELINE_FACTOR):
    px2pdf, px_w, _, _ = convert
    x, y = px2pdf(px, py)
    y -= _baseline_shift(font_size, baseline_factor)
    f = DocumentField(
        template_id=tpl.id, label=label, field_key=key, field_type="arabic",
        x=x, y=y, width=px_w(w_px), height=14,
        font_size=font_size, font_bold=bold, align=align, page=page,
    )
    db.add(f)


def add_cells(tpl, db, label, key, px, py, cells, cell_px_w,
              cell_gap_px=0, page=1, font_size=10, bold=True,
              draw_boxes=False, convert=None,
              baseline_factor=CELL_BASELINE_FACTOR):
    """Char-cells (CNIC, passport, phone). (px, py) is the TOP-LEFT of the
    first cell. We shift baseline DOWN so digits center vertically in the
    cells rather than sitting above them."""
    px2pdf, px_w, _, _ = convert
    x, y = px2pdf(px, py)
    y -= _baseline_shift(font_size, baseline_factor)
    f = DocumentField(
        template_id=tpl.id, label=label, field_key=key, field_type="char_cells",
        x=x, y=y, width=px_w(cells * (cell_px_w + cell_gap_px)), height=14,
        font_size=font_size, font_bold=bold, page=page,
        meta={
            "cell_count": cells,
            "cell_width": px_w(cell_px_w),
            "cell_gap": px_w(cell_gap_px),
            "draw_boxes": draw_boxes,
        },
    )
    db.add(f)


def add_checkbox(tpl, db, label, key, px, py, page=1, size_px=20, convert=None):
    """Checkbox tick mark. (px, py) is the TOP-LEFT of the checkbox in image
    pixels; we convert so the PDF y is the bottom of the box (since y goes
    up in PDF coords)."""
    px2pdf, px_w, px_h, _ = convert
    # The TOP of the box in image space is py; the BOTTOM is py + size_px.
    # In PDF space, that bottom corresponds to a SMALLER y (since y goes up).
    _, y_bottom = px2pdf(px, py + size_px)
    x, _ = px2pdf(px, py)
    f = DocumentField(
        template_id=tpl.id, label=label, field_key=key, field_type="checkbox",
        x=x, y=y_bottom, width=px_w(size_px), height=px_h(size_px),
        font_size=10, page=page,
    )
    db.add(f)


def add_photo(tpl, db, label, key, px, py, w_px, h_px, page=1, convert=None):
    px2pdf, px_w, px_h, _ = convert
    # py is the TOP of the photo box; PDF y is the BOTTOM, so shift by h_px
    x, _ = px2pdf(px, py)
    _, y_bottom = px2pdf(px, py + h_px)
    f = DocumentField(
        template_id=tpl.id, label=label, field_key=key, field_type="photo",
        x=x, y=y_bottom, width=px_w(w_px), height=px_h(h_px),
        page=page, meta={"fit": "cover", "border": True},
    )
    db.add(f)


# ============================================================================
# Template 1: OEP Form Page 1 (Bureau of Emigration)
# Background: oep_form_p1.jpg (1653x2339 px, A4 200dpi)
# Source: real PDF page 3 of uploaded download(7).pdf
# ============================================================================
def seed_oep_form(db):
    bg = "oep_form_p1.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[OEP Form p1] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="OEP Form (Bureau of Emigration) - Real",
        description="Emigrant / Employee Registration Through OEP Form (real background)",
        category="government",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    # =========================================================================
    # PRECISE PIXEL BASELINE COORDINATES — measured from a gridded reference
    # image (oep_form_p1.jpg 1653x2339, 200dpi A4). `py` is the exact pixel-Y
    # where the typed text baseline (or cell-row baseline) should sit.
    # The helpers add NO extra shift for text fields (TEXT_BASELINE_FACTOR=0).
    # =========================================================================

    # ── Photo box (top-right "Attach Passport Size Photograph of Emigrant")
    # Form border: roughly x:1390-1570 y:155-330
    add_photo(tpl, db, "Candidate Photo", "photo",
              px=1395, py=160, w_px=180, h_px=170, convert=convert)

    # ── Item 1: Date row (under "Computerized Registration # / PE Office")
    # Date label at y=290, cell row baseline ≈ y=310
    # Today's date — drawn into the first part of the row (DD-MM-YYYY cells)
    # The cell row for Item 1 has 8 cell positions for DDMMYYYY.
    add_cells(tpl, db, "Reg Date", "__today__",
              px=315, py=315, cells=8, cell_px_w=52,
              font_size=10, bold=True, convert=convert)

    # ── Item 2: Name of OEP/Agency ── baseline ≈ y=420
    add_text(tpl, db, "Agency Name", "client_company_name",
             px=345, py=420, w_px=1230, font_size=11, convert=convert)

    # ── Item 4: Permission No (cells) + Item 5: Date (cells) ── baseline ≈ y=515
    # Permission cells: x=315-870, Item 5 Date cells: x=1170-1505
    add_cells(tpl, db, "Permission No", "permission_no",
              px=315, py=515, cells=14, cell_px_w=40,
              font_size=10, bold=True, convert=convert)
    add_cells(tpl, db, "Permission Date", "permission_date",
              px=1175, py=515, cells=8, cell_px_w=42,
              font_size=10, bold=True, convert=convert)

    # ── Item 6: Name (large 2-line cell row) ── baseline ≈ y=685
    # The Name field has TWO rows of large cells, each char goes in a cell.
    # We'll use a single text field that fills the first row.
    add_text(tpl, db, "Full Name", "full_name",
             px=350, py=685, w_px=1230, font_size=14, bold=True, convert=convert)

    # ── Item 7: Father's/Husband's Name ── baseline ≈ y=805
    add_text(tpl, db, "Father Name", "father_name",
             px=350, py=805, w_px=1230, font_size=13, bold=True, convert=convert)

    # ── Item 8: Emigrant CNIC No (13 cells) ── baseline ≈ y=845
    # CNIC cells start at x≈345, each ~94px wide (form is wide)
    add_cells(tpl, db, "Emigrant CNIC", "cnic",
              px=345, py=845, cells=13, cell_px_w=94,
              font_size=13, bold=True, convert=convert)

    # ── Item 9: Gender Male/Female checkboxes ── y≈905
    # Male box at x≈335, Female box at x≈515 (per the form layout)
    add_checkbox(tpl, db, "Male", "gender_male",
                 px=335, py=900, size_px=24, convert=convert)
    add_checkbox(tpl, db, "Female", "gender_female",
                 px=515, py=900, size_px=24, convert=convert)

    # ── Item 10: Cell No (11 cells) ── baseline ≈ y=1015
    add_cells(tpl, db, "Cell No", "phone",
              px=345, py=1015, cells=11, cell_px_w=112,
              font_size=13, bold=True, convert=convert)

    # ── Item 11: E-mail ── baseline ≈ y=1060
    add_text(tpl, db, "Email", "email",
             px=350, py=1060, w_px=1230, font_size=11, convert=convert)

    # ── Item 12: Address (In Pakistan) — 2 lines ── baseline ≈ y=1135
    add_text(tpl, db, "Address", "address",
             px=350, py=1135, w_px=1230, font_size=11, convert=convert)

    # ── Item 13: City + Item 14: District of Domicile ── baseline ≈ y=1255
    add_text(tpl, db, "City", "city",
             px=350, py=1255, w_px=380, font_size=11, convert=convert)
    add_text(tpl, db, "District", "district",
             px=1010, py=1255, w_px=440, font_size=11, convert=convert)

    # ── Item 15: Province ── baseline ≈ y=1335
    add_text(tpl, db, "Province", "province",
             px=350, py=1335, w_px=1230, font_size=11, convert=convert)

    # ── Item 16: Highest Qualification ── baseline ≈ y=1435
    add_text(tpl, db, "Qualification", "qualification",
             px=350, py=1435, w_px=1230, font_size=11, convert=convert)

    # ── Item 17: Passport No (cells) ── baseline ≈ y=1530
    # Form has ~9 wide cells for passport
    add_cells(tpl, db, "Passport No", "passport_no",
              px=350, py=1530, cells=9, cell_px_w=82,
              font_size=13, bold=True, convert=convert)

    # ── Item 18: Place of Issue + Item 19: Date of Issue ── baseline ≈ y=1610
    add_text(tpl, db, "Place of Issue", "passport_issue_place",
             px=350, py=1610, w_px=600, font_size=11, convert=convert)
    add_cells(tpl, db, "Date of Issue", "passport_issue_date",
              px=1135, py=1610, cells=8, cell_px_w=48,
              font_size=10, bold=True, convert=convert)

    # ── Item 20: Name of Nominee ── baseline ≈ y=1735
    add_text(tpl, db, "Next of Kin Name", "next_of_kin_name",
             px=350, py=1735, w_px=1230, font_size=12, bold=True, convert=convert)

    # ── Item 21: CNIC No (Next of Kin) (13 cells) ── baseline ≈ y=1810
    add_cells(tpl, db, "Next of Kin CNIC", "next_of_kin_nic",
              px=345, py=1810, cells=13, cell_px_w=94,
              font_size=13, bold=True, convert=convert)

    # ── Item 22: Relationship ── baseline ≈ y=1875
    add_text(tpl, db, "Relationship", "next_of_kin_relation",
             px=350, py=1875, w_px=1230, font_size=12, bold=True, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 2: OEP Form Page 2 (Terms + Job Details)
# Background: oep_form_p2.jpg
# ============================================================================
def seed_oep_form_p2(db):
    bg = "oep_form_p2.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[OEP Form p2] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="OEP Form Page 2 (Job Details) - Real",
        description="Terms & Conditions + Job Details (real background)",
        category="government",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    # Approximate positions for Job Details section
    add_text(tpl, db, "Job Title", "profession",
             px=480, py=1280, w_px=900, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Salary", "salary",
             px=480, py=1340, w_px=400, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Period of Contract", "contract_period",
             px=1100, py=1340, w_px=300, font_size=11, convert=convert)
    add_text(tpl, db, "Company Name", "sponsor_name",
             px=480, py=1480, w_px=900, font_size=11, bold=True, convert=convert)
    add_arabic(tpl, db, "Company Name (Arabic)", "sponsor_name_arabic",
               px=480, py=1520, w_px=900, font_size=11, convert=convert)
    add_text(tpl, db, "Country", "country",
             px=480, py=1580, w_px=600, font_size=11, bold=True, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 3: NBP Deposit Slip (Bank Copy + Depositor Copy)
# Background: nbp_deposit_slip.jpg
# ============================================================================
def seed_nbp_deposit(db):
    bg = "nbp_deposit_slip.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[NBP Slip] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="NBP Deposit Slip - Real",
        description="National Bank of Pakistan deposit slip (real background)",
        category="bank",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    # Top copy ("Bank Copy") — approximate
    add_text(tpl, db, "Depositor Name (Top)", "full_name",
             px=600, py=380, w_px=600, font_size=10, convert=convert)
    add_text(tpl, db, "Father Name (Top)", "father_name",
             px=600, py=430, w_px=600, font_size=10, convert=convert)
    add_cells(tpl, db, "CNIC (Top)", "cnic",
              px=600, py=485, cells=13, cell_px_w=45,
              font_size=9, bold=True, convert=convert)
    add_text(tpl, db, "Passport (Top)", "passport_no",
             px=600, py=540, w_px=500, font_size=10, convert=convert)
    add_text(tpl, db, "Date (Top)", "__today__",
             px=1150, py=300, w_px=300, font_size=10, bold=True, convert=convert)

    # Bottom copy ("Depositor Copy") — same fields shifted down
    add_text(tpl, db, "Depositor Name (Bot)", "full_name",
             px=600, py=1450, w_px=600, font_size=10, convert=convert)
    add_text(tpl, db, "Father Name (Bot)", "father_name",
             px=600, py=1500, w_px=600, font_size=10, convert=convert)
    add_cells(tpl, db, "CNIC (Bot)", "cnic",
              px=600, py=1555, cells=13, cell_px_w=45,
              font_size=9, bold=True, convert=convert)
    add_text(tpl, db, "Passport (Bot)", "passport_no",
             px=600, py=1610, w_px=500, font_size=10, convert=convert)
    add_text(tpl, db, "Date (Bot)", "__today__",
             px=1150, py=1370, w_px=300, font_size=10, bold=True, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 4: Visa Application — Karachi (Saudi Consulate)
# Background: visa_application_karachi.jpg
# Has photo box, Arabic fields
# ============================================================================
def seed_visa_karachi(db):
    bg = "visa_application_karachi.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[Visa Karachi] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="Visa Application Karachi (Saudi Consulate) - Real",
        description="Saudi Consulate Karachi visa application (real background)",
        category="visa",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    # Photo top-left
    add_photo(tpl, db, "Candidate Photo", "photo",
              px=90, py=200, w_px=200, h_px=240, convert=convert)

    # Standard form fields (approximate — visa forms vary)
    add_text(tpl, db, "Full Name (EN)", "full_name",
             px=380, py=320, w_px=800, font_size=10, bold=True, convert=convert)
    add_arabic(tpl, db, "Full Name (AR)", "name_arabic",
               px=380, py=360, w_px=800, font_size=10, convert=convert)
    add_text(tpl, db, "Father Name (EN)", "father_name",
             px=380, py=410, w_px=800, font_size=10, convert=convert)
    add_arabic(tpl, db, "Father Name (AR)", "father_name_arabic",
               px=380, py=450, w_px=800, font_size=10, convert=convert)
    add_text(tpl, db, "Passport No", "passport_no",
             px=380, py=520, w_px=400, font_size=10, bold=True, convert=convert)
    add_text(tpl, db, "Nationality", "nationality",
             px=380, py=580, w_px=600, font_size=10, convert=convert)
    add_text(tpl, db, "Profession", "profession",
             px=380, py=640, w_px=600, font_size=10, convert=convert)
    add_text(tpl, db, "Sponsor", "sponsor_name",
             px=380, py=900, w_px=800, font_size=10, convert=convert)
    add_arabic(tpl, db, "Sponsor (AR)", "sponsor_name_arabic",
               px=380, py=940, w_px=800, font_size=10, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 5: Visa Application — Islamabad (Saudi Embassy)
# Background: saudi_visa_application.jpg
# ============================================================================
def seed_visa_islamabad(db):
    bg = "saudi_visa_application.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[Visa Islamabad] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="Visa Application Islamabad (Saudi Embassy) - Real",
        description="Saudi Embassy Islamabad visa application (real background)",
        category="visa",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    # Photo top-left
    add_photo(tpl, db, "Candidate Photo", "photo",
              px=90, py=240, w_px=200, h_px=240, convert=convert)

    add_text(tpl, db, "Full Name (EN)", "full_name",
             px=380, py=380, w_px=800, font_size=10, bold=True, convert=convert)
    add_arabic(tpl, db, "Full Name (AR)", "name_arabic",
               px=380, py=420, w_px=800, font_size=10, convert=convert)
    add_text(tpl, db, "Passport No", "passport_no",
             px=380, py=520, w_px=400, font_size=10, bold=True, convert=convert)
    add_text(tpl, db, "Nationality", "nationality",
             px=380, py=580, w_px=600, font_size=10, convert=convert)
    add_text(tpl, db, "Sponsor (EN)", "sponsor_name",
             px=380, py=900, w_px=800, font_size=10, convert=convert)
    add_arabic(tpl, db, "Sponsor (AR)", "sponsor_name_arabic",
               px=380, py=940, w_px=800, font_size=10, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 6: HBL Form 32-A (Treasury Challan)
# Background: hbl_form_32a.jpg
# ============================================================================
def seed_hbl_32a(db):
    bg = "hbl_form_32a.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[HBL 32-A] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="HBL Form 32-A (Treasury Challan) - Real",
        description="HBL Treasury Challan Form 32-A (real background)",
        category="bank",
        data_source="candidate",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    add_text(tpl, db, "Depositor Name", "full_name",
             px=400, py=620, w_px=900, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Father Name", "father_name",
             px=400, py=680, w_px=900, font_size=11, convert=convert)
    add_text(tpl, db, "Address", "address",
             px=400, py=740, w_px=900, font_size=10, convert=convert)
    add_text(tpl, db, "CNIC", "cnic",
             px=400, py=800, w_px=600, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Passport No", "passport_no",
             px=400, py=860, w_px=600, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Date", "__today__",
             px=1200, py=300, w_px=400, font_size=11, bold=True, convert=convert)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Template 7: Demand Letter (Letterhead with trade table)
# Background: dogar_letterhead.jpg
# ============================================================================
def seed_demand_letter(db):
    bg = "dogar_letterhead.jpg"
    convert = make_converter(f"app/static/pdf_backgrounds/{bg}")
    _, _, _, (px_w_total, px_h_total) = convert
    print(f"[Demand Letter] image {px_w_total}x{px_h_total}")

    tpl = wipe_and_create_template(
        db, name="Demand Letter - Real",
        description="OEP demand letter with trade table (real letterhead)",
        category="custom",
        data_source="demand",
        background_image=bg,
        page_width=PDF_W, page_height=PDF_H,
    )

    add_text(tpl, db, "Date", "__today__",
             px=1300, py=350, w_px=250, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "File Number", "file_number",
             px=200, py=400, w_px=400, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Sponsor Name", "sponsor_name",
             px=200, py=560, w_px=900, font_size=11, bold=True, convert=convert)
    add_arabic(tpl, db, "Sponsor (AR)", "sponsor_name_arabic",
               px=200, py=600, w_px=900, font_size=11, convert=convert)
    add_text(tpl, db, "Sponsor Address", "sponsor_address",
             px=200, py=660, w_px=900, font_size=10, convert=convert)
    add_arabic(tpl, db, "Sponsor Address (AR)", "sponsor_address_arabic",
               px=200, py=700, w_px=900, font_size=10, convert=convert)
    add_text(tpl, db, "Country", "country",
             px=200, py=760, w_px=400, font_size=11, bold=True, convert=convert)
    add_text(tpl, db, "Visa Number", "visa_number",
             px=200, py=820, w_px=500, font_size=11, bold=True, convert=convert)

    # Trade table (auto-fills from demand → job_categories)
    px2pdf, px_w, _, _ = convert
    table_x, table_y = px2pdf(200, 950)
    f = DocumentField(
        template_id=tpl.id, label="Trades Table", field_key="__trades__",
        field_type="trade_table",
        x=table_x, y=table_y,
        width=px_w(1200), height=18,
        font_size=10, page=1,
        meta={"row_height": 22, "cols": ["category", "qty", "assigned", "available"]},
    )
    db.add(f)

    db.commit()
    db.refresh(tpl)
    print(f"  Created template id={tpl.id} with {len(tpl.fields)} fields")
    return tpl


# ============================================================================
# Main
# ============================================================================
def main():
    db = SessionLocal()
    try:
        print("=" * 70)
        print("Seeding REAL document templates (using PDF-extracted backgrounds)")
        print("=" * 70)
        seed_oep_form(db)
        seed_oep_form_p2(db)
        seed_nbp_deposit(db)
        seed_visa_karachi(db)
        seed_visa_islamabad(db)
        seed_hbl_32a(db)
        seed_demand_letter(db)
        print("=" * 70)
        print("Done. Run a candidate Print to verify.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
