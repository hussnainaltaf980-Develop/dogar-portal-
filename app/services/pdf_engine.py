"""PDF Overlay Engine v2 — Professional Coordinate-Based Document Renderer.

Generates a filled PDF by overlaying coordinate-mapped fields on top of a real
document background image (visa form, bank slip, demand letter, etc.).

Supported field types (DocumentField.field_type):
    text        - plain Latin text
    arabic      - Arabic text (RTL, with proper shaping via arabic-reshaper + python-bidi)
    date        - same as text, but value is auto-formatted
    checkbox    - draws a tick / X mark if value is truthy
    static      - prints static_value verbatim
    char_cells  - one digit per cell across N evenly-spaced boxes
                  (use meta={"cell_count":13,"cell_width":18,"cell_gap":2})
    photo       - clips candidate.photo into the (x,y,width,height) box
                  (use meta={"fit":"cover","border":true})
    barcode     - Code128 barcode of the resolved value
    trade_table - auto-builds the Visa Category / Qty / Assigned / Available table

Key guarantees:
- ALWAYS produces a valid multi-page PDF (>= 1 page), even when the template
  has zero placed fields (auto-grid fallback).
- Real Arabic glyphs render correctly (no more tofu boxes).
- Candidate photo is clipped to the photo-box on OEP forms.
- Character-cell rows (CNIC 13-digit, passport, phone) auto-distribute.
"""
import os
import io
import re
from datetime import datetime, date
from typing import Optional, Any, List, Tuple, Dict

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black, white, Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode import code128, code39
from reportlab.graphics.barcode import eanbc, createBarcodeDrawing
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing

from app.core.config import settings
from app.models import (
    DocumentTemplate, DocumentField,
    Candidate, Demand, Client, Agent,
)


# ---------------------------------------------------------------------------
# Arabic shaping pipeline — reshapes Arabic so glyphs join correctly, then
# applies BiDi algorithm for proper right-to-left visual order.
# ---------------------------------------------------------------------------
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _ARABIC_LIBS_OK = True
except Exception as e:
    print(f"[pdf_engine] arabic libs not available: {e}")
    _ARABIC_LIBS_OK = False


_ARABIC_RANGES = [
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
]


