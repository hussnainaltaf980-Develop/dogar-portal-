# Dogar Trading Corporation Portal

A full-stack production-ready management portal for **Dogar Trading Corporation** — a corporate SaaS-style application for overseas employment promotion, recruitment, visa processing, financial tracking, and coordinate-based PDF document generation.

Built with **Python (FastAPI)** backend + **SQLAlchemy** ORM + **Jinja2** templates + **TailwindCSS** frontend.

> **Developed & maintained by** [HussnainTechVertex Pvt Ltd.](#) — branding logo embedded in every footer & login page.

---

## ✅ Demo-OEP Parity Fix Pass — 7 Problems Resolved (LATEST)

All seven defects raised against the live demo OEP (`demo.oep.com.pk`) have been
fixed, verified on the running app, and committed. 59/59 tests pass.

| # | Problem | Fix | Verified |
|---|---------|-----|----------|
| 1 | Field palette showed raw DB keys (`name_arabic`) | Palette renders **clean labels only** (key kept in `data-key`/search) | Screenshot — Customer group shows only "Name", "Name (Arabic)", … |
| 2 | Agency/recruiting-office name missing on forms | `customer_*` fields resolved from `company_settings` (Dogar Trading Corporation) | API + canvas |
| 3 | "Occupation (Arabic)" + selected trade missing | Added `profession_arabic` + `selected_trade_visa_category` to palette & overlay resolver | Palette dump |
| 4 | OCR garbled (`SUFYAN KKKKSKK ALI`, wrong CNIC) | ICAO-9303 MRZ check-digit validation + name de-noising + bio-data enrichment | `SUFYAN KKKKSKK ALI`→`SUFYAN ALI` |
| 5 | Two E-Barcodes different sizes | Fixed **95 mm** barcode width + quiet-zone auto-crop | Letterhead CSS |
| 6 | Print/PDF coordinates drifted off the form | **Top-anchor** print HTML + PDF text to match the designer box model (`baseline = y + height − font_size`) | Print preview **and** generated PDF screenshots — every field lands on its form line/box |
| 7 | Phone (HEIC) photos not understood | `pillow-heif` HEIC decode + browser-side downscale + relaxed content-type | OCR pipeline |

**Problem 6 detail (coordinate fix):** the designer canvas (`.field-marker`)
places each field's text at the **top** of its box (`box top = page_h − y −
height`, `line-height 1.05`). reportlab/`drawString` and the old print HTML
instead bottom-anchored the text at the stored `y` baseline, so whenever
`height ≠ font_size` the printed/PDF text drifted `(height − font_size)`pt below
where the user positioned it. The print HTML (`.ov-field`) is now
`align-items: flex-start` and the PDF (`_render_field`) draws at
`baseline = y + height − font_size`, so **designer == print HTML == PDF**.

---

## 🛠️ Jun 2026 Fix Pass — Print, Barcode & OCR (READ THIS)

Three concrete defects were diagnosed with live evidence and fixed end-to-end:

### 1. Documents printed almost-empty (only name + barcodes, no background)
**Root cause:** the HTML print page (`/api/documents/templates/{id}/print`) loaded
its background via a network `<img>`. On Android/Chrome the print engine drops
network images **and** background graphics by default, so the printed page came
out blank with only the top text + barcodes visible.
**Fix:** the print endpoint now (a) **inlines the background as a base64 `data:` URI**
so it can never fail to load, and (b) forces `print-color-adjust: exact` on
`html/body/.doc-page/.doc-bg` so the browser keeps the background when printing.
Result: the printed page now matches the reportlab PDF exactly (full form + data).

### 2. E-Barcode rendering
Verified clean & professional: Code128, sharp bars, centered human-readable
caption (e.g. `E214226395`). Rendered by `pdf_engine.barcode_to_data_uri()` and
embedded both in the PDF and the HTML print overlay.

### 3. OCR "Scan Passport" returned "0 of 0 fields filled"
**Root cause #1:** the **`tesseract-ocr` system binary was not installed**, so the
offline OCR fallback hard-failed.
**Root cause #2:** the AI-vision path returns `401 Invalid/expired token` when the
LLM proxy key is stale.
With no working path, extraction always returned 0 fields.
**Fix:** install the system binary (`sudo apt-get install -y tesseract-ocr`) — the
offline ICAO-9303 MRZ parser then extracts 11+ fields reliably. Also fixed an
MRZ name-cleaning bug where Tesseract misread the `<` filler glyphs as a run of
`C`/`E` characters and glued them onto the name (e.g. `SAQIBECCCCCC...` → `SAQIB`).

> **⚠️ OCR system dependency:** offline passport OCR needs the Tesseract binary.
> Install it once on the host: `sudo apt-get install -y tesseract-ocr`.
> Without it (and without a valid vision LLM key) the scanner will report that
> automatic reading is unavailable and ask the user to type the fields manually.

### Field-coordinate question answered
- **"Occupation / Profession"** field key = `profession` (now exposed in the
  designer palette under the Candidate group). On templates it sits at e.g.
  Template 20 `x=375.0, y=534.4` (field id 193).
- **"Visa Category"** field key = `trade_visa_category` (Selected Trade group),
  e.g. Template 14 `x=370.7, y=367.3` (field id 3751).

---

## 🏢 Enterprise Release (Jun 2026 v6 — multi-tenant, RBAC, super-admin, side-panel redesign)

This release is the **enterprise-grade hardening pass** completing the
roadmap demanded in the v4/v5 audit. Highlights:

### 🔐 Permanent Super-Admin (developer account)
- A locked super-admin row is **idempotently re-seeded on every boot**:
  - Email: `hussnainmr07@gmail.com`
  - Role: `admin` + `is_super_admin=True`
  - The account **cannot be deleted, demoted, or deactivated** via the
    API. Even other admins get `403 Forbidden`.
  - If the password hash is wiped externally, the next boot restores it.
- Implemented via `seed_super_admin()` in `app/db/init_db.py`, wired
  into `app/main.py`'s startup hook.

### 🏬 Multi-Tenant (subdomain-based isolation)
- New module `app/core/tenancy.py`:
  - `TenantResolverMiddleware` reads the `Host` header on every
    request and attaches `request.state.tenant`.
  - `demo.oep.com.pk` → looks up `Tenant.slug='demo'` → swaps to that
    tenant's isolated SQLite DB via a thread-safe sessionmaker cache.
  - `localhost` / IP literals / `www.*` fall back to the control DB.
- Per-tenant branding (uploaded under `data/tenants/<slug>/`):
  - **Logo** — appears in navbar & on every printed document
  - **Letterhead** — A4 background for receipts, protector letters,
    candidate print profiles
  - **Office name, demand-file format, receipt template, primary color**
- New API endpoints on `/api/tenants`:
  - `POST /{id}/logo` — upload tenant logo
  - `POST /{id}/letterhead` — upload tenant letterhead
  - `PUT  /{id}/branding` — set office name / demand format / receipt template
  - `GET  /_meta/current` — current resolved tenant (public)
- Portal-admin UI (`portal_admin.html`) gains a **"Brand" drawer** per
  tenant with logo/letterhead upload widgets + identity form.

### 👮 Role-Based Access Control (RBAC) — granular permissions
- New module `app/core/permissions.py` with:
  - `PERMISSION_CATALOG` — 48 permission keys across 11 modules
    (Dashboard, Clients, Demands, Candidates, Documents, Protector,
    Receipts, Reports, Users/Roles, Settings, Audit)
  - `ROLE_PRESETS` — sensible defaults for Admin/Manager/Staff
  - `user_permissions(user, db)` — resolver respecting super-admin
  - `require_permission("candidates:delete")` — FastAPI dependency factory
- API endpoints:
  - `GET /api/users/roles/_meta/catalog` — for the role-editor UI
  - `GET /api/users/me/permissions` — for frontend permission gates
- System roles are **idempotently re-seeded** on every boot using the
  latest presets, so newly added permission keys flow through.
- The `Admin` role is hard-locked to wildcard `*` — attempts to scope
  it down return `400`.
- Custom roles can be created freely via the new **roles.html** UI
  (Tailwind permission matrix with quick presets, group toggles).

### 🎨 Candidate Side-Panel — Professional Redesign
- Gradient hero header with photo / initials, name, ID & E-Number badges
- 8-stage horizontal workflow tracker (Received → Medical → E-Number →
  Visa → Clearance → Departure)
- 6 structured info cards: Personal · Identity & Passport · Contact &
  Address · Next of Kin · Linked Demand File · Visa Category
- **Print button now produces a true CV-style A4 page** on the
  letterhead (new route `GET /api/candidates/{id}/print-profile`)
- All document printing uses a **hidden-iframe direct-print pattern**
  — no new tabs, no popups, no screenshot bugs.

### 🔧 Critical Bugfix — Stage Update 500 Error
- Root cause: candidates with `Decimal` salary triggered
  `TypeError: Object of type Decimal is not JSON serializable` when
  the audit log row was committed.
- Fix: `app/services/audit.py` now coerces Decimal/date/datetime/
  bytes/lists/sets via `_json_safe()`; the commit is wrapped in
  try/except so audit failures **never break** the originating
  request.

### 📎 Demand File → Candidate Links
- In `demand_detail.html`, every assigned-candidate card now has a
  deep-link button (`<i class="fa-solid fa-arrow-up-right-from-square">`)
  that opens the Candidates page with the side-panel auto-focused on
  that candidate (`/candidates?focus=<id>`).

### 📄 E-Barcode Re-Categorized
- Moved from **Protector Documents** to under **Visa Category
  Documents** in the candidate documents drawer.

### ✅ Test Coverage
- `tests/test_super_admin.py` (6 tests) — super-admin idempotency, login,
  delete/demote protection, password restoration.
- `tests/test_audit_json_safety.py` (5 tests) — Decimal/date coercion
  + audit-doesn't-crash-request regression.
- `tests/test_rbac.py` (10 tests) — catalog integrity, preset sanity,
  user_permissions resolver, admin wildcard lock.
- `tests/test_tenancy.py` (6 tests) — subdomain parser, current-tenant
  endpoint, branding auth gates.
- **59 tests total — all passing.**

---

## 🆕 Hotfix Release (Jun 2026 v3 — letterhead-embed, clean E-Barcode, dedupe customize-save)

This patch fixes **three critical regressions** flagged in production
screenshots and ships a one-shot housekeeping cleanup:

### 1️⃣ Letterhead now ALWAYS prints (no more plain-white pages)
- **Problem**: when the printable Protector Letter Packet / Demand Packet
  HTML was opened via `blob:` URL (the `/protector-letter` workflow page
  POSTs the payload, wraps the response in a `Blob`, and `window.open()`s
  it), relative paths like `/static/pdf_backgrounds/dogar_letterhead.jpg`
  intermittently failed to resolve back to the parent origin → the
  letterhead JPG vanished in the print preview and documents printed on a
  plain white sheet.
- **Fix**: `app/services/letterhead_renderer.py` now embeds the
  letterhead as a **base64 `data:image/jpeg;base64,…` URL** (read once
  per process from `app/static/pdf_backgrounds/dogar_letterhead.jpg` and
  memo-cached). Every `<img class="bg" src="…">` is now self-contained —
  the document looks identical whether opened from a direct GET, a
  `blob:` URL, `file://`, copy-paste, or saved-then-reopened.
- **Bonus alignment fix**: content margins re-tuned to match the
  letterhead asset's true geometry (header strip = top 15 %, footer
  strip = bottom 8 %). New defaults: `top=46 mm, bottom=30 mm,
  left=22 mm, right=22 mm` (was 38/28/18/18). Body content no longer
  collides with the company header band.

