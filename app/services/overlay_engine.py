import datetime
import json
import re
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Designer meta-fence parser
# ---------------------------------------------------------------------------
# The designer persists a few non-coordinate-mapped settings (letterhead toggle,
# depositor toggle, free-form CONTENT-tab notes) inside an HTML-comment "fence"
# appended to the template's description column, so we don't need extra DB
# columns. The fence looks like:
#
#     "Standard OEP form\n<!--designer:{\"letterhead\":true,\"notes\":\"...\"}-->"
#
# Both the print HTML endpoint and the PDF generator call
# `parse_designer_meta(description)` to retrieve `{letterhead, depositor, notes}`
# so the saved Content-tab notes actually appear on the printed page (audit
# Fix 3) and the letterhead/depositor flags drive their respective renderers.
_DESIGNER_FENCE_RE = re.compile(r"<!--designer:(.+?)-->", re.DOTALL)


def parse_designer_meta(description: Optional[str]) -> Dict:
    """Return the parsed designer meta dict from a template description.

    Always returns a dict — never None — so callers can do
    `meta.get('notes', '')` without a None-guard. Malformed fences degrade
    silently to an empty dict.
    """
    if not description:
        return {}
    m = _DESIGNER_FENCE_RE.search(description)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def strip_designer_meta(description: Optional[str]) -> str:
    """Return the description with the designer meta-fence stripped — useful
    when you want to show the visible portion to the user (e.g. on the
    documents list page)."""
    if not description:
        return ""
    return _DESIGNER_FENCE_RE.sub("", description).strip()


