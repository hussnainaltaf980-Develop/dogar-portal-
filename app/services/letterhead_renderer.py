"""Letterhead Renderer
======================

Renders the four "letterhead" documents that the user prints from the
**Protector Letter** workflow:

    1.  E-Barcode        — single-candidate barcode sheet (E-Number)
    2.  Main Letter      — REGISTRATION OF N FOREIGN SERVICE AGREEMENTS
    3.  Undertaking-B    — short Rule 15-A receipt certificate
    4.  Undertaking      — Ghazanfar Manzoor Dogar undertaking + table

All four documents render directly on the existing
``dogar_letterhead.jpg`` background. Unlike the form-overlay templates
(OEP Form, NBP Slip, etc.) these letters have **dynamic content
length** (the table grows with the number of candidates), so they
cannot be coordinate-mapped — they're emitted as ready-to-print HTML
that the browser auto-prints into A4.

The renderer is intentionally independent of the PDF coordinate engine
(``pdf_engine.py``) so the document customize Designer can keep
evolving without affecting these letters.
"""
from __future__ import annotations

import base64
import io
import os
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Iterable, Optional

from reportlab.graphics.barcode import code128, createBarcodeDrawing
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPM
from reportlab.lib.units import mm

# ----------------------------------------------------------------------
# Letterhead background — the JPG already contains the company header,
# Arabic header, license number, footer address. We just overlay the
# content area in-between.
#
# CRITICAL: the printable HTML is OFTEN opened via blob: URL (the
# protector-letter page POSTs the payload, gets HTML back, wraps it in
# a Blob and opens it). Relative paths inside blob: URLs do NOT always
# resolve back to the parent origin reliably across browsers/print
# previews, so the letterhead would disappear. We embed the JPG as a
# base64 data: URL so the document is fully self-contained and the
# letterhead ALWAYS renders, regardless of how the HTML is opened
# (direct GET, blob, file://, even copy-pasted into another tab).
# ----------------------------------------------------------------------
_LETTERHEAD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "pdf_backgrounds", "dogar_letterhead.jpg",
)


@lru_cache(maxsize=1)
def _letterhead_data_url() -> str:
    """Return the letterhead JPG as a ``data:image/jpeg;base64,…`` URL.

    Cached for the process lifetime — the asset is ~265 KB so embedding
    it inline costs ~360 KB of base64 per packet (still tiny vs. the
    document content)."""
    try:
        with open(_LETTERHEAD_PATH, "rb") as fh:
            payload = fh.read()
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")
    except Exception as exc:  # pragma: no cover — file is part of the repo
        print(f"[letterhead_renderer] cannot read letterhead asset: {exc}")
        # Fall back to the static path so the document at least tries to
        # load it via HTTP (works when the HTML is GETted directly).
        return "/static/pdf_backgrounds/dogar_letterhead.jpg"


# Exposed as a *property-like* module attribute so callers can keep
# using ``LETTERHEAD_URL`` in f-strings — the value is computed once
# the first time the module is imported.
LETTERHEAD_URL = _letterhead_data_url()


# ---------------------------------------------------------------------
# Per-tenant letterhead support
# ---------------------------------------------------------------------
# A tenant may upload its own letterhead image via
# ``POST /api/tenants/{id}/letterhead``. The file is stored at
# ``data/tenants/<slug>/letterhead_xxxxxxxx.<ext>`` and we embed it as
# a data: URL so the rendered HTML stays self-contained (works inside
# blob: URLs, hidden iframes, file:// etc — same rationale as the
# default Dogar letterhead above).
# ---------------------------------------------------------------------
_TENANT_ASSETS_ROOT = "data/tenants"


@lru_cache(maxsize=64)
def _tenant_letterhead_data_url(slug: str, filename: str) -> str:
    """Return a data: URL for the tenant's uploaded letterhead, or an
    empty string if the file is missing/unreadable so the caller can
    fall back to the default ``LETTERHEAD_URL``.

    The (slug, filename) tuple is hashable — perfect for lru_cache —
    and the upload endpoint clears this cache via
    ``_tenant_letterhead_data_url.cache_clear()`` after a fresh upload.
    """
    if not slug or not filename:
        return ""
    fpath = os.path.join(_TENANT_ASSETS_ROOT, slug, filename)
    if not os.path.isfile(fpath):
        return ""
    try:
        with open(fpath, "rb") as fh:
            payload = fh.read()
    except OSError:
        return ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}
    mime = mime_map.get(ext, "image/png")
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def letterhead_url_for(tenant=None) -> str:
    """Pick the correct letterhead data: URL for the given tenant.

    Accepts either a ``ResolvedTenant`` (from ``request.state.tenant``)
    or a ``Tenant`` ORM row, or ``None`` to fall back to the global
    Dogar default.  Always returns a non-empty URL so callers can
    interpolate the result directly into an ``<img src=…>`` without
    additional guards.
    """
    if tenant is None:
        return LETTERHEAD_URL
    slug = getattr(tenant, "slug", None) or ""
    filename = (getattr(tenant, "letterhead_path", None)
                or getattr(tenant, "letterhead_filename", None)
                or "")
    if not filename:
        return LETTERHEAD_URL
    url = _tenant_letterhead_data_url(slug, filename)
    return url or LETTERHEAD_URL

# A4 page = 210mm × 297mm. The letterhead JPG fills the whole sheet.
# Per the letterhead asset's actual geometry the header strip occupies
# the top ~15 % of the page (≈44.5 mm) and the footer takes the bottom
# ~8 % (≈24 mm). We add a few mm of breathing room so the user's text
# never touches the printed framework.
#   top    = 46mm   (clears the company header + Arabic block + frame)
#   bottom = 30mm   (clears the bilingual footer strip + frame)
#   left   = 22mm   (clears the decorative side border)
#   right  = 22mm
CONTENT_TOP_MM = 46
CONTENT_BOTTOM_MM = 30
CONTENT_LEFT_MM = 22
CONTENT_RIGHT_MM = 22


# ======================================================================
# BARCODE — produce a base64 PNG so we can embed it inline in the HTML
# (no extra HTTP requests, plays nice with browser print).
# ======================================================================
def generate_barcode_png_b64(value: str, *,
                             bar_height_mm: float = 16,
                             bar_width: float = 1.2,
                             show_text: bool = True) -> str:
    """Render a Code-128 barcode for ``value`` to a base64-encoded PNG
    suitable for embedding via ``<img src="data:image/png;base64,…">``.

    Returns an empty string when ``value`` is empty or barcode
    generation fails so the caller can fall back gracefully.
    """
    if not value:
        return ""
    try:
        # createBarcodeDrawing returns a properly-sized Drawing that
        # renderPM can rasterize directly — much more reliable than
        # poking at code128.Code128 widget internals.
        d = createBarcodeDrawing(
            "Code128",
            value=str(value),
            barHeight=bar_height_mm * mm,
            barWidth=bar_width,
            humanReadable=show_text,
        )
        buf = io.BytesIO()
        renderPM.drawToFile(d, buf, fmt="PNG", dpi=300)
        raw = buf.getvalue()

        # ----------------------------------------------------------------
        # ALIGNMENT FIX: reportlab bakes a left/right/top "quiet zone" of
        # white padding into the PNG. That padding made the black bars
        # start further RIGHT than the name & caption text (3 different
        # left margins). We auto-crop the surrounding whitespace so the
        # FIRST black bar sits at pixel x=0 — now the name, the bars and
        # the *VALUE* caption all share one perfect left edge.
        # We then re-pad a tiny, EQUAL quiet zone on left+right only
        # (scanners need a minimum quiet zone) but keep top flush.
        # ----------------------------------------------------------------
        try:
            from PIL import Image, ImageOps
            im = Image.open(io.BytesIO(raw)).convert("L")
            # bbox of non-white content (invert so content is the bright part)
            inverted = ImageOps.invert(im)
            bbox = inverted.getbbox()
            if bbox:
                im = im.crop(bbox)                       # bars start at x=0,y=0
                # Keep the LEFT edge flush (x=0) so the first bar lines up
                # perfectly with the name & caption text. Add quiet zone on
                # the RIGHT only (scanners need trailing quiet space; the
                # left margin is provided by the page itself).
                qz = max(10, int(round(bar_width * 8)))
                im = ImageOps.expand(im, border=(0, 0, qz, 0), fill=255)
            out = io.BytesIO()
            im.convert("RGB").save(out, format="PNG")
            raw = out.getvalue()
        except Exception as crop_exc:  # pragma: no cover — cropping is best-effort
            print(f"[letterhead_renderer] barcode crop skipped for {value!r}: {crop_exc}")

        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # pragma: no cover — barcode lib is best-effort
        print(f"[letterhead_renderer] barcode error for {value!r}: {exc}")
        return ""


