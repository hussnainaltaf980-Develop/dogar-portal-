"""Passport OCR & MRZ parser.

Strategy (most robust to least robust):
1. Run Tesseract OCR over the full image and try to locate the MRZ
   (Machine-Readable Zone) at the bottom of the passport.
   The MRZ has a fixed, internationally standardized format (ICAO 9303)
   so we can parse it deterministically — no AI needed.
2. If the MRZ parse fails, fall back to text-pattern extraction from
   the rest of the page (passport number, dates, name labels).
3. Always return a normalized dict ready to be merged into the
   Candidate wizard fields.
"""
import re
import io
from datetime import datetime
from typing import Optional, Dict, Any

from PIL import Image, ImageOps, ImageFilter

# Register the HEIF/HEIC opener so iPhone photos (the most common "phone photo"
# input — iPhones save passport snaps as .heic) can be decoded by Pillow.
# (Problem 7 — "not understanding correct phone photo".)
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:  # pragma: no cover — optional dependency
    _HEIF_OK = False

try:
    import pytesseract
    _TESSERACT_OK = True
except ImportError:
    pytesseract = None  # type: ignore
    _TESSERACT_OK = False


# ----------------------------------------------------------------------
# Image pre-processing
# ----------------------------------------------------------------------
def _preprocess(img: Image.Image) -> Image.Image:
    """Boost contrast & grayscale so Tesseract has the cleanest input.

    Tuned for *phone-camera* passport photos (the real-world input): such
    shots are large but soft, with uneven lighting and JPEG noise. We:
      • fix EXIF orientation (phones rotate via metadata, not pixels),
      • grayscale + autocontrast to flatten lighting,
      • upscale small images so the printed bio-data text is tall enough for
        Tesseract (it needs ~20px cap-height to read reliably),
      • sharpen lightly.
    """
    try:
        img = ImageOps.exif_transpose(img)          # honour phone rotation
    except Exception:
        pass
    img = img.convert("L")                            # grayscale
    img = ImageOps.autocontrast(img, cutoff=2)
    # Upscale small images — phone crops are often < 1000px on the short side
    # which leaves the MRZ/bio text too small for Tesseract.
    w, h = img.size
    longest = max(w, h)
    if longest < 1800:
        scale = 1800.0 / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _mrz_check_digit(data: str) -> str:
    """ICAO-9303 check-digit for an MRZ field (weights 7,3,1). Returns a
    single digit char, or '' if the data is unusable."""
    weights = (7, 3, 1)
    total = 0
    for i, ch in enumerate(data):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch) - 55          # A=10 ... Z=35
        elif ch == "<":
            v = 0
        else:
            return ""
        total += v * weights[i % 3]
    return str(total % 10)


def _mrz_line_valid(line2: str) -> bool:
    """Sanity-check a TD3 second MRZ line via its passport-number check digit.
    Lets us REJECT garbled OCR (which otherwise produces names like
    'SUFYAN KKKKSKK ALI') instead of returning rubbish to the user."""
    if len(line2) < 28:
        return False
    pno = line2[0:9]
    expected = line2[9]
    calc = _mrz_check_digit(pno)
    # If the check digit matches, we trust the line. If Tesseract couldn't
    # read the check digit as a digit, fall back to a softer heuristic
    # (passport number must be mostly alnum, not filler noise).
    if expected.isdigit() and calc:
        return calc == expected
    alnum = sum(1 for c in pno if c.isalnum())
    return alnum >= 6


def _ocr_mrz_strip(img: Image.Image) -> str:
    """Run a dedicated OCR pass on the BOTTOM strip of the passport where the
    two MRZ lines live, restricting Tesseract to the MRZ character set. This
    is far more reliable than reading the MRZ out of a full-page OCR dump."""
    if not _TESSERACT_OK or pytesseract is None:
        return ""
    w, h = img.size
    # MRZ occupies roughly the bottom ~22% of a passport bio page.
    strip = img.crop((0, int(h * 0.74), w, h))
    # Binarize hard — MRZ is high-contrast OCR-B on white.
    strip = ImageOps.autocontrast(strip, cutoff=0)
    cfg = ("--oem 1 --psm 6 "
           "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789< "
           "-c preserve_interword_spaces=1")
    try:
        return pytesseract.image_to_string(strip, lang="eng", config=cfg)
    except Exception:
        return ""