def resolve_overlay_fields(
    candidate=None,
    demand=None,
    job_category=None,
    client=None,
    embassy_choice: Optional[str] = None,
    company=None
) -> Dict[str, str]:
    """
    Data mapping and overlay engine resolving fields with null-safe fallbacks.
    Provides stable overlay field names matching UI template expectations.
    """
    fields = {}

    # ------------------------------------------------------------------
    # 0. Customer / OEP (the agency itself) — comes from company_settings.
    # The demo OEP form prints the agency / recruiting-office name at the
    # top-left of every document via {{customer_name}}. These MUST be
    # resolved so the Agency Name (and OEP licence, owner, address, etc.)
    # populate on the printed document. (Problem 2)
    # ------------------------------------------------------------------
    if company is not None:
        cs_map = {
            "customer_name":           company.company_name or "",
            "customer_name_arabic":    getattr(company, "company_name_arabic", "") or "",
            "customer_owner_name":     getattr(company, "owner_name", "") or "",
            "customer_oep_license":    getattr(company, "oep_license_number", "") or "",
            "customer_address":        getattr(company, "address", "") or "",
            "customer_address_arabic": getattr(company, "address_arabic", "") or "",
            "customer_phone":          getattr(company, "phone", "") or "",
            "customer_mobile":         getattr(company, "mobile", "") or "",
            "customer_fax":            getattr(company, "fax", "") or "",
            "customer_email":          getattr(company, "email", "") or "",
            "customer_website":        getattr(company, "website", "") or "",
            "customer_subdomain":      getattr(company, "subdomain", "") or "",
            "customer_slug":           getattr(company, "slug", "") or "",
            "customer_file_prefix":    getattr(company, "file_prefix", "") or "",
            "customer_starting_point": str(getattr(company, "starting_point", "") or ""),
            "customer_status":         getattr(company, "status", "") or "",
            "customer_plan":           getattr(company, "plan", "") or "",
            # Office name/city default to the company name + address city when
            # the dedicated columns don't exist on company_settings.
            "customer_office_name":    company.company_name or "",
            "customer_emigrant_office": company.company_name or "",
        }
        ed = getattr(company, "expiry_date", None)
        if ed is not None:
            if isinstance(ed, (datetime.date, datetime.datetime)):
                cs_map["customer_expiry_date"] = ed.strftime("%d-%b-%Y")
            else:
                cs_map["customer_expiry_date"] = str(ed)
        # derive office city from the tail of the address if present
        addr = (getattr(company, "address", "") or "")
        cs_map["customer_office_city"] = ""
        for token in ("Sialkot", "Lahore", "Karachi", "Islamabad", "Daska", "Gujranwala"):
            if token.lower() in addr.lower():
                cs_map["customer_office_city"] = token
                break
        fields.update(cs_map)

    today = datetime.date.today()
    fields["__today__"] = today.strftime("%d-%b-%Y")
    fields["__now__"] = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    def extract(obj, prefix=""):
        if not obj:
            return
        for col in obj.__table__.columns:
            val = getattr(obj, col.name)
            if val is None:
                val = ""
            elif isinstance(val, (datetime.date, datetime.datetime)):
                val = val.strftime("%d-%b-%Y")
            fields[f"{prefix}{col.name}"] = str(val)

    # 1. Client fields
    extract(client, "client_")
    if client:
        fields["company_name"] = client.company_name or ""
        fields["client_type"] = client.client_type or ""
        fields["sponsor_name"] = client.sponsor_name or fields.get("company_name", "")

    # 2. Demand fields
    extract(demand, "demand_")
    if demand:
        fields["file_number"] = demand.file_number or ""
        fields["visa_number"] = demand.visa_number or ""
        fields["visa_issue_date"] = fields.get("demand_visa_issue_date", "")
        fields["permission_no"] = demand.permission_no or ""
        fields["permission_date"] = fields.get("demand_permission_date", "")
        fields["sponsor_name"] = demand.sponsor_name or fields.get("sponsor_name", "")
        fields["sponsor_name_arabic"] = demand.sponsor_name_arabic or ""
        fields["sponsor_address"] = demand.sponsor_address or ""
        fields["sponsor_address_arabic"] = demand.sponsor_address_arabic or ""
        fields["sponsor_phone"] = demand.sponsor_phone or ""
        fields["country"] = demand.country or "Saudi Arabia"
        
        # Override with specifically selected embassy for this assignment if provided
        fields["embassy"] = embassy_choice or demand.embassy or ""
        fields["visa_quota"] = str(demand.visa_quota) if demand.visa_quota else ""

    # 3. Job Category (Trade) fields
    extract(job_category, "job_")
    if job_category:
        fields["profession"] = job_category.trade or ""
        # The demo OEP visa form binds the Occupation cell to the SELECTED
        # trade/visa-category line item via {{selected_trade_visa_category}}.
        # Mirror it so dropped fields resolve correctly. (Problem 3)
        fields["selected_trade_visa_category"] = job_category.trade or ""
        fields["trade_visa_category"] = job_category.trade or ""
        fields["salary"] = str(job_category.salary) if job_category.salary else ""
        # contract_period — alias for contract_years (formatted as "N years")
        cy = getattr(job_category, "contract_years", None)
        fields["contract_years"] = str(cy) if cy else ""
        fields["contract_period"] = f"{cy} years" if cy else ""
        if getattr(job_category, 'custom_fields', None) and isinstance(job_category.custom_fields, dict):
            for k, v in job_category.custom_fields.items():
                fields[f"job_custom_{k}"] = str(v) if v is not None else ""

    # 4. Candidate fields
    extract(candidate, "candidate_")
    if candidate:
        fields["full_name"] = candidate.full_name or ""
        fields["name_arabic"] = candidate.name_arabic or ""
        fields["father_name"] = candidate.father_name or ""
        fields["father_name_arabic"] = candidate.father_name_arabic or ""
        # Occupation in Arabic — Problem 3. Prefer a dedicated candidate
        # column if present; otherwise fall back to the selected trade.
        fields["profession_arabic"] = (
            getattr(candidate, "profession_arabic", "") or
            fields.get("selected_trade_visa_category", "") or ""
        )
        fields["passport_no"] = candidate.passport_no or ""
        fields["cnic"] = candidate.cnic or ""
        fields["date_of_birth"] = fields.get("candidate_date_of_birth", "")
        fields["place_of_birth"] = candidate.place_of_birth or ""
        fields["nationality"] = candidate.nationality or "PAKISTANI"
        fields["address"] = candidate.address or ""
        fields["phone"] = candidate.phone or ""
        fields["email"] = candidate.email or ""
        if candidate.profession:
            fields["profession"] = candidate.profession
            
        fields["passport_issue_date"] = fields.get("candidate_passport_issue_date", "")
        fields["passport_expiry_date"] = fields.get("candidate_passport_expiry_date", "")
        fields["passport_issue_place"] = candidate.passport_issue_place or ""
        fields["religion"] = candidate.religion or "Islam"
        
        fields["next_of_kin_name"] = candidate.next_of_kin_name or ""
        fields["next_of_kin_relation"] = candidate.next_of_kin_relation or ""

        fields["protector_no"] = candidate.protector_no or ""
        fields["protector_date"] = fields.get("candidate_protector_date", "")
        fields["destination"] = candidate.destination or fields.get("country", "")
        fields["e_number"] = candidate.e_number or ""

        # Candidate may carry its own permission_no/permission_date. Prefer
        # candidate's value over demand's (the candidate is the document subject).
        cand_perm = candidate.permission_no or ""
        if cand_perm:
            fields["permission_no"] = cand_perm
        cand_perm_date = fields.get("candidate_permission_date", "")
        if cand_perm_date:
            fields["permission_date"] = cand_perm_date

        # --- DERIVED CHECKBOX FIELDS ----------------------------------------
        # gender_male / gender_female — for OEP/visa forms that have separate
        # tick boxes per gender. Engine treats checkbox fields as truthy when
        # value is non-empty/non-zero, so we provide either "1" or "".
        gender = (candidate.gender or "").strip().lower()
        fields["gender_male"]    = "1" if gender.startswith("m") else ""
        fields["gender_female"]  = "1" if gender.startswith("f") else ""

        # marital_single / married — derived from marital_status field if it
        # exists on the candidate model.
        ms = (getattr(candidate, "marital_status", "") or "").strip().lower()
        fields["marital_single"]   = "1" if ms.startswith("s") else ""
        fields["marital_married"]  = "1" if ms.startswith("m") else ""

        # religion_islam / religion_christian — common visa-form checkboxes
        rel = (candidate.religion or "").strip().lower()
        fields["religion_islam"]      = "1" if "islam" in rel or "muslim" in rel else ""
        fields["religion_christian"]  = "1" if "christ" in rel else ""

        # Convenience: city/district/province exposed flat
        fields["district"] = getattr(candidate, "district", "") or getattr(candidate, "city", "") or ""
        fields["province"] = getattr(candidate, "province", "") or ""
        fields["qualification"] = getattr(candidate, "qualification", "") or getattr(candidate, "education", "") or ""

        # Next-of-kin CNIC (may or may not exist on the candidate model)
        fields["next_of_kin_nic"] = getattr(candidate, "next_of_kin_cnic", "") or \
                                    getattr(candidate, "next_of_kin_nic", "") or ""

    return fields

