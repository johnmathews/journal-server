"""Tests for the simplified narrator provider (draft/closure/addendum modes).

Sectioning is gone — chapter boundaries now come from the judge provider
(`storyline_judge.py`), not the narrator. This narrator always returns one
flat `NarrativeResult`; the only mode-dependent behavior is the framing
sentence appended to the user query and, in closure mode only, an optional
``# <title>`` line parsed off the front of the response.
"""

from __future__ import annotations

from typing import Any

import pytest

from journal.models import DatedEntryExcerpt
from journal.providers.storyline_narrator import (
    AnthropicStorylineNarrator,
    NarrativeResult,
    NarratorMode,
    StorylineNarratorProtocol,
)


def _excerpt(entry_id: int, date: str) -> DatedEntryExcerpt:
    return DatedEntryExcerpt(
        entry_id=entry_id,
        entry_date=date,
        final_text=f"Entry {entry_id} final text, dated {date}.",
        quotes=[],
    )


class _FakeResponse:
    """Wraps a plain dict of ``content``/``usage`` in attribute access,
    matching the shape the Anthropic SDK returns (``response.content``,
    ``response.usage``), since ``_parse_narrative_response`` and
    ``_extract_usage`` read those via ``getattr``/``_attr_or_key``."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.content = raw.get("content", [])
        self.usage = raw.get("usage", {})


class _FakeClient:
    """Records the most recent request kwargs and returns a canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = _FakeResponse(response)
        self.last_kwargs: dict[str, Any] | None = None

    @property
    def messages(self) -> _FakeClient:
        return self

    def create(self, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        self.last_kwargs = kwargs
        return self._response


def test_closure_mode_extracts_title_line() -> None:
    resp = {
        "content": [
            {
                "type": "text",
                "text": "# The Comeback Week\nHe returned to the track.",
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative(
        [_excerpt(1, "2026-07-01")], "Running", mode="closure"
    )
    assert result.title == "The Comeback Week"
    assert result.segments[0]["text"] == "He returned to the track."


def test_draft_mode_has_no_title_and_ignores_stray_heading() -> None:
    resp = {
        "content": [{"type": "text", "text": "# Not a title request\nprose"}],
        "usage": {},
    }
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative([_excerpt(1, "2026-07-01")], "Running")
    assert result.title is None
    assert result.segments[0]["text"].startswith("# Not a title request")


def test_addendum_mode_ignores_stray_heading_too() -> None:
    """Title parsing is bounded to closure mode only — addendum mode must
    not accidentally strip a leading '# ...' line either."""
    resp = {
        "content": [{"type": "text", "text": "# Looks like a title\nmore prose"}],
        "usage": {},
    }
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative(
        [_excerpt(1, "2026-07-01")],
        "Running",
        mode="addendum",
        prior_narrative="Previously, he started running.",
    )
    assert result.title is None
    assert result.segments[0]["text"] == "# Looks like a title\nmore prose"


@pytest.mark.parametrize(
    "mode,needle,extra",
    [
        ("draft", "arc is ongoing", {}),
        ("closure", "Give the narrative a proper ending", {}),
        (
            "addendum",
            "brief addendum",
            {"prior_narrative": "He started running in June."},
        ),
    ],
)
def test_mode_selects_framing_instruction(
    mode: NarratorMode, needle: str, extra: dict[str, Any]
) -> None:
    resp = {"content": [{"type": "text", "text": "prose"}], "usage": {}}
    client = _FakeClient(resp)
    narrator = AnthropicStorylineNarrator(api_key="k", client=client)
    narrator.generate_narrative(
        [_excerpt(1, "2026-07-01")], "Running", mode=mode, **extra
    )
    assert client.last_kwargs is not None
    content_blocks = client.last_kwargs["messages"][0]["content"]
    text_blocks = [b for b in content_blocks if b.get("type") == "text"]
    # The user-query text block is the last content block (after the
    # per-entry document blocks).
    user_query = text_blocks[-1]["text"]
    assert needle in user_query
    if mode == "addendum":
        assert "He started running in June." in user_query


def test_addendum_mode_requires_prior_narrative() -> None:
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient({}))
    with pytest.raises(ValueError, match="prior_narrative"):
        narrator.generate_narrative(
            [_excerpt(1, "2026-07-01")], "Running", mode="addendum"
        )


def test_addendum_mode_blank_prior_narrative_also_raises() -> None:
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient({}))
    with pytest.raises(ValueError, match="prior_narrative"):
        narrator.generate_narrative(
            [_excerpt(1, "2026-07-01")],
            "Running",
            mode="addendum",
            prior_narrative="   ",
        )


def test_closure_mode_missing_title_line_leaves_title_none() -> None:
    resp = {
        "content": [{"type": "text", "text": "No heading here, just prose."}],
        "usage": {},
    }
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative(
        [_excerpt(1, "2026-07-01")], "Running", mode="closure"
    )
    assert result.title is None
    assert result.segments[0]["text"] == "No heading here, just prose."


def test_sectioned_api_is_gone() -> None:
    assert not hasattr(AnthropicStorylineNarrator, "generate_sectioned_narrative")


def test_narrative_result_is_a_dataclass_with_title_field() -> None:
    result = NarrativeResult()
    assert result.title is None
    assert result.segments == []
    assert result.source_entry_ids == []
    assert result.citation_count == 0
    assert result.model_used == ""
    assert result.raw_usage is None


def test_narrator_satisfies_protocol() -> None:
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient({}))
    assert isinstance(narrator, StorylineNarratorProtocol)


def test_empty_excerpts_short_circuits_without_api_call() -> None:
    client = _FakeClient({})
    narrator = AnthropicStorylineNarrator(api_key="k", client=client)
    result = narrator.generate_narrative([], "Running")
    assert result.segments == []
    assert result.title is None
    assert client.last_kwargs is None
