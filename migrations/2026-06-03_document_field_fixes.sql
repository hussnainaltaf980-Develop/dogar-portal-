-- ============================================================================
-- Document field coordinate / type fixes (2026-06-03)
-- ============================================================================
-- Purpose: Align candidate document data fields/coordinates to the demo portal
--          reference and fix mislabeled static fields that ignored resolved
--          candidate data. The runtime SQLite DB (data/dogar_trading.db) is
--          gitignored, so this patch makes the document_fields changes
--          reproducible and version-controlled.
--
-- Apply with:
--   sqlite3 data/dogar_trading.db < migrations/2026-06-03_document_field_fixes.sql
--
-- Affects table: document_fields
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Template 1 — Allied Bank Deposit Form (Form-7)
-- Coordinates measured against background allied_bank_form7.jpg (723x1024),
-- scaled to PDF (A4 595x842), scale factor ~0.822.
-- ----------------------------------------------------------------------------
UPDATE document_fields SET field_type='text',       x=138.0, y=653.0, width=250.0, font_size=10.0, meta=NULL
  WHERE id=1;   -- full_name (Section B depositor name)
UPDATE document_fields SET field_type='char_cells',  x=357.0, y=641.0, width=240.0, font_size=10.0,
  meta='{"format":"digits","cell_count":13,"cell_width":11.4,"draw_boxes":false}'
  WHERE id=2;   -- cnic (Section B id digit-cells)
UPDATE document_fields SET field_type='text',       x=185.0, y=641.0, width=200.0, font_size=10.0, meta=NULL
  WHERE id=3;   -- phone
UPDATE document_fields SET field_type='text',       x=185.0, y=631.0, width=360.0, font_size=8.5,  meta=NULL
  WHERE id=4;   -- address
UPDATE document_fields SET field_type='text',       x=423.0, y=619.0, width=170.0, font_size=10.0, meta=NULL
  WHERE id=5;   -- passport_no
UPDATE document_fields SET field_type='text',       x=448.0, y=750.0, width=130.0, font_size=10.0, meta=NULL
  WHERE id=6;   -- __today__ (deposit date)
UPDATE document_fields SET field_type='text',       x=234.0, y=503.0, width=180.0, font_size=10.0, meta=NULL
  WHERE id=7;   -- salary (amount in figures)

-- Section A depositor (added this session): name + CNIC digit-cells
-- NOTE: ids 3762/3763 are the values created this session. If applying on a
-- fresh DB where these ids do not exist, insert them (template_id=1).
INSERT OR IGNORE INTO document_fields (id, template_id, label, field_key, field_type, static_value, x, y, width, height, font_size, page, meta)
  VALUES (3762, 1, 'Depositor Name (Section A)', 'full_name', 'text', '', 138.0, 698.0, 250.0, 18.0, 10.0, 1, '{}');
INSERT OR IGNORE INTO document_fields (id, template_id, label, field_key, field_type, static_value, x, y, width, height, font_size, page, meta)
  VALUES (3763, 1, 'Depositor CNIC (Section A)', 'cnic', 'char_cells', '', 357.0, 686.0, 240.0, 18.0, 10.0, 1, '{"format":"digits","cell_count":13,"cell_width":11.4,"draw_boxes":false}');
UPDATE document_fields SET field_type='text',       x=138.0, y=698.0, width=250.0, font_size=10.0, meta='{}'
  WHERE id=3762;
UPDATE document_fields SET field_type='char_cells',  x=357.0, y=686.0, width=240.0, font_size=10.0,
  meta='{"format":"digits","cell_count":13,"cell_width":11.4,"draw_boxes":false}'
  WHERE id=3763;

-- ----------------------------------------------------------------------------
-- Template 16 — OEP Form
-- ----------------------------------------------------------------------------
UPDATE document_fields SET field_type='text', meta='{}'
  WHERE id=147;  -- father_name: was 'static' with empty value (ignored resolved data)
UPDATE document_fields SET field_type='text', x=420.0, y=642.0, width=130.0, font_size=12.0,
  meta='{"format":"dd-mm-yyyy"}'
  WHERE id=149;  -- date_of_birth: render clean 07-05-1992 instead of 07-May-1992
UPDATE document_fields SET field_type='text', width=300.0, meta='{}'
  WHERE id=156;  -- email: was 'checkbox', changed to text
-- Stray "Personal Driver" profession field removed this session:
DELETE FROM document_fields WHERE id=152;

-- ----------------------------------------------------------------------------
-- Template 20 — (barcodes + Arabic + photo form)
-- Fix full_name / father_name that were 'static' with empty static_value.
-- ----------------------------------------------------------------------------
UPDATE document_fields SET field_type='text', meta='{}' WHERE id=186;  -- full_name
UPDATE document_fields SET field_type='text', meta='{}' WHERE id=188;  -- father_name