# ======================================================================
# CANDIDATE → ROW DATA — keep all template look-ups in one place so the
# four HTML renderers below stay short.
# ======================================================================
def _date_fmt(v) -> str:
    if not v:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d/%m/%Y")
    return str(v)


def _split_name(full: str) -> tuple[str, str]:
    """Best-effort split of ``"GUL ZAIB"`` style names into (first, rest).
    The letterhead documents print Name + Father Name in separate
    columns even though we only store the combined ``full_name``.
    """
    if not full:
        return ("", "")
    parts = full.strip().split(None, 1)
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def candidate_row(cand) -> dict[str, Any]:
    """Normalize an ORM Candidate row into the dict the HTML templates
    consume. Defensive against ``None`` attributes — every key is
    always present so Jinja never raises.
    """
    name = (getattr(cand, "full_name", "") or "").strip()
    father = (getattr(cand, "father_name", "") or "").strip()
    # On older records sometimes the "father_name" got stored INSIDE
    # full_name (space-separated). Fall back to split() so the table
    # never shows blank.
    if not father:
        _, father = _split_name(name)
    return {
        "id":            getattr(cand, "id", None),
        "full_name":     name,
        "father_name":   father,
        "name_arabic":   getattr(cand, "name_arabic", "") or "",
        "father_name_arabic": getattr(cand, "father_name_arabic", "") or "",
        "passport_no":   (getattr(cand, "passport_no", "") or "").strip().upper(),
        "cnic":          (getattr(cand, "cnic", "") or "").strip(),
        "e_number":      (getattr(cand, "e_number", "") or "").strip(),
        "permission_no": (getattr(cand, "permission_no", "") or "").strip(),
        "permission_date": _date_fmt(getattr(cand, "permission_date", None)),
        "profession":    (getattr(cand, "profession", "") or "").strip(),
        "destination":   (getattr(cand, "destination", "") or "").strip(),
        "phone":         (getattr(cand, "phone", "") or "").strip(),
        "address":       (getattr(cand, "address", "") or "").strip(),
        "nationality":   (getattr(cand, "nationality", "PAKISTANI") or "PAKISTANI").strip(),
        "date_of_birth": _date_fmt(getattr(cand, "date_of_birth", None)),
    }


def demand_row(demand) -> dict[str, Any]:
    """Same flat-dict treatment for the linked demand file.
    Returns empty strings when ``demand`` is ``None`` so the templates
    can always reference ``ctx.demand.sponsor_name`` safely.
    """
    if demand is None:
        return {
            "file_number": "", "permission_no": "", "permission_date": "",
            "sponsor_name": "", "sponsor_name_arabic": "",
            "country": "", "embassy": "",
        }
    return {
        "file_number":         (getattr(demand, "file_number", "") or "").strip(),
        "permission_no":       (getattr(demand, "permission_no", "") or "").strip(),
        "permission_date":     _date_fmt(getattr(demand, "permission_date", None)),
        "sponsor_name":        (getattr(demand, "sponsor_name", "") or "").strip(),
        "sponsor_name_arabic": (getattr(demand, "sponsor_name_arabic", "") or "").strip(),
        "sponsor_address":     (getattr(demand, "sponsor_address", "") or "").strip(),
        "sponsor_address_arabic": (getattr(demand, "sponsor_address_arabic", "") or "").strip(),
        "sponsor_phone":       (getattr(demand, "sponsor_phone", "") or "").strip(),
        "country":             (getattr(demand, "country", "Saudi Arabia") or "Saudi Arabia").strip(),
        "embassy":             (getattr(demand, "embassy", "") or "").strip(),
        "visa_number":         (getattr(demand, "visa_number", "") or "").strip(),
        "visa_issue_date":     _date_fmt(getattr(demand, "visa_issue_date", None)),
        "visa_issue_date_hijri": (getattr(demand, "visa_issue_date_hijri", "") or "").strip(),
        "benefits":            (getattr(demand, "benefits", "") or "").strip(),
    }


def trades_from_demand(demand) -> list[dict[str, Any]]:
    """Pull the JobCategory rows off a Demand and shape them for the
    letterhead tables.  Returns an empty list when there are no trades."""
    out: list[dict[str, Any]] = []
    if demand is None:
        return out
    for jc in (getattr(demand, "job_categories", []) or []):
        out.append({
            "trade":           (getattr(jc, "trade", "") or "").strip(),
            "quantity":        int(getattr(jc, "quantity", 0) or 0),
            "salary":          float(getattr(jc, "salary", 0) or 0),
            "salary_currency": (getattr(jc, "salary_currency", "SAR") or "SAR").strip(),
            "contract_years":  int(getattr(jc, "contract_years", 2) or 2),
        })
    return out


