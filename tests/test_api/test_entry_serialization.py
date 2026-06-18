"""Tests for entry serialization helpers."""

from journal.api._shared import _entry_to_dict
from journal.models import Entry


def _entry(**kw):
    base = dict(
        id=1, entry_date="2026-01-01", source_type="photo",
        raw_text="tail body next", final_text="body", word_count=1,
    )
    base.update(kw)
    return Entry(**base)


def test_content_boundary_present_when_set():
    d = _entry_to_dict(_entry(content_start_char=5, content_end_char=9))
    assert d["content_boundary"] == {"char_start": 5, "char_end": 9}


def test_content_boundary_null_when_unset():
    d = _entry_to_dict(_entry())
    assert d["content_boundary"] is None