### 2️⃣ E-Barcode reverted to the legacy clean-print layout
- **Problem**: the printable barcode sheet was emitting two barcodes
  with `E-Number` / `File No.` text labels above each, padded
  irrelevant numbers (the 2nd barcode used a meaningless demand File
  Number padded with leading zeros), and a chunky `Powered by
  HussnainTechVertex Pvt Ltd.` developer strip at the bottom — none of
  which exists on the legacy dogars.com print the user wants us to
  clone.
- **Fix**: `render_e_barcode()` now renders:
  1. Candidate NAME (uppercase, top-left, 12 pt)
  2. **E-Number barcode** + `*VALUE*` underneath
  3. **Passport-Number barcode** + `*VALUE*` underneath
  
  No labels, no developer footer, no double-text, and the second
  barcode encodes the **Passport Number** (e.g. `ME1855631`) — never
  the demand File Number. Falls back to `candidate.id` zero-padded to
  9 digits if both passport and the legacy `file_number` arg are
  empty. Bars are uniformly **56 mm wide × 14 mm tall** so the layout
  stays stable regardless of how many characters each value encodes.

### 3️⃣ Document Customize save → duplicate-on-save bug killed
- **Problem**: every click of the **Save** button on the Document
  Designer (`/documents/customize/{tpl_id}`) appended a NEW row for
  every field instead of updating the existing one. A single Visa
  Application Form template had grown to **3 949 field rows** for what
  should be 23 unique fields (~170× duplication) because the bulk-save
  endpoint silently ignored matching by position when the client
  failed to send the `id` back.