# ======================================================================
# COMMON HTML SHELL — every letterhead document is rendered inside the
# same A4 page wrapper so the browser print dialog gets identical page
# geometry (paper size, margins, header offsets).
# ======================================================================
_BASE_STYLES = """
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin:0; padding:0; background:#525659; font-family: "Times New Roman", Times, serif; color:#0f172a; }

@page { size: A4 portrait; margin: 0; }

.toolbar{
    position:sticky; top:0; z-index:100;
    background:#1f2937; color:white; padding:10px 16px;
    display:flex; align-items:center; justify-content:space-between;
    box-shadow:0 2px 8px rgba(0,0,0,.35);
}
.toolbar h1{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:14px; margin:0; font-weight:600; }
.toolbar .sub{ font-family: ui-sans-serif, system-ui, sans-serif; font-size:11px; color:#cbd5e1; margin-top:2px; }
.toolbar button{
    font-family: ui-sans-serif, system-ui, sans-serif;
    background:#2563eb; color:white; border:0; padding:8px 18px;
    border-radius:4px; cursor:pointer; font-size:13px; font-weight:600;
    display:inline-flex; align-items:center; gap:6px;
}
.toolbar button:hover{ background:#1d4ed8; }
.toolbar button.secondary{ background:#475569; }
.toolbar button.secondary:hover{ background:#64748b; }

.sheet{
    position:relative;
    width:210mm; height:297mm;
    margin:18px auto;
    background:#fff;
    box-shadow:0 4px 18px rgba(0,0,0,.35);
    overflow:hidden;
    page-break-after:always;
}
.sheet:last-child{ page-break-after:auto; }
.sheet .bg{
    position:absolute; inset:0;
    width:100%; height:100%;
    object-fit:fill;
    user-select:none; pointer-events:none;
}
.sheet .content{
    position:absolute;
    top: %TOP%mm;
    bottom: %BOT%mm;
    left: %LEFT%mm;
    right: %RIGHT%mm;
    overflow:hidden;
}

/* Letter typography */
.letter        { font-size: 11.5pt; line-height: 1.45; }
.letter h1     { font-size: 13pt; font-weight: 700; text-align:center; margin:0 0 10px; text-decoration:underline; }
.letter h2     { font-size: 12pt; font-weight: 700; margin:8px 0 6px; }
.letter p      { margin: 6px 0; text-align: justify; }
.letter .meta-row{ display:flex; justify-content:space-between; margin-bottom: 14px; font-size: 11pt; }
.letter .meta-row b{ font-weight:700; }
.letter .sub   { font-weight: 700; text-decoration: underline; margin: 10px 0; text-align:center; }
.letter table.data{
    width:100%; border-collapse:collapse; margin: 6px 0 12px;
    font-size: 10.5pt;
}
.letter table.data th,
.letter table.data td{
    border:1px solid #000; padding: 4px 6px; vertical-align: middle;
}
.letter table.data th{ background:#f5f5f5; text-align:center; font-weight:700; }
.letter table.data td.c{ text-align:center; }
.letter .sig-block{
    margin-top: 26px; display:flex; justify-content: flex-end;
}
.letter .sig-block .sig{
    text-align:center; font-weight:700;
    min-width: 220px;
}
.letter .sig-block .sig .lbl{
    font-weight:400; font-size: 10.5pt; margin-bottom: 28px;
}
.letter .blanks .row{ margin: 8px 0; display:flex; align-items:baseline; gap:10px; }
.letter .blanks .row .lbl{ min-width: 220px; }
.letter .blanks .row .line{
    border-bottom: 1px solid #000; min-width: 80px;
    display:inline-block; padding: 0 6px; text-align:center;
}

/* ---------------------------------------------------------------- *
 * E-Barcode sheet — clean, minimal layout that clones the legacy
 * dogars.com print exactly: NAME at top, two barcodes stacked below
 * with their *value (e.g. *E01015229*) underneath. No "E-Number" or
 * "Passport No." label text — the barcodes speak for themselves.
 * Each block sits in normal flow with its own bottom margin so the
 * browser can NEVER collapse the two barcodes into a single band.
 * ---------------------------------------------------------------- */
.barcode-page{ font-family: "Times New Roman", Times, serif; position:relative; }
.barcode-page .bc-name{
    display: block;
    font-size: 12pt;
    font-weight: 700;
    letter-spacing: .4px;
    margin: 0 0 6mm 0;          /* gap before first barcode */
    line-height: 1.1;
    text-transform: uppercase;
    text-align: left;
    padding-left: 0;            /* aligned with the bar's left edge */
}
.barcode-block{
    display: block;
    margin: 0 0 10mm 0;          /* tight gap between barcodes (matches reference) */
    page-break-inside: avoid;
    break-inside: avoid;
    text-align: left;
    padding: 0;
    clear: both;
}
.barcode-block:last-of-type{ margin-bottom: 0; }
.barcode-block img{
    display: block;
    margin: 0;
}
/* Display the barcode at a FIXED HEIGHT with AUTO width so the high-res
   300-dpi PNG is never stretched/squashed — bars stay perfectly sharp and
   at their correct 1:1 module proportions (essential for reliable scanning).
   No object-fit:fill and no image-rendering:pixelated (both made the old
   sheet look blurry & cramped). A sensible max-width keeps very long codes
   from overflowing the page. */
/* CONSISTENT SIZE FIX (Problem 5 — "barcode size up, down"): both barcodes
   must look identical in size regardless of how many characters each value
   encodes. We pin BOTH a fixed height AND a fixed width so the two barcodes
   render as the same uniform rectangle (matching the demo OEP sheet). The
   underlying PNG is high-res (300 dpi) so light horizontal scaling keeps the
   bars crisp and fully scannable. */
.barcode-block img.bc-img{
    display: block;
    height: 20mm;
    width: 95mm;
    margin: 0;
    image-rendering: auto;
}
.barcode-block .num{
    display: block;
    font-size: 11pt;
    letter-spacing: 3px;
    margin: 1.5mm 0 0 0;
    font-family: ui-monospace, "Courier New", monospace;
    font-weight: 700;
}

/* Developer footer strip — HussnainTechVertex Pvt Ltd attribution */
.dev-footer{
    position: absolute; left: 0; right: 0; bottom: 0;
    display:flex; align-items:center; gap:6mm;
    padding: 4mm 0 0 0;
    border-top: 0.4mm solid #cbd5e1;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 8pt; color:#475569;
    letter-spacing: 0.3px;
}
.dev-footer img{ height: 6mm; width: auto; opacity:0.85; }
.dev-footer b{ color:#0f172a; font-weight: 700; }

/* Blank-page variant: PURE WHITE sheet, no letterhead background.
   Used by the E-Barcode document so the barcodes print "as-is" — scanners
   need a clean quiet zone, no logos / borders / watermarks.
   The .sheet.blank-sheet rule explicitly nukes any .bg image that might
   have leaked through from a parent CSS bundle. */
.sheet.blank-sheet{
    background: #ffffff !important;
}
.sheet.blank-sheet .bg{ display: none !important; }
.barcode-page-blank{
    padding: 0;            /* content flush to top-left of .content box */
    background: transparent;
}
.barcode-page-blank .bc-name{
    margin: 0 0 8mm 0;     /* tight gap below the name */
}

@media print {
    html, body{ background:#fff !important; }
    .toolbar{ display:none !important; }
    .sheet{ margin:0 !important; box-shadow:none !important; }
}
"""


def _styles_for(top=CONTENT_TOP_MM, bot=CONTENT_BOTTOM_MM,
                left=CONTENT_LEFT_MM, right=CONTENT_RIGHT_MM) -> str:
    return (_BASE_STYLES
            .replace("%TOP%", str(top))
            .replace("%BOT%", str(bot))
            .replace("%LEFT%", str(left))
            .replace("%RIGHT%", str(right)))


def _wrap_document(title: str, toolbar_sub: str, body_html: str,
                   auto_print: bool = True,
                   top=CONTENT_TOP_MM, bot=CONTENT_BOTTOM_MM,
                   left=CONTENT_LEFT_MM, right=CONTENT_RIGHT_MM) -> str:
    """Wrap the per-document inner HTML into the full standalone page
    (toolbar + style + print script).
    """
    print_script = ""
    if auto_print:
        # Wait for images to load (the letterhead bg + any barcode
        # data-URLs) before invoking window.print() so the print
        # preview shows the same thing the user sees on screen.
        print_script = """
        (function(){
            let printed = false;
            function go(){
                if (printed) return;
                printed = true;
                setTimeout(() => { window.focus(); window.print(); }, 250);
            }
            const imgs = [...document.images];
            if (!imgs.length) { go(); return; }
            let loaded = 0;
            imgs.forEach(im => {
                if (im.complete && im.naturalWidth > 0) { loaded++; }
                else {
                    im.addEventListener('load',  () => { if (++loaded === imgs.length) go(); });
                    im.addEventListener('error', () => { if (++loaded === imgs.length) go(); });
                }
            });
            if (loaded === imgs.length) go();
            setTimeout(go, 4000); // safety net
        })();
        """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{_styles_for(top, bot, left, right)}</style>
</head>
<body>
<div class="toolbar">
    <div>
        <h1>{title}</h1>
        <div class="sub">{toolbar_sub}</div>
    </div>
    <div style="display:flex; gap:8px;">
        <button class="secondary" onclick="window.close()">Close</button>
        <button onclick="window.print()">🖨️ Print Now</button>
    </div>
