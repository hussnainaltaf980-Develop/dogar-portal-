"""E-Barcode renderer: passport number is the 2nd barcode (not file number)."""
from __future__ import annotations


def test_render_e_barcode_uses_passport_number_as_second_barcode():
    """The E-Barcode hotfix: 2nd barcode value must be passport_number, not file_no."""
    from app.services.letterhead_renderer import render_e_barcode

    class FakeCand:
        id = 999
        full_name        = "Test Candidate"
        father_name      = "Test Father"
        passport_no      = "ME1855631"
        e_number         = "E815207393"      # 1st barcode
        file_no          = "F-12345"
        nationality      = "Pakistani"
        # everything else can be None — renderer is defensive

    html = render_e_barcode(FakeCand(), file_number="F-12345", auto_print=False)

    assert isinstance(html, str) and len(html) > 100
    # the passport number must appear in the rendered output
    assert "ME1855631" in html, \
        "E-Barcode output should contain the candidate's passport number"
    # there must be at least 2 barcode <img> elements (data:image/png URLs)
    assert html.count("data:image/png;base64,") >= 2, \
        "E-Barcode should embed at least 2 barcode PNG data URLs"


def test_render_e_barcode_clean_layout_no_dev_footer():
    """Print output must not leak developer-debug footers."""
    from app.services.letterhead_renderer import render_e_barcode

    class FakeCand:
        id = 1
        full_name = "X"
        father_name = "Y"
        passport_no = "P123"
        e_number    = "E123"
        file_no     = "F-1"
        nationality = "Pakistani"

    html = render_e_barcode(FakeCand(), auto_print=True).lower()
    # The hotfix removed dev-only debug strings from the print layout
    for forbidden in ("debug", "lorem ipsum", "todo:", "fixme"):
        assert forbidden not in html, \
            f"E-Barcode print output should not contain '{forbidden}'"
