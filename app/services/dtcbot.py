"""DtcBot — Dogar Trading Corporation AI assistant.

This is a rule-based / pattern-driven portal-workflow assistant.  It is
trained on:
  - the sidebar IA (Clients, Demand Files, Candidates, Documents, ...)
  - the field schemas of each model
  - common OEP recruiter tasks (create file, look up, generate PDF, OCR)

Design goals (per user request):
  - Auto-pilot: when given a passport image, run OCR and SHOW the
    extracted fields back to the user ready to paste into a form.
  - Conversational guidance: "How do I add a new demand file?" →
    walks through the wizard step-by-step.
  - Quick lookup: "find candidate ALI HASSAN" → returns links.
  - Always grounded: it never invents data — it queries the live DB.

The result returned by `answer()` is a structured dict so the frontend
can render rich responses (links, action buttons, OCR field tables).
"""
import re
from typing import Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models import (
    Client, Demand, Candidate, DocumentTemplate, Agent,
)
from app.models.lookups import CompanySettings


# ---------------------------------------------------------------------------
# Knowledge base — short, accurate workflow instructions
# ---------------------------------------------------------------------------
HELP_TOPICS = {
    "add_demand": {
        "title": "How to add a new demand file",
        "steps": [
            "Open the sidebar → Demand Files.",
            "Click the blue 'New Demand' button (top-right).",
            "Pick a Client (foreign sponsor) from the dropdown.",
            "The File Number is auto-generated as DTC/786/<seq> (e.g. DTC/786/8186) — leave blank or override.",
            "Fill Basic Info (receiving date, permission no), Sponsor Info, then Visa Info.",
            "Click Save Demand — the new file appears at the TOP of the list (newest first).",
        ],
        "links": [{"label": "Open Demand Files", "href": "/demands"}],
    },
    "add_candidate": {
        "title": "How to add a new candidate (worker)",
        "steps": [
            "Open the sidebar → Candidates.",
            "Click 'New Candidate' — a right-side drawer slides in.",
            "Step 1 · Personal Info — full name, parents, DOB, address.",
            "Step 2 · Identification — passport, CNIC. Use the green 'Scan Passport' button to auto-fill from an uploaded passport image.",
            "Step 3 · Employment — profession, salary, permission no.",
            "Step 4 · Next of Kin — name, NIC, relation.",
            "Click Save Candidate.",
        ],
        "links": [{"label": "Open Candidates", "href": "/candidates"}],
    },
    "add_client": {
        "title": "How to add a new client (foreign sponsor)",
        "steps": [
            "Open the sidebar → Clients.",
            "Click 'Add Client'.",
            "Enter Company Name (required), client type, country, contact phone & email.",
            "Add sponsor name (English + Arabic), sponsor address, full street/city/state.",
            "Optionally set Opening Balance and direction (Debit / Credit) with date.",
            "Click Save — the new client appears at the top of the table.",
        ],
        "links": [{"label": "Open Clients", "href": "/clients"}],
    },
    "generate_pdf": {
        "title": "How to generate a PDF (filled form)",
        "steps": [
            "Open the sidebar → Documents.",
            "Click any System Template pill at the top, OR click the printer icon next to a template in the table.",
            "First time: place fields on the form background (Customize page).",
            "Pick a record (candidate / demand / client) when generating.",
            "Download the PDF — it overlays your record's data on the real form background.",
        ],
        "links": [{"label": "Open Documents", "href": "/documents"}],
    },
    "scan_passport": {
        "title": "Passport OCR Scanner",
        "steps": [
            "On the Candidate wizard (Step 2 · Identification), click 'Scan Passport'.",
            "Upload a clear photo or scan of the passport bio-data page.",
            "DtcBot reads the MRZ (machine-readable zone) and fills: Full Name, Passport No, DOB, Sex, Nationality, Expiry Date, Issuing Authority, and CNIC (if present in the personal-number field).",
            "Review the auto-filled values and click Save Candidate.",
        ],
        "links": [{"label": "Open Candidates", "href": "/candidates"}],
    },
    "file_number_format": {
        "title": "File Number Format (Dogar Trading)",
        "steps": [
            "Every demand file uses the format DTC/786/<seq> where <seq> is a running integer.",
            "Internally the sequence is stored as a raw integer (e.g. 8185). On screen and on printed receipts it is rendered as DTC/786/8185.",
            "Each new demand auto-increments from the highest existing number — current real range is 5766 → 8185, so the next file becomes DTC/786/8186.",
            "You can override the auto value when creating a demand; typing either '9000' or 'DTC/786/9000' is accepted (both stored as 9000).",
            "The prefix and starting point are configurable in Settings → General (file_prefix = 'DTC/786/' and starting_point).",
        ],
        "links": [{"label": "Open Settings", "href": "/settings"}],
    },
    "newest_first": {
        "title": "List ordering",
        "steps": [
            "Demand Files, Clients, Candidates: newest record on top of page 1.",
            "Oldest records appear at the end (ORDER BY id DESC).",
        ],
    },
}