</div>
{body_html}
<script>{print_script}</script>
</body>
</html>"""


# ======================================================================
# DOCUMENT 1 — E-Barcode (single candidate)
# ======================================================================
def render_e_barcode(cand, *, file_number: str = "", auto_print: bool = True,
                     blank: bool = True, tenant=None) -> str:
    """Render the E-Barcode sheet for a single candidate.

    v8 layout — **clean BLANK white page**, content flush to TOP-LEFT
    exactly like the user's Turn 5 reference screenshot:
        ┌────────────────────────────────────┐
        │ QASIM ALI                          │  ← name (12pt bold)
        │                                    │
        │ ▌▌▌  ▌▌  ▌▌▌▌ ▌▌▌▌▌▌  ▌ ▌▌▌▌      │  ← E-Number barcode
        │ *E01015229*                        │
        │                                    │
        │ ▌▌▌▌  ▌▌  ▌▌▌▌▌▌▌▌▌ ▌▌▌  ▌▌▌      │  ← Passport barcode
        │ *BB6807443*                        │
        │                                    │
        │   (rest of page = pure white)      │
        │                                    │
        └────────────────────────────────────┘

    No letterhead. No header bar. No timestamps. Just the candidate name
    + 2 barcodes top-left. This is a *scannable* document — letterhead
    branding breaks barcode scanners and the user has shouted at us
    twice now to keep it clean.

    The ``blank`` kwarg is now a no-op (kept for API compat). All output
    is always blank/clean. To re-enable the legacy letterhead variant
    (NOT recommended), use ``render_e_barcode_branded()`` instead.
    """
    row = candidate_row(cand)
    name = (row["full_name"] or "—").upper()

    e_value = (row["e_number"] or "").strip()

    # ----- Second barcode = Passport Number -----
    pn_value = (row.get("passport_no") or "").strip()
    if not pn_value:
        pn_value = (file_number or "").strip()
    if not pn_value:
        cid = getattr(cand, "id", 0) or 0
        pn_value = f"{int(cid):09d}" if cid else ""

    # Bolder, taller bars for a crisp, scannable, professional look that
    # matches the user's reference sheet. bar_width=2.6 gives thick high-
    # contrast bars; bar_height_mm=20 gives a taller barcode. The image is
    # then displayed at its NATURAL proportions (no CSS stretching) so it
    # stays razor-sharp instead of blurry/cramped.
    blocks = []
    if e_value:
        e_b64 = generate_barcode_png_b64(e_value, bar_width=2.6, bar_height_mm=20, show_text=False)
        blocks.append(f"""
        <div class="barcode-block">
            {('<img src="' + e_b64 + '" alt="E-Number" class="bc-img">') if e_b64 else '<div style="color:#b91c1c">[E-Number barcode render failed]</div>'}
            <div class="num">*{_h(e_value)}*</div>
        </div>""")

    if pn_value:
        p_b64 = generate_barcode_png_b64(pn_value, bar_width=2.6, bar_height_mm=20, show_text=False)
        blocks.append(f"""
        <div class="barcode-block">
            {('<img src="' + p_b64 + '" alt="Passport Number" class="bc-img">') if p_b64 else '<div style="color:#b91c1c">[Passport barcode render failed]</div>'}
            <div class="num">*{_h(pn_value)}*</div>
        </div>""")

    if not blocks:
        blocks.append("""
        <div style="color:#b91c1c; padding: 20mm 0; font-family: sans-serif;">
            This candidate has neither an E-Number nor a Passport Number yet,
            so the E-Barcode sheet cannot be generated.
        </div>""")

    # ALWAYS blank — no letterhead variant. Content flush top-left.
    body = f"""
<section class="sheet blank-sheet">
    <div class="content barcode-page barcode-page-blank">
        <div class="bc-name">{_h(name)}</div>
        {''.join(blocks)}
    </div>