# ----------------------------------------------------------------------
# MRZ parsing (ICAO 9303 — TD3 / 2-line passport)
# ----------------------------------------------------------------------
# Line 1: P<COUNTRY<SURNAME<<GIVEN<NAMES<<<<<...      (44 chars)
# Line 2: PASSPORT_NO<C COUNTRY DOB<C SEX EXPIRY<C ... (44 chars)
MRZ_LINE_RE = re.compile(r"^[A-Z0-9<]{30,}$")


def _yymmdd_to_iso(s: str, future_pivot: int = 30) -> Optional[str]:
    """Convert 'YYMMDD' → 'YYYY-MM-DD'. If YY > pivot we assume 19xx; else 20xx."""
    if not s or len(s) != 6 or not s.isdigit():
        return None
    yy = int(s[0:2]); mm = int(s[2:4]); dd = int(s[4:6])
    year = 1900 + yy if yy > future_pivot else 2000 + yy
    try:
        return datetime(year, mm, dd).date().isoformat()
    except ValueError:
        return None


def _clean_mrz_name(seg: str) -> str:
    """Replace `<` with space and collapse whitespace.

    Tesseract frequently mis-reads the MRZ filler character ``<`` as a run of
    ``C`` / ``E`` / ``K`` glyphs (they share a similar profile in the OCR-B
    font). A long, vowel-less, all-consonant token at the end of a name segment
    is therefore almost always filler noise, not a real name part — so we strip
    any trailing junk token of 4+ consonants with no vowels.
    """
    txt = re.sub(r"\s+", " ", seg.replace("<", " ")).strip()
    cleaned_parts = []
    for part in txt.split(" "):
        if not part:
            continue
        # Trim trailing MRZ-filler OCR noise. Tesseract reads the long run of
        # filler ``<`` glyphs as a repetitive block of C/E/K/L glyphs which
        # often gets glued onto the end of the last real name token (e.g.
        # ``SAQIBECCCCCCCCECECECEC``). A genuine name never ends with a 4+ run
        # built only from that confusable glyph set, so strip such a tail.
        m = re.search(r"[CEKL]{4,}$", part)
        if m and m.start() > 0:
            part = part[: m.start()]
        if not part:
            continue
        # Also drop a token that is ENTIRELY filler-glyph noise.
        if len(part) >= 6 and re.fullmatch(r"[CEKLIT]+", part) and len(set(part)) <= 3:
            continue
        # Reject obviously-garbled OCR tokens: a real name part of 4+ letters
        # must contain at least one vowel. A run like "KKKKSKK" / "BCDFG" is
        # MRZ-filler noise mis-read by Tesseract, never a name. (Problem 4 —
        # the "SUFYAN KKKKSKK ALI" garbage.)
        if len(part) >= 4 and not re.search(r"[AEIOU]", part):
            continue
        # Drop a token with 3+ identical consecutive letters (e.g. "KKKK"),
        # which never occurs in real names but is classic filler-glyph noise.
        if re.search(r"(.)\1\1", part):
            continue
        cleaned_parts.append(part)
    return " ".join(cleaned_parts).strip()


# Map ISO-3 country codes (Pakistani passports mostly say PAK) → full names.
ISO3_COUNTRY = {
    "PAK": "Pakistan", "IND": "India", "SAU": "Saudi Arabia",
    "ARE": "United Arab Emirates", "QAT": "Qatar", "OMN": "Oman",
    "KWT": "Kuwait", "BHR": "Bahrain", "JOR": "Jordan",
    "USA": "United States", "GBR": "United Kingdom", "CAN": "Canada",
    "AUS": "Australia", "DEU": "Germany", "FRA": "France",
    "BGD": "Bangladesh", "LKA": "Sri Lanka", "NPL": "Nepal",
    "AFG": "Afghanistan", "IRN": "Iran", "TUR": "Türkiye",
    "MYS": "Malaysia", "IDN": "Indonesia",
}


