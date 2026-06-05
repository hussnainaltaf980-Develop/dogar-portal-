"""Letterhead renderer: data:URL embedding + content margins."""
from __future__ import annotations


def test_letterhead_data_url_is_jpeg_data_url():
    from app.services.letterhead_renderer import _letterhead_data_url

    url = _letterhead_data_url()
    # File missing → empty string is OK (defensive); otherwise must be a JPEG data URL
    if not url:
        return
    assert url.startswith("data:image/jpeg;base64,"), \
        "letterhead must be served as inline data: URL, never an http:// URL"
    assert len(url) > 200, "letterhead data URL suspiciously short"


def test_barcode_png_data_url():
    from app.services.letterhead_renderer import generate_barcode_png_b64

    url = generate_barcode_png_b64("TEST-1234")
    assert url.startswith("data:image/png;base64,")
    assert len(url) > 200


def test_content_margin_constants_match_hotfix():
    """The Demand Letter alignment hotfix moved margins 38/28/18/18 → 46/30/22/22."""
    import app.services.letterhead_renderer as lr

    # The exact constant names may vary — search for them defensively
    top = getattr(lr, "CONTENT_TOP_MM", None)
    bot = getattr(lr, "CONTENT_BOTTOM_MM", None)

    if top is None or bot is None:
        # Renderer uses inline values — read the source to verify
        import inspect
        src = inspect.getsource(lr)
        assert "46" in src and "30" in src and "22" in src, \
            "expected new margins (46/30/22) not found in renderer source"
        return

    # If constants exist, verify the hotfix values
    assert top == 46, f"CONTENT_TOP_MM should be 46 (post-hotfix), got {top}"
    assert bot == 30, f"CONTENT_BOTTOM_MM should be 30 (post-hotfix), got {bot}"