# ---------------------------------------------------------------------------
# Value pre-formatters used by char_cells (set via field.meta["format"])
# ---------------------------------------------------------------------------
_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _format_date_ddmmyyyy(value: str) -> str:
    """Normalize a date-like string into a compact DDMMYYYY digit run.

    Accepts a range of common inputs (already-numeric or with separators
    or month names) and returns an 8-char digit string when possible.

        "13-Feb-2015"   -> "13022015"
        "13-02-2015"    -> "13022015"
        "2015-02-13"    -> "13022015"
        "13/02/15"      -> "13022015"
        "13022015"      -> "13022015"
        ""              -> ""
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # Already an 8-digit run? (DDMMYYYY)
    digits_only = "".join(ch for ch in s if ch.isdigit())
    if len(digits_only) == 8 and "-" not in s and "/" not in s and " " not in s:
        return digits_only

    # Token-based parsing
    import re
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", s) if t]

    day = month = year = ""
    if len(tokens) >= 3:
        t0, t1, t2 = tokens[0], tokens[1], tokens[2]
        # Detect ISO YYYY-MM-DD
        if t0.isdigit() and len(t0) == 4:
            year, month, day = t0, t1, t2
        else:
            day, month, year = t0, t1, t2

        # Resolve month name if needed
        m_low = month.lower()[:4]
        if not month.isdigit():
            for key, num in _MONTH_MAP.items():
                if m_low.startswith(key):
                    month = num
                    break

        # Zero-pad
        if day.isdigit():
            day = day.zfill(2)
        if month.isdigit():
            month = month.zfill(2)
        # Two-digit year -> 20xx
        if year.isdigit() and len(year) == 2:
            yr = int(year)
            year = ("20" if yr < 50 else "19") + year

        if day.isdigit() and month.isdigit() and year.isdigit() and len(year) == 4:
            return f"{day}{month}{year}"

    # Fallback: best-effort numeric extraction
    return digits_only[:8]


def _has_arabic(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        for lo, hi in _ARABIC_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _looks_non_latin(text: str) -> bool:
    """True if text has chars Helvetica cannot render (Arabic/CJK/etc)."""
    if not text:
        return False
    for ch in text:
        if ord(ch) > 0x024F:
            return True
    return False


def shape_arabic(text: str) -> str:
    """Reshape Arabic text + apply BiDi so it renders correctly in a
    LTR-only PDF library (ReportLab). Falls back to original text if libs
    are missing or text contains no Arabic."""
    if not text or not _ARABIC_LIBS_OK or not _has_arabic(text):
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception as e:
        print(f"[pdf_engine] arabic shaping failed: {e}")
        return text


# ---------------------------------------------------------------------------
# Font registration — supports DejaVu (Latin+general Unicode) + Noto Sans/Naskh
# Arabic (proper Arabic glyphs).
# ---------------------------------------------------------------------------
_FONTS_REGISTERED = False
_FONT_FLAGS = {
    "dejavu": False,
    "dejavu_bold": False,
    "noto_arabic": False,
    "noto_arabic_bold": False,
    "noto_naskh": False,
    "noto_naskh_bold": False,
}


def _font_dir():
    for d in [
        os.path.join("app", "static", "fonts"),
        os.path.join(os.path.dirname(__file__), "..", "static", "fonts"),
    ]:
        if os.path.isdir(d):
            return d
    return None


def _register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    _FONTS_REGISTERED = True
    d = _font_dir()
    if not d:
        print("[pdf_engine] no font dir found")
        return

    registrations = [
        ("DejaVuSans", "DejaVuSans.ttf", "dejavu"),
        ("DejaVuSans-Bold", "DejaVuSans-Bold.ttf", "dejavu_bold"),
        ("NotoSansArabic", "NotoSansArabic-Regular.ttf", "noto_arabic"),
        ("NotoSansArabic-Bold", "NotoSansArabic-Bold.ttf", "noto_arabic_bold"),
        ("NotoNaskhArabic", "NotoNaskhArabic-Regular.ttf", "noto_naskh"),
        ("NotoNaskhArabic-Bold", "NotoNaskhArabic-Bold.ttf", "noto_naskh_bold"),
    ]
    for name, filename, flag in registrations:
        path = os.path.join(d, filename)
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                _FONT_FLAGS[flag] = True
            except Exception as e:
                print(f"[pdf_engine] register {name} failed: {e}")


_register_fonts()


def _font_name(bold: bool = False, italic: bool = False, text: str = "",
               prefer_arabic: bool = False) -> str:
    """Pick the right font family for the given text.

    - If text contains Arabic → Noto Sans Arabic (or Naskh) if registered.
    - Otherwise if non-Latin → DejaVuSans (Unicode coverage).
    - Otherwise → Helvetica (built-in).
    """
    if prefer_arabic or _has_arabic(text):
        if bold and _FONT_FLAGS["noto_arabic_bold"]:
            return "NotoSansArabic-Bold"
        if _FONT_FLAGS["noto_arabic"]:
            return "NotoSansArabic"
        if bold and _FONT_FLAGS["noto_naskh_bold"]:
            return "NotoNaskhArabic-Bold"
        if _FONT_FLAGS["noto_naskh"]:
            return "NotoNaskhArabic"
        # Fall through to DejaVu (no Arabic shaping but won't crash)
    if _looks_non_latin(text):
        if bold and _FONT_FLAGS["dejavu_bold"]:
            return "DejaVuSans-Bold"
        if _FONT_FLAGS["dejavu"]:
            return "DejaVuSans"
    # Pure Latin
    if bold and italic:
        return "Helvetica-BoldOblique"
    if bold:
        return "Helvetica-Bold"
    if italic:
        return "Helvetica-Oblique"
    return "Helvetica"


# ---------------------------------------------------------------------------
# Helpers — colors, record resolution, background loading
# ---------------------------------------------------------------------------
def _hex_to_color(hexcode: str):
    try:
        return HexColor(hexcode)
    except Exception:
        return black


def _resolve_record_value(record: Any, field_key: str) -> str:
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


def _load_record(db, data_source: str, record_id: Optional[int]):
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


def _resolve_background_path(template: DocumentTemplate) -> Optional[str]:
    bg = (template.background_image or "").strip()
    if not bg:
        return None
    lower = bg.lower()
    for prefix in ("/static/pdf_backgrounds/", "static/pdf_backgrounds/"):
        if lower.startswith(prefix):
            bg = bg[len(prefix):]
            break
    candidates = [
        os.path.join(settings.PDF_BG_DIR, bg),
        os.path.join("app", "static", "pdf_backgrounds", bg),
        os.path.join(os.path.dirname(__file__), "..", "static", "pdf_backgrounds", bg),
        bg if os.path.isabs(bg) else None,
    ]
    for path in candidates:
        if path and os.path.exists(path) and os.path.getsize(path) > 200:
            return path
    return None


def _draw_background(c: canvas.Canvas, bg_path: Optional[str], w: float, h: float):
    if not bg_path:
        return False
    try:
        img = Image.open(bg_path)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=w, height=h,
                    preserveAspectRatio=False, mask="auto")
        return True
    except Exception as e:
        try:
            c.drawImage(bg_path, 0, 0, width=w, height=h,
                        preserveAspectRatio=False)
            return True
        except Exception:
            print(f"[pdf_engine] background draw failed for {bg_path}: {e}")
            return False


# ---------------------------------------------------------------------------
# Photo overlay — clips candidate photo into a box on the background
# ---------------------------------------------------------------------------
def _resolve_photo_path(record: Any, field_key: str = "photo") -> Optional[str]:
    """Find the photo file for the given record. Tries common photo columns."""
    if record is None:
        return None
    # Try the explicit field_key first, then fallbacks
    for key in (field_key, "photo", "photo_path", "image", "profile_photo"):
        p = getattr(record, key, None)
        if not p:
            continue
        # Normalize stored path → absolute file path
        p = str(p).strip()
        if not p or p in (".", "/", "static", "/static"):
            continue
        if p.startswith("/static/"):
            p = p[1:]
        if p.startswith("http://") or p.startswith("https://"):
            continue  # external URL, skip for now
        basename = os.path.basename(p)
        if not basename:
            continue
        candidates = [
            p,
            os.path.join("app", p),
            os.path.join("app", "static", "uploads", basename),
            os.path.join(os.path.dirname(__file__), "..", p),
            os.path.join(os.path.dirname(__file__), "..", "static", "uploads", basename),
        ]
        for cand in candidates:
            if (cand
                    and os.path.exists(cand)
                    and os.path.isfile(cand)
                    and os.path.getsize(cand) > 200):
                return cand
    return None


def _draw_photo(c: canvas.Canvas, photo_path: str, x: float, y: float,
                w: float, h: float, fit: str = "cover", border: bool = True):
    """Draw the candidate photo, clipped to (x, y, w, h). Renders a
    placeholder box if the photo is missing or unreadable."""
    if not photo_path or not os.path.isfile(photo_path):
        c.setStrokeColor(HexColor("#9ca3af"))
        c.setLineWidth(0.5)
        c.rect(x, y, w, h, stroke=1, fill=0)
        c.setFont("Helvetica-Oblique", 7)
        c.setFillColor(HexColor("#9ca3af"))
        c.drawCentredString(x + w / 2, y + h / 2, "[photo]")
        return False
    try:
        img = Image.open(photo_path)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        # Fit logic
        iw, ih = img.size
        target_ratio = w / h
        src_ratio = iw / ih
        if fit == "cover":
            # Crop image so it fills box without distortion
            if src_ratio > target_ratio:
                # image is wider — crop sides
                new_w = int(ih * target_ratio)
                left = (iw - new_w) // 2
                img = img.crop((left, 0, left + new_w, ih))
            elif src_ratio < target_ratio:
                # image is taller — crop top/bottom
                new_h = int(iw / target_ratio)
                top = (ih - new_h) // 2
                img = img.crop((0, top, iw, top + new_h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        c.drawImage(ImageReader(buf), x, y, width=w, height=h,
                    preserveAspectRatio=False, mask="auto")
        if border:
            c.setStrokeColor(HexColor("#1f2937"))
            c.setLineWidth(0.6)
            c.rect(x, y, w, h, stroke=1, fill=0)
        return True
    except Exception as e:
        print(f"[pdf_engine] photo draw failed: {e}")
        # Empty box with placeholder
        c.setStrokeColor(HexColor("#9ca3af"))
        c.setLineWidth(0.5)
        c.rect(x, y, w, h, stroke=1, fill=0)
        c.setFont("Helvetica-Oblique", 7)
        c.setFillColor(HexColor("#9ca3af"))
        c.drawCentredString(x + w / 2, y + h / 2, "[photo]")
        return False


# ---------------------------------------------------------------------------
# Character cells — auto-distribute one digit per cell
# ---------------------------------------------------------------------------
def _draw_char_cells(c: canvas.Canvas, text: str, x: float, y: float,
                     cell_count: int, cell_width: float, cell_gap: float = 0,
                     font_size: float = 11, font_bold: bool = True,
                     color: Color = black, draw_boxes: bool = False,
                     height: float = 0):
    """Render `text` one character per cell, left-aligned across `cell_count`
    cells starting at (x, y). Each cell is `cell_width` wide with `cell_gap`
    between them. The text baseline is placed inside each cell.

    If `draw_boxes` is True we also stroke the cell borders (useful when the
    background doesn't already have them).
    """
    text = (text or "").strip()
    # Strip non-digit chars for numeric fields like CNIC; preserve otherwise
    # — we let the caller decide by passing the value as-is
    text = text.replace("-", "").replace(" ", "")

    fname = _font_name(font_bold, False, text)
    c.setFont(fname, font_size)
    c.setFillColor(color)

    for i in range(cell_count):
        cell_x = x + i * (cell_width + cell_gap)
        if draw_boxes and height > 0:
            c.setStrokeColor(HexColor("#9ca3af"))
            c.setLineWidth(0.4)
            c.rect(cell_x, y - 2, cell_width, height, stroke=1, fill=0)
        if i < len(text):
            ch = text[i]
            # Center the digit inside the cell
            c.drawCentredString(cell_x + cell_width / 2, y, ch)


# ---------------------------------------------------------------------------
# Checkbox / tick
# ---------------------------------------------------------------------------
def _draw_checkbox(c: canvas.Canvas, x: float, y: float, size: float = 10,
                   checked: bool = True, color: Color = black):
    if checked:
        c.setStrokeColor(color)
        c.setLineWidth(1.4)
        # Draw a check mark inside the box (without drawing the box itself —
        # the background already has it)
        c.line(x + 1, y + size * 0.5, x + size * 0.4, y + 1)
        c.line(x + size * 0.4, y + 1, x + size - 1, y + size - 1)


# ---------------------------------------------------------------------------
# Barcode  —  professional, multi-symbology engine
#
# Supports: Code 128, Code 39, EAN-13.
# Honours the meta attributes captured in the Template Designer barcode panel:
#   - symbology / format        ("code128" | "code39" | "ean13")
#   - bar_height                (height of the bars, in points)
#   - bar_width                 (narrowest bar width / module width, in points)
#   - show_text / human_readable(draw the readable caption under the bars)
#   - caption_font_size         (font size of the readable caption)
#   - align                     ("left" | "center" | "right" within the field box)
#   - quiet_zone                (white margin around the symbol, in points)
#
# The barcode is rendered to a vector Drawing then placed so it never overflows
# the field box, and the human-readable caption is drawn cleanly & centred.
# ---------------------------------------------------------------------------
def _sanitize_barcode_value(value: str, symbology: str) -> str:
    """Clean the value so it is encodable by the chosen symbology."""
    value = (value or "").strip()
    if symbology == "code39":
        # Code39 only supports: 0-9 A-Z space - . $ / + %  →  upper-case the rest.
        value = value.upper()
        value = re.sub(r"[^0-9A-Z\-.\ $/+%]", "", value)
    elif symbology == "ean13":
        digits = re.sub(r"\D", "", value)
        # EAN-13 needs exactly 12 data digits (checksum auto-added). Pad/truncate.
        value = digits[:12].rjust(12, "0") if digits else ""
    # code128 can encode the full ASCII set, so leave it as-is.
    return value


def _draw_barcode(c: canvas.Canvas, value: str, x: float, y: float,
                  width: float, height: float, show_text: bool = True,
                  symbology: str = "code128", bar_width: Optional[float] = None,
                  bar_height: Optional[float] = None,
                  caption_font_size: float = 8.0,
                  align: str = "center", quiet_zone: Optional[float] = None,
                  color: Color = black):
    """Draw a clean, professional barcode that fits inside the field box.

    The symbol is auto-scaled so it is fully contained within ``width`` while
    keeping crisp module edges; the readable caption is centred under the bars.
    """
    symbology = (symbology or "code128").lower().replace("-", "").replace("_", "")
    if symbology in ("code-128", "c128"):
        symbology = "code128"
    if symbology in ("code-39", "c39", "3of9"):
        symbology = "code39"

    raw = str(value or "")
    value = _sanitize_barcode_value(raw, symbology)
    if not value:
        return

    # Geometry --------------------------------------------------------------
    box_w = float(width or 160)
    box_h = float(height or 32)
    bh = float(bar_height) if bar_height else max(box_h - (caption_font_size + 3 if show_text else 2), 14)
    qz = float(quiet_zone) if quiet_zone is not None else 4.0

    try:
        # Build the barcode as a vector Drawing so we can measure & scale it
        # precisely, then nudge it to fit the box without clipping.
        if symbology == "ean13":
            bc = eanbc.Ean13BarcodeWidget(value)
            bc.barHeight = bh
            if bar_width:
                bc.barWidth = float(bar_width)
            bc.humanReadable = bool(show_text)
            bc.fontSize = caption_font_size
            d = Drawing()
            d.add(bc)
            bounds = bc.getBounds()
            sym_w = bounds[2] - bounds[0]
            sym_h = bounds[3] - bounds[1]
            # scale to fit inside box width (minus quiet zones)
            avail = box_w - 2 * qz
            scale = min(1.0, avail / sym_w) if sym_w else 1.0
            tx = x + qz + (avail - sym_w * scale) / 2.0 if align == "center" else (
                 x + box_w - qz - sym_w * scale if align == "right" else x + qz)
            c.saveState()
            c.translate(tx - bounds[0] * scale, y - bounds[1] * scale)
            c.scale(scale, scale)
            renderPDF.draw(d, c, 0, 0)
            c.restoreState()
            return

        # Code128 / Code39 — use the linear-barcode classes which draw directly.
        avail = box_w - 2 * qz
        if symbology == "code39":
            # First pass to measure width at requested module width
            bw = float(bar_width) if bar_width else 0.7
            bc = code39.Standard39(value, barHeight=bh, barWidth=bw,
                                   humanReadable=False, checksum=0,
                                   quiet=False)
        else:  # code128
            bw = float(bar_width) if bar_width else 0.8
            bc = code128.Code128(value, barHeight=bh, barWidth=bw,
                                 humanReadable=False, quiet=False)

        sym_w = bc.width
        # Auto-shrink the module width if the symbol is wider than the box.
        if sym_w > avail and sym_w > 0:
            bw = max(bw * (avail / sym_w), 0.33)  # 0.33pt ≈ min printable module
            if symbology == "code39":
                bc = code39.Standard39(value, barHeight=bh, barWidth=bw,
                                       humanReadable=False, checksum=0, quiet=False)
            else:
                bc = code128.Code128(value, barHeight=bh, barWidth=bw,
                                     humanReadable=False, quiet=False)
            sym_w = bc.width

        # Horizontal placement inside the box.
        if align == "center":
            bx = x + (box_w - sym_w) / 2.0
        elif align == "right":
            bx = x + box_w - qz - sym_w
        else:
            bx = x + qz

        # Caption sits below the bars; bars sit at the top of the box.
        cap_h = (caption_font_size + 2) if show_text else 0
        by = y + cap_h  # bottom of the bars
        c.setFillColor(color)
        bc.drawOn(c, bx, by)

        # Human-readable caption — clean, centred, monospaced-ish.
        if show_text:
            c.setFillColor(color)
            try:
                c.setFont(_font_name(False, False), caption_font_size)
            except Exception:
                c.setFont("Helvetica", caption_font_size)
            c.drawCentredString(bx + sym_w / 2.0, y + 1, raw.strip() or value)
    except Exception as e:
        print(f"[pdf_engine] barcode draw failed ({symbology!r}, value={value!r}): {e}")


def barcode_to_data_uri(value: str, symbology: str = "code128",
                        bar_height=None, bar_width=None, show_text: bool = True,
                        caption_font_size: float = 8.0) -> str:
    """Render a barcode to a base64 PNG ``data:`` URI for HTML previews.

    Produces the *same* clean symbol used in the printed PDF so the on-screen
    print-preview matches the final output exactly. Returns "" on failure or
    when the value is empty.
    """
    import base64
    symbology = (symbology or "code128").lower().replace("-", "").replace("_", "")
    if symbology in ("code-128", "c128"):
        symbology = "code128"
    if symbology in ("code-39", "c39", "3of9"):
        symbology = "code39"

    raw = str(value or "")
    enc = _sanitize_barcode_value(raw, symbology)
    if not enc:
        return ""

    bh = float(bar_height) if bar_height not in (None, "", "auto") else 26.0
    try:
        if symbology == "ean13":
            d = createBarcodeDrawing("EAN13", value=enc, barHeight=bh,
                                     humanReadable=bool(show_text),
                                     fontSize=caption_font_size)
        elif symbology == "code39":
            bw = float(bar_width) if bar_width not in (None, "", "auto") else 0.7
            d = createBarcodeDrawing("Standard39", value=enc, barHeight=bh,
                                     barWidth=bw, humanReadable=False, checksum=0)
        else:
            bw = float(bar_width) if bar_width not in (None, "", "auto") else 0.8
            d = createBarcodeDrawing("Code128", value=enc, barHeight=bh,
                                     barWidth=bw, humanReadable=False)

        # Render the bars at high DPI for crisp on-screen display.
        png_bytes = d.asString("png")

        # Normalise the quiet-zone padding so every barcode preview starts at
        # the same left edge and carries an identical, predictable margin —
        # otherwise barcodes encoding different-length values render at visibly
        # different widths/offsets ("barcode size up, down"). (Problem 5)
        try:
            from PIL import Image as _PILImg, ImageOps as _ImageOps
            _im = _PILImg.open(io.BytesIO(png_bytes)).convert("L")
            _bbox = _ImageOps.invert(_im).getbbox()
            if _bbox:
                _im = _im.crop(_bbox)
                _qz = max(8, int(round((bar_width or 0.8) * 8)))
                _im = _ImageOps.expand(_im, border=(_qz, 0, _qz, 0), fill=255)
                _buf = io.BytesIO()
                _im.convert("RGB").save(_buf, format="PNG")
                png_bytes = _buf.getvalue()
        except Exception as _crop_exc:
            print(f"[pdf_engine] barcode preview crop skipped: {_crop_exc}")

        bars_b64 = base64.b64encode(png_bytes).decode("ascii")

        if not show_text or symbology == "ean13":
            return f"data:image/png;base64,{bars_b64}"

        # Compose a caption underneath the bars using PIL for clean text.
        try:
            from PIL import Image as _PILImage, ImageDraw as _ImageDraw, ImageFont as _ImageFont
            bars_img = _PILImage.open(io.BytesIO(png_bytes)).convert("RGBA")
            bw_px, bh_px = bars_img.size
            cap_px = max(int(caption_font_size * 2.0), 12)
            canvas_img = _PILImage.new("RGBA", (bw_px, bh_px + cap_px + 2),
                                       (255, 255, 255, 0))
            canvas_img.paste(bars_img, (0, 0))
            draw = _ImageDraw.Draw(canvas_img)
            try:
                font = _ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", cap_px)
            except Exception:
                font = _ImageFont.load_default()
            caption = raw.strip() or enc
            try:
                tb = draw.textbbox((0, 0), caption, font=font)
                tw = tb[2] - tb[0]
            except Exception:
                tw = len(caption) * cap_px * 0.6
            draw.text(((bw_px - tw) / 2, bh_px + 1), caption,
                      fill=(0, 0, 0, 255), font=font)
            out = io.BytesIO()
            canvas_img.save(out, format="PNG")
            full_b64 = base64.b64encode(out.getvalue()).decode("ascii")
            return f"data:image/png;base64,{full_b64}"
        except Exception:
            return f"data:image/png;base64,{bars_b64}"
    except Exception as e:
        print(f"[pdf_engine] barcode_to_data_uri failed ({symbology!r}): {e}")
        return ""


# ---------------------------------------------------------------------------
# Trade table — auto-build the Visa Category / Qty / Assigned / Available rows
# ---------------------------------------------------------------------------
#: Default column spec for trade_table — used when meta.columns is absent.
#: Each entry: (header_label, data_key, weight, align)
#:   weight  = relative column-width share (sum need not equal 1)
#:   data_key = JobCategory attribute, or "_assigned"/"_available" for derived counts
_TRADE_TABLE_DEFAULT_COLUMNS = [
    ("Sr#",         "_sr",            0.06, "center"),
    ("Trade / Visa Category", "trade", 0.36, "left"),
    ("Qty",         "quantity",       0.08, "center"),
    ("Assigned",    "_assigned",      0.10, "center"),
    ("Available",   "_available",     0.10, "center"),
    ("Salary",      "salary",         0.15, "right"),
    ("Contract",    "contract_period", 0.15, "center"),
]


def _draw_trade_table(c: canvas.Canvas, demand_id: int, x: float, y: float,
                      width: float, row_height: float, font_size: float, db,
                      meta: Optional[Dict] = None):
    """Render the trades table for a demand starting at (x, y) and growing
    downward (PDF coords — origin bottom-left, so successive rows decrease y).

    The table is **fully configurable via the field's meta dict** — the
    designer property panel writes these keys:

        meta.columns       — list of {key, label, weight, align} OR a CSV of
                             column keys (e.g. "trade,quantity,salary"). When
                             omitted we render the full demo-OEP column set.
        meta.show_header   — bool (default True). Draw a bold header row.
        meta.row_height    — float pt (also accepted as the field's `height`).
        meta.padding_x     — horizontal cell padding in pt (default 3).
        meta.header_fill   — header background hex (default "#e5e7eb").
        meta.border        — draw cell borders? (default True).
        meta.zebra         — alternate-row background (default False).
        meta.max_rows      — clamp row count (default 0 = no limit).

    The function is no-op when there's no demand_id or db handle so it stays
    safe in unit tests / preview-without-data scenarios.
    """
    meta = meta or {}
    if not demand_id or not db:
        # Still render the HEADER even with no demand_id so the user sees the
        # table shape in the designer preview — important for visual fidelity
        # between designer and print.
        rows: List = []
    else:
        try:
            from app.models import JobCategory, CandidateAssignment
            from sqlalchemy import func as sqlfunc
            rows = db.query(
                JobCategory,
                sqlfunc.count(CandidateAssignment.id).label("assigned"),
            ).outerjoin(
                CandidateAssignment,
                CandidateAssignment.job_category_id == JobCategory.id,
            ).filter(JobCategory.demand_id == demand_id).group_by(JobCategory.id).all()
        except Exception as e:
            print(f"[pdf_engine] trade_table query failed: {e}")
            rows = []

    # ------- Resolve column spec -------
    raw_cols = meta.get("columns")
    columns: List[tuple] = []
    if isinstance(raw_cols, list) and raw_cols:
        # Full spec list
        for spec in raw_cols:
            if isinstance(spec, dict):
                columns.append((
                    str(spec.get("label") or spec.get("key") or ""),
                    str(spec.get("key") or ""),
                    float(spec.get("weight") or 1.0),
                    str(spec.get("align") or "left").lower(),
                ))
    elif isinstance(raw_cols, str) and raw_cols.strip():
        # CSV of keys — derive labels from default set
        keys = [k.strip() for k in raw_cols.split(",") if k.strip()]
        defaults = {k: (lbl, w, al) for (lbl, k, w, al) in _TRADE_TABLE_DEFAULT_COLUMNS}
        for k in keys:
            lbl, w, al = defaults.get(k, (k.replace("_", " ").title(), 0.15, "left"))
            columns.append((lbl, k, w, al))
    if not columns:
        columns = list(_TRADE_TABLE_DEFAULT_COLUMNS)

    show_header = bool(meta.get("show_header", True))
    pad_x = float(meta.get("padding_x", 3))
    header_fill = _hex_to_color(str(meta.get("header_fill", "#e5e7eb")))
    border = bool(meta.get("border", True))
    zebra = bool(meta.get("zebra", False))
    zebra_fill = _hex_to_color(str(meta.get("zebra_fill", "#f8fafc")))
    max_rows = int(meta.get("max_rows") or 0)

    # Normalise weights and compute column widths in pt
    total_weight = sum(w for (_, _, w, _) in columns) or 1.0
    col_widths = [width * (w / total_weight) for (_, _, w, _) in columns]

    if max_rows:
        rows = rows[:max_rows]

    # ------- Draw -------
    fname_bold = _font_name(True, False)
    fname_body = _font_name(False, False)

    cur_y = y  # baseline of the current row (PDF coords)

    def _draw_row_bg(top_y: float, fill_color):
        """Draw a filled rectangle for one row at baseline top_y."""
        c.saveState()
        c.setFillColor(fill_color)
        c.setStrokeColor(fill_color)
        # row spans [top_y - row_height + descender .. top_y + ascender]
        c.rect(x, top_y - (row_height - font_size), width, row_height,
               stroke=0, fill=1)
        c.restoreState()

    def _draw_row_cells(values: List[str], font_name: str, baseline_y: float):
        cx = x
        c.setFont(font_name, font_size)
        c.setFillColor(black)
        for (val, w, (_, _, _, align)) in zip(values, col_widths, columns):
            text = str(val or "")
            # Truncate long text to fit the cell width approximately
            max_chars = max(4, int((w - 2 * pad_x) / (font_size * 0.55)))
            if len(text) > max_chars:
                text = text[:max_chars - 1] + "…"
            if align == "center":
                c.drawCentredString(cx + w / 2, baseline_y, text)
            elif align == "right":
                c.drawRightString(cx + w - pad_x, baseline_y, text)
            else:
                c.drawString(cx + pad_x, baseline_y, text)
            cx += w

    def _draw_row_borders(top_y: float):
        if not border:
            return
        c.saveState()
        c.setStrokeColor(HexColor("#94a3b8"))
        c.setLineWidth(0.4)
        # outer + vertical separators
        row_top_css = top_y - (row_height - font_size)
        c.rect(x, row_top_css, width, row_height, stroke=1, fill=0)
        cx = x
        for w in col_widths[:-1]:
            cx += w
            c.line(cx, row_top_css, cx, row_top_css + row_height)
        c.restoreState()

    # Header row
    if show_header:
        _draw_row_bg(cur_y, header_fill)
        headers = [lbl for (lbl, _, _, _) in columns]
        _draw_row_cells(headers, fname_bold, cur_y)
        _draw_row_borders(cur_y)
        cur_y -= row_height

    # Data rows
    for idx, (jc, assigned) in enumerate(rows, start=1):
        qty = int(jc.quantity or 0)
        assigned = int(assigned or 0)
        derived = {
            "_sr":         str(idx),
            "_assigned":   str(assigned),
            "_available":  str(max(0, qty - assigned)),
        }
        values = []
        for (_, key, _, _) in columns:
            if key in derived:
                values.append(derived[key])
            else:
                v = getattr(jc, key, "")
                # Format common types nicely
                if key == "salary" and v:
                    try:
                        v = f"{float(v):,.0f}"
                    except (TypeError, ValueError):
                        pass
                values.append("" if v is None else str(v))

        if zebra and (idx % 2 == 0):
            _draw_row_bg(cur_y, zebra_fill)
        _draw_row_cells(values, fname_body, cur_y)
        _draw_row_borders(cur_y)
        cur_y -= row_height


# ---------------------------------------------------------------------------
# Auto-grid fallback (unchanged from v1 but using new font picker)
# ---------------------------------------------------------------------------
_AUTO_FIELDS = {
    "candidate": [
        ("Full Name", "full_name"),
        ("Father Name", "father_name"),
        ("CNIC", "cnic"),
        ("Passport No", "passport_no"),
        ("Passport Expiry", "passport_expiry_date"),
        ("Date of Birth", "date_of_birth"),
        ("Nationality", "nationality"),
        ("Profession", "profession"),
        ("Address", "address"),
        ("Phone", "phone"),
        ("Next of Kin", "next_of_kin_name"),
        ("Relation", "next_of_kin_relation"),
    ],
    "demand": [
        ("File Number", "file_number"),
        ("Permission No", "permission_no"),
        ("Sponsor Name", "sponsor_name"),
        ("Sponsor Address", "sponsor_address"),
        ("Sponsor Phone", "sponsor_phone"),
        ("Visa Number", "visa_number"),
        ("Country", "country"),
        ("Embassy", "embassy"),
        ("Receiving Date", "receiving_date"),
        ("Visa Issue Date", "visa_issue_date"),
    ],
    "client": [
        ("Company Name", "company_name"),
        ("Owner", "owner_name"),
        ("Country", "country"),
        ("City", "city"),
        ("Phone", "phone"),
        ("Email", "email"),
        ("Address", "address"),
        ("OEP License", "oep_license_number"),
    ],
    "agent": [
        ("Agent Name", "name"),
        ("Company", "company_name"),
        ("Phone", "phone"),
        ("Mobile", "mobile"),
        ("Email", "email"),
        ("City", "city"),
    ],
}


def _draw_record_header(c, template, record, w, h):
    band_h = 56
    c.saveState()
    c.setFillColor(HexColor("#1e40af"))
    c.rect(0, h - band_h, w, band_h, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20, h - 24, template.name or "Document")
    c.setFont("Helvetica", 10)
    subtitle = ""
    if record is not None:
        subtitle = (
            getattr(record, "full_name", None)
            or getattr(record, "file_number", None)
            or getattr(record, "company_name", None)
            or getattr(record, "name", None)
            or f"#{getattr(record, 'id', '')}"
        )
        if template.data_source == "demand" and subtitle and str(subtitle).isdigit():
            subtitle = f"DTC/786/{subtitle}"
    c.drawString(20, h - 42, f"{(template.data_source or 'record').title()}: {subtitle}")
    c.drawRightString(w - 20, h - 24, datetime.now().strftime("%d-%m-%Y %H:%M"))
    c.restoreState()


def _draw_auto_grid(c, template, record, w, h):
    if record is None:
        c.setFont("Helvetica-Oblique", 11)
        c.setFillColor(HexColor("#6b7280"))
        c.drawCentredString(w / 2, h / 2, "(No record selected — preview)")
        return
    auto = _AUTO_FIELDS.get(template.data_source or "", [])
    if not auto:
        return
    start_y = h - 110
    row_h = 22
    col_w = (w - 80) / 2
    c.setFont("Helvetica", 10)
    c.setFillColor(black)
    for idx, (label, key) in enumerate(auto):
        col = idx % 2
        row = idx // 2
        x = 40 + col * col_w
        y = start_y - row * row_h
        if y < 60:
            break
        value = _resolve_record_value(record, key) or "—"
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(HexColor("#374151"))
        c.drawString(x, y, f"{label}:")
        # Arabic-aware
        if _has_arabic(value):
            c.setFont(_font_name(False, False, value), 10)
            shaped = shape_arabic(value)
            c.setFillColor(black)
            c.drawString(x + 95, y, shaped)
        else:
            c.setFont(_font_name(False, False, value), 10)
            c.setFillColor(black)
            if len(value) > 38:
                value = value[:35] + "…"
            c.drawString(x + 95, y, value)


def _draw_footer(c, w, h):
    c.saveState()
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(HexColor("#9ca3af"))
    c.drawCentredString(w / 2, 18,
        f"Generated by Dogar Trading Corporation Portal · "
        f"{datetime.now().strftime('%d-%m-%Y %H:%M')}")
    c.restoreState()


def _draw_content_notes(c, notes: str, page_width: float, page_height: float):
    """Render the free-form notes from the designer's Content tab at the
    bottom of the page, just above the footer. Audit Fix 3 — previously the
    notes were saved (via the description meta-fence) but never appeared on
    the printed/PDF output.

    Layout:
        • full page width minus 30pt left/right margin
        • body starts ~50pt above the footer baseline (y=18)
        • automatic line-wrap at ~92 chars per line, max 8 lines
        • italic Helvetica 8pt, soft grey colour
    """
    if not notes:
        return
    text = str(notes).strip()
    if not text:
        return
    c.saveState()
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(HexColor("#475569"))
    margin = 30
    max_chars = max(40, int((page_width - 2 * margin) / 4.4))
    # Simple word-wrap
    lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            lines.append("")
            continue
        while len(line) > max_chars:
            # Break at the last space within max_chars, else hard-cut.
            cut = line.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
    lines = lines[:8]
    # Stack lines upward from y=50 (baseline of last line)
    base_y = 50
    line_h = 10
    for i, ln in enumerate(reversed(lines)):
        c.drawString(margin, base_y + i * line_h, ln)
    c.restoreState()


# ---------------------------------------------------------------------------
# Field rendering dispatcher
# ---------------------------------------------------------------------------
_MERGE_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _resolve_merge_text(text: str, resolved_data: Optional[Dict] = None,
                        record: Any = None) -> str:
    """Replace ``{{token}}`` placeholders with resolved values.

    Used for the barcode-content box (and any future free-text merge field),
    mirroring the demo portal where the barcode content is e.g. ``{{visa_number}}``
    or a combined value like ``E{{e_number}}`` or ``FILE-{{file_number}}``.
    Each token is looked up first in the pre-resolved data map, then on the
    record itself; unknown tokens collapse to an empty string.
    """
    if not text:
        return ""
    resolved_data = resolved_data or {}

    def _sub(m):
        key = m.group(1)
        if key in resolved_data and resolved_data[key] not in (None, ""):
            return str(resolved_data[key])
        val = _resolve_record_value(record, key) if record is not None else ""
        return str(val or "")

    return _MERGE_TOKEN_RE.sub(_sub, text).strip()


def _render_field(c: canvas.Canvas, field: DocumentField, value: str,
                  record: Any, db, page_w: float, page_h: float,
                  demand_id: Optional[int] = None,
                  resolved_data: Optional[Dict] = None):
    """Render a single coordinate-mapped field according to its type."""
    ft = (field.field_type or "text").lower()
    x = float(field.x or 0)
    y = float(field.y or 0)
    w = float(field.width or 200)
    h = float(field.height or 20)
    font_size = float(field.font_size or 11)
    bold = bool(field.font_bold)
    italic = bool(field.font_italic)
    color = _hex_to_color(field.color or "#000000")
    meta = field.meta or {}
    if isinstance(meta, str):
        try:
            import json
            meta = json.loads(meta)
        except Exception:
            meta = {}

    # COORDINATE FIX (Problem 6 — print/PDF coordinate drift):
    # The designer canvas (document_customize.html .field-marker) renders each
    # field as a BOX whose top edge sits at (page_h - y - h) and whose text
    # flows from the TOP of that box (line-height 1.05, no flex). reportlab's
    # drawString(x, y, ...) however puts the text BASELINE at y (the box
    # BOTTOM), so whenever h != font_size the PDF text drifts (h-font_size)pt
    # below where the user positioned it. We mirror the designer exactly by
    # drawing the baseline near the TOP of the box: baseline = y + h - ascent,
    # with ascent ≈ font_size (Arial cap+ascent). This makes designer == print
    # HTML == PDF, pixel-for-pixel.
    text_baseline_y = y + max(h - font_size, 0.0)

    # --- static text ---
    if ft == "static":
        text = field.static_value or ""
        if _has_arabic(text):
            text = shape_arabic(text)
        c.setFont(_font_name(bold, italic, text), font_size)
        c.setFillColor(color)
        if field.align == "center":
            c.drawCentredString(x + w / 2, text_baseline_y, text)
        elif field.align == "right":
            c.drawRightString(x + w, text_baseline_y, text)
        else:
            c.drawString(x, text_baseline_y, text)
        return

    # --- checkbox ---
    if ft == "checkbox":
        truthy = bool(value) and str(value).lower() not in ("", "0", "false", "no", "none")
        _draw_checkbox(c, x, y, size=max(h, 8), checked=truthy, color=color)
        return

    # --- photo ---
    if ft == "photo":
        photo_path = _resolve_photo_path(record, field.field_key or "photo")
        _draw_photo(c, photo_path or "", x, y, w, h,
                    fit=meta.get("fit", "cover"),
                    border=meta.get("border", True))
        return

    # --- char cells (CNIC, passport, phone digit grids) ---
    if ft == "char_cells":
        cell_count = int(meta.get("cell_count") or len(str(value or "")) or 13)
        cell_width = float(meta.get("cell_width") or (w / cell_count if cell_count else 18))
        cell_gap = float(meta.get("cell_gap") or 0)
        draw_boxes = bool(meta.get("draw_boxes", False))

        # Optional format pre-processing for cell values:
        #   "ddmmyyyy" — convert "13-Feb-2015" / "13-02-2015" → "13022015"
        #   "digits"   — strip all non-digit chars (e.g. CNIC "12345-6789012-3" → "1234567890123")
        #   "alnum"    — strip all non-alphanumeric chars
        cell_value = str(value or "")
        fmt = (meta.get("format") or "").lower()
        if fmt == "ddmmyyyy":
            cell_value = _format_date_ddmmyyyy(cell_value)
        elif fmt == "digits":
            cell_value = "".join(ch for ch in cell_value if ch.isdigit())
        elif fmt == "alnum":
            cell_value = "".join(ch for ch in cell_value if ch.isalnum())

        _draw_char_cells(c, cell_value, x, y,
                         cell_count=cell_count,
                         cell_width=cell_width,
                         cell_gap=cell_gap,
                         font_size=font_size,
                         font_bold=bold,
                         color=color,
                         draw_boxes=draw_boxes,
                         height=h)
        return

    # --- barcode ---
    if ft == "barcode":
        # Resolve the encoded value. Priority order (matches the demo portal):
        #   1) meta.barcode_content  — supports {{merge}} tokens + fixed prefixes
        #   2) field.static_value    — a literal value typed in the designer
        #   3) the field_key's resolved value (legacy behaviour)
        content_spec = (meta.get("barcode_content")
                        or meta.get("content")
                        or field.static_value
                        or "")
        if content_spec:
            barcode_value = _resolve_merge_text(content_spec, resolved_data, record)
        else:
            barcode_value = str(value or "")

        symbology = (meta.get("symbology") or meta.get("format") or "code128")
        bar_w = meta.get("bar_width")
        bar_h = meta.get("bar_height")
        cap_fs = float(meta.get("caption_font_size")
                       or meta.get("caption_font") or 8.0)
        show_txt = meta.get("show_text", meta.get("human_readable", True))
        bc_align = meta.get("align") or field.align or "center"
        _draw_barcode(
            c, barcode_value, x, y, w, h,
            show_text=bool(show_txt),
            symbology=symbology,
            bar_width=float(bar_w) if bar_w not in (None, "", "auto") else None,
            bar_height=float(bar_h) if bar_h not in (None, "", "auto") else None,
            caption_font_size=cap_fs,
            align=bc_align,
            quiet_zone=float(meta["quiet_zone"]) if meta.get("quiet_zone") not in (None, "") else None,
            color=color,
        )
        return

    # --- trade table ---
    if ft == "trade_table":
        row_h = float(meta.get("row_height") or h or 18)
        _draw_trade_table(c, demand_id, x, y, w, row_h, font_size, db, meta=meta)
        return

    # --- arabic explicit ---
    if ft == "arabic":
        text = shape_arabic(str(value or ""))
        c.setFont(_font_name(bold, italic, text, prefer_arabic=True), font_size)
        c.setFillColor(color)
        if field.align == "left":
            # Arabic with left-align: still draw at x
            c.drawString(x, text_baseline_y, text)
        elif field.align == "center":
            c.drawCentredString(x + w / 2, text_baseline_y, text)
        else:
            # default right-align for Arabic
            c.drawRightString(x + w, text_baseline_y, text)
        return

    # --- text/date (default) ---
    text = str(value or "")
    # Optional date reformatting via meta.format so boxed date rows
    # (e.g. the OEP "dd-mm-yyyy" boxes) get clean digit-with-dash output
    # instead of "07-May-1992".
    fmt = (meta.get("format") or "").lower() if isinstance(meta, dict) else ""
    if fmt in ("ddmmyyyy", "date_dashes", "dd-mm-yyyy", "dd/mm/yyyy") and text:
        digits = _format_date_ddmmyyyy(text)
        if len(digits) == 8:
            if fmt == "ddmmyyyy":
                text = digits
            elif fmt == "dd/mm/yyyy":
                text = f"{digits[0:2]}/{digits[2:4]}/{digits[4:8]}"
            else:  # date_dashes / dd-mm-yyyy
                text = f"{digits[0:2]}-{digits[2:4]}-{digits[4:8]}"
    if _has_arabic(text):
        text = shape_arabic(text)
    c.setFont(_font_name(bold, italic, text), font_size)
    c.setFillColor(color)
    if field.align == "center":
        c.drawCentredString(x + w / 2, text_baseline_y, text)
    elif field.align == "right":
        c.drawRightString(x + w, text_baseline_y, text)
    else:
        c.drawString(x, text_baseline_y, text)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def generate_pdf(
    db,
    template: DocumentTemplate,
    record_id: Optional[int] = None,
    output_path: Optional[str] = None,
) -> str:
    """Generate a filled PDF for the given template + record."""
    record = _load_record(db, template.data_source, record_id)

    from app.services.overlay_engine import resolve_all_related_data_for_record
    resolved_data = {}
    demand_id = None
    if db and record_id:
        resolved_data = resolve_all_related_data_for_record(
            db, template.data_source, record_id
        )
        # Determine demand_id for trade_table support
        if template.data_source == "demand":
            demand_id = record_id
        elif template.data_source == "candidate":
            try:
                from app.models import CandidateAssignment, JobCategory
                a = (db.query(CandidateAssignment)
                       .filter(CandidateAssignment.candidate_id == record_id)
                       .order_by(CandidateAssignment.id.desc())
                       .first())
                if a:
                    jc = db.query(JobCategory).filter(JobCategory.id == a.job_category_id).first()
                    if jc:
                        demand_id = jc.demand_id
            except (AttributeError, Exception) as exc:  # noqa: BLE001 — SQLAlchemy may raise various
                # Demand resolution is best-effort — the renderer must still
                # produce SOMETHING even if the candidate has no assignment.
                import logging
                logging.getLogger("dtc.pdf_engine").warning(
                    "Could not resolve demand from candidate assignment: %s", exc
                )

    page_width = float(template.page_width or A4[0])
    page_height = float(template.page_height or A4[1])

    if output_path is None:
        out_dir = os.path.join(settings.UPLOAD_DIR, "generated_pdfs")
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(ch for ch in (template.name or "doc")
                            if ch.isalnum() or ch in "_-")[:50] or "doc"
        output_path = os.path.join(
            out_dir, f"{safe_name}_{record_id or 'blank'}_{stamp}.pdf"
        )

    c = canvas.Canvas(output_path, pagesize=(page_width, page_height))
    c.setTitle(template.name or "Document")
    c.setAuthor("Dogar Trading Corporation Portal")
    c.setSubject(f"{template.data_source or 'record'} #{record_id or 'blank'}")

    bg_path = _resolve_background_path(template)

    # Parse designer meta (Content-tab notes etc.) from the description
    # meta-fence so they get rendered on the printed/PDF page. (Audit Fix 3)
    from app.services.overlay_engine import parse_designer_meta
    _designer_meta = parse_designer_meta(getattr(template, "description", "") or "")
    _content_notes = (_designer_meta.get("notes") or "").strip()

    # Group fields by page
    fields = sorted(template.fields, key=lambda f: (f.page or 1, -(f.y or 0)))
    pages_map: Dict[int, List[DocumentField]] = {}
    for f in fields:
        pages_map.setdefault(int(f.page or 1), []).append(f)
    if not pages_map:
        pages_map[1] = []

    for page_no in sorted(pages_map.keys()):
        drew_bg = _draw_background(c, bg_path, page_width, page_height)
        page_fields = pages_map[page_no]

        if not drew_bg and not page_fields:
            _draw_record_header(c, template, record, page_width, page_height)
            _draw_auto_grid(c, template, record, page_width, page_height)
        elif not page_fields:
            _draw_record_header(c, template, record, page_width, page_height)
        else:
            for field in page_fields:
                key = field.field_key or ""
                if key in resolved_data:
                    val = resolved_data[key]
                else:
                    val = _resolve_record_value(record, key)
                _render_field(c, field, val, record, db,
                              page_width, page_height,
                              demand_id=demand_id,
                              resolved_data=resolved_data)

        # Notes (Content-tab) printed on every page, above the footer.
        if _content_notes:
            _draw_content_notes(c, _content_notes, page_width, page_height)
        _draw_footer(c, page_width, page_height)
        c.showPage()

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Available field schema (used by the Template Designer field picker)
# ---------------------------------------------------------------------------
def get_available_fields(data_source: str) -> list:
    """Return a list of field tokens grouped for the Template Designer sidebar.

    Each item: { key, label, group, arabic? }
    """
    schemas = {
        "candidate": [
            # Identity
            ("full_name", "Full Name", "Candidate", False),
            ("name_arabic", "Name (Arabic)", "Candidate", True),
            ("father_name", "Father Name", "Candidate", False),
            ("father_name_arabic", "Father Name (Arabic)", "Candidate", True),
            ("mother_name", "Mother Name", "Candidate", False),
            ("gender", "Gender", "Candidate", False),
            ("marital_status", "Marital Status", "Candidate", False),
            ("religion", "Religion", "Candidate", False),
            ("date_of_birth", "Date of Birth", "Candidate", False),
            ("place_of_birth", "Place of Birth", "Candidate", False),
            ("nationality", "Nationality", "Candidate", False),
            # CNIC / Identification
            ("cnic", "CNIC", "Identification", False),
            ("nadra_token_no", "Nadra Token No", "Identification", False),
            # Passport
            ("passport_no", "Passport No", "Passport", False),
            ("passport_issue_date", "Passport Issue Date", "Passport", False),
            ("passport_expiry_date", "Passport Expiry Date", "Passport", False),
            ("passport_issue_place", "Passport Issue Place", "Passport", False),
            ("issuing_authority", "Issuing Authority", "Passport", False),
            ("issuing_authority_arabic", "Issuing Authority (Arabic)", "Passport", True),
            # Contact / Address
            ("phone", "Phone", "Contact", False),
            ("email", "Email", "Contact", False),
            ("address", "Address", "Contact", False),
            ("tehsil", "Tehsil", "Contact", False),
            ("district", "District", "Contact", False),
            ("province", "Province", "Contact", False),
            # Profession / Misc
            ("qualification", "Qualification", "Profession", False),
            ("profession", "Profession / Trade", "Profession", False),
            ("salary", "Salary", "Profession", False),
            # Next of Kin
            ("next_of_kin_name", "Next of Kin Name", "Next of Kin", False),
            ("next_of_kin_nic", "Next of Kin NIC", "Next of Kin", False),
            ("next_of_kin_relation", "Next of Kin Relation", "Next of Kin", False),
            # Workflow / Tracking
            ("protector_no", "Protector No", "Workflow", False),
            ("protector_date", "Protector Date", "Workflow", False),
            ("gamca_number", "GAMCA Number", "Workflow", False),
            ("medical_date", "Medical Date", "Workflow", False),
            ("e_number", "E-Number", "Workflow", False),
            ("flight_no", "Flight Number", "Workflow", False),
            ("destination", "Destination", "Workflow", False),
            ("ticket_no", "Ticket Number", "Workflow", False),
            ("status", "Status", "Workflow", False),
            # Photo
            ("photo", "Candidate Photo", "Media", False),
        ],
        "demand": [
            # Demand file basics
            ("file_number", "File Number", "Demand File", False),
            ("receiving_date", "Receiving Date", "Demand File", False),
            ("permission_no", "Permission No", "Demand File", False),
            ("permission_date", "Permission Date", "Demand File", False),
            ("reference", "Reference", "Demand File", False),
            ("notes", "Notes", "Demand File", False),
            # Sponsor
            ("sponsor_name", "Sponsor Name", "Sponsor", False),
            ("sponsor_name_arabic", "Sponsor Name (Arabic)", "Sponsor", True),
            ("sponsor_address", "Sponsor Address", "Sponsor", False),
            ("sponsor_address_arabic", "Sponsor Address (Arabic)", "Sponsor", True),
            ("sponsor_phone", "Sponsor Phone", "Sponsor", False),
            ("sponsor_alt_phone", "Sponsor Alt Phone", "Sponsor", False),
            # Visa
            ("visa_number", "Visa Number", "Visa", False),
            ("bataka_number", "Bataka Number", "Visa", False),
            ("visa_issue_date", "Visa Issue Date", "Visa", False),
            ("visa_issue_date_hijri", "Visa Issue Date (Hijri)", "Visa", False),
            ("country", "Country", "Visa", False),
            ("embassy", "Embassy", "Visa", False),
            ("embassy_city", "Embassy City", "Visa", False),
            ("visa_quota", "Visa Quota", "Visa", False),
            ("benefits", "Benefits", "Visa", False),
            ("status", "Status", "Visa", False),
        ],
        "client": [
            ("company_name", "Company Name", "Client", False),
            ("company_name_arabic", "Company Name (Arabic)", "Client", True),
            ("owner_name", "Owner Name", "Client", False),
            ("oep_license_number", "OEP License No", "Client", False),
            ("client_type", "Client Type", "Client", False),
            ("country", "Country", "Client", False),
            ("city", "City", "Client", False),
            ("address", "Address", "Client", False),
            ("phone", "Phone", "Client", False),
            ("mobile", "Mobile", "Client", False),
            ("email", "Email", "Client", False),
            ("website", "Website", "Client", False),
            ("subdomain", "Subdomain", "Client", False),
            ("slug", "Slug", "Client", False),
            ("file_prefix", "File Prefix", "Client", False),
            ("starting_point", "Starting Point", "Client", False),
            ("status", "Status", "Client", False),
        ],
        "agent": [
            ("name", "Agent Name", "Agent", False),
            ("company_name", "Company Name", "Agent", False),
            ("phone", "Phone", "Agent", False),
            ("mobile", "Mobile", "Agent", False),
            ("email", "Email", "Agent", False),
            ("address", "Address", "Agent", False),
            ("city", "City", "Agent", False),
            ("status", "Status", "Agent", False),
        ],
    }
    base = schemas.get(data_source, [])
    base = base + [
        ("__today__", "[System] Today's Date", "System", False),
        ("__now__", "[System] Date + Time", "System", False),
    ]
    return [
        {"key": k, "label": l, "group": g, "arabic": ar}
        for k, l, g, ar in base
    ]