def parse_mrz(text: str) -> Optional[Dict[str, Any]]:
    """Find and parse a TD3 (2-line passport) MRZ block inside OCR text.
    Returns None if no MRZ-looking block is found.
    """
    lines = [re.sub(r"\s+", "", ln).upper() for ln in text.splitlines() if ln.strip()]
    # Find 2 consecutive lines of length 40+ made of [A-Z 0-9 <]
    for i in range(len(lines) - 1):
        l1, l2 = lines[i], lines[i + 1]
        if MRZ_LINE_RE.match(l1) and MRZ_LINE_RE.match(l2) and l1.startswith(("P<", "P", "PP", "PM", "PS")):
            return _parse_td3(l1.ljust(44, "<")[:44], l2.ljust(44, "<")[:44])
    # Sometimes line 1 is mis-read and starts mid-name. Try to use line 2 alone.
    for i, ln in enumerate(lines):
        if MRZ_LINE_RE.match(ln) and len(ln) >= 40 and ln[0].isalnum() and "<" in ln[8:14]:
            # second line heuristic — extract what we can
            try:
                return _parse_td3("P<UNK", ln.ljust(44, "<")[:44])
            except (ValueError, IndexError, KeyError):
                # Malformed MRZ line — try the next candidate.
                continue
    return None


def _parse_td3(line1: str, line2: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"raw_mrz": line1 + "\n" + line2}

    # ---- Line 1 ---- P<ISSUER<SURNAME<<GIVEN<NAMES<<<<...
    country_code = line1[2:5].replace("<", "")
    out["issuing_authority_code"] = country_code
    out["issuing_authority"] = ISO3_COUNTRY.get(country_code, country_code) or "PAKISTAN"

    name_segment = line1[5:].rstrip("<")
    if "<<" in name_segment:
        surname, given = name_segment.split("<<", 1)
        out["surname"] = _clean_mrz_name(surname)
        out["given_names"] = _clean_mrz_name(given)
        # Full Name: GIVEN SURNAME
        out["full_name"] = (out["given_names"] + " " + out["surname"]).strip()
    else:
        out["full_name"] = _clean_mrz_name(name_segment)

    # ---- Line 2 ----
    # 0-8   passport number (+ check digit at 9)
    # 10-12 nationality
    # 13-18 date of birth
    # 19    check digit
    # 20    sex
    # 21-26 expiry date
    # 27    check digit
    # 28-41 personal number
    out["passport_no"] = line2[0:9].replace("<", "").strip()
    nat = line2[10:13].replace("<", "")
    out["nationality_code"] = nat
    out["nationality"] = (ISO3_COUNTRY.get(nat, nat) or "PAKISTANI").upper()
    if out["nationality"] == "PAKISTAN":
        out["nationality"] = "PAKISTANI"
    out["date_of_birth"] = _yymmdd_to_iso(line2[13:19])
    sex = line2[20]
    out["gender"] = "Male" if sex == "M" else ("Female" if sex == "F" else "")
    out["passport_expiry_date"] = _yymmdd_to_iso(line2[21:27], future_pivot=90)
    personal_no = line2[28:42].replace("<", "").strip()
    # In many Pakistani passports the CNIC (13 digits) shows up in the personal-no field
    if personal_no:
        digits_only = re.sub(r"\D", "", personal_no)
        if len(digits_only) == 13:
            out["cnic"] = f"{digits_only[:5]}-{digits_only[5:12]}-{digits_only[12]}"
        elif len(digits_only) >= 5:
            # Only treat as a real token if it actually contains a meaningful
            # run of digits — otherwise it's MRZ-filler OCR noise (e.g. 'CCC').
            out["nadra_token_no"] = personal_no

    return out


