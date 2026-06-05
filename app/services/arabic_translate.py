"""English → Saudi Arabic name translation / transliteration service.

Two-tier strategy:
  1. **LLM (primary)** — call the GenSpark / OpenAI-compatible vision-capable
     model to get an accurate Arabic-script transliteration of a Pakistani
     name (e.g. "ALI HASSAN" → "علي حسن"). The LLM also handles place names
     and authority names correctly.

  2. **Deterministic fallback** — if no LLM key is configured or the call
     fails, use a Latin→Arabic phoneme-mapping table that covers the common
     Pakistani/Arabic name sounds. Guarantees the feature *never* hard-fails
     so the Saudi visa form column always gets filled even offline.

The output dict uses the SAME keys as the input so the frontend can map
field-by-field straight into the wizard form.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict

logger = logging.getLogger("arabic_translate")

# ---------------------------------------------------------------------------
# Tier 1 — LLM-driven translation
# ---------------------------------------------------------------------------
_LLM_PROMPT = """You are an expert Arabic transliterator for a Pakistani overseas-employment
agency that processes Saudi Arabia work visas.

You will receive a JSON object with English field values. Return STRICT JSON
with the SAME keys whose values are the **Saudi-Arabic-script equivalents**.

Rules:
  - Use Saudi-Arabic orthography (Khaleeji style).
  - Preserve traditional Arabic spellings for common names. Examples:
        ALI HASSAN          → علي حسن
        MUHAMMAD            → محمد
        ABDULLAH            → عبدالله
        BASHIR AHMED        → بشير أحمد
        FATIMA              → فاطمة
        KARACHI             → كراتشي
        ISLAMABAD           → إسلام آباد
        LAHORE              → لاهور
        PESHAWAR            → بيشاور
        SIALKOT             → سيالكوت
        PAKISTAN            → باكستان
        DG I&P              → المديرية العامة للجوازات
  - Do NOT translate (i.e. do not change meaning) — just transliterate
    proper nouns. Country / city names use their canonical Arabic spelling.
  - Return JSON ONLY, no prose, no markdown fences.

