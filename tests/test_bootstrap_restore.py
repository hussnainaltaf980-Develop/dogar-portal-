"""Clean-distribution invariants + optional bundled-SQL restore path.

The production-hardened package ships WITHOUT any bundled SQL dump,
databases, or demo data — a fresh schema is created on first boot. These
tests assert that invariant, and exercise the optional restore code path
only when a dump is present (e.g. an operator dropped one in manually).
"""
from __future__ import annotations
import os
import sqlite3
import tempfile

import pytest

_DUMP_CANDIDATES = [
    "migrations/dogar_full_backup.sql.gz",
    "migrations/dogar_full_backup.sql",
]


def _present_dump() -> str | None:
    for p in _DUMP_CANDIDATES:
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            return p
    return None


def test_clean_package_ships_without_bundled_dump():
    """The clean distribution must NOT bundle a production SQL dump.

    Shipping a real-data dump would (a) bloat the package and (b) auto-
    inject third-party data on first boot. The clean build starts from an
    empty schema instead.
    """
    assert _present_dump() is None, (
        "A bundled SQL dump was found. The clean package must not ship "
        "migrations/dogar_full_backup.sql[.gz]; a fresh DB is created on boot."
    )


def test_bundled_dump_restores_full_dataset():
    """If an operator DOES drop a dump in, the restore path must work.

    Skipped in the clean package (no dump present).
    """
    src = _present_dump()
    if not src:
        pytest.skip("No bundled SQL dump present (clean distribution)")

    if src.endswith(".gz"):
        import gzip
        with gzip.open(src, "rt", encoding="utf-8") as fh:
            sql = fh.read()
    else:
        with open(src, "r", encoding="utf-8") as fh:
            sql = fh.read()

    fd, path = tempfile.mkstemp(suffix=".db", prefix="dtc_restore_test_")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(sql)
        conn.commit()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM document_templates")
        assert c.fetchone()[0] >= 10
        conn.close()
    finally:
        for ext in ("", "-shm", "-wal"):
            try:
                os.unlink(path + ext)
            except OSError:
                pass
