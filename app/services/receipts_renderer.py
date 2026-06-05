"""Receipts Renderer
====================

Official, ready-to-print HTML receipts for the portal:

    1. **Demand File Receipt** — issued when a new Demand File is opened
       with the client.  Carries the company logo (top-left), an angled
       watermark across the body, the file/sponsor/visa info, signature
       block, and a verification QR code that links to the file URL.

    2. **Payment Receipt** — issued for every ClientStatement row that is
       a PAYMENT.  Same official chrome (logo + watermark), shows the
       amount in figures and words, payment method, receipt number, and
       a QR code that links back to the payment record.

Both receipts:

    * Render on plain white A4 (NOT the dogar_letterhead.jpg — these are
      purpose-built receipts with their own header/footer; the letterhead
      JPG is used for permission documents only).
    * Are emitted as a single self-contained HTML document with embedded
      base64 PNGs (logo, watermark, QR code) so the print preview is
      one-shot with no extra HTTP requests.
    * Include a print toolbar at the top of the on-screen view that is
      hidden in @media print.

The renderer is deliberately framework-free (just stdlib + Pillow +
qrcode + ReportLab for barcode); callers pass plain dicts so we don't
import SQLAlchemy models here.
"""
from __future__ import annotations

import base64
import io
import os
from datetime import date, datetime
from typing import Any, Dict, Optional

try:
    import qrcode  # type: ignore
    from qrcode.constants import ERROR_CORRECT_M  # type: ignore
except Exception:  # pragma: no cover — defensive
    qrcode = None  # type: ignore
    ERROR_CORRECT_M = 0  # type: ignore

# ----------------------------------------------------------------------
# Asset paths — disk paths so we can read them and embed as base64.
# ----------------------------------------------------------------------
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app
LOGO_DISK_PATH = os.path.join(_HERE, "static", "img", "dogar_logo.png")
LOGO_FALLBACK_PATH = os.path.join(_HERE, "static", "img", "logo.png")


# ======================================================================
# UTILITIES
# ======================================================================
def _h(s: Any) -> str:
    """HTML-escape helper."""
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


def _fmt_date(v: Any) -> str:
    if not v:
        return "—"
    if isinstance(v, (datetime, date)):
        return v.strftime("%d %B %Y")
    return str(v)


def _logo_data_url() -> str:
    """Read the company logo from disk and return a base64 data URL.

    Falls back to ``logo.png`` if ``dogar_logo.png`` is missing.
    Returns an empty string if neither asset exists so callers can omit
    the <img> tag gracefully.
    """
    for path in (LOGO_DISK_PATH, LOGO_FALLBACK_PATH):
        try:
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    raw = fh.read()
                return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except (OSError, IOError) as exc:
            import logging
            logging.getLogger("dtc.receipts").debug(
                "Could not read logo candidate %r: %s", path, exc
            )
            continue
    return ""


def _qr_data_url(payload: str, box_size: int = 6) -> str:
    """Generate a QR code PNG for ``payload`` and return a base64 data URL.

    Returns an empty string when ``qrcode`` is unavailable so callers can
    skip rendering the QR block.
    """
    if not payload or qrcode is None:
        return ""
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=box_size,
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # pragma: no cover
        return ""