PORTAL_NAV_HINTS = [
    ("dashboard", "/dashboard", "Dashboard — overview stats"),
    ("user", "/users", "Manage Users"),
    ("login history", "/login-history", "Login History"),
    ("role", "/roles", "Roles & Permissions"),
    ("client", "/clients", "Clients (sponsors)"),
    ("demand", "/demands", "Demand Files"),
    ("candidate", "/candidates", "Candidates (workers)"),
    ("visa categor", "/visa-categories", "Visa Categories"),
    ("embassy", "/embassies", "Embassies"),
    ("medical", "/medical-centers", "Medical Centers"),
    ("contact", "/contacts", "Contacts"),
    ("document", "/documents", "Documents (PDF templates)"),
    ("setting", "/settings", "General Settings"),
    ("depositor", "/depositors", "Depositors"),
    ("service charge", "/service-charges", "Service Charges"),
    ("agent", "/agents", "Sub-Agents"),
    ("support", "/contact-support", "Contact HussnainTechVertex Support"),
]


GREETING = [
    "Hi! I'm DtcBot. I can help you create demand files, find clients & candidates, generate documents, or scan a passport. What would you like to do?",
    "Welcome! Try: 'Add a new demand file', 'Find candidate by passport AB1234567', or upload a passport image and I'll auto-fill the data.",
]