- **Backend fix** in `POST /api/documents/templates/{tpl_id}/fields/bulk`:
  - **Position-match fallback** — when the incoming payload omits
    `id`, match an existing row by `(field_key, page, round(x),
    round(y))` before falling through to INSERT.
  - **Per-request dedupe** — drop exact-position duplicates *within*
    a single payload.
  - **REPLACE-ALL semantics** — any DB row whose id is not represented
    in the payload is deleted at the end. The frontend already
    submits the complete canvas state on every Save, so this makes
    the canvas the single source of truth.
- **Housekeeping**: new admin endpoint `POST /api/documents/admin/dedupe-all-fields`
  collapses all existing duplicates across every template (OLDEST row
  wins). Idempotent — safe to invoke any time. Ran once on the
  production DB during this release → template #14 went from 3 949 →
  23 fields, no data loss.
- **Per-template dedupe**: `POST /api/documents/templates/{tpl_id}/fields/dedupe`
  for ad-hoc cleanup of a single template.

---

## 🆕 Previous Release (Jun 2026 — Vercel-v0-style actionable DtcBot + official receipts + barcode polish)

This release upgrades **DtcBot** from a text-guided helper into a true
**actionable compute agent** (matching the Vercel v0 / Lovable pattern) and
adds two **official printable receipts** (Demand File + Payment) with logo,
watermark, and scannable QR code.

### 🤖 DtcBot — Actionable Agent (end-to-end OCR → data entry)
- `POST /api/chatbot/upload?auto_create=true` (now the default) — the bot
  **runs OCR on the uploaded passport** *and* immediately calls
  `dtcbot_agent.create_candidate()` (or `update_candidate()` if the passport
  / CNIC already exists), then returns a **`type: "action"`** response with
  `verb`, `candidate_id`, `url`, and `navigate: true`.
- The chat widget's `renderResponse()` now handles the `action` type:
  renders a green/blue/violet success card with the entered fields + Open
  Record button, and **auto-navigates** the parent window to the candidate
  edit screen after 1.8 s. **No more guidance text — the agent does the work.**
- Natural-language commands also produce actions:
  `create candidate named ALI HASSAN, passport AB1234567` →
  `set salary of ALI HASSAN to 1500` →
  `assign ALI HASSAN to trade #12` — each returns an actionable response
  with `navigate: true` and the canonical record URL.
- Upload-progress label: typing dots now show
  *"🤖 Reading passport · running OCR · creating candidate record…"*.

### 🧾 Official Receipts (logo + QR + watermark, A4)
- **`GET /api/demands/{id}/file-receipt`** — Demand File Receipt with company
  logo header, sponsor block, recruitment-categories table, "amount in words"
  in Pakistani style (Crore / Lakh / Thousand), a **scannable QR** linking
  back to `/demands/{id}/file-receipt`, an angled CSS watermark
  (`::before` pseudo-element at –32°, 6% opacity), terms + signature row, and
  HussnainTechVertex Pvt Ltd. footer.
- **`GET /api/demands/{id}/payments/{pid}/receipt`** — Payment Receipt with
  PAID/INVOICE stamp variant, same logo + watermark, QR code linking back to
  the receipt URL.
- Renderer: `app/services/receipts_renderer.py` (≈600 lines) — uses
  `qrcode==7.4.2`, base-64 PNG embedding for logo and QR, shared
  `_company_head_html` / `_verify_row_html` / `_page_footer_html` builders.
- **UI**: green **File Receipt** button added next to *Print All (Packet)*
  on the Demand Detail page → opens the new receipt in a new tab with
  auto-print.

### 📊 Barcode polish (E-Barcode sheet)
- Second barcode is now the **Demand File Number** (numeric, e.g. `8176`)
  resolved from the candidate's assignment → job_category → demand chain,
  with leading-zero padding to match the E-Number length so both barcodes
  print at the **same visual width** (`<img class="bc-img">` is forced to
  80mm width via CSS).
- 25 mm vertical separation between blocks (was 14 mm) so they can NEVER
  visually overlap, plus 22 mm tall bars and 1.6 module width for robust
  print scanning.
- HussnainTechVertex Pvt Ltd. footer strip on every E-Barcode sheet.

### 🎨 Brand identity (HussnainTechVertex Pvt Ltd.)
- New `app/static/img/dev_logo.png` (740 × 742) + 64 px + 32 px icon
  variants generated via Pillow.
- Logo + name embedded in **base.html footer**, **login page footer**, and
  every printed receipt / barcode sheet.

