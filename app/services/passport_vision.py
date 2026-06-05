"""LLM-Vision passport / CNIC reader.

This is the **primary**, most-accurate passport extraction path. It sends the
uploaded passport image to a vision-capable model through the OpenAI-compatible
proxy and asks for a strict JSON of Candidate-shaped fields.

It is *optional*: if no API key is configured, or the call fails, the caller
(`passport_ocr.extract_passport_data`) silently falls back to the offline
Tesseract-MRZ / regex pipeline so the scanner never hard-fails.

Returned `fields` keys are deliberately identical to Candidate column names so
the frontend can drop them straight into the wizard form:
    full_name, name_arabic, father_name, mother_name, gender, date_of_birth,
    place_of_birth, nationality, passport_no, passport_issue_date,
    passport_expiry_date, issuing_authority, passport_issue_place, cnic,
    nadra_token_no
"""
from __future__ import annotations

import os
import io
import json
import base64
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("passport_vision")

# Re-use the same config loader / client cache that the bot uses.
try:
    from app.services.dtcbot_llm import _get_client, _load_llm_config  # type: ignore
except Exception:  # pragma: no cover
    _get_client = lambda: None  # type: ignore
    _load_llm_config = lambda: {"api_key": "", "base_url": ""}  # type: ignore


VISION_MODEL = os.environ.get("DTC_VISION_MODEL", "gpt-5")

_VISION_PROMPT = """You are an expert passport & ID document data-extraction engine for a
Pakistani overseas-employment agency. The attached image is a passport bio-data
page (sometimes a CNIC / national ID card).

Extract every field you can read and return STRICT JSON only — no prose, no
markdown fences. Use exactly these keys (omit a key if not present, never guess):

{
  "full_name":            "given names + surname, as printed, UPPER CASE",
  "name_arabic":          "the name in Arabic script if present, else ''",
  "father_name":          "father / husband name if printed",
  "mother_name":          "mother name if printed",
  "gender":               "Male or Female",
  "date_of_birth":        "YYYY-MM-DD",
  "place_of_birth":       "city/country as printed",
  "nationality":          "e.g. PAKISTANI",
  "passport_no":          "the passport number (letters+digits, no spaces)",
  "passport_issue_date":  "YYYY-MM-DD",
  "passport_expiry_date": "YYYY-MM-DD",
  "issuing_authority":    "e.g. PAKISTAN / DG I&P",
  "passport_issue_place": "place of issue if printed",
  "cnic":                 "13-digit national ID, formatted XXXXX-XXXXXXX-X if possible",
  "nadra_token_no":       "tracking/citizen number if present"
}

Rules:
- Read the MRZ (the two `<<<` lines at the bottom) to confirm passport number,
  dates of birth/expiry, sex and nationality — the MRZ is the ground truth.
- Convert ALL dates to ISO YYYY-MM-DD. Pakistani passports print dates as
  DD MMM YYYY (e.g. `24 MAY 2027`) or DD/MM/YYYY.
- Return ONLY the JSON object.
"""


def _img_to_data_url(image_bytes: bytes) -> str:
    """Encode raw image bytes as a base64 data URL (resized to keep payload small)."""
    mime = "image/jpeg"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes))
        # Convert/clean and cap longest side at 1600px for cost & speed.
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        max_side = 1600
        if max(im.size) > max_side:
            ratio = max_side / float(max(im.size))
            im = im.resize((int(im.size[0] * ratio), int(im.size[1] * ratio)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        image_bytes = buf.getvalue()
    except (OSError, ValueError) as exc:
        # Pillow couldn't decode/resize — send original bytes; the vision
        # API will give us its own error if it can't parse them either.
        import logging
        logging.getLogger("dtc.vision").debug(
            "Pillow preprocess failed, sending raw bytes: %s", exc
        )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def available() -> bool:
    """True if a vision-capable LLM client could be created (any key present)."""
    try:
        cfg = _load_llm_config()
        return bool(cfg.get("api_key") or cfg.get("fallback_keys"))
    except (OSError, ValueError, KeyError) as exc:
        # Config missing / unreadable — treat as "vision unavailable".
        import logging
        logging.getLogger("dtc.vision").debug("vision config unreadable: %s", exc)
        return False


def _vision_clients():
    """Yield an OpenAI client for every available key (primary + fallbacks) so
    a single expired token doesn't kill the whole vision path. Falls back to
    the shared cached client if the key list is empty."""
    try:
        from openai import OpenAI  # type: ignore
        from app.services.dtcbot_llm import all_api_keys, _load_llm_config
        cfg = _load_llm_config()
        base = cfg.get("base_url") or None
        keys = all_api_keys()
        if keys:
            for k in keys:
                try:
                    yield OpenAI(api_key=k, base_url=base)
                except Exception:
                    continue
            return
    except Exception:
        pass
    c = _get_client()
    if c is not None:
        yield c


def extract_with_vision(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Run LLM-vision extraction. Returns a `fields` dict or None on failure.

    Tries each configured key in turn so an expired/invalid primary token
    transparently rotates to a working fallback (Problem 4 robustness)."""
    data_url = _img_to_data_url(image_bytes)
    last_err = None
    for client in _vision_clients():
        try:
            resp = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VISION_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            # Strip accidental code fences
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            # Find the JSON object
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                logger.warning("Vision OCR: no JSON found in response: %s", raw[:200])
                continue
            obj = json.loads(raw[start:end + 1])
            # Keep only non-empty values
            fields = {k: v for k, v in obj.items() if isinstance(v, str) and v.strip()}
            # Normalise nationality
            if fields.get("nationality", "").upper() == "PAKISTAN":
                fields["nationality"] = "PAKISTANI"
            if fields:
                return fields
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("Vision OCR attempt failed (rotating key): %s", e)
            continue
    if last_err:
        logger.warning("Vision OCR failed on all keys: %s", last_err)
    return None