def resolve_trade_table_rows(db, demand_id: Optional[int]) -> list:
    """Return a list of trade rows for the given demand_id, shaped for the
    designer overlay / print HTML. Each row is::

        {sr, trade, quantity, assigned, available,
         salary, contract_period}

    Returns an empty list when there's no demand or on any DB error so the
    caller can safely render a header-only table in preview mode.
    """
    if not demand_id or not db:
        return []
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
        print(f"[overlay_engine] resolve_trade_table_rows failed: {e}")
        return []

    out = []
    for idx, (jc, assigned) in enumerate(rows, start=1):
        qty = int(jc.quantity or 0)
        ass = int(assigned or 0)
        out.append({
            "_sr": str(idx),
            "trade": jc.trade or "",
            "quantity": qty,
            "_assigned": ass,
            "_available": max(0, qty - ass),
            "salary": jc.salary or "",
            "contract_period": getattr(jc, "contract_period", "") or "",
        })
    return out


def validate_overlay_mappings(template_fields: list, resolved_data: dict) -> list:
    """
    Validates that overlay template fields can be satisfied by the resolved user data.
    Returns a list of field_keys that are missing or empty.
    """
    missing_fields = []
    for f in template_fields:
        if getattr(f, "field_type", "text") == "static":
            continue
        key = getattr(f, "field_key", None)
        if not key:
            continue
        # Check against resolved values
        val = resolved_data.get(key)
        if val is None or str(val).strip() == "":
            missing_fields.append(key)
            
    return missing_fields


def resolve_all_related_data_for_record(db, data_source: str, record_id: int) -> dict:
    """
    Retrieve all related database models for a given data source and record ID,
    and resolve them using the core resolve_overlay_fields mapper.
    """
    candidate = None
    demand = None
    job_category = None
    client = None
    embassy_choice = None

    # The agency / OEP info is the same for every document in this single-tenant
    # deployment — always load it so {{customer_*}} fields populate. (Problem 2)
    company = None
    try:
        from app.models import CompanySettings
        company = db.query(CompanySettings).order_by(CompanySettings.id.asc()).first()
    except Exception:
        company = None

    if not record_id:
        return resolve_overlay_fields(company=company)

    from app.models import Candidate, CandidateAssignment, JobCategory, Demand, Client, Agent

    if data_source == "candidate":
        candidate = db.query(Candidate).filter(Candidate.id == record_id).first()
        if candidate:
            row = (
                db.query(CandidateAssignment, JobCategory, Demand, Client)
                .join(JobCategory, JobCategory.id == CandidateAssignment.job_category_id)
                .join(Demand, Demand.id == JobCategory.demand_id)
                .outerjoin(Client, Client.id == Demand.client_id)
                .filter(CandidateAssignment.candidate_id == candidate.id)
                .order_by(CandidateAssignment.assigned_at.desc(), CandidateAssignment.id.desc())
                .first()
            )
            if row:
                assignment, job_category, demand, client = row
                embassy_choice = assignment.embassy or demand.embassy or None
    elif data_source == "demand":
        demand = db.query(Demand).filter(Demand.id == record_id).first()
        if demand:
            client = db.query(Client).filter(Client.id == demand.client_id).first()
            embassy_choice = demand.embassy or None
    elif data_source == "client":
        client = db.query(Client).filter(Client.id == record_id).first()
    elif data_source == "agent":
        agent = db.query(Agent).filter(Agent.id == record_id).first()
        fields = resolve_overlay_fields(company=company)
        if agent:
            for col in agent.__table__.columns:
                val = getattr(agent, col.name)
                if val is None:
                    val = ""
                fields[col.name] = str(val)
                fields[f"agent_{col.name}"] = str(val)
        return fields

    return resolve_overlay_fields(
        candidate=candidate,
        demand=demand,
        job_category=job_category,
        client=client,
        embassy_choice=embassy_choice,
        company=company
    )
