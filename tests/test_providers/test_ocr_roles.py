from journal.providers.ocr import (
    ENTRY_BEGINS,
    ENTRY_ENDS,
    PageRole,
    role_prompt_clause,
)


def test_marker_tokens_are_distinct_triple_angle():
    assert ENTRY_BEGINS == "<<<ENTRY BEGINS>>>"
    assert ENTRY_ENDS == "<<<ENTRY ENDS>>>"
    assert ENTRY_BEGINS != ENTRY_ENDS


def test_none_role_yields_empty_clause():
    assert role_prompt_clause(None) == ""


def test_first_role_mentions_begins_not_ends():
    clause = role_prompt_clause(PageRole.FIRST)
    assert ENTRY_BEGINS in clause
    assert ENTRY_ENDS not in clause


def test_last_role_mentions_ends_not_begins():
    clause = role_prompt_clause(PageRole.LAST)
    assert ENTRY_ENDS in clause
    assert ENTRY_BEGINS not in clause


def test_middle_role_mentions_neither_marker():
    clause = role_prompt_clause(PageRole.MIDDLE)
    assert ENTRY_BEGINS not in clause
    assert ENTRY_ENDS not in clause


def test_only_role_mentions_both_markers():
    clause = role_prompt_clause(PageRole.ONLY)
    assert ENTRY_BEGINS in clause
    assert ENTRY_ENDS in clause