### Files added / modified this release
| File | Status | Purpose |
|------|--------|---------|
| `app/services/receipts_renderer.py` | **new** | Official Demand File + Payment Receipt with logo, QR, watermark, amount-in-words |
| `app/services/letterhead_renderer.py` | edit | E-Barcode now uses File No. (numeric) as second barcode, padded for uniform width, with HussnainTechVertex footer |
| `app/services/dtcbot.py` | edit | `handle_uploaded_document()` actively creates / updates candidate from OCR (auto-execute path) |
| `app/services/dtcbot_agent.py` | edit | `_wrap_action()` now returns `verb`, `url`, `navigate: true` for frontend auto-redirect |
| `app/api/endpoints/demands.py` | edit | New `/file-receipt` endpoint + payment receipt rewritten to call `receipts_renderer` |
| `app/api/endpoints/chatbot.py` | edit | `/upload` injects DB session + `auto_create` flag → end-to-end OCR → record creation |
| `app/templates/base.html` | edit | DtcBot widget JS now renders `action` type + auto-navigates; footer carries HussnainTechVertex logo + name |
| `app/templates/login.html` | edit | Brand logo in card footer + page footer |
| `app/templates/demand_detail.html` | edit | "File Receipt" button next to "Print All (Packet)" |
| `app/templates/protector_letter.html` | edit | Emerald → blue accents |
| `app/templates/candidates.html` | edit | Drawer hidden when `flyout+wizard_mode`; instant `openWizard()` init |
| `app/main.py` | edit | `wizard_mode` template var passed when `?wizard=1` or `?edit=N` |
| `app/static/img/dev_logo*.png` | **new** | HussnainTechVertex brand asset (3 sizes) |
| `requirements.txt` | edit | + `qrcode==7.4.2` |

---

## 🗂️ Previous Changes (May 2026 — Centered-modal data entry + view-only drawer, demo-faithful lists & docs)

Reworked the candidate UI to match the **real `demo.oep.com.pk` flow**: data
entry is a **centered modal**, while the right-side panel is a **read-only view**.

- **Candidate data entry = centered modal** (`#candidateModal`, `.modal-box-wizard`,
  max-width 880px). Used for **both Add and Edit**. Step 1 leads with the photo
  upload, fields laid out in a 3-column grid, horizontal 7-step stepper at top,
  Cancel (left) / Prev / Save Draft / Next / Finalize (right). Opened via
  `openWizard(id,'edit')` or `openCandidateModal()`.
- **Right-side drawer = view-only** (`openView(id)`): read-only **Details /
  Documents / Checklist / Status / Activity** tabs with **Print** + **Edit
  Candidate** (the Edit button bridges to the centered modal via
  `editFromDrawer()`). The old in-drawer wizard was removed.
- **Status tab** now has a **workflow-stage dropdown** (populated from the
  canonical `/api/candidates/stages`) that PUTs the new stage to
  `/api/candidates/{id}` (emitting a `stage_change` audit event), plus a
  **Status History timeline** built from the `stage_change` audit entries, and
  the existing processing-details grid + assignments.
- **Demand File → Trades tab**: "Assign" opens the candidate centered modal
  **full-screen** (the flyout iframe expands so the modal is centered on the
  whole viewport, not a narrow side panel). Assigned candidates render as
  **photo cards** (photo, name, S/O father, status pill, Details / Edit /
  Unassign). On finalize the parent flyout closes, shows a toast, and reloads
  trades.
- **Document Print** (Protector Documents / Visa Process groups) is wired
  through the **coordinate-overlay engine** (`/api/documents/templates/{id}/print`)
  on real form-image backgrounds: **OEP Form p1 & p2, Allied Bank Deposit
  Form-7, NBP/HBL slips, Protector Certificate** (letterhead), and embassy-
  filtered **Visa Application (Karachi / Islamabad)**. Verified all render 200
  with field overlays + auto print dialog.
- **Demand list columns**: **File # · Visa # · Receiving Date · Sponsor ·
  Embassy · Status · Trades (assigned/total)**. The list API now returns
  `assigned_count` (single grouped query over the visible page).
- **Client → Demand Files list columns**: **File # · Date · Sponsor · Embassy ·
  Status**; the client-demands API now returns `embassy`.
- **Legacy draft upgrade**: `POST /api/candidates/wizard/from-candidate/{id}`
  now **backfills** any pre-7-step draft (5 slices) up to 7 slices and bumps
  `total_steps`, so Steps 6 & 7 hydrate correctly from the candidate row while
  preserving any in-progress edits.

---

## 🆕 Previous Changes (May 2026 — Candidate Wizard expanded to 7 steps)

The candidate-centered modal wizard now models the **full overseas-employment
lifecycle** in 7 resumable steps (was 5). Two new steps were inserted between
*Next of Kin* and *Charge Summary* to capture the real-world processing stages
that map to the canonical workflow (`medical`, `protector`, `visa_stamping`,
`e_number`, `travel_ready`, `deployed`):

- **Step 5 · Medical & Protector** — GAMCA/slip no., medical center, medical
  date, consignment no., send date, courier, plus Protector of Emigrants no. &
  date (`gamca_number`, `medical_center`, `medical_date`,
  `medical_consignment_no`, `medical_send_date`, `medical_courier_name`,
  `protector_no`, `protector_date`).
- **Step 6 · Visa & Travel** — visa stamp date, E-number, destination, flight
  no., date of departure, ticket no. (`visa_stamp_date`, `e_number`,
  `destination`, `flight_no`, `date_of_departure`, `ticket_no`).
- **Step 7 · Charge Summary** — the former Step 5 (price, allowances, ticket
  included, workflow stage) is now the final step.

All new fields already existed on the `Candidate` model, so **no DB migration
was required**. The canonical step list lives in `app/core/workflow.py`
(`WIZARD_STEPS` / `TOTAL_WIZARD_STEPS = 7`) and is consumed by both the backend
(`/api/candidates/stages`, wizard step/finalize endpoints) and the frontend
stepper/`STEP_FIELDS`. Edit-mode = 6 steps (skips Charge Summary); New/Assign =
7 steps. The Status tab's grid surfaces all the new medical/visa/travel values.