# ----------------------------------------------------------------------
# AMOUNT → WORDS — converts an int amount into English words.  Handles
# values up to 99,99,99,999 (Pakistani/Indian numbering).
# ----------------------------------------------------------------------
_UNITS = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
          "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
          "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_thousand(n: int) -> str:
    if n == 0:
        return ""
    if n < 20:
        return _UNITS[n]
    if n < 100:
        return _TENS[n // 10] + (("-" + _UNITS[n % 10]) if (n % 10) else "")
    return _UNITS[n // 100] + " Hundred" + ((" " + _below_thousand(n % 100)) if (n % 100) else "")


def amount_in_words(amount: float, currency: str = "PKR") -> str:
    """Convert a numeric amount to English words (Pakistani numbering).

    e.g. amount_in_words(125000, "PKR") -> "Rupees One Lakh Twenty-Five Thousand Only"
    """
    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        amount = 0.0
    rupees = int(amount)
    paisa = int(round((amount - rupees) * 100))

    if rupees == 0:
        words = "Zero"
    else:
        parts: list[str] = []
        crore = rupees // 10000000
        rupees = rupees % 10000000
        lakh = rupees // 100000
        rupees = rupees % 100000
        thousand = rupees // 1000
        rupees = rupees % 1000
        if crore:
            parts.append(_below_thousand(crore) + " Crore")
        if lakh:
            parts.append(_below_thousand(lakh) + " Lakh")
        if thousand:
            parts.append(_below_thousand(thousand) + " Thousand")
        if rupees:
            parts.append(_below_thousand(rupees))
        words = " ".join(parts).strip()

    label = {"PKR": "Rupees", "USD": "Dollars", "SAR": "Riyals",
             "AED": "Dirhams", "EUR": "Euros", "GBP": "Pounds"}.get(currency.upper(), currency)

    out = f"{label} {words}"
    if paisa:
        out += f" and {_below_thousand(paisa)} Paisa"
    out += " Only"
    return out


def _format_amount(amount: float, currency: str = "PKR") -> str:
    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        amount = 0.0
    # Thousands separators with Pakistani lakhs-style grouping is overkill;
    # standard "," grouping is fine for the printout.
    return f"{currency} {amount:,.2f}"


# ======================================================================
# SHARED CSS — official receipt chrome (logo, watermark, QR, footer)
# ======================================================================
_RECEIPT_STYLES = """
*, *::before, *::after { box-sizing: border-box; }
html, body { margin:0; padding:0; }
body {
    font-family: "Times New Roman", "Liberation Serif", Times, serif;
    color: #1f2937;
    background: #e2e8f0;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}

@page { size: A4 portrait; margin: 0; }

/* On-screen toolbar (hidden during print) */
.toolbar {
    position: sticky; top: 0; z-index: 100;
    background: #1e3a8a; color: white;
    padding: 10px 18px;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 2px 6px rgba(0,0,0,.3);
}
.toolbar h1 { font: 600 14px ui-sans-serif, system-ui, sans-serif; margin: 0; }
.toolbar .sub { font: 400 11px ui-sans-serif, system-ui, sans-serif; color: #cbd5e1; margin-top: 2px; }
.toolbar button {
    font: 600 13px ui-sans-serif, system-ui, sans-serif;
    background: #2563eb; color: white; border: 0;
    padding: 7px 16px; border-radius: 4px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
}
.toolbar button:hover { background: #1d4ed8; }
.toolbar button.secondary { background: #475569; }

/* The A4 sheet itself */
.sheet {
    position: relative;
    width: 210mm; min-height: 297mm;
    margin: 18px auto;
    background: #fff;
    box-shadow: 0 4px 18px rgba(0,0,0,.35);
    padding: 18mm 18mm 22mm 18mm;
    overflow: hidden;
}

/* Angled watermark — pure CSS, no images. The text comes from
   --watermark in the per-document style block. */
.sheet::before {
    content: attr(data-watermark);
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%) rotate(-32deg);
    font-size: 90pt;
    font-weight: 900;
    color: rgba(15, 23, 42, 0.06);
    letter-spacing: 4px;
    white-space: nowrap;
    pointer-events: none;
    user-select: none;
    z-index: 0;
}

/* All content sits above the watermark */
.sheet > * { position: relative; z-index: 1; }

/* Document header — logo + company block + receipt type stamp */
.doc-head {
    display: flex; align-items: flex-start; justify-content: space-between;
    border-bottom: 3px double #1e3a8a;
    padding-bottom: 10mm; margin-bottom: 8mm;
}
.doc-head .left { display: flex; align-items: center; gap: 12px; }
.doc-head .logo img { height: 28mm; width: auto; object-fit: contain; }
.doc-head .company .nm {
    font: 700 18pt "Times New Roman", serif; color: #1e3a8a;
    letter-spacing: .3px; line-height: 1.1;
}
.doc-head .company .tag {
    font: 400 10pt "Times New Roman", serif; color: #475569;
    font-style: italic; margin-top: 3px;
}
.doc-head .company .meta {
    font: 400 9pt ui-sans-serif, system-ui, sans-serif; color: #475569;
    margin-top: 4px; line-height: 1.4;
}
.doc-head .stamp {
    border: 3px solid #16a34a; color: #16a34a;
    font: 800 16pt "Times New Roman", serif;
    padding: 8px 14px; border-radius: 6px;
    transform: rotate(-4deg);
    letter-spacing: 3px;
    align-self: flex-start;
    margin-top: 4mm;
}
.doc-head .stamp.alt { border-color: #ea580c; color: #ea580c; }
.doc-head .stamp.muted { border-color: #475569; color: #475569; }

.doc-title-row {
    display: flex; align-items: baseline; justify-content: space-between;
    margin: 0 0 6mm 0;
}
.doc-title-row h2 {
    font: 700 16pt "Times New Roman", serif; color: #0f172a;
    margin: 0; text-transform: uppercase; letter-spacing: 1px;
}
.doc-title-row .receipt-no {
    font: 700 11pt ui-monospace, "Courier New", monospace;
    color: #1e3a8a; background: #eff6ff;
    padding: 4px 10px; border-radius: 4px;
    border: 1px solid #bfdbfe;
}

/* Key/value table */
table.kv {
    width: 100%; border-collapse: collapse;
    margin: 4mm 0 6mm; font-size: 11pt;
}
table.kv td {
    padding: 5px 8px;
    border-bottom: 1px solid #e2e8f0;
    vertical-align: top;
}
table.kv td:first-child {
    width: 34%; color: #475569; font-weight: 600;
}

/* Amount highlight box */
.amt-box {
    background: #f1f5f9; border: 1.5px solid #cbd5e1;
    border-radius: 8px; padding: 5mm 6mm;
    margin: 5mm 0; display: flex;
    align-items: center; justify-content: space-between;
}
.amt-box .label {
    color: #475569; font: 700 10pt ui-sans-serif, system-ui, sans-serif;
    letter-spacing: 1px;
}
.amt-box .amt {
    font: 800 22pt "Times New Roman", serif; color: #1e3a8a;
}
.amt-words {
    font: italic 11pt "Times New Roman", serif;
    color: #475569; margin-top: 2mm; padding-left: 4mm;
    border-left: 3px solid #1e3a8a;
}

/* Particulars grid */
.particulars {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 4mm; margin: 4mm 0 6mm;
}
.particulars .pair {
    border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 3mm 4mm; background: #fafafa;
}
.particulars .pair .lbl {
    font: 700 9pt ui-sans-serif, system-ui, sans-serif;
    color: #64748b; text-transform: uppercase; letter-spacing: 1px;
}
.particulars .pair .val {
    font: 600 11pt "Times New Roman", serif;
    color: #1f2937; margin-top: 1.5mm; line-height: 1.3;
}

/* Trade table */
table.trades {
    width: 100%; border-collapse: collapse;
    font-size: 10.5pt; margin: 4mm 0 6mm;
}
table.trades th, table.trades td {
    border: 1px solid #1f2937; padding: 5px 8px;
    vertical-align: middle;
}
table.trades th {
    background: #1e3a8a; color: white; text-align: center;
    font-weight: 700; letter-spacing: .5px; font-size: 10pt;
}
table.trades td.c { text-align: center; }

/* Verification footer — QR code + signature blocks */
.verify-row {
    display: grid; grid-template-columns: 32mm 1fr 50mm;
    gap: 6mm; align-items: flex-start;
    margin-top: 8mm; padding-top: 5mm;
    border-top: 1px solid #cbd5e1;
}
.verify-row .qr {
    width: 32mm; text-align: center;
}
.verify-row .qr img {
    width: 28mm; height: 28mm; display: block; margin: 0 auto;
    border: 1px solid #e2e8f0; padding: 1.5mm; background: white;
}
.verify-row .qr .caption {
    font: 700 7.5pt ui-sans-serif, system-ui, sans-serif;
    color: #64748b; margin-top: 1mm; line-height: 1.2;
}
.verify-row .terms {
    font: 9.5pt "Times New Roman", serif; color: #475569;
    text-align: justify; line-height: 1.3;
}
.verify-row .terms strong { color: #0f172a; }
.verify-row .signature {
    text-align: center; padding-top: 14mm;
}
.verify-row .signature .line {
    border-top: 1.5px solid #0f172a; padding-top: 1.5mm;
}
.verify-row .signature .lbl {
    font: 700 9.5pt "Times New Roman", serif; color: #1f2937;
}
.verify-row .signature .sub {
    font: 8.5pt "Times New Roman", serif; color: #64748b; margin-top: .5mm;
}

/* Page footer strip (tagline / contact) */
.page-footer {
    position: absolute; left: 18mm; right: 18mm; bottom: 8mm;
    padding-top: 3mm; border-top: 1px solid #cbd5e1;
    display: flex; justify-content: space-between; align-items: center;
    font: 8.5pt ui-sans-serif, system-ui, sans-serif; color: #64748b;
}
.page-footer .gen {
    font-style: italic;
}

@media print {
    html, body { background: white !important; }
    .toolbar { display: none !important; }
    .sheet { margin: 0 !important; box-shadow: none !important; }
}
"""

# Auto-print + image-loaded wait helper (same pattern as letterhead_renderer)
_PRINT_SCRIPT = """
(function(){
    let printed = false;
    function go(){
        if (printed) return;
        printed = true;
        setTimeout(() => { window.focus(); window.print(); }, 300);
    }
    const imgs = [...document.images];
    if (!imgs.length) { go(); return; }
    let loaded = 0;
    imgs.forEach(im => {
        if (im.complete) { if (++loaded === imgs.length) go(); return; }
        im.addEventListener('load',  () => { if (++loaded === imgs.length) go(); });
        im.addEventListener('error', () => { if (++loaded === imgs.length) go(); });
    });
    // Safety timeout — fire print even if some image never reports load
    setTimeout(go, 4000);
})();
"""


def _wrap(title: str, toolbar_sub: str, body_html: str,
          auto_print: bool = True) -> str:
    """Wrap a per-document body in the standalone HTML shell."""
    script = f"<script>{_PRINT_SCRIPT}</script>" if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_h(title)}</title>
<style>{_RECEIPT_STYLES}</style>
</head>
<body>
<div class="toolbar">
    <div>
        <h1>{_h(title)}</h1>
        <div class="sub">{_h(toolbar_sub)}</div>
    </div>
    <div>
        <button onclick="window.print()" type="button">🖨 Print</button>
        <button class="secondary" onclick="window.close()" type="button">Close</button>
    </div>
</div>
{body_html}
{script}
</body>
</html>"""


# ======================================================================
# COMPANY HEADER  — shared between Demand Receipt and Payment Receipt
# ======================================================================
def _company_head_html(company: Dict[str, Any], stamp_text: str,
                       stamp_kind: str = "ok") -> str:
    """Render the shared logo + company block + status stamp.

    ``stamp_kind`` is one of 'ok' (green), 'alt' (orange), 'muted' (grey).
    """
    logo = _logo_data_url()
    logo_html = (f'<div class="logo"><img src="{logo}" alt="logo"></div>'
                 if logo else "")
    nm = _h(company.get("name") or "Dogar Trading Corporation")
    tag = _h(company.get("tagline") or "Overseas Employment Promoters")
    addr = _h(company.get("address") or "")
    phone = _h(company.get("phone") or "")
    email = _h(company.get("email") or "")
    license_no = _h(company.get("license_no") or "")

    meta_lines: list[str] = []
    if addr:
        meta_lines.append(addr)
    contact_line: list[str] = []
    if phone:
        contact_line.append(f"Tel: {phone}")
    if email:
        contact_line.append(email)
    if contact_line:
        meta_lines.append(" · ".join(contact_line))
    if license_no:
        meta_lines.append(f"OEP License No: <strong>{license_no}</strong>")
    meta_html = "<br>".join(meta_lines) if meta_lines else ""

    stamp_class = {"ok": "stamp", "alt": "stamp alt", "muted": "stamp muted"}.get(
        stamp_kind, "stamp")

    return f"""
<div class="doc-head">
    <div class="left">
        {logo_html}
        <div class="company">
            <div class="nm">{nm}</div>
            <div class="tag">{tag}</div>
            <div class="meta">{meta_html}</div>
        </div>
    </div>
    <div class="{stamp_class}">{_h(stamp_text)}</div>
</div>"""


def _verify_row_html(qr_payload: str, qr_caption: str,
                     terms: str, signer: str, signer_role: str) -> str:
    """Render the verification row at the bottom: QR + terms + signature."""
    qr_img = _qr_data_url(qr_payload)
    qr_html = (f'<img src="{qr_img}" alt="QR">'
               if qr_img else
               '<div style="width:28mm;height:28mm;border:1px dashed #94a3b8;display:flex;'
               'align-items:center;justify-content:center;font-size:8pt;color:#94a3b8;'
               'background:white;">QR<br>n/a</div>')
    return f"""
<div class="verify-row">
    <div class="qr">
        {qr_html}
        <div class="caption">{_h(qr_caption)}</div>
    </div>
    <div class="terms">{terms}</div>
    <div class="signature">
        <div class="line"></div>
        <div class="lbl">{_h(signer)}</div>
        <div class="sub">{_h(signer_role)}</div>
    </div>
</div>"""


def _page_footer_html(company_name: str = "Dogar Trading Corporation") -> str:
    now = datetime.now().strftime("%d %b %Y · %H:%M")
    return f"""
<div class="page-footer">
    <div>© {datetime.now().year} {_h(company_name)} · All rights reserved</div>
    <div class="gen">Generated on {now} · Computer-issued receipt</div>
</div>"""


# ======================================================================
# DEMAND FILE RECEIPT — issued when a demand file is opened
# ======================================================================
def render_demand_file_receipt(demand: Dict[str, Any],
                               client: Optional[Dict[str, Any]] = None,
                               company: Optional[Dict[str, Any]] = None,
                               trades: Optional[list[Dict[str, Any]]] = None,
                               *,
                               verify_url: Optional[str] = None,
                               auto_print: bool = True) -> str:
    """Render an official receipt for a Demand File.

    ``demand`` must include: ``id``, ``file_number``, ``receiving_date``,
    ``reference``, ``permission_no``, ``permission_date``, ``sponsor_name``,
    ``sponsor_address``, ``country``, ``embassy``, ``visa_number``,
    ``status``.

    ``trades`` is an optional list of ``{trade, quantity, salary,
    salary_currency, contract_years}`` dicts.

    ``verify_url`` defaults to ``/demands/{id}`` so scanning the QR opens
    the demand detail page in the portal.
    """
    company = company or {}
    client = client or {}
    trades = trades or []

    file_no = demand.get("file_number") or "—"
    sponsor = demand.get("sponsor_name") or "—"
    status = (demand.get("status") or "active").upper()
    stamp_kind = {"ACTIVE": "ok", "PROCESSING": "alt",
                  "FILLED": "muted", "EXPIRED": "muted",
                  "CANCELLED": "alt"}.get(status, "ok")
    stamp_text = status

    head = _company_head_html(company, stamp_text, stamp_kind)

    # Total trade slots / first-row salary as headline figure
    total_qty = sum(int(t.get("quantity") or 0) for t in trades)
    headline_amount = ""
    headline_currency = ""
    if trades:
        headline_amount = trades[0].get("salary") or 0
        headline_currency = trades[0].get("salary_currency") or "SAR"

    trades_html = ""
    if trades:
        rows = "".join(f"""
            <tr>
                <td class="c">{i + 1}</td>
                <td>{_h(t.get('trade'))}</td>
                <td class="c">{_h(t.get('quantity') or 0)}</td>
                <td class="c">{_h(t.get('salary_currency') or 'SAR')} {float(t.get('salary') or 0):,.0f}</td>
                <td class="c">{_h(t.get('contract_years') or 2)} years</td>
            </tr>""" for i, t in enumerate(trades))
        trades_html = f"""
        <h3 style="font:700 12pt 'Times New Roman',serif;color:#1e3a8a;margin:4mm 0 2mm;
                   border-left:4px solid #1e3a8a;padding-left:3mm;">
            Recruitment Categories
        </h3>
        <table class="trades">
            <thead>
                <tr>
                    <th style="width:8%">S.NO</th>
                    <th>Trade / Profession</th>
                    <th style="width:12%">Vacancies</th>
                    <th style="width:22%">Monthly Salary</th>
                    <th style="width:14%">Contract</th>
                </tr>
            </thead>
            <tbody>{rows}
                <tr style="background:#f1f5f9; font-weight:700;">
                    <td class="c" colspan="2">TOTAL VACANCIES</td>
                    <td class="c">{total_qty}</td>
                    <td colspan="2"></td>
                </tr>
            </tbody>
        </table>"""

    receiving_date = _fmt_date(demand.get("receiving_date") or demand.get("created_at"))
    perm_no = _h(demand.get("permission_no") or "—")
    perm_date = _fmt_date(demand.get("permission_date"))

    # Verification URL/QR payload — falls back to a printable signature
    if verify_url is None:
        verify_url = f"https://www.dogars.com/demands/{demand.get('id', '')}"
    qr_caption = f"Verify File<br>{_h(file_no)}"

    body = f"""
<div class="sheet" data-watermark="DOGAR TRADING">
    {head}

    <div class="doc-title-row">
        <h2>Demand File Receipt</h2>
        <div class="receipt-no">FILE: {_h(file_no)}</div>
    </div>

    <table class="kv">
        <tr>
            <td>File Number</td>
            <td><strong style="font-size:12pt;color:#1e3a8a;">{_h(file_no)}</strong></td>
            <td>Receiving Date</td>
            <td>{receiving_date}</td>
        </tr>
        <tr>
            <td>Reference</td>
            <td>{_h(demand.get('reference') or '—')}</td>
            <td>File Status</td>
            <td><strong>{_h(status)}</strong></td>
        </tr>
        <tr>
            <td>Permission No.</td>
            <td>{perm_no}</td>
            <td>Permission Date</td>
            <td>{perm_date}</td>
        </tr>
    </table>

    <h3 style="font:700 12pt 'Times New Roman',serif;color:#1e3a8a;margin:4mm 0 2mm;
               border-left:4px solid #1e3a8a;padding-left:3mm;">
        Sponsor / Employer Information
    </h3>
    <div class="particulars">
        <div class="pair">
            <div class="lbl">Sponsor / Employer</div>
            <div class="val">{_h(sponsor)}</div>
        </div>
        <div class="pair">
            <div class="lbl">Country / Embassy</div>
            <div class="val">{_h(demand.get('country') or '—')} · {_h(demand.get('embassy') or '—')}</div>
        </div>
        <div class="pair" style="grid-column: span 2;">
            <div class="lbl">Sponsor Address</div>
            <div class="val">{_h(demand.get('sponsor_address') or '—')}</div>
        </div>
        <div class="pair">
            <div class="lbl">Visa Number</div>
            <div class="val">{_h(demand.get('visa_number') or '—')}</div>
        </div>
        <div class="pair">
            <div class="lbl">Client / Agent</div>
            <div class="val">{_h(client.get('company_name') or '—')}</div>
        </div>
    </div>

    {trades_html}

    {_verify_row_html(
        qr_payload=verify_url,
        qr_caption=qr_caption,
        terms=(
            "This receipt acknowledges the opening of the above-mentioned Demand File "
            "on behalf of the named sponsor/employer.  All recruitment under this file "
            "shall be carried out in accordance with the Emigration Ordinance 1979 and "
            "the rules made thereunder.  The file is valid for the period of the "
            "permission granted.  <strong>This is a computer-generated receipt and "
            "does not require a manual signature.</strong>"
        ),
        signer=company.get("authorised_signatory") or "Ghazanfar Manzoor Dogar",
        signer_role="Authorised Signatory / Proprietor",
    )}

    {_page_footer_html(company.get('name') or 'Dogar Trading Corporation')}
</div>"""

    return _wrap(
        title="Demand File Receipt",
        toolbar_sub=f"File {file_no} · {sponsor}",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# PAYMENT RECEIPT — issued for every PAYMENT row
# ======================================================================
def render_payment_receipt(payment: Dict[str, Any],
                           demand: Optional[Dict[str, Any]] = None,
                           client: Optional[Dict[str, Any]] = None,
                           company: Optional[Dict[str, Any]] = None,
                           *,
                           verify_url: Optional[str] = None,
                           auto_print: bool = True) -> str:
    """Render an official receipt for a payment / invoice row.

    ``payment`` must include: ``id``, ``receipt_no``, ``entry_date``,
    ``entry_type`` ('PAYMENT' or 'INVOICE'), ``debit``, ``credit``,
    ``payment_method``, ``reference``, ``description``, ``received_by``.
    """
    company = company or {}
    client = client or {}
    demand = demand or {}

    is_payment = (payment.get("entry_type") or "").upper() == "PAYMENT"
    amount = float(payment.get("credit") or 0) + float(payment.get("debit") or 0)
    currency = payment.get("currency") or "PKR"

    title = "Official Payment Receipt" if is_payment else "Tax Invoice"
    stamp = "PAID" if is_payment else "INVOICE"
    stamp_kind = "ok" if is_payment else "alt"
    watermark = "PAID" if is_payment else "INVOICE"

    head = _company_head_html(company, stamp, stamp_kind)
    rcpt_no = _h(payment.get("receipt_no") or "—")
    entry_date = _fmt_date(payment.get("entry_date"))

    if verify_url is None:
        verify_url = (
            f"https://www.dogars.com/demands/{demand.get('id', '')}"
            f"/payments/{payment.get('id', '')}"
        )
    qr_caption = f"Verify Receipt<br>{rcpt_no}"

    body = f"""
<div class="sheet" data-watermark="{_h(watermark)}">
    {head}

    <div class="doc-title-row">
        <h2>{_h(title)}</h2>
        <div class="receipt-no">№ {rcpt_no}</div>
    </div>

    <table class="kv">
        <tr>
            <td>Date</td>
            <td><strong>{entry_date}</strong></td>
            <td>Payment Method</td>
            <td><strong>{_h(payment.get('payment_method') or '—')}</strong></td>
        </tr>
        <tr>
            <td>Client / Company</td>
            <td>{_h(client.get('company_name') or '—')}</td>
            <td>Demand File No.</td>
            <td><strong>{_h(demand.get('file_number') or '—')}</strong></td>
        </tr>
        <tr>
            <td>Reference</td>
            <td>{_h(payment.get('reference') or '—')}</td>
            <td>Received By</td>
            <td>{_h(payment.get('received_by') or '—')}</td>
        </tr>
        <tr>
            <td>Description</td>
            <td colspan="3">{_h(payment.get('description') or '—')}</td>
        </tr>
    </table>

    <div class="amt-box">
        <div class="label">{'AMOUNT RECEIVED' if is_payment else 'AMOUNT INVOICED'}</div>
        <div class="amt">{_format_amount(amount, currency)}</div>
    </div>
    <div class="amt-words"><strong>In words:</strong> {_h(amount_in_words(amount, currency))}</div>

    {_verify_row_html(
        qr_payload=verify_url,
        qr_caption=qr_caption,
        terms=(
            "Received the above amount with thanks against the services described above.  "
            "This receipt is valid only after the funds have cleared in our bank account "
            "(in case of cheque / bank transfer).  Any disputes must be raised within 7 "
            "days of issue.  <strong>This is a computer-generated receipt — scan the QR "
            "to verify authenticity.</strong>"
            if is_payment else
            "This invoice is payable within 7 days of issue.  Late payments may attract "
            "a service surcharge as per company policy.  Cheques must be drawn in favour "
            "of <strong>" + _h(company.get('name') or 'Dogar Trading Corporation') +
            "</strong>.  Scan the QR code to verify this invoice."
        ),
        signer=payment.get("received_by") or company.get("authorised_signatory") or "Ghazanfar Manzoor Dogar",
        signer_role="Authorised Signatory" if is_payment else "Invoice Issued By",
    )}

    {_page_footer_html(company.get('name') or 'Dogar Trading Corporation')}
</div>"""

    return _wrap(
        title=title,
        toolbar_sub=f"Receipt {rcpt_no} · {client.get('company_name') or '—'}",
        body_html=body,
        auto_print=auto_print,
    )


# ======================================================================
# CLIENT STATEMENT — v7 (account statement / ledger printout)
# ======================================================================
def render_client_statement(client: Dict[str, Any],
                            entries: Optional[list[Dict[str, Any]]] = None,
                            summary: Optional[Dict[str, Any]] = None,
                            demands: Optional[list[Dict[str, Any]]] = None,
                            company: Optional[Dict[str, Any]] = None,
                            *,
                            verify_url: Optional[str] = None,
                            auto_print: bool = True) -> str:
    """Render an official client account statement.

    Built to match the on-screen Client Profile (summary cards +
    Account Statement table + Demand Files list) but rendered as a
    clean, printable A4 document — replaces the legacy
    `window.print()`-the-UI flow shown in the user's screenshots.

    ``client`` keys: company_name, type, owner, contact_name, phone,
        mobile, email, fax, address, city, country, status, opening_balance.
    ``summary`` keys: txn_count, total_debit, total_credit, balance.
    ``entries`` is the list of ClientStatement rows (dicts with
        entry_date, description, reference, debit, credit, balance,
        entry_type, receipt_no).
    ``demands`` is an optional list of {file_number, country, embassy,
        status, candidates_count, sponsor_name}.
    """
    client = client or {}
    company = company or {}
    summary = summary or {}
    entries = entries or []
    demands = demands or []

    head = _company_head_html(company, "STATEMENT", "muted")
    name = _h(client.get("company_name") or "—")
    addr_parts = [client.get("address"), client.get("city"), client.get("country")]
    addr = ", ".join(p for p in addr_parts if p)
    now = datetime.now().strftime("%d %b %Y")

    if verify_url is None:
        verify_url = f"https://www.dogars.com/clients/{client.get('id', '')}"

    txn_count = int(summary.get("txn_count") or 0)
    total_debit = float(summary.get("total_debit") or 0)
    total_credit = float(summary.get("total_credit") or 0)
    balance = float(summary.get("balance") or 0)
    bal_kind = "ok" if balance >= 0 else "alt"

    # ---- summary cards ----
    summary_html = f"""
    <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; margin: 4mm 0 6mm;">
        <div style="border:0.3mm solid #e2e8f0; border-radius:1.5mm; padding:3mm; background:#f8fafc;">
            <div style="font-size:8pt; color:#64748b; text-transform:uppercase; letter-spacing:.5px;">Transactions</div>
            <div style="font-size:14pt; font-weight:700; color:#0f172a; margin-top:1mm;">{txn_count}</div>
            <div style="font-size:8pt; color:#94a3b8;">Total entries</div>
        </div>
        <div style="border:0.3mm solid #fecaca; border-radius:1.5mm; padding:3mm; background:#fef2f2;">
            <div style="font-size:8pt; color:#991b1b; text-transform:uppercase; letter-spacing:.5px;">Total Debit</div>
            <div style="font-size:14pt; font-weight:700; color:#991b1b; margin-top:1mm;">{_format_amount(total_debit)}</div>
            <div style="font-size:8pt; color:#b91c1c;">Owed by client</div>
        </div>
        <div style="border:0.3mm solid #bbf7d0; border-radius:1.5mm; padding:3mm; background:#f0fdf4;">
            <div style="font-size:8pt; color:#166534; text-transform:uppercase; letter-spacing:.5px;">Total Credit</div>
            <div style="font-size:14pt; font-weight:700; color:#166534; margin-top:1mm;">{_format_amount(total_credit)}</div>
            <div style="font-size:8pt; color:#15803d;">Received</div>
        </div>
        <div style="border:0.3mm solid #cbd5e1; border-radius:1.5mm; padding:3mm; background:{'#ecfdf5' if balance >= 0 else '#fef3c7'};">
            <div style="font-size:8pt; color:#475569; text-transform:uppercase; letter-spacing:.5px;">Net Balance</div>
            <div style="font-size:14pt; font-weight:700; color:{'#15803d' if balance >= 0 else '#b45309'}; margin-top:1mm;">{_format_amount(balance)}</div>
            <div style="font-size:8pt; color:#64748b;">{'Client owes us' if balance > 0 else ('We owe client' if balance < 0 else 'Settled')}</div>
        </div>
    </div>
    """

    # ---- statement rows ----
    if entries:
        rows_html = []
        for r in entries:
            d = _fmt_date(r.get("entry_date"))
            desc = _h(r.get("description") or r.get("entry_type") or "—")
            ref = _h(r.get("reference") or r.get("receipt_no") or "—")
            deb = float(r.get("debit") or 0)
            crd = float(r.get("credit") or 0)
            bal = float(r.get("balance") or 0)
            rows_html.append(f"""
            <tr>
                <td style="white-space:nowrap;">{d}</td>
                <td>{desc}</td>
                <td style="font-family:ui-monospace,monospace; font-size:8.5pt;">{ref}</td>
                <td style="text-align:right; color:{'#991b1b' if deb else '#94a3b8'}; font-weight:{'600' if deb else '400'};">{_format_amount(deb) if deb else '—'}</td>
                <td style="text-align:right; color:{'#166534' if crd else '#94a3b8'}; font-weight:{'600' if crd else '400'};">{_format_amount(crd) if crd else '—'}</td>
                <td style="text-align:right; font-weight:600; color:#0f172a;">{_format_amount(bal)}</td>
            </tr>""")
        entries_html = f"""
        <div style="margin-top: 4mm;">
            <h3 style="font-size:11pt; font-weight:700; color:#0f172a; margin: 0 0 2mm 0; padding-bottom: 1.5mm; border-bottom: 0.3mm solid #cbd5e1;">📋 Account Statement</h3>
            <table style="width:100%; border-collapse:collapse; font-size:9pt;">
                <thead>
                    <tr style="background:#f1f5f9; color:#475569; font-weight:700; font-size:8pt; text-transform:uppercase; letter-spacing:.4px;">
                        <th style="text-align:left; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Date</th>
                        <th style="text-align:left; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Description</th>
                        <th style="text-align:left; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Reference</th>
                        <th style="text-align:right; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Debit</th>
                        <th style="text-align:right; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Credit</th>
                        <th style="text-align:right; padding:2mm 2.5mm; border-bottom: 0.3mm solid #cbd5e1;">Balance</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(f'<tr style="border-bottom: 0.2mm solid #f1f5f9;">{r[4:]}' if r.startswith(chr(10)+chr(10)) else r for r in rows_html)}
                </tbody>
            </table>
        </div>
        """
    else:
        entries_html = """
        <div style="margin-top: 4mm; padding: 8mm; text-align:center; background:#f8fafc; border:0.3mm dashed #cbd5e1; border-radius:2mm; color:#94a3b8; font-size:10pt;">
            No transactions recorded for this client yet.
        </div>
        """

    # ---- demand files block ----
    demands_html = ""
    if demands:
        rows = []
        for d in demands:
            rows.append(f"""
            <tr>
                <td style="font-family:ui-monospace,monospace; font-weight:600; color:#0f172a;">{_h(d.get('file_number') or '—')}</td>
                <td>{_h(d.get('country') or '—')}</td>
                <td>{_h(d.get('embassy') or '—')}</td>
                <td>{_h(d.get('sponsor_name') or '—')}</td>
                <td style="text-align:center;">{int(d.get('candidates_count') or 0)}</td>
                <td><span style="display:inline-block; padding:0.5mm 2mm; background:#dbeafe; color:#1e40af; border-radius:1mm; font-size:8pt; font-weight:600;">{_h(d.get('status') or '—')}</span></td>
            </tr>""")
        demands_html = f"""
        <div style="margin-top: 5mm; page-break-inside: avoid;">
            <h3 style="font-size:11pt; font-weight:700; color:#0f172a; margin: 0 0 2mm 0; padding-bottom: 1.5mm; border-bottom: 0.3mm solid #cbd5e1;">📁 Demand Files</h3>
            <table style="width:100%; border-collapse:collapse; font-size:9pt;">
                <thead>
                    <tr style="background:#f1f5f9; color:#475569; font-weight:700; font-size:8pt; text-transform:uppercase; letter-spacing:.4px;">
                        <th style="text-align:left; padding:2mm 2.5mm;">File No.</th>
                        <th style="text-align:left; padding:2mm 2.5mm;">Country</th>
                        <th style="text-align:left; padding:2mm 2.5mm;">Embassy</th>
                        <th style="text-align:left; padding:2mm 2.5mm;">Sponsor</th>
                        <th style="text-align:center; padding:2mm 2.5mm;">Candidates</th>
                        <th style="text-align:left; padding:2mm 2.5mm;">Status</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """

    body = f"""
