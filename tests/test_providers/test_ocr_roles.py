from journal.providers.ocr import (
    ENTRY_BEGINS,
    ENTRY_ENDS,
    SYSTEM_PROMPT,
    OCRProvider,
    PageRole,
    role_prompt_clause,
)


def test_system_prompt_does_not_contain_new_entry_marker() -> None:
    """SYSTEM_PROMPT must not instruct the model to emit <<<NEW ENTRY>>>.

    That clause was superseded by the per-page role mechanism (ENTRY_BEGINS /
    ENTRY_ENDS); leaving it in would cause the model to emit a stale marker
    that the parser does not recognise, corrupting ingested text.
    """
    assert "NEW ENTRY" not in SYSTEM_PROMPT


def test_marker_tokens_are_distinct_triple_angle():
    assert ENTRY_BEGINS == "<<<ENTRY BEGINS>>>"
    assert ENTRY_ENDS == "<<<ENTRY ENDS>>>"
    assert ENTRY_BEGINS != ENTRY_ENDS


def test_none_role_yields_empty_clause():
    assert role_prompt_clause(None) == ""


def test_every_role_yields_distinct_nonempty_clause():
    clauses = {role: role_prompt_clause(role) for role in PageRole}
    assert all(c.strip() for c in clauses.values())
    assert len(set(clauses.values())) == len(clauses)


def test_first_role_emits_begins_and_forbids_ends():
    clause = role_prompt_clause(PageRole.FIRST)
    assert "FIRST" in clause
    assert ENTRY_BEGINS in clause
    assert "Never emit" in clause


def test_last_role_emits_ends_and_forbids_begins():
    clause = role_prompt_clause(PageRole.LAST)
    assert "LAST" in clause
    assert ENTRY_ENDS in clause
    assert "Never emit" in clause


def test_middle_role_forbids_both_markers():
    clause = role_prompt_clause(PageRole.MIDDLE)
    assert "MIDDLE" in clause
    assert "Do NOT emit" in clause


def test_only_role_mentions_both_markers():
    clause = role_prompt_clause(PageRole.ONLY)
    assert ENTRY_BEGINS in clause
    assert ENTRY_ENDS in clause


class _FakeExtractor:
    """Minimal stand-in asserting the protocol's new default arg exists."""

    def extract(
        self,
        image_data: bytes,
        media_type: str,
        page_role: PageRole | None = None,
    ) -> tuple[bytes, str, PageRole | None]:
        return (image_data, media_type, page_role)


def test_extract_accepts_optional_page_role_and_defaults_none():
    fake = _FakeExtractor()
    # default omitted → None
    assert fake.extract(b"x", "image/png")[2] is None
    # explicit role passes through
    assert fake.extract(b"x", "image/png", PageRole.FIRST)[2] is PageRole.FIRST
    # structural protocol check
    assert isinstance(fake, OCRProvider)
