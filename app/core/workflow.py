"""
============================================================================
Canonical Workflow Model - Dogar Trading Portal
============================================================================

Single source of truth for every status string used across the application.
Replaces the previous fragmented enums:

    Before (5 inconsistent enums)              After (one canonical model)
    -----------------------------------        -------------------------------
    Candidate.status:                          CandidateStage (this file)
       pending | documents_pending |             new | docs_pending | docs_complete |
       processing | issued | deployed           protector | medical | visa_stamping |
                                                e_number | travel_ready | deployed |
                                                cancelled
    CandidateAssignment.status:                AssignmentStage  (alias of CandidateStage)
       pending | documents_pending |
       processing | visa_issued | deployed
    Demand.status:                             DemandStatus  (this file)
       active | processing | filled |             active | processing | filled |
       expired | cancelled                        expired | cancelled
    Client.status:                             ClientStatus (this file)
       active | inactive                          active | inactive
    LoginHistory.status:                       LoginAttemptStatus (this file)
       Success | Failed                          success | failed

Business rule: "Protector" IS a real workflow stage. It refers to the
**Protector of Emigrants** office of the Government of Pakistan, Bureau of
Emigration & Overseas Employment.  Every Pakistani worker going abroad must
get a "Protector stamp" on their passport before they can legally fly out.
We therefore keep it as a first-class canonical stage between document
completion and visa stamping.

----------------------------------------------------------------------------
CANDIDATE WORKFLOW (state machine)
----------------------------------------------------------------------------
    new                  -> Candidate created (wizard incomplete or just saved)
    docs_pending         -> Assigned to a demand-file trade, documents being collected
    docs_complete        -> All required documents on file
    protector            -> Submitted for Protector of Emigrants stamp
    medical              -> Medical / GAMCA in progress
    visa_stamping        -> Visa being stamped at embassy
    e_number             -> E-number issued, ready for ticketing
    travel_ready         -> Flight booked, awaiting departure
    deployed             -> Departed for destination country (final happy path)
    cancelled            -> Process aborted (worker withdrew, rejection, etc.)

----------------------------------------------------------------------------
LEGACY -> CANONICAL MIGRATION TABLE
----------------------------------------------------------------------------
    pending             -> docs_pending     (treated as "documents being collected")
    documents_pending   -> docs_pending
    processing          -> protector        (legacy umbrella stage)
    issued              -> visa_stamping
    visa_issued         -> visa_stamping
    Pending             -> docs_pending
    Documents Pending   -> docs_pending
    Success             -> success          (LoginHistory only)
    Failed              -> failed           (LoginHistory only)
"""

from __future__ import annotations
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ============================================================================
# Candidate / Assignment workflow stages
# ============================================================================
class CandidateStage(str, Enum):
    NEW = "new"
    DOCS_PENDING = "docs_pending"
    DOCS_COMPLETE = "docs_complete"
    PROTECTOR = "protector"
    MEDICAL = "medical"
    VISA_STAMPING = "visa_stamping"
    E_NUMBER = "e_number"
    TRAVEL_READY = "travel_ready"
    DEPLOYED = "deployed"
    CANCELLED = "cancelled"


# Alias: CandidateAssignment.status uses the same canonical enum.
AssignmentStage = CandidateStage


# Allowed forward transitions (state machine).
# A backward transition is allowed only to CANCELLED, or by an admin override.
STAGE_TRANSITIONS: Dict[CandidateStage, List[CandidateStage]] = {
    CandidateStage.NEW:          [CandidateStage.DOCS_PENDING, CandidateStage.CANCELLED],
    CandidateStage.DOCS_PENDING: [CandidateStage.DOCS_COMPLETE, CandidateStage.CANCELLED, CandidateStage.NEW],
    CandidateStage.DOCS_COMPLETE:[CandidateStage.PROTECTOR, CandidateStage.MEDICAL, CandidateStage.DOCS_PENDING, CandidateStage.CANCELLED],
    CandidateStage.PROTECTOR:    [CandidateStage.MEDICAL, CandidateStage.VISA_STAMPING, CandidateStage.DOCS_COMPLETE, CandidateStage.CANCELLED],
    CandidateStage.MEDICAL:      [CandidateStage.VISA_STAMPING, CandidateStage.PROTECTOR, CandidateStage.CANCELLED],
    CandidateStage.VISA_STAMPING:[CandidateStage.E_NUMBER, CandidateStage.MEDICAL, CandidateStage.CANCELLED],
    CandidateStage.E_NUMBER:     [CandidateStage.TRAVEL_READY, CandidateStage.VISA_STAMPING, CandidateStage.CANCELLED],
    CandidateStage.TRAVEL_READY: [CandidateStage.DEPLOYED, CandidateStage.E_NUMBER, CandidateStage.CANCELLED],
    CandidateStage.DEPLOYED:     [],   # terminal
    CandidateStage.CANCELLED:    [CandidateStage.NEW, CandidateStage.DOCS_PENDING],   # can be re-opened
}


