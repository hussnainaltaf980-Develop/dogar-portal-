"""DtcBot LLM Brain — Real-time, practically-trained AI assistant.

Unlike the old rule-based dtcbot.py, this version uses an actual LLM
(GPT-5 family via the Genspark LLM proxy) with **function/tool calling**
that lets the model query the live SQLite database to give grounded,
data-backed answers.

Key capabilities:
  - Live DB lookups: candidates, demands, clients, payments, templates
  - Workflow guidance trained on the OEP/DTC sidebar IA
  - OCR result narration
  - Multi-turn conversation memory (per-session)
  - Safe fallback to the rule-based bot when the LLM is unreachable

Environment:
  - OPENAI_API_KEY / OPENAI_BASE_URL (auto-loaded from ~/.genspark_llm.yaml)
  - DTCBOT_MODEL env var to override model (default: gpt-5-mini)
"""
from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.models import Client, Demand, Candidate, DocumentTemplate
from app.models.candidate import CandidateAssignment
from app.models.demand import JobCategory
from app.models.lookups import ClientStatement, CompanySettings

logger = logging.getLogger("dtcbot")

# ---------------------------------------------------------------------------
# OpenAI client (lazy, optional)
# ---------------------------------------------------------------------------
_CLIENT = None
_CLIENT_INIT_TRIED = False


def _load_llm_config() -> Dict[str, str]:
    """Load API key + base URL from env or ~/.genspark_llm.yaml."""
    cfg = {
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "base_url": os.environ.get("OPENAI_BASE_URL", ""),
        "fallback_keys": [],
    }
    try:
        import yaml  # type: ignore
        yml_path = os.path.expanduser("~/.genspark_llm.yaml")
        if os.path.exists(yml_path):
            with open(yml_path) as f:
                data = yaml.safe_load(f) or {}
            oa = data.get("openai") or {}
            if not cfg["api_key"]:
                cfg["api_key"] = oa.get("api_key", "")
            if not cfg["base_url"]:
                cfg["base_url"] = oa.get("base_url", "")
            # Extra keys the proxy may rotate through when the primary expires.
            fb = oa.get("fallback_keys") or []
            if isinstance(fb, list):
                cfg["fallback_keys"] = [k for k in fb if k]
    except Exception as e:
        logger.warning("Could not load LLM yaml config: %s", e)
    return cfg


def all_api_keys() -> list:
    """Return [primary, *fallbacks] de-duplicated — used by callers that want
    to retry on an expired/invalid token (e.g. passport vision OCR)."""
    cfg = _load_llm_config()
    keys = [cfg.get("api_key", "")] + list(cfg.get("fallback_keys", []))
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _get_client():
    """Return a cached OpenAI client, or None if LLM is unavailable."""
    global _CLIENT, _CLIENT_INIT_TRIED
    if _CLIENT is not None or _CLIENT_INIT_TRIED:
        return _CLIENT
    _CLIENT_INIT_TRIED = True
    try:
        from openai import OpenAI  # type: ignore
        cfg = _load_llm_config()
        if not cfg["api_key"]:
            logger.warning("DtcBot LLM disabled: no API key.")
            return None
        _CLIENT = OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"] or None,
        )
        logger.info("DtcBot LLM client initialised (base_url=%s).", cfg["base_url"])
        return _CLIENT
    except Exception as e:
        logger.warning("Could not init OpenAI client: %s", e)
        return None


# ---------------------------------------------------------------------------
# Database tools (callable by the LLM via tool/function calling)
# ---------------------------------------------------------------------------
def _tool_portal_stats(db: Session, **_) -> Dict[str, Any]:
    """High-level dashboard counts."""
    return {
        "candidates_total":     db.query(func.count(Candidate.id)).scalar() or 0,
        "demands_total":        db.query(func.count(Demand.id)).scalar() or 0,
        "clients_total":        db.query(func.count(Client.id)).scalar() or 0,
        "assignments_total":    db.query(func.count(CandidateAssignment.id)).scalar() or 0,
        "doc_templates_total":  db.query(func.count(DocumentTemplate.id)).scalar() or 0,
    }


def _tool_search_candidates(db: Session, query: str = "", limit: int = 10, **_) -> Dict[str, Any]:
    """Search candidates by name / passport / cnic / phone / father / profession."""
    q = (query or "").strip()
    limit = max(1, min(int(limit or 10), 25))
    items = []
    qs = db.query(Candidate)
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(
            Candidate.full_name.ilike(like),
            Candidate.passport_no.ilike(like),
            Candidate.cnic.ilike(like),
            Candidate.phone.ilike(like),
            Candidate.father_name.ilike(like),
            Candidate.profession.ilike(like),
        ))
    for c in qs.order_by(Candidate.id.desc()).limit(limit).all():
        items.append({
            "id": c.id,
            "full_name": c.full_name,
            "father_name": c.father_name,
            "passport_no": c.passport_no,
            "cnic": c.cnic,
            "profession": c.profession,
            "phone": c.phone,
            "status": getattr(c, "status", None),
            "url": "/candidates",
        })
    return {"count": len(items), "items": items}


