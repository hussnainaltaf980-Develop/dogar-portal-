"""Seed sensible default field placements for all document templates that
currently have zero fields, so the Print PDF flow produces filled documents
out of the box. Coordinates are in PDF-points (origin = bottom-left, A4 page
595 x 842 pt).

The placements are "good-enough defaults" — the template designer UI lets the
user fine-tune positions later via drag-and-drop without losing the data.
"""
import sqlite3
import os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "dogar_trading.db")
DB = os.path.abspath(DB)


# Field layout templates -----------------------------------------------------
# Each entry: (label, field_key, x, y, width, font_size, bold, align)

# Candidate-centric forms (Allied Bank, HBL, NBP, OEP, Protector, State Life,
# Visa Application Forms, etc.) — use a vertical key/value layout with the
# candidate's identity, passport, and address starting near the top.
CANDIDATE_LAYOUT = [
    ("Full Name",        "full_name",            150, 740, 380, 11, True,  "left"),
    ("Father Name",      "father_name",          150, 715, 380, 11, False, "left"),
    ("CNIC",             "cnic",                 150, 690, 250, 11, False, "left"),
    ("Passport No",      "passport_no",          400, 690, 160, 11, False, "left"),
    ("Date of Birth",    "date_of_birth",        150, 665, 200, 11, False, "left"),
    ("Nationality",      "nationality",          400, 665, 160, 11, False, "left"),
    ("Profession",       "profession",           150, 640, 250, 11, False, "left"),
    ("Passport Expiry",  "passport_expiry_date", 400, 640, 160, 11, False, "left"),
    ("Address",          "address",              150, 615, 410, 10, False, "left"),
    ("District",         "district",             150, 590, 250, 11, False, "left"),
    ("Province",         "province",             400, 590, 160, 11, False, "left"),
    ("Phone",            "phone",                150, 565, 200, 11, False, "left"),
    ("Next of Kin",      "next_of_kin_name",     150, 540, 300, 11, False, "left"),
    ("Relation",         "next_of_kin_relation", 400, 540, 160, 11, False, "left"),
    ("Today's Date",     "__today__",            450, 110, 100, 10, True,  "left"),
]

# Demand-centric forms (Demand Letter, Permission Undertakings, Permissions
# for Recruitment) — sponsor + visa info layout.
DEMAND_LAYOUT = [
    ("File Number",      "file_number",          150, 740, 300, 12, True,  "left"),
    ("Permission No",    "permission_no",        150, 715, 300, 11, False, "left"),
    ("Permission Date",  "permission_date",      150, 690, 200, 11, False, "left"),
    ("Receiving Date",   "receiving_date",       400, 690, 160, 11, False, "left"),
    ("Sponsor Name",     "sponsor_name",         150, 660, 400, 11, True,  "left"),
    ("Sponsor (Arabic)", "sponsor_name_arabic",  150, 635, 400, 11, False, "left"),
    ("Sponsor Address",  "sponsor_address",      150, 610, 400, 10, False, "left"),
    ("Sponsor Phone",    "sponsor_phone",        150, 585, 250, 11, False, "left"),
    ("Country",          "country",              400, 585, 160, 11, False, "left"),
    ("Visa Number",      "visa_number",          150, 560, 250, 11, False, "left"),
    ("Visa Issue Date",  "visa_issue_date",      400, 560, 160, 11, False, "left"),
    ("Visa Quota",       "visa_quota",           150, 535, 100, 11, False, "left"),
    ("Embassy",          "embassy",              250, 535, 300, 11, False, "left"),
    ("Today's Date",     "__today__",            450, 110, 100, 10, True,  "left"),
]


