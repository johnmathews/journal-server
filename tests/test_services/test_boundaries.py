from journal.providers.ocr import ENTRY_BEGINS, ENTRY_ENDS, PageRole
from journal.services.ingestion.boundaries import (
    ContentWindow,
    assign_roles,
    extract_content_window,
)


def test_assign_roles_single():
    assert assign_roles(1) == [PageRole.ONLY]


def test_assign_roles_two():
    assert assign_roles(2) == [PageRole.FIRST, PageRole.LAST]


def test_assign_roles_four():
    assert assign_roles(4) == [
        PageRole.FIRST,
        PageRole.MIDDLE,
        PageRole.MIDDLE,
        PageRole.LAST,
    ]


def test_no_markers_is_full_window():
    w = extract_content_window("Hello world", [])
    assert w == ContentWindow(text="Hello world", start=0, end=11, spans=[])


def test_begins_marker_sets_start_and_strips_token():
    text = f"old tail\n{ENTRY_BEGINS}\nMy entry"
    w = extract_content_window(text, [])
    assert ENTRY_BEGINS not in w.text
    # content is everything from "My entry"
    assert w.text[w.start:w.end] == "My entry"
    assert w.text[: w.start] == "old tail\n\n"


def test_ends_marker_sets_end_and_strips_token():
    text = f"My entry\n{ENTRY_ENDS}\nnext entry heading"
    w = extract_content_window(text, [])
    assert ENTRY_ENDS not in w.text
    assert w.text[w.start:w.end] == "My entry\n"
    assert w.text[w.end:] == "\nnext entry heading"


def test_both_markers_window_is_between():
    text = f"tail\n{ENTRY_BEGINS}\nbody\n{ENTRY_ENDS}\nnext"
    w = extract_content_window(text, [])
    assert ENTRY_BEGINS not in w.text and ENTRY_ENDS not in w.text
    assert w.text[w.start:w.end] == "body\n"


def test_spans_shift_after_removed_begins_marker():
    # span covers "body" which sits after the removed BEGINS marker
    prefix = f"{ENTRY_BEGINS}\n"
    text = prefix + "body"
    body_start = len(prefix)
    w = extract_content_window(text, [(body_start, body_start + 4)])
    # after removal the span addresses "body" at the new offset
    assert [w.text[s:e] for s, e in w.spans] == ["body"]


def test_inverted_window_falls_back_to_full_text():
    # ENDS before BEGINS → malformed → full text
    text = f"{ENTRY_ENDS}\nx\n{ENTRY_BEGINS}\ny"
    w = extract_content_window(text, [])
    assert (w.start, w.end) == (0, len(w.text))
    assert ENTRY_BEGINS not in w.text and ENTRY_ENDS not in w.text


# Additional edge-case tests
def test_assign_roles_zero():
    assert assign_roles(0) == []


def test_assign_roles_three():
    assert assign_roles(3) == [PageRole.FIRST, PageRole.MIDDLE, PageRole.LAST]


def test_only_ends_marker_uses_zero_start():
    # No BEGINS → start defaults to 0
    text = f"My entry\n{ENTRY_ENDS}\nafter"
    w = extract_content_window(text, [])
    assert w.start == 0
    assert ENTRY_ENDS not in w.text
    assert w.text[w.start:w.end] == "My entry\n"


def test_only_begins_marker_uses_full_end():
    # No ENDS → end defaults to len(clean)
    text = f"before\n{ENTRY_BEGINS}\nMy entry"
    w = extract_content_window(text, [])
    assert w.end == len(w.text)
    assert ENTRY_BEGINS not in w.text
    assert w.text[w.start:] == "My entry"


def test_begins_marker_at_end_of_text_yields_empty_window():
    # ENTRY_BEGINS with no content after it → empty window at end, no crash, no fallback noise
    text = f"some body\n{ENTRY_BEGINS}"
    w = extract_content_window(text, [])
    assert ENTRY_BEGINS not in w.text
    assert w.start == w.end == len(w.text)