<div class="sheet" data-watermark="STATEMENT">
    {head}

    <div class="doc-title-row">
        <h2>Client Account Statement</h2>
        <div class="receipt-no">As of {now}</div>
    </div>

    <table class="kv">
        <tr>
            <td>Client / Company</td>
            <td><strong>{name}</strong></td>
            <td>Status</td>
            <td><span style="text-transform:uppercase; font-weight:600; color:{'#15803d' if (client.get('status') or '').lower() == 'active' else '#b91c1c'};">{_h(client.get('status') or 'active')}</span></td>
        </tr>
        <tr>
            <td>Owner / Contact</td>
            <td>{_h(client.get('owner') or client.get('contact_name') or '—')}</td>
            <td>Type</td>
            <td>{_h(client.get('type') or '—')}</td>
        </tr>
        <tr>
            <td>Phone / Mobile</td>
            <td>{_h(client.get('phone') or client.get('mobile') or '—')}</td>
            <td>Email</td>
            <td>{_h(client.get('email') or '—')}</td>
        </tr>
        <tr>
            <td>Address</td>
            <td colspan="3">{_h(addr or '—')}</td>
        </tr>
    </table>

    {summary_html}
    {entries_html}
    {demands_html}

    {_verify_row_html(
        qr_payload=verify_url,
        qr_caption=f"Verify Client<br>{name[:18]}",
        terms=(
            "This is a true and accurate account statement generated from our records "
            "as of the date shown above.  Any discrepancies must be reported in writing "
            "within 7 days of issue.  Balances reflect funds cleared in our books at the "
            "time of generation.  <strong>This is a computer-generated statement — scan "
            "the QR to verify authenticity.</strong>"
        ),
        signer=company.get("authorised_signatory") or "Ghazanfar Manzoor Dogar",
        signer_role="Authorised Signatory",
    )}

    {_page_footer_html(company.get('name') or 'Dogar Trading Corporation')}
</div>"""

    return _wrap(
        title=f"Statement · {name}",
        toolbar_sub=f"{txn_count} entries · Balance {_format_amount(balance)}",
        body_html=body,
        auto_print=auto_print,
    )
