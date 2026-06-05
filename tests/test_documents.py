"""Documents API: template listing + bulk-save dedupe/REPLACE-ALL semantics."""
from __future__ import annotations


def test_list_templates_requires_auth(client):
    r = client.get("/api/documents/templates")
    assert r.status_code == 401


def test_list_templates_returns_seeded_templates(client, auth_headers):
    r = client.get("/api/documents/templates", headers=auth_headers)
    assert r.status_code == 200, r.text
    tpls = r.json()
    assert isinstance(tpls, list)
    # init_db seeds a known list of templates
    assert len(tpls) > 0, "no document templates seeded"
    sample = tpls[0]
    assert "id" in sample and "name" in sample


def test_create_and_bulk_save_fields_replace_all_semantics(client, auth_headers):
    # 1. create a fresh template just for this test (so we don't pollute the seeded set)
    r = client.post(
        "/api/documents/templates",
        headers=auth_headers,
        json={"name": "TestTemplateBulkSave", "data_source": "candidate"},
    )
    assert r.status_code == 200, r.text
    tpl_id = r.json()["id"]

    # 2. bulk-save N fields
    fields_v1 = [
        {"field_key": f"field_{i}", "label": f"F{i}",
         "x_mm": float(i), "y_mm": float(i), "font_size": 10, "font_bold": False}
        for i in range(5)
    ]
    r = client.post(
        f"/api/documents/templates/{tpl_id}/fields/bulk",
        headers=auth_headers,
        json={"fields": fields_v1},
    )
    assert r.status_code == 200, r.text

    # 3. verify exactly 5 saved
    r = client.get(f"/api/documents/templates/{tpl_id}/fields", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()) == 5, f"expected 5 fields, got {len(r.json())}"

    # 4. REPLACE-ALL — send only 3 fields, verify old 5 are gone and only 3 remain
    fields_v2 = [
        {"field_key": f"new_{i}", "label": f"N{i}",
         "x_mm": 100.0, "y_mm": 100.0, "font_size": 12, "font_bold": True}
        for i in range(3)
    ]
    r = client.post(
        f"/api/documents/templates/{tpl_id}/fields/bulk",
        headers=auth_headers,
        json={"fields": fields_v2},
    )
    assert r.status_code == 200

    r = client.get(f"/api/documents/templates/{tpl_id}/fields", headers=auth_headers)
    assert r.status_code == 200
    final = r.json()
    assert len(final) == 3, \
        f"REPLACE-ALL semantics failed: expected 3 fields, got {len(final)}"
    keys = {f["field_key"] for f in final}
    assert keys == {"new_0", "new_1", "new_2"}, f"unexpected keys: {keys}"


def test_bulk_save_dedupes_exact_position_duplicates(client, auth_headers):
    """If the same field_key at the same x/y is sent multiple times, only one survives."""
    r = client.post(
        "/api/documents/templates",
        headers=auth_headers,
        json={"name": "TestTemplateDedupe", "data_source": "candidate"},
    )
    assert r.status_code == 200
    tpl_id = r.json()["id"]

    # 10 copies of the exact same field
    dupes = [
        {"field_key": "name", "label": "Name", "x_mm": 50.0, "y_mm": 50.0,
         "font_size": 10, "font_bold": False}
        for _ in range(10)
    ]
    r = client.post(
        f"/api/documents/templates/{tpl_id}/fields/bulk",
        headers=auth_headers,
        json={"fields": dupes},
    )
    assert r.status_code == 200

    r = client.get(f"/api/documents/templates/{tpl_id}/fields", headers=auth_headers)
    assert r.status_code == 200
    saved = r.json()
    assert len(saved) == 1, \
        f"dedupe failed: 10 identical fields produced {len(saved)} rows"


def test_dedupe_endpoint_clears_existing_duplicates(client, auth_headers):
    """The /dedupe endpoint should be idempotent — running it on clean data
    leaves the row count unchanged."""
    r = client.post(
        "/api/documents/templates",
        headers=auth_headers,
        json={"name": "TestTemplateDedupeEndpoint", "data_source": "candidate"},
    )
    assert r.status_code == 200
    tpl_id = r.json()["id"]

    r = client.post(
        f"/api/documents/templates/{tpl_id}/fields/bulk",
        headers=auth_headers,
        json={"fields": [
            {"field_key": "a", "label": "A", "x_mm": 1.0, "y_mm": 1.0,
             "font_size": 10, "font_bold": False},
            {"field_key": "b", "label": "B", "x_mm": 2.0, "y_mm": 2.0,
             "font_size": 10, "font_bold": False},
        ]},
    )
    assert r.status_code == 200

    before = client.get(f"/api/documents/templates/{tpl_id}/fields",
                        headers=auth_headers).json()
    r = client.post(f"/api/documents/templates/{tpl_id}/fields/dedupe",
                    headers=auth_headers)
    assert r.status_code == 200, r.text
    after = client.get(f"/api/documents/templates/{tpl_id}/fields",
                       headers=auth_headers).json()
    assert len(before) == len(after) == 2, \
        f"dedupe changed clean data: before={len(before)} after={len(after)}"
