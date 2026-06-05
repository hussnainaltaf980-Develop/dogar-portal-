# DEMO OEP Portal — Replication Blueprint
> Source: `demo.oep.com.pk` screen recording (Recording_20260603_232913, 720×1560, ~110s).
> Goal: rebuild the **current Dogar portal** so its frontend look + document coordinate engine
> match this demo **exactly** (structure, layout, style, fonts, colors, spacing).

---

## 1. FRONTEND / UI

### 1.1 Global shell
- **Top-left logo**: green square badge "DEM" + text "DEMO OEP" (bold green) / "OVERSEAS EMPLOYMENT PROMOTERS" (tiny grey caps).
- **Sidebar** (fixed, ~240px, **white** `#FFFFFF`, light border `#E0E0E0`):
  - Items (top→bottom), each = slim line icon + label:
    1. Dashboard
    2. Users ⟩ (expandable, chevron right)
    3. Clients
    4. Demand Files
    5. Candidates
    6. Visa Categories
    7. Embassies
    8. Cities
    9. Medical Centers
    10. Contacts
    11. **Documents** (active state = light-blue pill `#E8F0FE` bg, blue text/icon)
    12. Settings ⟩ (expandable)
  - Bottom: **Logout** (red `#D32F2F`, icon + text).
  - Collapsible (icon-only ↔ icon+label).
- **Top bar** (white): right side user pill = purple avatar circle "D" + "Demo Admin" / "Admin" (small grey subtitle) + chevron.
- **Page background**: light cool grey `#F5F7FA`.
- **Page header**: large bold dark title (e.g. "Documents") left; primary action button right.

### 1.2 Colors (hex)
| Role | Hex |
|---|---|
| Primary blue (buttons, active) | `#2563EB` / `#1A73E8` |
| Active nav pill bg | `#E8F0FE` |
| Sidebar bg | `#FFFFFF` |
| Sidebar border | `#E0E0E0` |
| Page bg | `#F5F7FA` |
| Primary text | `#1F2937` / `#263238` |
| Secondary/grey text | `#6B7280` |
| Danger / logout / delete | `#D32F2F` |
| Logo green | `#1B7A2E` (dark green) |

### 1.3 Typography
- UI font: **Inter / Public Sans** (system sans-serif fallback).
- Page title: ~20–22px bold.
- Section labels (form): ~12–13px, grey, regular.
- Body / table: 13–14px.
- Buttons: 13–14px, medium weight.

### 1.4 Buttons
- Primary: solid blue, white text, rounded ~6px, e.g. `+ Add Document`, `Create`.
- Secondary: white/light bg, border, dark text, e.g. `Cancel`.

---

## 2. DOCUMENTS — list & editor

### 2.1 Documents page
- Header "Documents" + blue `+ Add Document`.
- (List/table of templates below — striped rows, action icons right.)

### 2.2 Customize Document page (header form)
Fields, top→bottom:
- **Name*** (text) — e.g. "Visa Application Form - Islamabad", "OEP Form".
- **Description** (text) — "Optional description shown under title when printing".
- Row: **Category** (select; options incl. "Visa Process (system)", "Protector Documents (system)") | **Attach to** (select; "Candidate").
- Row: **Status** (select; "Enabled") | toggle **"Use letterhead when printing (aligned with fields)"**.
- Toggle **"Requires depositor (show depositor selector when printing)"**.
- **Tabs**: `Content` | `Template Designer`.
- Footer: `Logout` (left), `Cancel` + `Create` (right).

### 2.3 Template Designer — 3-column layout
**LEFT panel "Fields"**
- Search box "Search fields...".
- "Custom text (Enter for new line)" input + `T Add` button.
- Collapsible group **Other**: `Insert checkbox`, `Insert text box`, `Insert barcode`, `Insert trade table`.
- Collapsible field groups (click a field → adds it to canvas):
  - **Customer**: Customer Name, Customer Name (Arabic), Customer Owner Name, OEP License Number, Customer Emigrant Office, Customer Emigrant Office Name, Customer Emigrant Office City, Customer Address, Customer Address (Arabic), Customer Phone, Customer Mobile, Customer Fax, Customer Email, Customer Subdomain, Customer Slug, Customer File Prefix, Customer Starting Point, Customer Status, Customer Plan Name, Customer Expiry Date.
  - **Category**: Document Category.
  - **Candidate**: Candidate Photo, Candidate Name, Candidate Name (Arabic), Father Name, Father Name (Arabic), Mother Name, CNIC Number, NADRA Token No, Date of Birth, Nationality, Address, Phone Number, Gender, Marital Status, Religion, Place of Birth, Place of Birth (Arabic), Tehsil, District, Province, Qualification, Age / Employee, Permission No, Permission Date, Salary, Price, Ticket Included, Accommodation Allowance, Food Allowance, Slot Notes, Status, Protector No & Date, Next of Kin Name, Next of Kin NIC No, Next of Kin Relation, Medical Center, GAMCA Number, Medical Date, Medical Consignment No, Medical Send Date, Medical Courier Name, **E Number**, Date of Departure, Flight No, Destination, Ticket No, Visa Stamp Date, Current Date.
  - **Passport**: (collapsed — passport_no, issue/expiry etc.).
  - **Selected Trade**: Selected Trade Sr#, Selected Trade Visa Category, Selected Trade Qty, Selected Trade Assigned, Selected Trade Available, Selected Trade Salary.
  - **Visa File**: File Number, Client Name, Sponsor Name, Sponsor Address, Sponsor Phone, **Visa Number**, Permission Number, Visa Issue Date, Visa Issue Date (Hijri), Receiving Date, Country, Embassy, Embassy city, Reference, Notes, Trades Table.
  - **Depositor**: (collapsed — depositor name/cnic/etc.).