def _tool_search_demands(db: Session, query: str = "", limit: int = 10, **_) -> Dict[str, Any]:
    """Search demand files by file number / sponsor / country / embassy."""
    q = (query or "").strip()
    limit = max(1, min(int(limit or 10), 25))
    qs = db.query(Demand)
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(
            Demand.file_number.ilike(like),
            Demand.sponsor_name.ilike(like),
            Demand.country.ilike(like),
            Demand.embassy.ilike(like),
        ))
    items = []
    for d in qs.order_by(Demand.id.desc()).limit(limit).all():
        items.append({
            "id": d.id,
            "file_number": d.file_number,
            "sponsor_name": d.sponsor_name,
            "country": d.country,
            "embassy": d.embassy,
            "status": getattr(d, "status", None),
            "url": f"/demands/{d.id}",
        })
    return {"count": len(items), "items": items}


def _tool_search_clients(db: Session, query: str = "", limit: int = 10, **_) -> Dict[str, Any]:
    """Search clients by company / country / sponsor / email."""
    q = (query or "").strip()
    limit = max(1, min(int(limit or 10), 25))
    qs = db.query(Client)
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(
            Client.company_name.ilike(like),
            Client.country.ilike(like),
            Client.sponsor_name.ilike(like),
            Client.email.ilike(like),
        ))
    items = []
    for c in qs.order_by(Client.id.desc()).limit(limit).all():
        items.append({
            "id": c.id,
            "company_name": c.company_name,
            "country": c.country,
            "sponsor_name": c.sponsor_name,
            "phone": c.phone,
            "email": c.email,
            "url": f"/clients/{c.id}",
        })
    return {"count": len(items), "items": items}


def _tool_demand_payments(db: Session, demand_id: int, **_) -> Dict[str, Any]:
    """Get payment summary for a specific demand file."""
    try:
        did = int(demand_id)
    except Exception:
        return {"error": "demand_id must be an integer"}
    d = db.query(Demand).filter(Demand.id == did).first()
    if not d:
        return {"error": f"Demand #{did} not found"}
    rows = db.query(ClientStatement).filter(ClientStatement.demand_id == did)\
              .order_by(ClientStatement.entry_date.desc()).limit(25).all()
    tot_d = sum(float(r.debit or 0) for r in rows)
    tot_c = sum(float(r.credit or 0) for r in rows)
    return {
        "demand_id": d.id,
        "file_number": d.file_number,
        "embassy": d.embassy,
        "total_invoiced": round(tot_d, 2),
        "total_received": round(tot_c, 2),
        "outstanding": round(tot_d - tot_c, 2),
        "items": [
            {
                "id": r.id,
                "date": str(r.entry_date) if r.entry_date else None,
                "receipt_no": r.receipt_no,
                "method": r.payment_method,
                "debit": float(r.debit or 0),
                "credit": float(r.credit or 0),
                "description": r.description,
            } for r in rows
        ],
        "url": f"/demands/{d.id}",
    }


def _tool_list_templates(db: Session, category: str = "", **_) -> Dict[str, Any]:
    """List available document templates, optionally filtered by category."""
    qs = db.query(DocumentTemplate)
    if category:
        qs = qs.filter(DocumentTemplate.category == category)
    items = [{"id": t.id, "name": t.name, "category": t.category,
              "data_source": t.data_source, "url": f"/documents/customize/{t.id}"}
             for t in qs.order_by(DocumentTemplate.name.asc()).limit(50).all()]
    return {"count": len(items), "items": items}


