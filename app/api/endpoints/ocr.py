"""OCR endpoints — passport / ID scanner + Arabic auto-translate.

POST /api/ocr/passport  (multipart file=passport.jpg)
    Returns: { ok, method, fields, raw_text }
        fields → Candidate-shaped dict (full_name, father_name, passport_no,
        date_of_birth, passport_expiry_date, passport_issue_date,
        passport_issue_place, cnic, nationality, gender, ...)

POST /api/ocr/translate-to-arabic  (JSON body: {full_name, father_name, place_of_birth, ...})
    Returns: { ok, arabic: { full_name, father_name, place_of_birth, ... } }
        v8 NEW — feeds the candidate's English name into the LLM and gets
        back the Saudi-Arabic transliteration ready for Saudi visa forms.
"""
from typing import Optional, Dict
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.models import User
from app.services.passport_ocr import extract_passport_data
from app.services.arabic_translate import translate_to_arabic

router = APIRouter()


@router.post("/passport")
async def scan_passport(file: UploadFile = File(...), user: User = Depends(get_current_user)):
    """Upload a passport image (jpg / png / webp). Returns extracted fields
    ready for the Candidate wizard."""
    # Phone uploads frequently arrive as application/octet-stream or
    # image/heic (iPhone), so accept by content-type OR file extension and let
    # the decoder (Pillow + pillow-heif) be the real gatekeeper. (Problem 7)
    name = (file.filename or "").lower()
    ct = (file.content_type or "").lower()
    img_exts = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")
    looks_image = ct.startswith("image/") or name.endswith(img_exts) or ct in (
        "application/octet-stream", "")
    if not looks_image:
        raise HTTPException(400, "Only image files are accepted (JPG, PNG, WEBP, HEIC)")

    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 25 MB)")
    if len(content) < 1024:
        raise HTTPException(400, "Image too small or empty")

    result = extract_passport_data(content)
    if not result.get("ok") and not result.get("fields"):
        return {
            "ok": False,
            "error": result.get("error", "OCR failed"),
            "fields": {},
            "raw_text": result.get("raw_text", ""),
        }

    # Always return success if any fields were extracted (text-fallback)
    return {
        "ok": True,
        "method": result.get("method", "text"),
        "fields": result.get("fields", {}),
        "raw_text": result.get("raw_text", ""),
    }


# ---------------------------------------------------------------------------
# v8 NEW: English → Saudi Arabic auto-translate for Saudi visa forms
# ---------------------------------------------------------------------------
class TranslateRequest(BaseModel):
    full_name: Optional[str] = ""
    father_name: Optional[str] = ""
    place_of_birth: Optional[str] = ""
    issuing_authority: Optional[str] = ""


@router.post("/translate-to-arabic")
async def translate_endpoint(payload: TranslateRequest, user: User = Depends(get_current_user)):
    """Translate / transliterate English candidate fields into Saudi Arabic
    so the Saudi visa application form (Arabic columns) can be auto-filled.

    Returns the same field names with their Arabic-script equivalents.
    Falls back to a deterministic transliteration table if the LLM is
    unreachable so the feature *always* works, even offline.
    """
    inputs: Dict[str, str] = {
        "full_name":         (payload.full_name or "").strip(),
        "father_name":       (payload.father_name or "").strip(),
        "place_of_birth":    (payload.place_of_birth or "").strip(),
        "issuing_authority": (payload.issuing_authority or "").strip(),
    }
    # Drop empty inputs so we don't waste tokens on blank translations
    inputs = {k: v for k, v in inputs.items() if v}
    if not inputs:
        return {"ok": False, "error": "Nothing to translate", "arabic": {}}

    arabic = translate_to_arabic(inputs)
    return {"ok": True, "arabic": arabic, "method": arabic.pop("_method", "llm")}