</section>"""

    return _wrap_document(
        title=f"E-Barcode · {name}",
        toolbar_sub=f"{len(blocks)} barcode(s) · clean blank page",
        body_html=body,
        auto_print=auto_print,
        # Top-left flush — only a small left/top margin so scanner has clear
        # quiet-zone around the barcodes (per ISO/IEC 15417 + barcode best practice).
        top=12, bot=8, left=14, right=14,
    )


# ======================================================================
# DOCUMENT 2 — Main Letter (registration of N foreign service agreements)
# ======================================================================
def render_main_letter(candidates: Iterable[Any],
                       demand: Optional[Any] = None,
                       *,
                       file_number: str = "",
                       letter_date: Optional[date] = None,
                       embassy_entry_airport: str = "",
                       registration_fee: str = "",
                       challan_no: str = "",
                       challan_date: str = "",
                       permission_granted: str = "",
                       fsa_submitted: str = "",
                       balance: str = "",
                       auto_print: bool = True) -> str:
    """The main letter to THE PROTECTOR OF EMIGRANTS, SIALKOT.
    Faithful reproduction of paragraphs a)–e) and the trailing
    "Permission granted for / FSA now submitted for / Balance" block.
    """
    rows = [candidate_row(c) for c in candidates]
    n = len(rows)
    dem = demand_row(demand)
    letter_date = letter_date or date.today()

    # Auto-fill from demand when not provided
    if not file_number:
        file_number = dem["file_number"] or dem["permission_no"]
    if not fsa_submitted:
        fsa_submitted = str(n)

    table_html = "".join([
        f"""<tr>
            <td class="c">{i+1}</td>
            <td>{_h(r['full_name'])} {_h(r['father_name'])}</td>
            <td class="c">{_h(dem['permission_no'] or r['permission_no'])} / Dated {_h(dem['permission_date'] or r['permission_date'])}</td>
            <td class="c">{_h(r['passport_no'])}</td>
            <td class="c">{_h(r['profession']) or '—'}</td>
            <td>{_h(dem['sponsor_name']) or '—'}</td>
        </tr>"""
        for i, r in enumerate(rows)
    ]) or """<tr><td colspan="6" style="text-align:center; padding: 14px; color:#64748b;">— no candidates selected —</td></tr>"""

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <div class="meta-row">
            <div><b>File Number:</b> {_h(file_number) or '_______________'}</div>
            <div><b>Date:</b> {_h(letter_date.strftime("%d/%m/%Y"))}</div>
        </div>

        <p><b>THE PROTECTOR OF EMIGRANTS,</b><br><b>SIALKOT.</b></p>

        <p class="sub">SUBJECT: REGISTRATION OF {n:02d} FOREIGN SERVICE AGREEMENT{'S' if n != 1 else ''}.</p>

        <p>Dear Sir,</p>
        <p style="text-indent: 30px;">
            We are submitting herewith {_num_word(n)} ({n}) Foreign Service Agreement{'s' if n != 1 else ''}
            along with document, in respect of the following emigrants as per list
            attached for registration against permission no.
            <b>{_h(dem['permission_no']) or '____________'}</b>.
        </p>

        <table class="data">
            <thead>
                <tr>
                    <th style="width:6%">S.No</th>
                    <th style="width:24%">Name &amp; Father Name</th>
                    <th style="width:18%">Permission No. &amp; Date</th>
                    <th style="width:14%">Passport No</th>
                    <th style="width:14%">Category</th>
                    <th style="width:24%">Sponsor Name</th>
                </tr>
            </thead>
            <tbody>
                {table_html}
            </tbody>
        </table>

        <p><b>a)</b> It is further submitted that visas have been stamped on the passports of the intending emigrants.</p>
        <p><b>b)</b> It is submitted that the visas of the intending emigrants are available at the airport of entry namely
            <u>{_h(embassy_entry_airport) or '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'}</u>
            the photocopies are attached herewith.</p>
        <p><b>c)</b> Original bank Certificate on Form-7 &amp; 7 (A) in respect of each worker is hereby presented and
            registration fees of Rs.<u>{_h(registration_fee) or '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'}</u>
            has been deposited in the Government Treasury/State Bank of Pakistan
            <u>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</u>
            vide challan No. <u>{_h(challan_no) or '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'}</u>
            dated <u>{_h(challan_date) or '&nbsp;___/___/___'}</u> (original enclosed)</p>
        <p><b>d)</b> It is certified that contents of the Foreign Service Agreements have been explained to the emigrants in their own language.</p>
        <p><b>e)</b> It is certified that all the information given above correct.</p>

        <div class="blanks" style="margin-top: 14px;">
            <div class="row"><span class="lbl">Permission granted for</span><span class="line">{_h(permission_granted) or '&nbsp;&nbsp;&nbsp;&nbsp;'}</span><span>Workers</span></div>
            <div class="row"><span class="lbl">FSA now submitted for</span><span class="line">{_h(fsa_submitted) or '&nbsp;&nbsp;&nbsp;&nbsp;'}</span><span>Workers</span></div>
            <div class="row"><span class="lbl">Balance in the Permission</span><span class="line">{_h(balance) or '&nbsp;&nbsp;&nbsp;&nbsp;'}</span><span>Workers</span></div>
        </div>

        <div class="sig-block">
            <div class="sig">
                <div class="lbl">Thanking You,<br>Your Faithfully,<br>For DOGAR TRADING CORPORATION</div>
                (GHAZANFAR MANZOOR DOGAR)
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title=f"Main Letter · Registration of {n} FSA",
        toolbar_sub=f"{n} emigrant(s) · File {file_number or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# DOCUMENT 3 — Undertaking-B
# ======================================================================
def render_undertaking_b(candidates: Iterable[Any],
                         demand: Optional[Any] = None,
                         *,
                         letter_date: Optional[date] = None,
                         auto_print: bool = True) -> str:
    """The short Rule 15-A undertaking. The screenshot shows ONE
    paragraph + signature; the user's batch may cover N candidates
    but the document itself is per-batch, not per-candidate.
    """
    rows = [candidate_row(c) for c in candidates]
    dem = demand_row(demand)
    letter_date = letter_date or date.today()
    n = len(rows)
    permission_label = dem["permission_no"] or (rows[0]["permission_no"] if rows else "")
    permission_date  = dem["permission_date"] or (rows[0]["permission_date"] if rows else "")

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <div style="border: 1.5px solid #000; padding: 14mm 10mm; min-height: 220mm; position: relative;">
            <div class="meta-row" style="margin-bottom: 22px;">
                <div><b>{_h(permission_label) or 'PE/L/1338/'}</b> &nbsp;&nbsp; Dated {_h(permission_date) or '_____________'}</div>
                <div><b>{_h(permission_label) or 'PE/L/1338/'}</b> &nbsp;&nbsp; Dated {_h(permission_date) or '_____________'}</div>
            </div>

            <h1 style="margin-top: 6px;">UNDERTAKING-B</h1>

            <p style="margin-top: 24px; text-align: justify; font-size: 12pt; line-height: 1.6;">
                It Is Further Certified That Receipt Of Actual Expenses Occurred On Air
                Ticketing Medical Work Permit, Levy Visa And Documentation Of The Emigrant
                Under Rule 15-A Have Been Issued To The Emigrant Selected Against
                Permission Preferred In Our Letter.
            </p>

            <div style="position: absolute; right: 14mm; bottom: 18mm; text-align: center; font-weight: 700;">
                <div style="height: 28px;"></div>
                Signature of OEP
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Undertaking-B",
        toolbar_sub=f"Rule 15-A receipt · {n} candidate(s)",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# DOCUMENT 4 — Undertaking (Ghazanfar Manzoor Dogar)
# ======================================================================
def render_undertaking(candidates: Iterable[Any],
                       demand: Optional[Any] = None,
                       *,
                       letter_date: Optional[date] = None,
                       auto_print: bool = True) -> str:
    rows = [candidate_row(c) for c in candidates]
    n = len(rows)

    table_html = "".join([
        f"""<tr>
            <td class="c">{i+1}</td>
            <td>{_h(r['full_name'])}</td>
            <td>{_h(r['father_name'])}</td>
            <td class="c">{_h(r['passport_no'])}</td>
        </tr>"""
        for i, r in enumerate(rows)
    ]) or """<tr><td colspan="4" style="text-align:center; padding:14px; color:#64748b;">— no candidates selected —</td></tr>"""

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <h1>UNDERTAKING</h1>

        <p style="margin-top: 14px; text-align: justify;">
            I, <b>Ghazanfar Manzoor Dogar</b> Proprietor Of M/S. <b>Dogar Trading Corporation</b>
            OEP Licence <b>No.1338/SKT</b>. Do Here By Undertake That The Documents And Visa
            In This Respect Of The Following Emigrants Whose Cases Have Been Submitted For
            Registration And Genuine.
        </p>

        <table class="data" style="margin-top: 10px;">
            <thead>
                <tr>
                    <th style="width:6%">SR</th>
                    <th style="width:34%">Name</th>
                    <th style="width:34%">Father Name</th>
                    <th style="width:26%">Passport No</th>
                </tr>
            </thead>
            <tbody>
                {table_html}
            </tbody>
        </table>

        <p style="text-align: justify; margin-top: 14px;">
            I, The Documents/ Visa Registration Fee Challan Welfare Fund Insurance
            Certificate And Nadra Token Are Genuine And In Case These Are Found/Bogus,
            I Shall Be Responsible And Liable For Punitive Action under the relevant Law.
            I further certify that emigrants mentioned above are bonfire Pakistani
            National and have been produced for briefing in the Protector of Emigrants.
        </p>

        <div class="sig-block" style="margin-top: 36px;">
            <div class="sig">
                <div class="lbl" style="margin-bottom: 24px;">&nbsp;</div>
                (GHAZANFAR MANZOOR DOGAR)
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Undertaking",
        toolbar_sub=f"{n} emigrant(s)",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# COMBINED PRINT — render all three (Main + Undertaking-B + Undertaking)
# back-to-back in a single tab so the user can fire ONE print job that
# spits out the complete protector packet.
# ======================================================================
def render_protector_packet(candidates: Iterable[Any],
                            demand: Optional[Any] = None,
                            *,
                            file_number: str = "",
                            letter_date: Optional[date] = None,
                            embassy_entry_airport: str = "",
                            registration_fee: str = "",
                            challan_no: str = "",
                            challan_date: str = "",
                            permission_granted: str = "",
                            fsa_submitted: str = "",
                            balance: str = "",
                            include: tuple[str, ...] = ("main", "undertaking_b", "undertaking"),
                            auto_print: bool = True) -> str:
    rows = list(candidates)
    pieces: list[str] = []

    if "main" in include:
        # Extract just the <section class="sheet"> from each renderer.
        pieces.append(_extract_sheet(render_main_letter(
            rows, demand,
            file_number=file_number, letter_date=letter_date,
            embassy_entry_airport=embassy_entry_airport,
            registration_fee=registration_fee, challan_no=challan_no,
            challan_date=challan_date, permission_granted=permission_granted,
            fsa_submitted=fsa_submitted, balance=balance,
            auto_print=False,
        )))
    if "undertaking_b" in include:
        pieces.append(_extract_sheet(render_undertaking_b(
            rows, demand, letter_date=letter_date, auto_print=False,
        )))
    if "undertaking" in include:
        pieces.append(_extract_sheet(render_undertaking(
            rows, demand, letter_date=letter_date, auto_print=False,
        )))

    n = len(rows)
    body = "\n".join(pieces)
    return _wrap_document(
        title=f"Protector Letter Packet · {n} emigrant(s)",
        toolbar_sub=f"{len(pieces)} page(s) · prints to A4",
        body_html=body,
        auto_print=auto_print,
    )


def _extract_sheet(full_html: str) -> str:
    """Pull every ``<section class="sheet">…</section>`` block out of a
    fully-rendered single-letter HTML so we can concatenate multiple
    letters into one document.
    """
    import re
    matches = re.findall(r"<section class=\"sheet\".*?</section>", full_html, flags=re.DOTALL)
    return "\n".join(matches)


# ======================================================================
# DEMAND DOCUMENTS — five letterhead-backed documents that come off a
# single Demand row (visa permission paperwork). Unlike the Protector
# letters above, these are PER-DEMAND not per-batch-of-candidates.
# ======================================================================
def _format_currency(amount: float, currency: str = "SAR") -> str:
    """Format like '1200 SR' / '1500 SAR' for the salary columns."""
    if not amount:
        return ""
    try:
        i = int(amount)
        s = str(i) if abs(amount - i) < 0.005 else f"{amount:.2f}"
    except Exception:
        s = str(amount)
    cur = (currency or "SR").upper()
    if cur == "SAR":
        cur = "SR"
    return f"{s} {cur}"


def _trades_total_qty(trades: list[dict[str, Any]]) -> int:
    return sum(int(t.get("quantity", 0) or 0) for t in trades)


def _benefits_lines(demand_benefits: str) -> str:
    """Convert the freeform 'benefits' column on the demand into the
    short benefits paragraph shown on the Demand Letter."""
    text = (demand_benefits or "").strip()
    if text:
        return _h(text)
    return ("Free food, Accommodation, Medical Treatment, Passage included in salary "
            "&amp; other benefits as per Saudi labour laws.")


# ----------------------------------------------------------------------
# DEMAND DOC 1 — DEMAND LETTER
# ----------------------------------------------------------------------
def render_demand_letter(demand, *, letter_date: Optional[date] = None,
                         auto_print: bool = True) -> str:
    dem = demand_row(demand)
    trades = trades_from_demand(demand)
    total_qty = _trades_total_qty(trades) or sum(t["quantity"] for t in trades)
    letter_date = letter_date or date.today()

    visa_blurb = ""
    if dem["visa_number"]:
        visa_blurb = f"Visa No <b>{_h(dem['visa_number'])}</b>"
        if dem["visa_issue_date_hijri"]:
            visa_blurb += f" Dated <b>{_h(dem['visa_issue_date_hijri'])}</b>"
        elif dem["visa_issue_date"]:
            visa_blurb += f" Dated <b>{_h(dem['visa_issue_date'])}</b>"

    rows_html = "".join([
        f"""<tr>
            <td>{_h(t['trade'])}</td>
            <td class="c">{t['quantity']}</td>
            <td class="c">{_h(_format_currency(t['salary'], t['salary_currency']))}</td>
        </tr>"""
        for t in trades
    ]) or '<tr><td colspan="3" class="c" style="color:#64748b;">— no trades on this demand —</td></tr>'

    # contract_years — take from first trade (typical demand has one period)
    contract_period = "TWO YEARS"
    if trades:
        n = trades[0]["contract_years"]
        contract_period = {1:"ONE YEAR", 2:"TWO YEARS", 3:"THREE YEARS",
                           4:"FOUR YEARS", 5:"FIVE YEARS"}.get(n, f"{n} YEARS")

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <h1>DEMAND LETTER</h1>
        <p style="text-align:center; font-style: italic; margin-top:-4px; margin-bottom: 12px;">
            According to the instruction from our principal
        </p>

        <p>M/s. <b>{_h(dem['sponsor_name'])}</b>{(' , ' + _h(dem['sponsor_address'])) if dem['sponsor_address'] else ''}{(', ' + _h(dem['country'])) if dem['country'] else ''}.
        We are authorize to recruit the personal for following for categories
        against {visa_blurb or 'Visa No <u>____________</u> Dated <u>____________</u>'}
        on his behalf Terms &amp; Conditions are given below:-</p>

        <table class="data" style="margin-top: 10px;">
            <thead>
                <tr>
                    <th style="width:50%">TRADE</th>
                    <th style="width:25%">NO OF VACANCIES</th>
                    <th style="width:25%">SALARY PER MONTH</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
                <tr>
                    <td class="c"><b>Total</b></td>
                    <td class="c"><b>{total_qty}</b></td>
                    <td></td>
                </tr>
            </tbody>
        </table>

        <p style="margin-top: 14px;">{_benefits_lines(dem['benefits'])}</p>

        <p style="margin-top: 18px; font-weight: 700; text-decoration: underline;">
            PERIOD OF CONTRACT: {contract_period}
        </p>

        <div style="margin-top: 40px; display:flex; justify-content:space-between; align-items:flex-end;">
            <div>Date <u>&nbsp;&nbsp;{_h(letter_date.strftime('%d/%m/%Y'))}&nbsp;&nbsp;</u></div>
            <div style="text-align:center; font-weight: 700;">
                <div style="height: 20px;"></div>
                ATTORNY<br>
                (GHAZANFAR MANZOOR DOGAR)
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Demand Letter",
        toolbar_sub=f"{dem['sponsor_name'] or 'Demand'} · File {dem['file_number'] or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ----------------------------------------------------------------------
# DEMAND DOC 2 — UNDERTAKING (Demand undertaking with trade table)
# ----------------------------------------------------------------------
def render_demand_undertaking(demand, *, letter_date: Optional[date] = None,
                              auto_print: bool = True) -> str:
    dem = demand_row(demand)
    trades = trades_from_demand(demand)
    letter_date = letter_date or date.today()
    date_str = letter_date.strftime("%d/%m/%Y")

    rows_html = "".join([
        f"""<tr>
            <td class="c">{i+1}</td>
            <td>{_h(t['trade'])}</td>
            <td class="c">{t['quantity']}</td>
            <td class="c">{_h(_format_currency(t['salary'], t['salary_currency']))}</td>
            <td class="c">{t['contract_years']} Years</td>
        </tr>"""
        for i, t in enumerate(trades)
    ]) or '<tr><td colspan="5" class="c" style="color:#64748b;">— no trades —</td></tr>'

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <h1>UNDERTAKING</h1>

        <div class="meta-row" style="margin-top: 10px;">
            <div><b>FILE NO.</b> {_h(dem['file_number']) or '_______________'}</div>
            <div><b>DATED:</b> {_h(date_str)}</div>
        </div>

        <p style="margin-top: 14px;">
            WE M/S. <u><b>DOGAR TRADING CORPORATION</b></u> OEPL/NO. <u><b>1338/SKT</b></u>
            Solemnly affirm that M/S. <u><b>{_h(dem['sponsor_name']) or '________________'}</b></u>
            Vide sikka wakala/khitab Letter No. <u><b>{_h(dem['visa_number']) or '____________'}</b></u>
            DATED: <u>{_h(dem['visa_issue_date_hijri'] or dem['visa_issue_date']) or '__/__/____'}</u>
        </p>

        <p>Have authorized us to recruit workers and arrange their departure to the
        employer. We further confirm and stand guarantee that the employer is in need of
        workers in the following categories of workers and shall provide the salary and
        fringe benefits as detailed below.</p>

        <table class="data" style="margin-top: 10px;">
            <thead>
                <tr>
                    <th style="width:8%">SR#</th>
                    <th style="width:40%">Category</th>
                    <th style="width:16%">Number Required</th>
                    <th style="width:18%">Salary</th>
                    <th style="width:18%">Contract Period</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>

        <p style="margin-top: 12px;"><b>Other fringe benefits:</b></p>
        <div style="padding-left: 8mm; line-height: 1.6;">
            a). Accommodation, Medical And Local Transport Are Free<br>
            b). Food Free or Twenty Five Percent Of The Basic Salary<br>
            c). Air Passage Free/Air Passage Not Provided<br>
            d). Other Benefits As Per Labour Law
        </div>

        <p style="margin-top: 12px;">
            We undertake that the employer shall provide salary and other fringe benefits
            as enumerated above. In case of violations of the above conditions we shall be
            liable for action under emigration laws.
        </p>

        <div class="sig-block" style="margin-top: 30px;">
            <div class="sig">
                <div class="lbl">FOR DOGAR TRADING CORPORATION</div>
                GHAZANFAR MANZOOR DOGAR
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Undertaking (Demand)",
        toolbar_sub=f"File {dem['file_number'] or '—'} · {dem['sponsor_name'] or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ----------------------------------------------------------------------
# DEMAND DOC 3 — UNDERTAKING (Visa Genuine — short undertaking)
# ----------------------------------------------------------------------
def render_demand_visa_undertaking(demand, *, auto_print: bool = True) -> str:
    dem = demand_row(demand)
    visa_date = dem["visa_issue_date_hijri"] or dem["visa_issue_date"] or "___________"

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <h1>UNDERTAKING</h1>

        <p style="margin-top: 14px;">
            <b>1.</b> I, <u><b>GHAZANFAR MANZOOR DOGAR</b></u> proprietor of M/S. <u><b>DOGAR TRADING CORPORATION</b></u>
            OEP License No. <u><b>1338/SKT</b></u> do hereby that Employment Visa No.
            <u><b>{_h(dem['visa_number']) or '____________'}</b></u> Dated:
            <u><b>{_h(visa_date)}</b></u> for <u><b>{_h(dem['country']) or 'SAUDI ARABIA'}</b></u>
            workers in respect of our Principal/Employer M/S. <u><b>{_h(dem['sponsor_name']) or '________________'}</b></u>
            Telephone no. <u>{_h(dem['sponsor_phone']) or '_______________'}</u>
            address: <u><b>{_h(dem['sponsor_address']) or '________________'}</b></u>{(', ' + _h(dem['country'])) if dem['country'] else ''}
            is genuine.
        </p>

        <p><b>2.</b> The above mentioned Principal/employer has appointed our agency to recruit these
        persons and complete all the relevant procedure in their departure to kingdom of Saudi
        Arabia. We further stand guarantee that if the electronic visas and authorization submitted by
        us are found fake/false and agreement is violated by the Principal/employer we will be
        liable for legal action under Emigration Ordinance 1979 and rules made there under.</p>

        <p><b>3.</b> Due care has been taken to ensure that workers will work with the same employer they
        have been advised to abide by the law of their country of employment. This statement is
        correct to the best of knowledge and belief and nothing has been willfully concealed or
        malafide intension.</p>

        <p><b>4.</b> We will charge PKR 20,000/-</p>

        <div class="sig-block" style="margin-top: 28px;">
            <div class="sig">
                <div class="lbl">Yours sincerely</div>
                Signature: ___________________<br>
                Name: <b>GHAZANFAR MANZOOR DOGAR</b>
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Undertaking (Visa Genuine)",
        toolbar_sub=f"Visa {dem['visa_number'] or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ----------------------------------------------------------------------
# DEMAND DOC 4 — UNDERTAKING (Roman-numeral clauses)
# ----------------------------------------------------------------------
def render_demand_roman_undertaking(demand, *, auto_print: bool = True) -> str:
    dem = demand_row(demand)

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter">
        <h1>UNDERTAKING</h1>

        <p style="margin-top: 14px;">
            I, <u><b>GHAZANFAR MANZOOR DOGAR</b></u> proprietor of M/S.
            <u><b>DOGAR TRADING CORPORATION</b></u> OEP License No.
            <u><b>1338/SKT</b></u> here by undertaking that:-
        </p>

        <p><b>I)</b> In the event of the aforesaid information being found false or incorrected in any
        respect the recruitment operation shall be liable to legal/other action.</p>

        <p><b>II)</b> In case any worker proceeding abroad in accordance with the permission is to be
        repatriated due to inability of the employer or is unable to secure a job under the
        employment contract, I shall bear all cost of such repatriation.</p>

        <p><b>III)</b> In case of any dispute regarding terms &amp; conditions laid down by me in the demand
        on behalf of the employer M/s <u><b>{_h(dem['sponsor_name']) or '________________'}</b></u>{(', ' + _h(dem['sponsor_address'])) if dem['sponsor_address'] else ''}{(', ' + _h(dem['country'])) if dem['country'] else ''}.
        I shall held responsible.</p>

        <p><b>IV)</b> That Visa confirmation by Royal Saudi Embassy / Consulate is genuine and in case of
        any mishap I shall be liable for legal action.</p>

        <div class="sig-block" style="margin-top: 40px;">
            <div class="sig">
                <div class="lbl">For DOGAR TRADING CORPORATION</div>
                (GHAZANFAR MANZOOR DOGAR)
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Undertaking (Clauses)",
        toolbar_sub=f"{dem['sponsor_name'] or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ----------------------------------------------------------------------
# DEMAND DOC 5 — PERMISSION APPLICATION / Cover letter to Protector
# ----------------------------------------------------------------------
def render_demand_permission_request(demand, *, letter_date: Optional[date] = None,
                                     auto_print: bool = True) -> str:
    dem = demand_row(demand)
    trades = trades_from_demand(demand)
    total_qty = _trades_total_qty(trades)
    letter_date = letter_date or date.today()

    visa_blurb = ""
    if dem["visa_number"]:
        visa_blurb = f"Visa number <u><b>{_h(dem['visa_number'])}</b></u>"
        if dem["visa_issue_date_hijri"]:
            visa_blurb += f" Dated <u><b>{_h(dem['visa_issue_date_hijri'])}</b></u>"
        elif dem["visa_issue_date"]:
            visa_blurb += f" Dated <u><b>{_h(dem['visa_issue_date'])}</b></u>"
    else:
        visa_blurb = "Visa number <u>____________</u> Dated <u>____________</u>"

    benefits_cell = "Provided with Free Food, Accommodation, Medical &amp; Passage, other facilities will be provided to local According to Labour Law"
    rows_html = "".join([
        f"""<tr>
            <td>{_h(t['trade'])}</td>
            <td class="c">{t['quantity']}</td>
            <td class="c">{_h(_format_currency(t['salary'], t['salary_currency']))}</td>
            <td class="c">{t['contract_years']} Years</td>
            <td>{benefits_cell if i == 0 else ''}</td>
        </tr>"""
        for i, t in enumerate(trades)
    ]) or '<tr><td colspan="5" class="c" style="color:#64748b;">— no trades —</td></tr>'

    body = f"""