def _tool_create_demand(db: Session, client_id: int, sponsor_name: str = "", country: str = "", embassy: str = "", user=None, **_) -> Dict[str, Any]:
    """Create a new demand file."""
    if not user:
        return {"error": "Authentication required to create demand."}
    try:
        from app.api.endpoints.demands import _next_file_number, _get_prefix, normalize_demand_status, display_file_number
        from app.services import audit as audit_svc
        from app.models.lookups import AuditEntity, AuditAction
        file_number = _next_file_number(db)
        obj = Demand(
            file_number=file_number,
            demand_code=file_number,
            client_id=client_id,
            sponsor_name=sponsor_name,
            country=country,
            embassy=embassy,
            status=normalize_demand_status("Active"),
            created_by_id=user.id
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        
        # log 
        audit_svc.log_event(
            db, entity_type=AuditEntity.DEMAND.value, entity_id=obj.id,
            action=AuditAction.CREATE.value, actor=user, request=None,
            summary=f"Created Demand File (Bot) {display_file_number(obj.file_number, _get_prefix(db))}",
            after={"file_number": obj.file_number}
        )
        
        return {
            "success": True,
            "id": obj.id,
            "file_number": display_file_number(obj.file_number, _get_prefix(db)),
            "url": f"/demands/{obj.id}"
        }
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

def _tool_create_candidate(db: Session, full_name: str, passport_no: str = "", cnic: str = "", profession: str = "", user=None, **_) -> Dict[str, Any]:
    """Create a new candidate."""
    if not user:
        return {"error": "Authentication required to create candidate."}
    try:
        from app.services import audit as audit_svc
        from app.models.lookups import AuditEntity, AuditAction
        
        obj = Candidate(
            full_name=full_name,
            passport_no=passport_no,
            cnic=cnic,
            profession=profession,
            status="New",
            created_by_id=user.id
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        
        audit_svc.log_event(
            db, entity_type=AuditEntity.CANDIDATE.value, entity_id=obj.id,
            action=AuditAction.CREATE.value, actor=user, request=None,
            summary=f"Created candidate (Bot) '{obj.full_name}'",
            after={"full_name": obj.full_name}
        )
        
        return {
            "success": True,
            "id": obj.id,
            "full_name": obj.full_name,
            "url": f"/candidates?open={obj.id}"
        }
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

def _tool_assign_candidate(db: Session, trade_id: int, candidate_id: int, user=None, **_) -> Dict[str, Any]:
    """Assign a candidate to a demand file's trade."""
    if not user:
        return {"error": "Authentication required."}
    try:
        from app.models.demand import JobCategory
        from app.models.candidate import CandidateAssignment
        from app.services.workflow import CandidateStage
        from app.services import audit as audit_svc
        
        jc = db.query(JobCategory).filter(JobCategory.id == trade_id).first()
        if not jc:
            return {"error": f"Trade {trade_id} not found."}
        cand = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        if not cand:
            return {"error": f"Candidate {candidate_id} not found."}
            
        a = CandidateAssignment(
            candidate_id=candidate_id,
            job_category_id=trade_id,
            status=CandidateStage.DOCS_PENDING.value,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        
        audit_svc.log_assign(
            db, candidate=cand, job_category=jc, assignment_obj=a,
            inherited_fields={}, actor=user, request=None
        )
        return {
            "success": True,
            "assignment_id": a.id,
            "candidate_name": cand.full_name,
            "trade": jc.trade,
            "demand_id": jc.demand_id,
            "url": f"/demands/{jc.demand_id}"
        }
    except Exception as e:
        db.rollback()
        return {"error": str(e)}

TOOLS_REGISTRY = {
    "portal_stats":       _tool_portal_stats,
    "search_candidates":  _tool_search_candidates,
    "search_demands":     _tool_search_demands,
    "search_clients":     _tool_search_clients,
    "demand_payments":    _tool_demand_payments,
    "list_templates":     _tool_list_templates,
    "create_demand":      _tool_create_demand,
    "create_candidate":   _tool_create_candidate,
    "assign_candidate":   _tool_assign_candidate,
}


# OpenAI tools schema (function-calling)
TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "portal_stats",
            "description": "Get high-level portal counts: total candidates, demands, clients, assignments, document templates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_candidates",
            "description": "Search the candidate (worker) database by name, passport, CNIC, phone, father name, or profession. Use when the user mentions a person, asks about workers, or wants to look up an employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term (name, passport, cnic, phone, profession)"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_demands",
            "description": "Search demand files (job orders) by file number, sponsor name, country, or embassy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_clients",
            "description": "Search foreign-sponsor clients by company name, country, sponsor name, or email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "demand_payments",
            "description": "Get payment / invoice summary and recent items for a specific demand file (by demand_id). Use when the user asks about money, payments, invoices, balance, outstanding, or receipts for a demand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "demand_id": {"type": "integer"},
                },
                "required": ["demand_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_templates",
            "description": "List available document templates. Optional category filter: 'permission', 'protector', or 'visa_process'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["", "permission", "protector", "visa_process"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_demand",
            "description": "Create a new demand file in the system. Use this when the user asks to add or create a demand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "The ID of the client sponsor."},
                    "sponsor_name": {"type": "string"},
                    "country": {"type": "string"},
                    "embassy": {"type": "string"}
                },
                "required": ["client_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_candidate",
            "description": "Create a new candidate/worker in the system. Use this when the user asks to add a candidate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string", "description": "Full name of the candidate."},
                    "passport_no": {"type": "string"},
                    "cnic": {"type": "string"},
                    "profession": {"type": "string"}
                },
                "required": ["full_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assign_candidate",
            "description": "Assign a candidate to a demand file's trade. Use this when the user asks to assign a candidate to a demand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_id": {"type": "integer", "description": "The ID of the trade/job category within the demand file."},
                    "candidate_id": {"type": "integer", "description": "The ID of the candidate."}
                },
                "required": ["trade_id", "candidate_id"]
            }
        }
    }
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are **DtcBot**, the live AI operations assistant inside the Dogar Trading Corporation overseas employment SaaS portal.