# ----------------------------------------------------------------------
# Fallback: pattern-based extraction (no MRZ)
# ----------------------------------------------------------------------
def parse_text_fallback(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    upper = text.upper()

    # Passport number — typically 1 letter + 7 digits OR 8-9 alnum
    m = re.search(r"\b([A-Z]{1,2}\d{6,8})\b", upper)
    if m:
        out["passport_no"] = m.group(1)

    # CNIC pattern XXXXX-XXXXXXX-X
    m = re.search(r"\b(\d{5}-\d{7}-\d)\b", text)
    if m:
        out["cnic"] = m.group(1)

    # Dates: dd/mm/yyyy or dd MMM yyyy
    dates = re.findall(r"(\d{1,2}[/\-.\s][A-Z0-9]{1,3}[/\-.\s]\d{2,4})", upper)
    parsed = []
    for d in dates:
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y"):
            try:
                parsed.append(datetime.strptime(d.strip(), fmt).date().isoformat())
                break
            except ValueError:
                continue
    parsed = sorted(set(parsed))
    if len(parsed) >= 1:
        out["date_of_birth"] = parsed[0]
    if len(parsed) >= 2:
        out["passport_issue_date"] = parsed[-2] if len(parsed) >= 3 else parsed[0]
        out["passport_expiry_date"] = parsed[-1]
    elif len(parsed) == 1:
        out["date_of_birth"] = parsed[0]

    # Sex / Gender
    if re.search(r"\bSEX[:\s]*F\b", upper):
        out["gender"] = "Female"
    elif re.search(r"\bSEX[:\s]*M\b", upper):
        out["gender"] = "Male"

    # Father / surname / given-name labels (multiple passport formats).
    # IMPORTANT: keep the capture on a SINGLE line ([^\n] not \s) so we don't
    # greedily swallow the next label/line (which produced values like
    # "Ghulam Ali\n\nDate Of Birth"). We also trim any trailing label word.
    STOP_WORDS = ("DATE", "PLACE", "SEX", "GENDER", "NATIONALITY", "PASSPORT",
                  "BIRTH", "ISSUE", "EXPIRY", "AUTHORITY", "CNIC", "NUMBER",
                  "NO", "TYPE", "CODE", "HUSBAND", "MOTHER", "FATHER")
    for label, key in [
        ("FATHER'S NAME", "father_name"), ("FATHER", "father_name"),
        ("HUSBAND", "father_name"),
        ("MOTHER", "mother_name"),
        ("SURNAME", "surname"), ("GIVEN NAMES", "given_names"),
    ]:
        if key in out:
            continue
        m = re.search(rf"{label}\s*[:\.]?\s*([A-Z][A-Z .\-]{{1,40}})", upper)
        if m:
            val = m.group(1).strip()
            # Drop a trailing label word that ran on from the next line.
            words = val.split()
            while words and words[-1] in STOP_WORDS:
                words.pop()
            val = " ".join(words).strip(" .-")
            if len(val) >= 2:
                out[key] = val.title()

    # Place of issue (Pakistani passports print "Place of Issue: SIALKOT")
    m = re.search(r"PLACE OF ISSUE\s*[:\.]?\s*([A-Z][A-Z .\-]{2,30})", upper)
    if m:
        words = m.group(1).split()
        while words and words[-1] in STOP_WORDS:
            words.pop()
        if words:
            out["passport_issue_place"] = " ".join(words).strip(" .-").title()

    if "surname" in out and "given_names" in out:
        out["full_name"] = (out["given_names"] + " " + out["surname"]).strip()

    return out


# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------
def extract_passport_data(image_bytes: bytes) -> Dict[str, Any]:
    """Run the full passport-OCR pipeline on raw image bytes.

    Pipeline (best → fallback):
      1. **AI Vision** (gpt-5 vision via LLM proxy) — most accurate, reads the
         whole bio-data page + MRZ. Used when an LLM key is configured.
      2. **Tesseract MRZ** — deterministic ICAO-9303 parse (offline).
      3. **Tesseract text regex** — loose pattern extraction (offline).

    Returns a dict like:
        {
          "ok": True,
          "method": "vision" | "mrz" | "text",
          "fields": { full_name, passport_no, date_of_birth, ... },
          "raw_text": "...",
        }
    """
    # ---- 1) AI Vision (primary) -------------------------------------------
    try:
        from app.services import passport_vision
        if passport_vision.available():
            vfields = passport_vision.extract_with_vision(image_bytes)
            if vfields and (vfields.get("passport_no") or vfields.get("full_name")):
                return {
                    "ok": True,
                    "method": "vision",
                    "fields": vfields,
                    "raw_text": "(extracted by AI vision)",
                }
    except Exception as exc:   # noqa: BLE001 — vision SDK can raise many provider-specific errors
        # The whole point of this block is "try cloud, fall back to local
        # Tesseract" — so we log + continue rather than re-raising. The
        # next block (offline OCR) handles the user-visible failure path.
        import logging
        logging.getLogger("dtc.passport_ocr").warning(
            "AI vision OCR failed, falling back to offline Tesseract: %s", exc
        )

    # ---- 2/3) Offline Tesseract path --------------------------------------
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        return {"ok": False, "error": f"Could not open image: {e}", "fields": {}, "raw_text": ""}

    img = _preprocess(img)

    if not _TESSERACT_OK or pytesseract is None:
        return {
            "ok": False,
            "error": ("Automatic reading is unavailable on this server right now "
                      "(AI vision key not active and Tesseract not installed). "
                      "Please type the passport details into the form — the fields "
                      "are ready below."),
            "fields": {},
            "raw_text": "",
        }

    config = "--oem 1 --psm 6 -c preserve_interword_spaces=1"
    try:
        text = pytesseract.image_to_string(img, lang="eng", config=config)
    except Exception as e:
        return {"ok": False, "error": f"OCR failed: {e}", "fields": {}, "raw_text": ""}

    # --- Dedicated MRZ pass (most reliable, offline) -----------------------
    # Read the bottom MRZ strip with a restricted OCR-B-friendly charset, then
    # validate it with the ICAO check digit so we only trust a CLEAN read and
    # never hand back garbled names. (Problem 4)
    mrz_text = _ocr_mrz_strip(img)
    for candidate_text in (mrz_text, text):
        if not candidate_text:
            continue
        lines = [re.sub(r"\s+", "", ln).upper()
                 for ln in candidate_text.splitlines() if ln.strip()]
        # locate the TD3 second line (passport-number line) and validate it
        for i in range(len(lines)):
            l2 = lines[i].ljust(44, "<")[:44]
            if MRZ_LINE_RE.match(lines[i]) and _mrz_line_valid(l2):
                mrz = parse_mrz(candidate_text)
                if mrz and (mrz.get("passport_no") or mrz.get("full_name")):
                    fields = {k: v for k, v in mrz.items() if v not in (None, "", [])}
                    fields.pop("raw_mrz", None)
                    # Enrich with father/issue/place from the full-page text —
                    # the MRZ doesn't carry these but the printed bio-data does.
                    extra = parse_text_fallback(text)
                    for k in ("father_name", "passport_issue_date",
                              "passport_issue_place", "place_of_birth", "cnic"):
                        if not fields.get(k) and extra.get(k):
                            fields[k] = extra[k]
                    return {"ok": True, "method": "mrz", "fields": fields, "raw_text": text}
                break

    # Legacy MRZ parse (no strict validation) as a softer attempt
    mrz = parse_mrz(text)
    if mrz and (mrz.get("passport_no") or mrz.get("full_name")):
        fields = {k: v for k, v in mrz.items() if v not in (None, "", [])}
        fields.pop("raw_mrz", None)
        return {"ok": True, "method": "mrz", "fields": fields, "raw_text": text}

    # Fallback to general pattern extraction
    fb = parse_text_fallback(text)
    if fb:
        return {"ok": True, "method": "text", "fields": fb, "raw_text": text}

    return {"ok": False, "error": "Could not extract passport data — try a sharper image.",
            "fields": {}, "raw_text": text}