<section class="sheet">
    <img class="bg" src="{LETTERHEAD_URL}" alt="">
    <div class="content letter" style="font-size: 10.5pt;">
        <div class="meta-row">
            <div>
                <b>The Protector of Emigrants,</b><br>
                Government of Pakistan,<br>
                <b>SIALKOT</b>
            </div>
            <div><b>File No:</b> {_h(dem['file_number']) or '_______________'}</div>
        </div>

        <p class="sub" style="margin-top: 8px;">
            Subject: PERMISSION FOR RECRUITMENT OF PERSONNEL FOR EMPLOYMENT ABROAD.
        </p>

        <p>Dear Sir,</p>
        <p style="text-indent: 24px;">
            We have been instructed by our principal M/S
            <u><b>{_h(dem['sponsor_name']) or '________________'}</b></u>{(' ' + _h(dem['sponsor_address'])) if dem['sponsor_address'] else ''}{(', ' + _h(dem['country'])) if dem['country'] else ', SAUDI ARABIA'}.
            To recruit personnel for following category / categories against their
            {visa_blurb} Vide their power of attorney and demand letter copies enclosed.
        </p>

        <table class="data">
            <thead>
                <tr>
                    <th style="width:28%">TRADE</th>
                    <th style="width:12%">No. Of Vacancies</th>
                    <th style="width:14%">Salary Per Month</th>
                    <th style="width:14%">Period Of Contract</th>
                    <th>Other Benefits</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
                <tr>
                    <td class="c"><b>Total</b></td>
                    <td class="c"><b>{total_qty}</b></td>
                    <td></td><td></td><td></td>
                </tr>
            </tbody>
        </table>

        <div style="display: grid; grid-template-columns: 1.4fr 1fr; gap: 8mm; margin-top: 8px; font-size: 9.5pt; line-height: 1.45;">
            <div>
                <b>The Process will be carried through:</b><br>
                (a) Advertisement collection of CVs. Interview/Test and Recruitment<br>
                (b) Advertisement Interview/Test Recruitment<br>
                (c) Recruitment from left over CVs, Against Demand<br>
                (d) Nominees of the employer.<br>
                The will be complete in the following premises:<br>
                (i) Approved Head Office at<br>
                (ii) Approved Branch Office at<br>
                (iii) Other Place (s)<br>
                <span style="font-style: italic;">NOTE: Strike which is not relevant and authentication it with Stamp and Signature</span>
            </div>
            <div style="text-align: right;">
                To above vacancy / vacancies will be selected be the representative of the Principal<br><br>
                Thanking You,<br>
                Yours Faithfully,<br><br>
                <b>DOGAR TRADING CORPORATION</b><br><br>
                <b>(GHAZANFAR MANZOOR DOGAR)</b>
            </div>
        </div>

        <div style="border: 1.5px solid #000; margin-top: 8mm; padding: 4mm;">
            <div style="text-align:center; font-weight: 700; text-decoration: underline; font-size: 10pt;">
                FOR OFFICIAL ONLY
            </div>
            <p style="font-size: 9pt; margin: 3mm 0; text-align: justify;">
                Refresh Your Request above. Your Request has considered and permitted to Advertise and / or process
                application / requirement personnel for the above listed categories, subject to the normal terms &amp;
                conditions as laid down in the officer circular no BE-14(1)/72, Dated 30-6-72. The permission has been
                granted and the following conditions. The permission is valid for 120 days. Please get it revalidated
                if required immediately of its expiry.
            </p>
            <div style="display:flex; justify-content:space-between; margin-top: 4mm; font-size: 9.5pt;">
                <div>
                    LICENCE NO 1338/SKT<br>
                    PE/L/1338 _____________<br>
                    DATED _____________<br>
                    Expired on _____________
                </div>
                <div style="text-align: right; font-weight: 700; align-self: flex-end;">
                    Protector of Emigrant
                </div>
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title="Permission Application",
        toolbar_sub=f"File {dem['file_number'] or '—'} · {dem['sponsor_name'] or '—'}",
        body_html=body,
        auto_print=auto_print,
        # This document is denser — give it a touch more vertical room.
        top=34, bot=22,
    )


