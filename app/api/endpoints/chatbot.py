"""DtcBot — Dogar Trading Corporation AI assistant API.

POST /api/chatbot/message      JSON: { "message": "..." }
POST /api/chatbot/upload       multipart: file=<image>
GET  /api/chatbot/welcome      initial greeting
"""
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.deps import get_current_user
from app.models import User
from app.services.dtcbot import answer, handle_uploaded_document
from app.services.passport_ocr import extract_passport_data

router = APIRouter()


@router.get("/welcome")
def welcome():
    return answer(None, "", user=None)  # type: ignore

@router.post("/message")
def chat(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    msg = (payload.get("message") or "").strip()
    history = payload.get("history") or []
    if not isinstance(history, list):
        history = []
    return answer(db, msg, history=history, user=user)


@router.post("/data-entry")
def data_entry(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Direct compute-agent data entry.

    The UI (or chat) can POST either:
      { "block": "Full Name: ALI\\nPassport No: AB123..." }  -> create/update candidate
      { "fields": {col: value, ...}, "candidate_ref": "<optional id/name/passport>" }

    When ``candidate_ref`` is provided the agent updates that candidate;
    otherwise it creates a new one (requires full_name).
    """
    from app.services import dtcbot_agent as agent

    block = (payload.get("block") or "").strip()
    fields = payload.get("fields") or {}
    ref = (payload.get("candidate_ref") or "").strip()

    if block and not fields:
        fields = agent.parse_field_block(block)

    # Coerce/whitelist incoming explicit fields too
    clean = {}
    for k, v in (fields or {}).items():
        col = k if k in agent.VALID_COLUMNS else agent._normalize_key(k)
        if col and col in agent.VALID_COLUMNS:
            clean[col] = agent._coerce(col, v)

    if not clean:
        raise HTTPException(400, "No recognisable candidate fields supplied.")

    if ref:
        res = agent.update_candidate(db, ref, clean, user=user)
    elif clean.get("full_name"):
        res = agent.create_candidate(db, clean, user=user)
    else:
        # try resolve by passport/cnic, else error
        auto_ref = clean.get("passport_no") or clean.get("cnic")
        if auto_ref:
            res = agent.update_candidate(db, str(auto_ref), clean, user=user)
        else:
            raise HTTPException(400, "Provide a full_name (to create) or candidate_ref (to update).")

    if not res.get("ok"):
        raise HTTPException(422, res.get("error", "Data entry failed."))
    return res


@router.post("/upload")
async def chat_upload(
    file: UploadFile = File(...),
    auto_create: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """User dragged a passport image into the chat — run OCR and (by
    default) **actively create or update the candidate record** so the
    end-to-end OCR → data-entry loop completes without manual steps.

    Pass ``?auto_create=false`` to skip the auto-create behaviour and
    fall back to the legacy ``ocr_result`` response (just shows the
    extracted fields and a link to the manual form).
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are accepted")
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 8 MB)")
    result = extract_passport_data(content)
    return handle_uploaded_document(
        file.filename or "passport.jpg",
        result,
        db=db,
        user=user,
        auto_create=auto_create,
    )