Verified end-to-end: create draft → save steps 1/5/6/7 → finalize correctly
persists every medical, protector, visa and travel field with proper date
coercion and stage transition.

---

## 🆕 Previous Changes (May 2026 — DtcBot Compute Agent + AI Vision OCR + Profile Photo)

This release upgrades **DtcBot from a guide into an offline-capable *compute
agent* that actually performs data entry**, adds an AI-vision-first passport
scanner with graceful offline fallback, and brings the candidate wizard's
**Step 1 profile photo upload** in line with the reference videos.

- **DtcBot Compute Agent (`app/services/dtcbot_agent.py`) — deterministic, offline**
  - Parses natural-language commands **and** pasted "Field: value" data blocks,
    maps human phrasing (e.g. *father*, *passport no*, *cnic*, *dob*) to real
    `Candidate` columns via `FIELD_ALIASES`, coerces types (dates, ints, money,
    gender), then performs **real DB writes** with full audit logging.
  - Supported actions (all execute against the live DB, no LLM key required):
    - **Create** — `create candidate` + a data block, or `... named ALI HASSAN`.
    - **Update / Set** — `set passport no of ALI HASSAN to AB1234567`.
    - **Assign** — `assign ALI to trade #12`.
    - **Bulk data-entry** — paste an OCR/passport block; if it has a `Full Name`
      it creates a candidate, otherwise it updates the matching passport/CNIC.
  - Wired into `dtcbot.answer()` **before** the LLM/rule fallback, so the bot
    *acts* even when the external LLM key is expired (per the user's request:
    *"data entry to dtc bot as trained compute agent, not only guided"*).
- **Direct data-entry API** — `POST /api/chatbot/data-entry`
  - Body: `{ "block": "Full Name: ...\nPassport No: ..." }` **or**
    `{ "fields": {col: value}, "candidate_ref": "<id|name|passport>" }`.
  - Creates (needs `full_name`) or updates (with `candidate_ref` / matched
    passport/CNIC). Returns the saved candidate snapshot.
- **AI Vision passport OCR (`app/services/passport_vision.py`)**
  - New primary OCR path: sends the passport image to an OpenAI-compatible
    vision model and parses strict JSON of `Candidate`-shaped fields.
  - `extract_passport_data()` now runs a **3-tier pipeline**:
    **(1) AI Vision → (2) Tesseract MRZ (ICAO 9303 TD3) → (3) text regex**,
    returning `method = "vision" | "mrz" | "text"`.
  - When neither a valid vision key nor Tesseract is present, it returns a
    clear **"type the details into the form"** message instead of failing —
    the wizard fields stay ready for manual entry.
- **Candidate Wizard · Step 1 — Profile Photo upload**
  - New photo uploader with live preview at the top of *Personal Information*.
  - Uploads via existing `POST /api/candidates/upload-photo`
    (stored at `/static/uploads/photos/<file>`); the filename is saved into the
    candidate's `photo` field and re-rendered when re-opening the wizard.
- **Dependencies** — `openai==2.38.0` and `PyYAML==6.0.3` added to
  `requirements.txt` (LLM brain + vision OCR + config loading).

> **Note on the LLM key:** the conversational LLM brain and AI-vision OCR both
> require a valid OpenAI-compatible key in `~/.genspark_llm.yaml`. If the key is
> expired, **all of the above still work**: the compute agent and rule engine
> run fully offline, and OCR degrades gracefully to manual entry.

---

## 🆕 Previous Changes (May 2026 — Client detail parity + print stylesheet)

This release fully aligns the **Client Detail** page with the reference
mobile SaaS screenshots (Profile / Contacts / **Demand Files** / Statement),
adds proper print support across the portal, and re-shapes the
`/api/clients/{id}/demands` payload for richer per-row context.

- **Client Detail · Demand Files tab — schema + UI parity with reference**
  - API `/api/clients/{id}/demands` now returns the envelope `{items, total}`
    with the columns shown in the reference screenshot:
    `file_number, receiving_date (Date), sponsor_name, visa_number, visas (count), country, permission_no, status`.
  - The `visas` column is the **aggregated trade quantity** (sum of all
    `JobCategory.quantity` for that demand) — done in one batched
    `GROUP BY` query (no N+1).
  - The HTML table now shows: **File No · Date · Sponsor · Visa No · Visas · Status**
    exactly matching the reference. Sponsor names are intelligently truncated
    with full text on hover.
  - Added inline **search box** (file / sponsor / visa) and `· N files` counter
    in the card header for fast scanning of large client portfolios.
  - Status pills are color-coded (active → green, processing → amber,
    cancelled/expired → rose) to mirror the screenshot's green "active" badge.
- **Client Detail header — Print + Delete buttons + status pill**
  - Restored the **Print** / **Edit** (now blue, primary) / **Delete** action
    cluster shown in the reference screenshot.
  - Top-right status pill mirrors the live client status.
  - Header subtitle reformatted to `Client profile · <City>, <Country>`.
  - **Delete** confirmation modal warns about cascade effects on linked
    demand files and statements.
- **Portal-wide `@media print` stylesheet** (`app/static/css/app.css`)
  - Hides sidebar, top bar, action buttons, and last-column row actions.
  - Keeps tables, stat tiles, profile cards readable on A4 paper.
  - Triggered by the **Print** button on the Client Detail page; document
    PDFs still flow through the overlay engine as before.
- **Maintenance / hardening**
  - `.gitignore` now excludes `core`, `core.*`, `*.coredump`, `server.out.log`,
    `server.err.log` (caught a 285 MB stray Playwright chromium core file
    that would otherwise have bloated the production zip).
  - All 30+ endpoints + 12 HTML pages pass the bundled smoke test
    (`api/auth/login → /api/clients/871/demands` etc.) — see `tests/`.