def _intro() -> Dict[str, Any]:
    return {
        "type": "text",
        "text": GREETING[0],
        "quick_replies": [
            {"label": "Add demand file", "msg": "How do I add a new demand file?"},
            {"label": "Add candidate", "msg": "How do I add a new candidate?"},
            {"label": "Generate PDF", "msg": "How do I generate a PDF?"},
            {"label": "Scan a passport", "msg": "How do I scan a passport?"},
        ],
    }


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------
def _topic(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(hi|hello|hey|salam|salaam|assalam)\b", t):
        return "greet"
    if "demand" in t and any(w in t for w in ("add", "new", "create", "make", "how")):
        return "add_demand"
    if any(w in t for w in ("add", "new", "create")) and any(w in t for w in ("candidate", "worker", "employee")):
        return "add_candidate"
    if any(w in t for w in ("add", "new", "create")) and "client" in t:
        return "add_client"
    if "passport" in t and any(w in t for w in ("scan", "ocr", "read", "extract", "upload")):
        return "scan_passport"
    if any(w in t for w in ("pdf", "generate", "print", "document")) and any(w in t for w in ("how", "make", "create", "generate")):
        return "generate_pdf"
    if "file" in t and ("format" in t or "number" in t or "dtc" in t):
        return "file_number_format"
    if any(w in t for w in ("order", "sort", "newest", "latest")):
        return "newest_first"
    if any(w in t for w in ("help", "what can you do", "menu")):
        return "help"
    return "search"


# ---------------------------------------------------------------------------
# Search across portal entities
# ---------------------------------------------------------------------------
def _search(db: Session, q: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not q or len(q) < 2:
        return results
    like = f"%{q}%"

    # Demand by file_number / permission_no / sponsor
    for d in db.query(Demand).filter(
        or_(Demand.file_number.ilike(like), Demand.permission_no.ilike(like),
            Demand.sponsor_name.ilike(like), Demand.visa_number.ilike(like))
    ).order_by(Demand.id.desc()).limit(5).all():
        results.append({
            "kind": "demand", "title": f"Demand {d.file_number}",
            "subtitle": f"{d.sponsor_name or '—'} · {d.country or '—'}",
            "href": f"/demands/{d.id}",
        })

    # Candidate by name / passport / cnic / profession / phone / father / district
    for c in db.query(Candidate).filter(
        or_(Candidate.full_name.ilike(like), Candidate.passport_no.ilike(like),
            Candidate.cnic.ilike(like), Candidate.profession.ilike(like),
            Candidate.phone.ilike(like), Candidate.father_name.ilike(like),
            Candidate.district.ilike(like))
    ).order_by(Candidate.id.desc()).limit(8).all():
        results.append({
            "kind": "candidate", "title": c.full_name,
            "subtitle": f"{c.profession or '—'} · Passport: {c.passport_no or '—'} · CNIC: {c.cnic or '—'}",
            "href": f"/candidates",
        })

    # Client by company name / country / sponsor / email
    for cl in db.query(Client).filter(
        or_(Client.company_name.ilike(like), Client.country.ilike(like),
            Client.sponsor_name.ilike(like), Client.email.ilike(like))
    ).order_by(Client.id.desc()).limit(5).all():
        results.append({
            "kind": "client", "title": cl.company_name,
            "subtitle": f"{cl.country or '—'} · {cl.phone or '—'}",
            "href": f"/clients/{cl.id}",
        })

    # Templates
    for t in db.query(DocumentTemplate).filter(DocumentTemplate.name.ilike(like)).limit(5).all():
        results.append({
            "kind": "template", "title": t.name,
            "subtitle": t.description or "PDF template",
            "href": f"/documents/customize/{t.id}",
        })
    return results


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def answer(db: Session, message: str, history=None, user=None) -> Dict[str, Any]:
    message = (message or "").strip()
    if not message:
        return _intro()

    # ---- Compute-agent FIRST: actually PERFORM data-entry actions offline ----
    # (create candidate, set/update field, assign trade, paste data block).
    # This works even when the external LLM key is missing/expired.
    try:
        from app.services.dtcbot_agent import try_execute
        action_resp = try_execute(db, message, user=user)
        if action_resp is not None:
            return action_resp
    except Exception as _act_err:
        import logging
        logging.getLogger("dtcbot").warning("Compute-agent error: %s", _act_err)
        try:
            db.rollback()
        except Exception as _rb_err:   # noqa: BLE001 — SQLAlchemy rollback can raise driver-specific errors
            import logging
            logging.getLogger("dtcbot").debug("rollback after compute-agent error failed: %s", _rb_err)

    # First try the LLM brain (real-time trained, tool-calling, grounded on live DB)
    try:
        from app.services.dtcbot_llm import llm_answer
        llm_resp = llm_answer(db, message, history=history, user=user)
        if llm_resp and not llm_resp.get("_fallback"):
            return llm_resp
    except Exception as _llm_err:
        import logging
        logging.getLogger("dtcbot").warning("LLM brain unavailable, using rule-based fallback: %s", _llm_err)

    # ---- Rule-based fallback (still works offline) ----

    topic = _topic(message)

    if topic == "greet":
        return _intro()

    if topic == "help":
        return {
            "type": "help_menu",
            "text": "Here's what I can help with:",
            "items": [
                {"label": "Add a new demand file", "msg": "How do I add a new demand file?"},
                {"label": "Add a new candidate", "msg": "How do I add a new candidate?"},
                {"label": "Add a new client", "msg": "How do I add a new client?"},
                {"label": "Generate a PDF", "msg": "How do I generate a PDF?"},
                {"label": "Scan a passport", "msg": "How do I scan a passport?"},
                {"label": "File number format", "msg": "What is the file number format?"},
                {"label": "List ordering", "msg": "What order are demand files in?"},
            ],
        }

    if topic in HELP_TOPICS:
        h = HELP_TOPICS[topic]
        return {
            "type": "guide",
            "title": h["title"],
            "steps": h["steps"],
            "links": h.get("links", []),
        }

    # Search intent — try entity search first
    results = _search(db, message)
    if results:
        return {
            "type": "results",
            "text": f"I found {len(results)} match(es) for '{message}':",
            "results": results,
        }

    # Navigation hint
    msg_lower = message.lower()
    for keyword, path, label in PORTAL_NAV_HINTS:
        if keyword in msg_lower:
            return {
                "type": "text",
                "text": f"Sure — opening **{label}**.",
                "links": [{"label": label, "href": path}],
            }

    # Final fallback
    return {
        "type": "text",
        "text": "I'm not sure I got that. Try asking 'How do I add a candidate?' or search for a passport number, CNIC, or company name.",
        "quick_replies": [
            {"label": "Help menu", "msg": "help"},
            {"label": "Add demand", "msg": "How do I add a new demand file?"},
            {"label": "Scan passport", "msg": "How do I scan a passport?"},
        ],
    }


def handle_uploaded_document(
    filename: str,
    ocr_result: Dict[str, Any],
    *,
    db=None,
    user=None,
    auto_create: bool = True,
) -> Dict[str, Any]:
    """Build a chat response describing a document the user uploaded
    (passport OCR result).

    **Vercel-v0-style actionable agent**: when ``db`` is supplied and
    ``auto_create`` is True (the default), the bot does NOT just return
    text — it actively:

      1. Maps the OCR fields to Candidate columns via dtcbot_agent
      2. Looks up an existing candidate by passport/CNIC first
      3. Creates the candidate row (or updates if it already exists)
      4. Returns an ``action`` response telling the front-end to
         navigate straight to the candidate's edit screen with the
         remaining fields pre-filled — completing the end-to-end loop
         from raw image upload to data-entry without any further user
         clicks.

    If ``db`` is None or auto-creation fails for any reason we fall
    back to the legacy ``ocr_result`` response so the user can still
    open the form manually.
    """
    if not ocr_result or not ocr_result.get("ok"):
        return {
            "type": "text",
            "text": f"I couldn't extract data from **{filename}**. Please upload a clearer image.",
        }
    f = ocr_result.get("fields", {}) or {}
    method = ocr_result.get("method", "text")
    method_label = "MRZ scan" if method == "mrz" else (
        "AI vision" if method == "ai" else "text scan"
    )

    # ------------------------------------------------------------------
    # ACTIONABLE PATH — actually create / update the candidate now.
    # ------------------------------------------------------------------
    if db is not None and auto_create:
        try:
            from app.services import dtcbot_agent as agent

            # Coerce + whitelist all OCR fields into valid Candidate columns.
            clean: Dict[str, Any] = {}
            for k, v in f.items():
                if v in (None, "", []):
                    continue
                col = k if k in agent.VALID_COLUMNS else agent._normalize_key(k)
                if col and col in agent.VALID_COLUMNS:
                    clean[col] = agent._coerce(col, v)

            if not clean:
                # OCR returned nothing useful — fall through to guidance.
                raise RuntimeError("no_clean_fields")

            # Try to find an existing candidate first (by passport / CNIC)
            existing_ref = clean.get("passport_no") or clean.get("cnic")
            existing = None
            if existing_ref:
                existing = agent._find_candidate(db, str(existing_ref))

            if existing:
                # UPDATE branch
                res = agent.update_candidate(
                    db, str(existing.id),
                    {k: v for k, v in clean.items() if k != "id"},
                    user=user,
                )
                if res.get("ok"):
                    cand = res.get("candidate", {})
                    cid = cand.get("id") or existing.id
                    name = cand.get("full_name") or clean.get("full_name") or "candidate"
                    return {
                        "type": "action",
                        "verb": "updated",
                        "text": (
                            f"✅ Found existing candidate **{name}** (#{cid}) "
                            f"and updated {len(res.get('updated_fields') or res.get('fields_set') or [])} "
                            f"field(s) from **{filename}** via {method_label}. "
                            f"Opening the candidate's edit screen now…"
                        ),
                        "fields": clean,
                        "candidate_id": cid,
                        "url": f"/candidates?edit={cid}&flyout=0",
                        "navigate": True,
                        "method": method_label,
                    }
            else:
                # CREATE branch — requires at least full_name
                if not clean.get("full_name"):
                    # Try to synthesize a name from passport surname/given if any
                    surname = (f.get("surname") or "").strip()
                    given = (f.get("given_names") or f.get("given_name") or "").strip()
                    full = (f"{given} {surname}").strip() or (f.get("name") or "").strip()
                    if full:
                        clean["full_name"] = full.upper()

                if clean.get("full_name"):
                    res = agent.create_candidate(db, clean, user=user)
                    if res.get("ok"):
                        cand = res.get("candidate", {})
                        cid = cand.get("id")
                        name = cand.get("full_name") or clean.get("full_name") or "candidate"
                        return {
                            "type": "action",
                            "verb": "created",
                            "text": (
                                f"✅ Created candidate **{name}** (#{cid}) from "
                                f"**{filename}** via {method_label} — "
                                f"{len(res.get('fields_set') or [])} field(s) saved. "
                                f"Opening the record now so you can review and complete "
                                f"the remaining steps…"
                            ),
                            "fields": clean,
                            "candidate_id": cid,
                            "url": f"/candidates?edit={cid}&flyout=0",
                            "navigate": True,
                            "method": method_label,
                        }
        except Exception as exc:
            # Auto-create failed — fall through to the legacy guidance
            # response so the user still sees the OCR data.
            try:
                if db is not None:
                    db.rollback()
            except Exception as _rb_err:   # noqa: BLE001
                import logging
                logging.getLogger(__name__).debug("rollback after auto-create failed: %s", _rb_err)
            import logging
            logging.getLogger(__name__).warning(
                "DtcBot auto-create failed for %s: %s", filename, exc
            )

    # ------------------------------------------------------------------
    # FALLBACK — show OCR fields and let the user open the form.
    # ------------------------------------------------------------------
    lines = [
        f"Successfully read **{filename}** ({method_label}).",
        "Here's what I extracted — I'll open the New Candidate wizard "
        "with these fields pre-filled. Click Save to commit the record:",
    ]
    return {
        "type": "ocr_result",
        "text": "\n".join(lines),
        "fields": f,
        "links": [{"label": "Open Candidates → New Candidate", "href": "/candidates?wizard=1"}],
    }
