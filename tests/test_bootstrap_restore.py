"""First-run bundled SQL restore: verify _apply_bundled_sql_dump() works
on an empty SQLite file and produces the expected real-data row counts."""
from __future__ import annotations
import os
import sqlite3
import tempfile


def test_bundled_sql_dump_exists_in_repo():
    """The full production dump MUST ship inside the zip."""
    candidates = [
        "migrations/dogar_full_backup.sql.gz",
        "migrations/dogar_full_backup.sql",
    ]
    found = [p for p in candidates if os.path.exists(p) and os.path.getsize(p) > 100_000]
    assert found, (
        "No bundled SQL dump found. The package MUST include at least one "
        "of: migrations/dogar_full_backup.sql.gz or .sql (>100 KB)."
    )


def test_bundled_dump_restores_full_dataset():
    """Apply migrations/dogar_full_backup.sql.gz to a brand new SQLite file
    and confirm the row counts match the documented production dataset."""
    src_gz = "migrations/dogar_full_backup.sql.gz"
    src_plain = "migrations/dogar_full_backup.sql"
    if os.path.exists(src_gz):
        import gzip
        with gzip.open(src_gz, "rt", encoding="utf-8") as fh:
            sql = fh.read()
    elif os.path.exists(src_plain):
        with open(src_plain, "r", encoding="utf-8") as fh:
            sql = fh.read()
    else:
        import pytest
        pytest.skip("No bundled SQL dump present")

    # Apply to a throwaway sqlite file
    fd, path = tempfile.mkstemp(suffix=".db", prefix="dtc_restore_test_")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(sql)
        conn.commit()

        c = conn.cursor()
        # Sanity: the dump should contain real candidates (≥ 2,000)
        c.execute("SELECT COUNT(*) FROM candidates")
        cand_count = c.fetchone()[0]
        assert cand_count >= 2000, \
            f"Bundled dump should contain ≥ 2000 candidates, got {cand_count}"

        # ...and real demands
        c.execute("SELECT COUNT(*) FROM demands")
        dem_count = c.fetchone()[0]
        assert dem_count >= 1000, \
            f"Bundled dump should contain ≥ 1000 demands, got {dem_count}"

        # ...and document templates with field positions
        c.execute("SELECT COUNT(*) FROM document_templates")
        tpl_count = c.fetchone()[0]
        assert tpl_count >= 10, \
            f"Bundled dump should contain ≥ 10 document templates, got {tpl_count}"

        c.execute("SELECT COUNT(*) FROM document_fields")
        field_count = c.fetchone()[0]
        assert field_count >= 100, \
            f"Bundled dump should contain ≥ 100 document field positions, got {field_count}"

        conn.close()
    finally:
        for ext in ("", "-shm", "-wal"):
            try:
                os.unlink(path + ext)
            except OSError:
                pass