### Earlier (May 2026 — UI polish + Document customize fix)
This release brings the candidate list visuals fully in line with the
reference SaaS theme screenshots, and fixes a regression where the
Document Customize page failed to load background images.

- **Candidates List — visual match with reference**
  - **`+ New Candidate`** blue button restored (top-right) — matches reference exactly.
  - Removed the `Client / Demand File No. / Embassy` columns from the *list view*
    (that data is still loaded into the candidate-detail drawer header — no data loss).
  - Removed the long blue info-banner; the workflow constraint is preserved via a
    clean modal instead.
- **`+ New Candidate` modal** (workflow + UX preserved)
  - Step 1: pick Demand File (auto-loaded list).
  - Step 2: pick Trade (auto-loaded from selected demand, shows assigned/quantity).
  - On Continue → redirects to `/demands/{id}?trade_id=...&action=assign_candidate`
    where the original 5-step wizard launches with full context (client/demand/embassy
    badges shown in drawer header).
  - The backend still rejects any wizard create without `job_category_id`, so
    direct candidate creation is impossible even via API — data integrity guaranteed.
- **Document Customize page — background image fix**
  - The DB stores the background path as `/static/pdf_backgrounds/foo.jpg` (full),
    but the JS was double-prefixing it → `Failed to load background image`.
  - Fixed `setBackground()` to normalize: if the string already starts with `/` or
    `http`, use it as-is; otherwise prepend `/static/pdf_backgrounds/`.

### Earlier (May 2026)
- **Demand Detail** → `[Print Documents]` (was "Generate"), Embassy badge in header.
  - **Documents tab** = PERMISSION DOCUMENTS group with per-row `[Print]` button hitting the **real-image data-overlay engine** (Pillow + ReportLab, NOT generation). A `⚙` gear opens the coordinate-customise page.
  - **Payments tab** = stat tiles (Total Invoiced / Received / Outstanding) + table of payments + receipt-print on demand. Payments are stored as `ClientStatement` rows tied to a Demand File so they also appear on the client statement. Auto receipt-numbering `RCP-YYYY-NNNN`. Receipt is rendered as a printable HTML with `@media print` CSS, PAID stamp, amount-in-words (lakh/crore), signature lines.
  - **Edit Trade Modal** — supports `custom_fields` (JSON) merged into PDF templates by the overlay engine.
- **Candidate Detail (drawer)** → Embassy / Client / Demand File No. badges in the header.
  - **Documents tab** = PROTECTOR DOCUMENTS + VISA PROCESS groups (data-overlay print). Visa Application Form is **auto-filtered by embassy** (Karachi vs Islamabad).
  - **Wizard step count is conditional** — Edit mode = 4 steps (skips Charge Summary), New/Assign = 5 steps.
- **Candidate List** → Photo column removed (per user request). New columns: `Client`, `Demand File No.`, `Embassy`. Skeleton loader + 60s client-side cache + invalidation on save.
- **OCR** — Tesseract MRZ TD3 pipeline confirmed end-to-end at `POST /api/ocr/passport`. Reachable from the green **Scan Passport** button in candidate wizard Step 2 and demand-detail.
- **AI Chatbot (DtcBot)** — Upgraded from rule-based to **real LLM** (GPT-5-mini via Genspark proxy) with **tool/function calling** that queries the live SQLite DB (candidates, demands, clients, payments, templates). Multi-turn memory, Markdown rendering, graceful fallback to rule-based bot when the LLM is unreachable. Source: `app/services/dtcbot_llm.py`.
- **Performance** — Full portal speed pass:
  - SQLite WAL journal + 64MB cache + mmap_io + memory temp store
  - Single-query JOIN batch-loader (eliminated N+1 in candidate-context derivation)
  - GZip middleware (~90% payload reduction on list endpoints)
  - 24h `Cache-Control: immutable` for `/static/`, 30s private cache for read-only API
  - Frontend skeleton loaders + in-memory list cache (60s TTL with invalidation on mutation)
  - Benchmark: `/api/candidates/?limit=50` went from ~100ms/88KB → **~50ms/9KB**

---

## ✨ Features

### Core Modules
- **Dashboard** — Real-time statistics, charts, recent activity
- **Clients / Companies** — Client (Foreign Sponsor) management with full profiles
- **Demand Files** — Job demand letters with job categories and quantities
- **Candidates** — Full candidate lifecycle (personal info, passport, CNIC, trade)
- **Agents** — Sub-agent / travel agent management (migrated from old data)
- **Agent Cash Book** — Debit / Credit ledger per agent (migrated from old data)
- **Visa Tracking** — Status workflow (Pending → Processing → Issued → Expired)
- **Documents & PDF Overlay Engine** — Coordinate-based document builder
- **Reports** — Financial, recruitment, visa reports
- **Users & Roles** — Multi-user with admin/staff permissions

### PDF Overlay Engine (★ Core Feature)
This is the heart of the portal — replicating the engine seen in the reference video:

1. **Upload background** — Upload any official form (visa, bank slip, government form) as JPG/PNG/PDF
2. **Place fields visually** — Drag-and-drop database fields onto the form
3. **Set coordinates** — Precise X, Y positioning with font size, color, bold/italic
4. **Save template** — Reusable document template tied to data source
5. **Generate PDF** — One-click filled PDF for any candidate / record

Supported document templates (pre-built):
- Allied Bank Deposit Slip
- NBP / NSEP Deposit Slip
- OEP Government Form (multi-page)
- Saudi / GCC Visa Application
- Demand Letter
- Permission Undertaking
- Custom (build your own!)

### Existing Data Migrated
The legacy MySQL database (`backup-18-05-2026.sql`) has been migrated:
- ✅ `agents` table → 1 agent (NEW CHAUDHARY TRAVELS) + extensible
- ✅ `agents_cash` table → 70+ transaction ledger entries
- ✅ Default office: **Dogar Trading Corporation**
- ✅ Logo embedded throughout