# ----------------------------------------------------------------------
# COMBINED DEMAND PACKET — all 5 docs in one print job
# ----------------------------------------------------------------------
def render_demand_packet(demand, *, letter_date: Optional[date] = None,
                         include: tuple[str, ...] = (
                             "demand_letter", "demand_undertaking",
                             "visa_undertaking", "roman_undertaking",
                             "permission_request",
                         ),
                         auto_print: bool = True) -> str:
    """Concatenate any subset of the five demand documents into a single
    print job (multi-page A4)."""
    pieces: list[str] = []
    if "demand_letter" in include:
        pieces.append(_extract_sheet(render_demand_letter(demand, letter_date=letter_date, auto_print=False)))
    if "demand_undertaking" in include:
        pieces.append(_extract_sheet(render_demand_undertaking(demand, letter_date=letter_date, auto_print=False)))
    if "visa_undertaking" in include:
        pieces.append(_extract_sheet(render_demand_visa_undertaking(demand, auto_print=False)))
    if "roman_undertaking" in include:
        pieces.append(_extract_sheet(render_demand_roman_undertaking(demand, auto_print=False)))
    if "permission_request" in include:
        pieces.append(_extract_sheet(render_demand_permission_request(demand, letter_date=letter_date, auto_print=False)))

    dem = demand_row(demand)
    body = "\n".join(pieces)
    return _wrap_document(
        title=f"Demand Packet · File {dem['file_number'] or '—'}",
        toolbar_sub=f"{len(pieces)} page(s) · {dem['sponsor_name'] or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# DOCUMENT — NOC Verification by Agency (per Turn 5 user reference image)
# ======================================================================
def render_noc_verification(cand, *, demand=None, letter_date: Optional[date] = None,
                            auto_print: bool = True, tenant=None) -> str:
    """Render the NOC Verification letter (REQUEST FOR ISSUE VISA) to the
    Royal Saudi Consulate, with the candidate's NAME / FATHER / PP# / VISA#
    table — printed on the **company letterhead** (overlay pattern).

    User requested in Turn 5: this letter should sit on the letterhead
    background (logo/license/address) with body content overlaid on top.
    Matches the reference Word document shown in the screenshot.
    """
    row = candidate_row(cand)
    dem = demand_row(demand) if demand is not None else demand_row(None)
    letter_date = letter_date or date.today()
    # Embassy short label drives the salutation
    embassy = (dem.get("embassy") or getattr(cand, "embassy", "") or "Islamabad").strip()
    embassy_low = embassy.lower()
    if "karachi" in embassy_low:
        embassy_city = "Karachi"
    elif "lahore" in embassy_low:
        embassy_city = "Lahore"
    else:
        embassy_city = "Islamabad"

    visa_no = (dem.get("visa_number") or "").strip() or "________________"
    lh_url = letterhead_url_for(tenant)

    body = f"""
<section class="sheet">
    <img class="bg" src="{lh_url}" alt="">
    <div class="content letter" style="font-family: 'Times New Roman', Times, serif; font-size: 12pt; line-height: 1.55;">
        <div style="text-align: right; margin-bottom: 18px;">
            Dated: <u>&nbsp;{_h(letter_date.strftime('%d-%m-%Y'))}&nbsp;</u>
        </div>

        <div style="margin-bottom: 18px;">
            His Excellency,<br>
            The Royal Saudi Consulate<br>
            {_h(embassy_city)}
        </div>

        <h2 style="text-align:center; text-decoration: underline; font-size: 13pt; font-weight: 700; margin: 16px 0 18px 0;">
            SUBJECT: REQUEST FOR ISSUE VISA
        </h2>

        <p>You're Excellency,</p>

        <p style="text-align: justify;">
            With due respect and honor that, I Proprietor of <b><u>DOGAR TRADING CORPORATION</u></b>
            submitting the passport in Royal Saudi Consulate for Stamping of Visa the following Candidate
            mention below:
        </p>

        <table class="data" style="margin: 14px auto; border-collapse: collapse; width: 95%;">
            <thead>
                <tr>
                    <th style="border: 1px solid #000; padding: 6px 10px; font-weight: 700; text-align:center;">NAME</th>
                    <th style="border: 1px solid #000; padding: 6px 10px; font-weight: 700; text-align:center;">FATHER NAME</th>
                    <th style="border: 1px solid #000; padding: 6px 10px; font-weight: 700; text-align:center;">PP #</th>
                    <th style="border: 1px solid #000; padding: 6px 10px; font-weight: 700; text-align:center;">VISA #</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="border: 1px solid #000; padding: 6px 10px; text-align:center;">{_h((row['full_name'] or '—').upper())}</td>
                    <td style="border: 1px solid #000; padding: 6px 10px; text-align:center;">{_h((row['father_name'] or '—').upper())}</td>
                    <td style="border: 1px solid #000; padding: 6px 10px; text-align:center;">{_h(row['passport_no'] or '—')}</td>
                    <td style="border: 1px solid #000; padding: 6px 10px; text-align:center;">{_h(visa_no)}</td>
                </tr>
            </tbody>
        </table>

        <p style="text-align: justify; margin-top: 14px;">
            In case of any Mishap with the above said person in <b>SAUDI ARABIA</b> regarding this visa,
            I shall take responsibility this person. I request your Excellency to please grant permission to issue
            visa. There is no fake document are attached with this.
        </p>

        <table style="width:100%; margin-top: 22px; border-collapse: collapse;">
            <tr>
                <td style="border: 1.2px solid #000; padding: 10px 14px; width: 50%; vertical-align: top;">
                    <div style="text-align:center; font-weight: 700; text-decoration: underline; margin-bottom: 6px;">
                        Acknowledgment form candidate
                    </div>
                    <div style="text-align:center; font-size: 11.5pt;">
                        I, absolutely agreed and known about<br>
                        agreement from Company/Kafeel upon that.
                    </div>
                </td>
                <td style="border: 1.2px solid #000; padding: 10px 14px; width: 50%; vertical-align: top;">
                    <div style="text-align:center; font-size: 11.5pt;">
                        If there is any mistake in E-Number<br>
                        system, kindly issue visa as per Profession<br>
                        mentioned on Visa Form. He did not have<br>
                        any Saudi Arabia valid visa
                    </div>
                </td>
            </tr>
        </table>

        <p style="margin-top: 22px;">
            Your cooperation in this regard will be highly appreciated &amp; thank you in anticipation.
        </p>

        <div style="margin-top: 36px; display:flex; justify-content:space-between; align-items:flex-end;">
            <div>
                <div style="font-weight: 700; text-decoration: underline;">CANDIDATE SIGN</div>
            </div>
            <div style="text-align:center; font-weight: 700;">
                <div style="text-decoration: underline;">DOGARTRADING<br>CORPORATION</div>
                <div style="font-weight: 500; margin-top: 4px;">License # 1338/SKT</div>
            </div>
        </div>
    </div>
</section>"""
    return _wrap_document(
        title=f"NOC Verification · {row['full_name'] or '—'}",
        toolbar_sub=f"To: Royal Saudi Consulate {embassy_city} · {row['passport_no'] or 'No PP'}",
        body_html=body,
        auto_print=auto_print,
        top=46, bot=30, left=22, right=22,
    )


# ======================================================================
# Utils
# ======================================================================
def _h(v) -> str:
    """HTML-escape (safe to interpolate user content)."""
    if v is None:
        return ""
    s = str(v)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


_NUM_WORDS = ["zero", "one", "two", "three", "four", "five",
              "six", "seven", "eight", "nine", "ten",
              "eleven", "twelve", "thirteen", "fourteen", "fifteen"]


def _num_word(n: int) -> str:
    if 0 <= n < len(_NUM_WORDS):
        return _NUM_WORDS[n]
    return str(n)