YOUR ROLE
- You help recruiters, accountants and managers do their day-to-day work inside the portal.
- You have **read and write access to the live database** via tool calls. Always call the right tool to ground your answer in real data or execute actions like creating records. Never invent IDs, numbers or URLs.
- You act as a compute engine and autopilot. If the user asks to do something (e.g. create a demand file), use your tool calls to do it!
- You are practically trained on the real workflow, not a generic chatbot.

THE PORTAL (Dogar Trading Corporation)
- Sidebar: Dashboard, Clients, Demand Files, Candidates, Documents, Reports, Settings.
- **Clients** = foreign sponsors (companies abroad). Each client has contacts, statement, demand files.
- **Demand Files** = job orders from a client. Format `DTC/786/<seq>` where `<seq>` is a running integer (e.g. `DTC/786/8185`). 
- **Candidates** = workers being placed abroad. 
- **Documents** are NOT generated — they use a **real-image data-overlay engine** (Pillow + ReportLab) that prints a candidate/demand/client's data onto the actual real document image at coordinate-mapped fields. The button is **Print PDF**, not "Generate".

STYLE
- Be concise. Use **bold** for key terms.
- When you find or create records, list them with their clickable portal links (use the `url` field from tool results).
- If creating a file/record, just do it. Report success and the URL immediately.
- If you don't have enough info, ask ONE short clarifying question.

OUTPUT FORMAT
- Reply in clean Markdown. Use headings sparingly; prefer short paragraphs and bullet lists.
- Never expose internal table names, SQL, or raw JSON — translate them into natural language.
"""


# ---------------------------------------------------------------------------
# LLM driver
# ---------------------------------------------------------------------------
def llm_answer(db: Session, message: str,
               history: Optional[List[Dict[str, str]]] = None, user=None) -> Dict[str, Any]:
    """Call the LLM with tool-calling. Returns a structured response dict."""
    client = _get_client()
    if client is None:
        return {"_fallback": True}

    model = os.environ.get("DTCBOT_MODEL", "gpt-5-mini")
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        for h in history[-8:]:  # last 8 turns max
            role = h.get("role")
            content = h.get("content") or ""
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": message})

    used_tools: List[Dict[str, Any]] = []
    grounded_data: Dict[str, Any] = {}

    # Allow up to 4 tool-call rounds
    for _round in range(4):
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model=model,
                messages=msgs,
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
            )
            logger.info("DtcBot LLM round %d in %.2fs", _round, time.time() - t0)
        except Exception as e:
            logger.exception("LLM call failed: %s", e)
            return {"_fallback": True, "_error": str(e)}

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            # Append assistant turn with tool_calls
            msgs.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    } for tc in tool_calls
                ],
            })
            # Execute each tool
            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                fn = TOOLS_REGISTRY.get(fn_name)
                if not fn:
                    result = {"error": f"Unknown tool: {fn_name}"}
                else:
                    try:
                        result = fn(db, user=user, **fn_args)
                    except Exception as ex:
                        logger.exception("Tool %s failed", fn_name)
                        result = {"error": str(ex)}
                used_tools.append({"name": fn_name, "args": fn_args})
                # Stash collated grounded data for the frontend
                grounded_data.setdefault(fn_name, []).append(result)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:8000],
                })
            continue  # loop for next round

        # No tool calls — final answer
        final_text = (msg.content or "").strip()
        return {
            "type": "ai",
            "text": final_text or "I'm not sure how to help with that — could you rephrase?",
            "tools_used": [t["name"] for t in used_tools],
            "grounded": grounded_data,
        }

    # Exceeded rounds
    return {
        "type": "ai",
        "text": "I gathered some information but couldn't finalise a response. Please try a more specific question.",
        "tools_used": [t["name"] for t in used_tools],
        "grounded": grounded_data,
    }