Input keys you may see: full_name, father_name, place_of_birth,
issuing_authority. Omit keys whose input was empty.
"""


def _try_llm(inputs: Dict[str, str]) -> Dict[str, str] | None:
    """Call the LLM proxy; return None on any failure so caller falls back."""
    try:
        from app.services.dtcbot_llm import _get_client, _load_llm_config
    except Exception as e:
        logger.warning("Could not import LLM client: %s", e)
        return None

    client = _get_client()
    if client is None:
        return None

    import os
    model = os.environ.get("DTC_TRANSLATE_MODEL", "gpt-5-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": _LLM_PROMPT},
                {"role": "user",   "content": json.dumps(inputs, ensure_ascii=False)},
            ],
            max_tokens=400,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Strip ```json fences if the model added them
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            # Only keep values that contain Arabic-range characters
            out = {}
            for k, v in data.items():
                if isinstance(v, str) and _has_arabic(v):
                    out[k] = v.strip()
            if out:
                out["_method"] = "llm"
                return out
    except Exception as e:
        logger.warning("LLM Arabic translation failed: %s", e)
    return None


def _has_arabic(s: str) -> bool:
    """True if string contains any Arabic Unicode character."""
    return any("\u0600" <= ch <= "\u06FF" for ch in s)


# ---------------------------------------------------------------------------
# Tier 2 — Deterministic Latin → Arabic transliteration fallback
# ---------------------------------------------------------------------------

# Common pre-canned names so the fallback nails the typical cases without
# any phoneme guessing.
_NAME_DICT = {
    # Male first names (Pakistani/Arabic)
    "MUHAMMAD": "محمد", "MOHAMMAD": "محمد", "MOHAMED": "محمد",
    "AHMAD": "أحمد", "AHMED": "أحمد",
    "ALI": "علي",
    "HASSAN": "حسن", "HASAN": "حسن", "HUSSAIN": "حسين", "HUSAIN": "حسين", "HUSSEIN": "حسين",
    "ABDULLAH": "عبدالله", "ABDUL": "عبد",
    "BASHIR": "بشير", "BASHEER": "بشير",
    "OMAR": "عمر", "UMAR": "عمر",
    "KHALID": "خالد", "KHALED": "خالد",
    "RASHID": "راشد", "RASHEED": "رشيد",
    "QASIM": "قاسم", "KASIM": "قاسم",
    "TARIQ": "طارق", "TARIK": "طارق",
    "IMRAN": "عمران",
    "USMAN": "عثمان", "OSMAN": "عثمان",
    "BILAL": "بلال",
    "ZAHID": "زاهد", "ZAID": "زيد",
    "WAQAR": "وقار",
    "SAQIB": "ثاقب",
    "ASIF": "آصف",
    "FAISAL": "فيصل",
    "NASIR": "ناصر",
    "AAMIR": "عامر", "AMIR": "عامر",
    "SAEED": "سعيد",
    "RIZWAN": "رضوان",
    "FAHAD": "فهد",
    "SHAHID": "شاهد",
    "JAVED": "جاويد", "JAVID": "جاويد",
    "NOOR": "نور",
    "ADEEL": "عديل",
    # Female first names
    "FATIMA": "فاطمة", "FATIMAH": "فاطمة",
    "AISHA": "عائشة", "AYESHA": "عائشة",
    "KHADIJA": "خديجة",
    "MARYAM": "مريم",
    "ZAINAB": "زينب",
    "SAMIA": "سامية",
    "SAIRA": "سائرة",
    "NAILA": "نائلة",
    "AMNA": "آمنة",
    "ASMA": "أسماء",
    "HINA": "حنا",
    "SANA": "ثناء",
    # City / country / authority canonical Arabic spellings
    "PAKISTAN":    "باكستان",
    "PAKISTANI":   "باكستاني",
    "KARACHI":     "كراتشي",
    "ISLAMABAD":   "إسلام آباد",
    "LAHORE":      "لاهور",
    "PESHAWAR":    "بيشاور",
    "RAWALPINDI":  "روالبندي",
    "MULTAN":      "ملتان",
    "FAISALABAD":  "فيصل آباد",
    "SIALKOT":     "سيالكوت",
    "GUJRANWALA":  "غوجرانوالا",
    "QUETTA":      "كويتا",
    "HYDERABAD":   "حيدر آباد",
    "SUKKUR":      "سكر",
    "PUNJAB":      "البنجاب",
    "SINDH":       "السند",
    "SAUDI ARABIA": "المملكة العربية السعودية",
    "DG I&P":      "المديرية العامة للجوازات",
    "DIRECTORATE GENERAL OF IMMIGRATION AND PASSPORTS": "المديرية العامة للهجرة والجوازات",
}

# Latin sub-string → Arabic mapping, sorted by length (longest first) so the
# greedy walker handles digraphs (sh, kh, gh, ch, th) before single letters.
# This is the *fallback*; the LLM produces vastly better output when available.
_PHONEME_MAP = [
    ("KHAN", "خان"),
    ("UDDIN", "الدين"),
    ("ULLAH", "الله"),
    ("ABDUL", "عبد"),
    ("BIBI", "بيبي"),
    ("BEGUM", "بيغوم"),
    ("MIRZA", "ميرزا"),
    ("SHAH", "شاه"),
    ("SHEIKH", "شيخ"),
    ("MALIK", "ملك"),
    ("CHAUDHRY", "تشودري"), ("CHAUDHARY", "تشودري"),
    ("RAJA", "راجا"),
    # digraphs
    ("SH", "ش"), ("KH", "خ"), ("GH", "غ"), ("CH", "تش"),
    ("TH", "ث"), ("DH", "ذ"), ("AA", "آ"), ("EE", "ي"),
    ("OO", "و"), ("AI", "اي"), ("AU", "او"), ("AI", "اي"),
    # single letters
    ("A", "ا"), ("B", "ب"), ("C", "ك"), ("D", "د"), ("E", "ي"),
    ("F", "ف"), ("G", "ج"), ("H", "ه"), ("I", "ي"), ("J", "ج"),
    ("K", "ك"), ("L", "ل"), ("M", "م"), ("N", "ن"), ("O", "و"),
    ("P", "ب"), ("Q", "ق"), ("R", "ر"), ("S", "س"), ("T", "ت"),
    ("U", "و"), ("V", "ف"), ("W", "و"), ("X", "كس"), ("Y", "ي"),
    ("Z", "ز"),
]


def _translit_word(word: str) -> str:
    """Greedy phoneme-based Latin → Arabic transliteration for one word."""
    w = word.upper().strip()
    if not w:
        return ""
    # Exact dictionary hit?
    if w in _NAME_DICT:
        return _NAME_DICT[w]
    out = []
    i = 0
    while i < len(w):
        ch = w[i]
        if not ch.isalpha():
            # Keep digits / punctuation as-is so passport authority codes
            # like "DG I&P" still look readable.
            out.append(ch)
            i += 1
            continue
        matched = False
        for src, dst in _PHONEME_MAP:
            if w[i:i+len(src)] == src:
                out.append(dst)
                i += len(src)
                matched = True
                break
        if not matched:
            i += 1  # skip unknown char
    return "".join(out)


def _translit_phrase(text: str) -> str:
    """Transliterate a multi-word string, preserving spaces."""
    if not text:
        return ""
    parts = re.split(r"(\s+)", text.strip())
    return "".join(p if p.isspace() else _translit_word(p) for p in parts).strip()


def _fallback(inputs: Dict[str, str]) -> Dict[str, str]:
    out = {k: _translit_phrase(v) for k, v in inputs.items() if v}
    out["_method"] = "fallback"
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def translate_to_arabic(inputs: Dict[str, str]) -> Dict[str, str]:
    """Translate the given English fields into Saudi Arabic.

    Tries the LLM proxy first, falls back to a deterministic transliteration
    so the feature *always* returns Arabic text — never an empty result.
    """
    if not inputs:
        return {}
    # Tier 1
    llm_out = _try_llm(inputs)
    if llm_out:
        # Make sure every requested key has *some* Arabic value — if the LLM
        # silently omitted one, fill it via fallback so the form is complete.
        for k, v in inputs.items():
            if k not in llm_out and v:
                llm_out[k] = _translit_phrase(v)
        return llm_out
    # Tier 2
    return _fallback(inputs)