# Display labels + pill colour classes (used by Jinja & API).
STAGE_DISPLAY: Dict[str, Tuple[str, str]] = {
    CandidateStage.NEW.value:           ("New",           "bg-slate-100 text-slate-700"),
    CandidateStage.DOCS_PENDING.value:  ("Documents Pending",   "bg-amber-50 text-amber-700"),
    CandidateStage.DOCS_COMPLETE.value: ("Documents Complete",  "bg-sky-50 text-sky-700"),
    CandidateStage.PROTECTOR.value:     ("Protector",     "bg-indigo-50 text-indigo-700"),
    CandidateStage.MEDICAL.value:       ("Medical",       "bg-purple-50 text-purple-700"),
    CandidateStage.VISA_STAMPING.value: ("Visa Stamping", "bg-blue-50 text-blue-700"),
    CandidateStage.E_NUMBER.value:      ("E-Number",      "bg-violet-50 text-violet-700"),
    CandidateStage.TRAVEL_READY.value:  ("Travel Ready",  "bg-teal-50 text-teal-700"),
    CandidateStage.DEPLOYED.value:      ("Deployed",      "bg-emerald-50 text-emerald-700"),
    CandidateStage.CANCELLED.value:     ("Cancelled",     "bg-rose-50 text-rose-700"),
}


# Legacy -> canonical aliases.  Use normalize_stage() to apply.
_LEGACY_STAGE_ALIASES: Dict[str, str] = {
    # legacy candidate
    "pending":            CandidateStage.DOCS_PENDING.value,
    "documents_pending":  CandidateStage.DOCS_PENDING.value,
    "documents pending":  CandidateStage.DOCS_PENDING.value,
    "docs pending":       CandidateStage.DOCS_PENDING.value,
    "documents_complete": CandidateStage.DOCS_COMPLETE.value,
    "documents complete": CandidateStage.DOCS_COMPLETE.value,
    "docs complete":      CandidateStage.DOCS_COMPLETE.value,
    "processing":         CandidateStage.PROTECTOR.value,
    "issued":             CandidateStage.VISA_STAMPING.value,
    "visa_issued":        CandidateStage.VISA_STAMPING.value,
    "visa issued":        CandidateStage.VISA_STAMPING.value,
    "visa stamping":      CandidateStage.VISA_STAMPING.value,
    "e number":           CandidateStage.E_NUMBER.value,
    "e-number":           CandidateStage.E_NUMBER.value,
    "travel ready":       CandidateStage.TRAVEL_READY.value,
    "deployed":           CandidateStage.DEPLOYED.value,
    "cancelled":          CandidateStage.CANCELLED.value,
    "canceled":           CandidateStage.CANCELLED.value,
    "new":                CandidateStage.NEW.value,
    "protector":          CandidateStage.PROTECTOR.value,
    "medical":            CandidateStage.MEDICAL.value,
    "":                   CandidateStage.NEW.value,
    None:                 CandidateStage.NEW.value,
}


def normalize_stage(value: Optional[str]) -> str:
    """Coerce any legacy / casing variant to the canonical lowercase value."""
    if value is None:
        return CandidateStage.NEW.value
    key = str(value).strip().lower()
    if key in _LEGACY_STAGE_ALIASES:
        return _LEGACY_STAGE_ALIASES[key]
    # If it's already a canonical value, keep it; otherwise default to NEW.
    valid = {s.value for s in CandidateStage}
    return key if key in valid else CandidateStage.NEW.value


def can_transition(from_stage: str, to_stage: str) -> bool:
    """Check if a stage transition is permitted by the state machine."""
    try:
        f = CandidateStage(normalize_stage(from_stage))
        t = CandidateStage(normalize_stage(to_stage))
    except ValueError:
        return False
    return t in STAGE_TRANSITIONS.get(f, [])