**CENTER canvas**
- `Page 1` / `Page 2` page tabs + `+` (add page) → multi-page templates.
- Paper-size select: **Letter (8.5×11 in)**, **A4 (210×297 mm)**, **Legal (8.5×14 in)**.
- Background = the form's static image; merge tokens drawn at their X/Y as draggable boxes.
- Tokens render as `{{key}}` placeholders on the canvas (e.g. `{{customer_name}}`, `{{nationality}}`, `{{passport_issue_date}}`, `{{district}}`, `{{place}}`).

**RIGHT panel — Field properties** (empty state: "Select a field on the canvas to edit properties")
For a **text field** (e.g. "Nationality"):
- Title = field label.
- **Custom / preview text** (textarea) — e.g. `{{nationality}}`.
- **X**, **Y** (numeric, PDF/point coords).
- **Width**, **Height** (numeric or `auto`).
- **Align** (left/center/right) ×2 groups (horizontal + maybe vertical).
- **Background**: ☑ None + color box.
- **Font** (select, "Inherit") | **Size** (numeric, e.g. 12).
- **Style**: B / I / U / S toggle buttons.
- **Multiline**: No/Yes select.
- **Color** (swatch).
- **Padding** | **Letter sp**.
- **Line H** | **Transform** (None / uppercase / …).
- **Opacity** (0–100).
- **Remove** (red).

For a **barcode** field:
- **Barcode content** (textarea) — e.g. `{{passport_no}}`. Help: "Combine merge fields and text: e.g. `{{passport_no}}-{{cnic_no}}`, FILE-`{{file_number}}`, or fixed prefixes. Each `{{key}}` is replaced at print time."
- **Bar height** (36) | **Bar width** (1.8).
- **Caption font (readable line)** (9).
- **Symbology** (select; **CODE128**).
- ☑ **Show human-readable text**.
- **X**(50) **Y**(50) **Width**(140) **Height**(52).
- **Align**, **Background None**, **Bar color** (black), **Remove**.

---

## 3. DOCUMENT TYPES seen
1. **Visa Application Form (Saudi Arabia)** — Letter size, single page.
   - Top: CODE128 barcode "P01", Saudi green palm/sword crest centered.
   - Bilingual EN/AR fields: Place of birth, Present nationality, Marital status, Religion, Profession, Sex (Male ✓), Date of passport issue, Date of arrival, Date of birth, Carrier's name, Visa No, Type, Cash/Cheque No/Date, Authorization, Signature/Name.
   - `{{customer_name}}` block, photo box (right).
2. **OEP Form** — "Emigrant/Employee Registration Through OEP Form", Protector Documents, **A4**, **2 pages**.
   - Heavy **char-cell grid** for name & CNIC (one box per letter/digit).
   - Date boxes with `(dd/mm/yyyy)` hints.
   - District/place/issue-date tokens.
   - **Bank table** at bottom: columns Bank Branch Name / Branch Code / Date, multiple "Text box" rows.
3. **Bank Deposit Slip (Allied Bank)** — 3-part (Bank/OEP/Candidate copies); same Amount/Name/ID synced across segments. (Depositor toggle relevant.)

---

## 4. COORDINATE ENGINE BEHAVIOUR
- Coordinate-based overlay on static background image → PDF.
- Field types: **text**, **static custom text**, **checkbox**, **barcode**, **trade/bank table**, **char-cells** (digit/letter grid), **photo**, **arabic/RTL**.
- Merge tokens `{{key}}` resolved at print time from candidate + related (demand/visa file/depositor) data.
- Dates: `dd-mm-yyyy` / `dd/mm/yyyy`, also split into digit boxes.
- IDs (CNIC/passport): char-spaced into pre-printed grid boxes.
- Barcode: CODE128 default, configurable height/width/caption/human-readable.
- Multi-page + paper size (Letter/A4/Legal) per template.
- Toggles: letterhead-on-print, requires-depositor.

---

## 5. CURRENT PORTAL ↔ DEMO — alignment notes
The current Dogar portal already has the coordinate engine (`pdf_engine.py`), the designer
(`document_customize.html`), char_cells, barcode, arabic, trade_table, merge tokens. The work is to
make the **frontend chrome** (sidebar items, colors, fonts, header, user pill, logout) and the
**designer property panel layout / field dictionary** match the demo above, and ensure the
document templates carry the same fields/coordinates.

(Gap audit performed next against the running app's templates & UI.)