# Template-specific overrides where the form has a clear known field layout.
# These are best-effort approximations matching common Saudi/Pakistani forms.
SPECIFIC = {
    "Allied Bank Deposit Form": [
        ("Depositor Name",   "full_name",            180, 695, 360, 11, True,  "left"),
        ("CNIC",             "cnic",                 180, 670, 260, 11, False, "left"),
        ("Phone",            "phone",                430, 670, 130, 11, False, "left"),
        ("Address",          "address",              180, 645, 380, 10, False, "left"),
        ("Passport No",      "passport_no",          180, 615, 260, 11, False, "left"),
        ("Today's Date",     "__today__",            430, 615, 130, 11, True,  "left"),
        ("Amount in Words",  "salary",               180, 480, 380, 11, False, "left"),
    ],
    "HBL - Overseas Employment Corporation": [
        ("Depositor Name",   "full_name",            180, 700, 360, 11, True,  "left"),
        ("CNIC",             "cnic",                 180, 670, 260, 11, False, "left"),
        ("Passport No",      "passport_no",          430, 670, 130, 11, False, "left"),
        ("Phone",            "phone",                180, 640, 260, 11, False, "left"),
        ("Address",          "address",              180, 610, 380, 10, False, "left"),
        ("Today's Date",     "__today__",            430, 640, 130, 11, True,  "left"),
    ],
    "Visa Application Form - Karachi": [
        ("Candidate Name",      "full_name",            155, 698, 250, 11, True,  "left"),
        ("Name (Arabic)",       "name_arabic",          410, 698, 165, 11, False, "left"),
        ("Father Name",         "father_name",          155, 673, 250, 11, False, "left"),
        ("Father (Arabic)",     "father_name_arabic",   410, 673, 165, 11, False, "left"),
        ("Passport No",         "passport_no",          155, 648, 180, 11, False, "left"),
        ("Issue Date",          "passport_issue_date",  340, 648, 110, 11, False, "left"),
        ("Expiry Date",         "passport_expiry_date", 460, 648, 110, 11, False, "left"),
        ("Issue Place",         "passport_issue_place", 155, 623, 250, 11, False, "left"),
        ("Date of Birth",       "date_of_birth",        410, 623, 165, 11, False, "left"),
        ("Place of Birth",      "place_of_birth",       155, 598, 250, 11, False, "left"),
        ("Nationality",         "nationality",          410, 598, 165, 11, False, "left"),
        ("Profession",          "profession",           155, 573, 250, 11, False, "left"),
        ("Religion",            "religion",             410, 573, 165, 11, False, "left"),
        ("Address",             "address",              155, 548, 420, 10, False, "left"),
        ("CNIC",                "cnic",                 155, 523, 250, 11, False, "left"),
        ("Phone",               "phone",                410, 523, 165, 11, False, "left"),
    ],
    "Visa Application Form - Islamabad": [
        ("Candidate Name",      "full_name",            155, 698, 250, 11, True,  "left"),
        ("Name (Arabic)",       "name_arabic",          410, 698, 165, 11, False, "left"),
        ("Father Name",         "father_name",          155, 673, 250, 11, False, "left"),
        ("Passport No",         "passport_no",          155, 648, 180, 11, False, "left"),
        ("Expiry Date",         "passport_expiry_date", 460, 648, 110, 11, False, "left"),
        ("Date of Birth",       "date_of_birth",        410, 623, 165, 11, False, "left"),
        ("Nationality",         "nationality",          410, 598, 165, 11, False, "left"),
        ("Profession",          "profession",           155, 573, 250, 11, False, "left"),
        ("Address",             "address",              155, 548, 420, 10, False, "left"),
        ("Phone",               "phone",                410, 523, 165, 11, False, "left"),
    ],
    "OEP Form": [
        ("Full Name",        "full_name",            150, 735, 380, 12, True,  "left"),
        ("Father Name",      "father_name",          150, 705, 380, 11, False, "left"),
        ("CNIC",             "cnic",                 150, 675, 250, 11, False, "left"),
        ("Date of Birth",    "date_of_birth",        410, 675, 170, 11, False, "left"),
        ("Passport No",      "passport_no",          150, 645, 250, 11, False, "left"),
        ("Passport Expiry",  "passport_expiry_date", 410, 645, 170, 11, False, "left"),
        ("Profession",       "profession",           150, 615, 250, 11, False, "left"),
        ("Nationality",      "nationality",          410, 615, 170, 11, False, "left"),
        ("Address",          "address",              150, 585, 430, 10, False, "left"),
        ("Phone",            "phone",                150, 555, 250, 11, False, "left"),
        ("Email",            "email",                410, 555, 170, 11, False, "left"),
        ("Next of Kin",      "next_of_kin_name",     150, 525, 300, 11, False, "left"),
        ("Relation",         "next_of_kin_relation", 410, 525, 170, 11, False, "left"),
        ("Today's Date",     "__today__",            450, 110, 100, 10, True,  "left"),
    ],
    "Protector Certificate": [
        ("Candidate Name",   "full_name",            150, 700, 400, 12, True,  "left"),
        ("Father Name",      "father_name",          150, 670, 400, 11, False, "left"),
        ("Passport No",      "passport_no",          150, 640, 200, 11, False, "left"),
        ("CNIC",             "cnic",                 360, 640, 200, 11, False, "left"),
        ("Profession",       "profession",           150, 610, 200, 11, False, "left"),
        ("Destination",      "destination",          360, 610, 200, 11, False, "left"),
        ("Protector No",     "protector_no",         150, 580, 200, 11, False, "left"),
        ("Protector Date",   "protector_date",       360, 580, 200, 11, False, "left"),
        ("Address",          "address",              150, 550, 410, 10, False, "left"),
    ],
    "Demand Letter": [
        ("Date",             "__today__",            450, 750, 100, 11, True,  "left"),
        ("Reference",        "file_number",          90,  725, 200, 11, True,  "left"),
        ("Sponsor Name",     "sponsor_name",         90,  680, 400, 11, True,  "left"),
        ("Sponsor (Arabic)", "sponsor_name_arabic",  90,  655, 400, 11, False, "left"),
        ("Sponsor Address",  "sponsor_address",      90,  630, 420, 10, False, "left"),
        ("Country",          "country",              90,  600, 200, 11, False, "left"),
        ("Permission No",    "permission_no",        310, 600, 200, 11, False, "left"),
        ("Visa Number",      "visa_number",          90,  570, 200, 11, False, "left"),
        ("Visa Issue Date",  "visa_issue_date",      310, 570, 200, 11, False, "left"),
        ("Visa Quota",       "visa_quota",           90,  540, 200, 11, False, "left"),
    ],
    "Permission Undertaking 1": DEMAND_LAYOUT,
    "Permission Undertaking 2": DEMAND_LAYOUT,
    "Permission Undertaking 3": DEMAND_LAYOUT,
    "Permissions for Recruitment": DEMAND_LAYOUT,
    "Passport Submission Letter": [
        ("Date",             "__today__",            450, 750, 100, 11, True,  "left"),
        ("Candidate Name",   "full_name",            90,  700, 400, 11, True,  "left"),
        ("Father Name",      "father_name",          90,  675, 400, 11, False, "left"),
        ("Passport No",      "passport_no",          90,  650, 250, 11, False, "left"),
        ("CNIC",             "cnic",                 360, 650, 200, 11, False, "left"),
        ("Profession",       "profession",           90,  620, 250, 11, False, "left"),
        ("Destination",      "destination",          360, 620, 200, 11, False, "left"),
    ],
    "E-Number Enrollment Request Form": [
        ("Date",             "__today__",            450, 750, 100, 11, True,  "left"),
        ("Candidate Name",   "full_name",            150, 700, 410, 12, True,  "left"),
        ("Father Name",      "father_name",          150, 670, 410, 11, False, "left"),
        ("Passport No",      "passport_no",          150, 640, 200, 11, False, "left"),
        ("CNIC",             "cnic",                 360, 640, 200, 11, False, "left"),
        ("Date of Birth",    "date_of_birth",        150, 610, 200, 11, False, "left"),
        ("Profession",       "profession",           360, 610, 200, 11, False, "left"),
        ("Destination",      "destination",          150, 580, 200, 11, False, "left"),
        ("E-Number",         "e_number",             360, 580, 200, 11, True,  "left"),
    ],
    "State Life Insurance Form": [
        ("Candidate Name",   "full_name",            150, 700, 400, 11, True,  "left"),
        ("Father Name",      "father_name",          150, 670, 400, 11, False, "left"),
        ("CNIC",             "cnic",                 150, 640, 250, 11, False, "left"),
        ("Date of Birth",    "date_of_birth",        410, 640, 150, 11, False, "left"),
        ("Passport No",      "passport_no",          150, 610, 250, 11, False, "left"),
        ("Profession",       "profession",           410, 610, 150, 11, False, "left"),
        ("Address",          "address",              150, 580, 410, 10, False, "left"),
        ("Phone",            "phone",                150, 550, 250, 11, False, "left"),
        ("Next of Kin",      "next_of_kin_name",     150, 520, 300, 11, False, "left"),
        ("Relation",         "next_of_kin_relation", 410, 520, 150, 11, False, "left"),
    ],
    "NBP Deposit Slip": [
        ("Depositor Name",   "full_name",            180, 700, 360, 11, True,  "left"),
        ("CNIC",             "cnic",                 180, 670, 250, 11, False, "left"),
        ("Phone",            "phone",                430, 670, 130, 11, False, "left"),
        ("Address",          "address",              180, 640, 380, 10, False, "left"),
        ("Passport No",      "passport_no",          180, 610, 250, 11, False, "left"),
        ("Today's Date",     "__today__",            430, 610, 130, 11, True,  "left"),
    ],
    "NBP Deposit Slip - New": [
        ("Depositor Name",   "full_name",            180, 700, 360, 11, True,  "left"),
        ("CNIC",             "cnic",                 180, 670, 250, 11, False, "left"),
        ("Phone",            "phone",                430, 670, 130, 11, False, "left"),
        ("Address",          "address",              180, 640, 380, 10, False, "left"),
        ("Passport No",      "passport_no",          180, 610, 250, 11, False, "left"),
        ("Today's Date",     "__today__",            430, 610, 130, 11, True,  "left"),
    ],
}