---

## 🚀 Quick Start

### 1. Setup environment
```bash
cd dogar_trading_portal
python -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
cp .env.example .env
```

### 2. Initialize database & seed legacy data
```bash
python -m app.db.init_db
```

### 3. Run the server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Login
Open **http://localhost:8000** and login:
- **Email:** the value of `DEFAULT_ADMIN_EMAIL` from your `.env` file
- **Password:** the value of `DEFAULT_ADMIN_PASSWORD` from your `.env` file

> ⚠️ **Security**: The bootstrap admin is created with `must_change_password=true`
> — on first login the UI redirects you to the password-change screen and the
> initial password cannot be re-used. In `ENV=production` mode the app refuses
> to start if the well-known weak passwords (`admin123`, `password`, …) are
> still in `.env` — see [`.env.example`](./.env.example) for guidance.

---

## 🏗️ Tech Stack

| Layer        | Technology                                  |
|--------------|---------------------------------------------|
| Backend      | Python 3.10+, FastAPI 0.115                 |
| ORM          | SQLAlchemy 2.0                              |
| Database     | SQLite (default) / PostgreSQL / MySQL ready |
| Auth         | JWT (python-jose) + bcrypt                  |
| Templates    | Jinja2                                      |
| Frontend     | TailwindCSS (CDN), Alpine.js, Chart.js      |
| PDF Engine   | ReportLab + Pillow + pypdf                  |
| Icons        | FontAwesome 6                               |

---

## 📁 Project Structure
```
dogar_trading_portal/
├── app/
│   ├── main.py                  # FastAPI entry point
│   ├── core/                    # Config, security, deps
│   ├── db/                      # Database session, init/seed
│   ├── models/                  # SQLAlchemy ORM models
│   ├── schemas/                 # Pydantic schemas
│   ├── services/                # Business logic (PDF engine, etc.)
│   ├── api/endpoints/           # REST API routes
│   ├── templates/               # Jinja2 HTML pages
│   └── static/                  # CSS, JS, images, uploads
├── migrations/                  # Original SQL backup + migration script
├── data/                        # SQLite database (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🌐 Main URLs

| Path                            | Purpose                              |
|---------------------------------|--------------------------------------|
| `/`                             | Login page                           |
| `/dashboard`                    | Main dashboard with stats            |
| `/clients`                      | Client / Company management          |
| `/demands`                      | Demand files list                    |
| `/candidates`                   | Candidates list                      |
| `/agents`                       | Sub-agents list                      |
| `/agents/{id}/cashbook`         | Agent ledger (debit/credit)          |
| `/documents`                    | Document templates list              |
| `/documents/builder/{id}`       | PDF coordinate builder (★)           |
| `/documents/generate/{id}`      | Generate filled PDF                  |
| `/reports`                      | Reports module                       |
| `/users`                        | User management (admin only)         |
| `/api/docs`                     | Swagger API documentation            |
| `/api/redoc`                    | ReDoc API documentation              |

---

## 🔐 Bootstrap Admin

The bootstrap admin user is created **from the values you set in `.env`**:

```bash
DEFAULT_ADMIN_EMAIL=...      # required
DEFAULT_ADMIN_PASSWORD=...   # required, ≥12 chars, not in weak list
```

On first login the user is forced to change their password (`must_change_password=true`),
so even if a weak placeholder slips through development, it cannot survive past the
first sign-in. In `ENV=production` mode the application **refuses to boot** with any
of the documented weak passwords — see `app/core/config.py::_FORBIDDEN_ADMIN_PASSWORDS`.

---

## 📦 Data Architecture

### Bundled Production Dataset

The zip ships with a **complete real-data SQL dump** at
`migrations/dogar_full_backup.sql.gz` (~650 KB compressed, ~3.8 MB
uncompressed). On first startup, if the SQLite database is empty,
`app/main.py::_apply_bundled_sql_dump()` automatically restores:

| Table                  | Rows  |
|------------------------|-------|
| `candidates`           | 2,674 |
| `demands`              | 2,260 |
| `clients`              | 1,243 |
| `job_categories`       | 2,374 |
| `candidate_assignments`| 2,671 |
| `document_templates`   | 18    |
| `document_fields`      | 202   |

No operator intervention required — a fresh server volume produces a
fully-populated portal on first boot.

### Core Entities
- **User** — Portal users with roles (admin / staff)
- **Client** — Foreign sponsor companies
- **Demand** — Job demand letters (1 client → many demands)
- **JobCategory** — Trade / profession entries inside a demand
- **Candidate** — Workers being recruited (full bio data)
- **CandidateAssignment** — Candidate ↔ JobCategory link with status
- **Agent** — Sub-agents / travel agencies
- **AgentCash** — Agent ledger transactions (debit/credit)
- **DocumentTemplate** — PDF template definition with background
- **DocumentField** — Coordinate-mapped field on a template
- **GeneratedDocument** — Log of every generated PDF

### Storage
- **SQLite** (default) — Zero-config, file-based
- **PostgreSQL / MySQL** — Just change `DATABASE_URL` in `.env`

---

## 🎨 Theme
Corporate **light SaaS theme** matching the reference video:
- Primary: Forest Green `#2E7D32` / Brand Blue `#1565C0`
- Sidebar: White, collapsible, icon-based navigation
- Status badges color-coded (Pending=Gray, Processing=Blue, Filled=Green, Expired=Red)
- Logo: **Dogar Trading Corporation** branding throughout

---

## 🛣️ Roadmap (Optional Enhancements)
- [ ] Multi-language (English / Urdu / Arabic)
- [ ] Email notifications (visa status changes)
- [ ] Two-Factor Authentication
- [x] **Audit log (who changed what)** — implemented (see below)
- [ ] Mobile-responsive PDA scanner integration
- [ ] Bulk import from Excel

---

## 🧭 Canonical Workflow Model

All candidate / demand / assignment status writes flow through a single source of
truth: **`app/core/workflow.py`**.