def stage_label(value: Optional[str]) -> str:
    """Human-readable label for a stage."""
    v = normalize_stage(value)
    return STAGE_DISPLAY.get(v, (v, ""))[0]


def stage_pill_classes(value: Optional[str]) -> str:
    """Tailwind classes for the status pill."""
    v = normalize_stage(value)
    return STAGE_DISPLAY.get(v, ("", "bg-slate-100 text-slate-600"))[1]


def all_stages() -> List[Dict[str, str]]:
    """List of {value, label, classes} for dropdowns / filters."""
    return [
        {"value": s.value, "label": STAGE_DISPLAY[s.value][0], "classes": STAGE_DISPLAY[s.value][1]}
        for s in CandidateStage
    ]


# ============================================================================
# Demand file status (independent of candidate workflow)
# ============================================================================
class DemandStatus(str, Enum):
    ACTIVE = "active"
    PROCESSING = "processing"
    FILLED = "filled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


DEMAND_DISPLAY: Dict[str, Tuple[str, str]] = {
    DemandStatus.ACTIVE.value:     ("Active",     "bg-emerald-50 text-emerald-700"),
    DemandStatus.PROCESSING.value: ("Processing", "bg-blue-50 text-blue-700"),
    DemandStatus.FILLED.value:     ("Filled",     "bg-emerald-50 text-emerald-700"),
    DemandStatus.EXPIRED.value:    ("Expired",    "bg-rose-50 text-rose-700"),
    DemandStatus.CANCELLED.value:  ("Cancelled",  "bg-rose-50 text-rose-700"),
}


def normalize_demand_status(value: Optional[str]) -> str:
    if not value: return DemandStatus.ACTIVE.value
    v = str(value).strip().lower()
    valid = {s.value for s in DemandStatus}
    return v if v in valid else DemandStatus.ACTIVE.value


# ============================================================================
# Client status
# ============================================================================
class ClientStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


# ============================================================================
# Login history (auth attempts)
# ============================================================================
class LoginAttemptStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


def normalize_login_status(value: Optional[str]) -> str:
    if not value: return LoginAttemptStatus.FAILED.value
    v = str(value).strip().lower()
    return LoginAttemptStatus.SUCCESS.value if v.startswith("succ") else LoginAttemptStatus.FAILED.value


# ============================================================================
# Wizard step constants  (used by CandidateWizardState)
# ============================================================================
class WizardStep(int, Enum):
    PERSONAL_INFO     = 1
    IDENTIFICATION    = 2
    EMPLOYMENT        = 3
    NEXT_OF_KIN       = 4
    MEDICAL_PROTECTOR = 5
    VISA_TRAVEL       = 6
    CHARGE_SUMMARY    = 7


WIZARD_STEPS = [
    {"step": 1, "key": "personal_info",     "label": "Personal Info"},
    {"step": 2, "key": "identification",    "label": "Identification"},
    {"step": 3, "key": "employment",        "label": "Employment"},
    {"step": 4, "key": "next_of_kin",       "label": "Next of Kin"},
    {"step": 5, "key": "medical_protector", "label": "Medical & Protector"},
    {"step": 6, "key": "visa_travel",       "label": "Visa & Travel"},
    {"step": 7, "key": "charge_summary",    "label": "Charge Summary"},
]
TOTAL_WIZARD_STEPS = len(WIZARD_STEPS)


# ============================================================================
# Audit log action constants
# ============================================================================
class AuditAction(str, Enum):
    CREATE          = "create"
    UPDATE          = "update"
    DELETE          = "delete"
    ASSIGN          = "assign"
    UNASSIGN        = "unassign"
    STAGE_CHANGE    = "stage_change"
    WIZARD_STEP     = "wizard_step"
    WIZARD_FINALIZE = "wizard_finalize"
    WIZARD_REOPEN   = "wizard_reopen"
    LOGIN           = "login"
    LOGOUT          = "logout"


class AuditEntity(str, Enum):
    CANDIDATE       = "candidate"
    ASSIGNMENT      = "assignment"
    DEMAND          = "demand"
    TRADE           = "trade"
    CLIENT          = "client"
    USER            = "user"
    DOCUMENT        = "document"
    WIZARD_STATE    = "wizard_state"