def seed():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT id, name, data_source FROM document_templates ORDER BY id")
    templates = cur.fetchall()

    seeded = 0
    for (tpl_id, name, data_source) in templates:
        cur.execute("SELECT COUNT(*) FROM document_fields WHERE template_id=?", (tpl_id,))
        if cur.fetchone()[0] > 0:
            print(f"  SKIP #{tpl_id} {name!r}: already has fields")
            continue

        layout = SPECIFIC.get(name)
        if layout is None:
            layout = CANDIDATE_LAYOUT if data_source == "candidate" else DEMAND_LAYOUT

        for (label, key, x, y, w, fs, bold, align) in layout:
            cur.execute(
                """
                INSERT INTO document_fields
                  (template_id, label, field_key, field_type, x, y, width, height,
                   font_size, font_bold, font_italic, color, align, page)
                VALUES (?, ?, ?, 'text', ?, ?, ?, 20, ?, ?, 0, '#000000', ?, 1)
                """,
                (tpl_id, label, key, x, y, w, fs, int(bool(bold)), align)
            )
        seeded += 1
        print(f"  + #{tpl_id} {name!r}: placed {len(layout)} fields")

    conn.commit()

    # Verify
    cur.execute("""
        SELECT t.id, t.name, COUNT(f.id)
          FROM document_templates t
          LEFT JOIN document_fields f ON f.template_id = t.id
         GROUP BY t.id
         ORDER BY t.id
    """)
    print("\nFinal field counts:")
    for (tid, n, c) in cur.fetchall():
        print(f"  #{tid:2d}  {n[:40]:40s}  {c} fields")

    conn.close()
    print(f"\nSeeded {seeded} template(s) with default field positions.")


if __name__ == "__main__":
    seed()
