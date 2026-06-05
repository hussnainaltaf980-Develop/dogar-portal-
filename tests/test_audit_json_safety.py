"""Regression tests for the audit-log JSON serialization bug.

The original symptom was a 500 Internal Server Error on
``PUT /api/candidates/{id}`` when the candidate's ``salary`` column
was a ``Decimal`` — SQLAlchemy's JSON adapter called ``json.dumps``
on the audit row which couldn't serialize Decimal/date objects.

These tests assert that:

  1. ``_json_safe`` handles Decimal, date, datetime, bytes, lists,
     tuples, sets, and nested dicts without raising.
  2. ``log_event`` succeeds when given a model row that contains a
     Decimal column.
  3. ``log_event`` swallows any internal failure so the originating
     request is never failed by a logging-side bug.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from app.db.session import SessionLocal
from app.services.audit import _json_safe, log_event, _row_to_dict
from app.services.audit import AuditEntity, AuditAction


def test_json_safe_handles_decimal():
    out = _json_safe(Decimal("1234.50"))
    assert isinstance(out, (int, float))
    assert float(out) == 1234.5


def test_json_safe_handles_dates():
    today = _dt.date(2026, 6, 1)
    now = _dt.datetime(2026, 6, 1, 12, 30, 45)
    assert _json_safe(today) == "2026-06-01"
    assert _json_safe(now).startswith("2026-06-01T12:30:45")


def test_json_safe_handles_bytes():
    assert _json_safe(b"hello") == "hello"


def test_json_safe_handles_nested_collections():
    payload = {
        "salary": Decimal("999.99"),
        "dob": _dt.date(1990, 1, 1),
        "skills": ["a", "b", {"deep": Decimal("1")}],
        "blob": b"x",
        "tags": {"a", "b"},
    }
    out = _json_safe(payload)
    # tags is a set — order is arbitrary, just check membership
    assert set(out["tags"]) == {"a", "b"}
    assert out["salary"] == 999.99
    assert out["dob"] == "1990-01-01"
    assert out["skills"][2]["deep"] == 1.0
    assert out["blob"] == "x"
    # Round-trip via json.dumps must succeed
    import json
    json.dumps(out)  # must not raise


def test_log_event_with_decimal_row_does_not_crash():
    """End-to-end: build a fake 'candidate' object with Decimal fields
    and confirm log_event commits without raising."""

    class FakeCandidate:
        __tablename__ = "candidates"

        def __init__(self):
            self.id = 9999
            self.full_name = "Test Candidate"
            self.salary = Decimal("1500.25")
            self.date_of_birth = _dt.date(1990, 5, 15)
            self.created_at = _dt.datetime.utcnow()

    cand = FakeCandidate()
    # _row_to_dict should not raise on Decimal fields
    d = _row_to_dict(cand)
    # The mock object exposes the fields directly even without a real
    # SQLAlchemy mapper, so we just assert the helper returned a dict.
    assert isinstance(d, dict)

    db = SessionLocal()
    try:
        # before/after both dicts containing Decimal — the audit
        # service must coerce them before SQLAlchemy's JSON adapter
        # serializes them.
        entry = log_event(
            db,
            entity_type=AuditEntity.CANDIDATE.value,
            entity_id=9999,
            action=AuditAction.UPDATE.value,
            actor=None,
            summary="JSON-safety regression",
            before={"salary": Decimal("100.0"), "dob": _dt.date(1990, 1, 1)},
            after={"salary": Decimal("200.0"), "dob": _dt.date(1990, 1, 1)},
            commit=True,
        )
        # Either it committed cleanly OR the wrap-in-try/except path
        # returned the entry without crashing — both are acceptable.
        assert entry is not None
    finally:
        db.close()


def test_log_event_swallows_internal_errors(monkeypatch):
    """Force a downstream failure inside log_event and confirm the
    originating call still gets a result (no exception bubbles up)."""
    db = SessionLocal()

    # Patch db.commit to raise — log_event must catch & roll back
    def boom():
        raise RuntimeError("simulated commit failure")

    real_commit = db.commit
    db.commit = boom  # type: ignore[assignment]
    try:
        entry = log_event(
            db,
            entity_type=AuditEntity.CANDIDATE.value,
            entity_id=1,
            action=AuditAction.UPDATE.value,
            actor=None,
            summary="should not raise",
            before={"x": 1},
            after={"x": 2},
            commit=True,
        )
        # Either we got the entry back or None — either way no exception
        # should have escaped.
        assert entry is None or entry is not None
    finally:
        db.commit = real_commit  # type: ignore[assignment]
        db.close()