### Candidate Stages (10 canonical values)

| Value             | Display Label        | Pill Color | Meaning                                       |
| ----------------- | -------------------- | ---------- | --------------------------------------------- |
| `new`             | New                  | slate      | Candidate created, not yet assigned           |
| `docs_pending`    | Documents Pending    | amber      | Assigned to a trade, collecting docs          |
| `docs_complete`   | Documents Complete   | sky        | All passport/CNIC/photo etc. on file          |
| `protector`       | Protector            | indigo     | At Protector of Emigrants (Pakistani govt)    |
| `medical`         | Medical              | purple     | Medical examination phase                     |
| `visa_stamping`   | Visa Stamping        | blue       | Embassy / consulate visa stamping             |
| `e_number`        | E-Number             | violet     | Emigration number issued                      |
| `travel_ready`    | Travel Ready         | teal       | Ticket booked, ready to fly                   |
| `deployed`        | Deployed             | emerald    | Travelled, working overseas                   |
| `cancelled`       | Cancelled            | rose       | Terminal — withdrawn or rejected              |

The `protector` stage is intentionally retained — it represents the **Pakistani
Bureau of Emigration's Protector of Emigrants office**, a mandatory real-world
stage in the overseas-employment workflow.

State transitions are validated via `STAGE_TRANSITIONS` in `workflow.py`.
Legacy / variant status values from the migrated MySQL backup are auto-normalised
through `_LEGACY_STAGE_ALIASES` so old records keep working.

API: **`GET /api/candidates/stages`** returns the full canonical list + the
5-step wizard layout for any front-end to consume.

---

## 📝 Candidate Wizard — Resumable Multi-Step Intake

Candidate intake is a **7-step right-side drawer wizard** with **per-step
persistence**, so partially filled candidates can be resumed later from a
"Drafts" shelf at the top of `/candidates`.

| Step | Key                 | Captures                                              |
| ---- | ------------------- | ----------------------------------------------------- |
| 1    | `personal_info`     | Name (EN/Arabic), father, DOB, gender, photo, etc.    |
| 2    | `identification`    | Passport, CNIC, NADRA token, issuing authority        |
| 3    | `employment`        | Profession, permission no./date, qualification, salary|
| 4    | `next_of_kin`       | Emergency contact, relation, notes                    |
| 5    | `medical_protector` | GAMCA/medical center & dates, Protector no./date      |
| 6    | `visa_travel`       | Visa stamp date, E-number, destination, flight, ticket|
| 7    | `charge_summary`    | Charge breakdown, allowances, workflow stage          |

Each step is saved to `candidate_wizard_states.step_data` (JSON column keyed
`"1".."7"`) the moment the user clicks **Save & Next** or **Save Draft**.

### Wizard API

```
POST   /api/candidates/wizard                       Create a fresh draft
GET    /api/candidates/wizard/active                List unfinished drafts
GET    /api/candidates/wizard/{id}                  Load one draft
PATCH  /api/candidates/wizard/{id}/step/{n}         Save step n
POST   /api/candidates/wizard/{id}/finalize         Materialise into Candidate row
POST   /api/candidates/wizard/{id}/reopen           Re-open a finalised candidate
POST   /api/candidates/wizard/from-candidate/{cid}  Edit an existing candidate
DELETE /api/candidates/wizard/{id}                  Discard a draft
```

Every wizard action emits an audit event (see below).

---

## 🔁 Assign / Unassign — Cleanup + Audit

Workflow linkage between **Candidates ↔ Demand Trades** is fully transactional:

### `POST /api/demands/trades/{trade_id}/assign`
1. Inserts a `candidate_assignment` row.
2. If candidate was `new`, auto-transitions to `docs_pending`.
3. Emits `audit_log` entries: `assignment.assign` + `candidate.stage_change`.

### `DELETE /api/demands/assignments/{assignment_id}` (full cleanup)
1. Captures snapshot of the assignment.
2. Deletes the `candidate_assignment` row.
3. Clears any `GeneratedDocument.demand_id` / `.job_category_id` that referenced
   this trade for this candidate.
4. If the candidate has no remaining assignments **and** is not in a terminal
   stage (`deployed` / `cancelled`), resets `candidate.status` back to `new`.
5. Returns `{ "cleared_fields": [...] }` so the UI can show what changed.
6. Emits `audit_log` entries: `assignment.unassign` + `candidate.stage_change`
   (when status was reset).

The **Demand-file → Trades** tab now renders rich candidate cards (photo,
name + Arabic name, S/O father, status pill, passport, phone) instead of
empty placeholders. Unassign confirms with a dialog that explains the
cleanup.

---

## 📋 Audit Log

Every workflow-impacting action writes a row to `audit_logs` with:

| Field        | Purpose                                            |
| ------------ | -------------------------------------------------- |
| `entity_type`| `candidate` / `demand` / `assignment` / `wizard`   |
| `entity_id`  | PK of the affected row                             |
| `action`     | `create` / `update` / `assign` / `unassign` / `stage_change` / `wizard_step` / `wizard_finalize` / `wizard_reopen` |
| `actor_id` / `actor_email` | Who did it                           |
| `before_json` / `after_json` | Full snapshots for diffing         |
| `ip` / `ua`  | Request metadata                                   |
| `occurred_at`| UTC timestamp                                      |

API: **`GET /api/candidates/{candidate_id}/audit`** returns the full audit
trail for a candidate, surfaced in the Candidate drawer's **Activity** tab.

---

## 📜 License
Proprietary — Dogar Trading Corporation © 2026

---

**Deployment Status:** ✅ Production-ready • Last updated: 2026-05-29
**Data Status:** 2,662 candidates · 2,249 demands · 1,243 clients · 2,527 assignments (all migrated from legacy MySQL backup, status values normalised to canonical workflow)
**Tech Stack:** FastAPI · SQLAlchemy 2.0 · Jinja2 · TailwindCSS · Alpine.js · ReportLab
