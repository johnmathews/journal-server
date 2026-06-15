"""Tests for the sectioned-narrative path of the storyline narrator (W2).

The narrator can emit an ordered list of titled sections instead of one
continuous narrative. The model begins each section with a heading line
``## <short title>`` on its own; the content-block parser opens a new
section on a heading-shaped text segment and accrues subsequent
prose/citation segments to the current section. Citations stay attached
to prose blocks exactly as in the flat path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from journal.models import DatedEntryExcerpt
from journal.providers.storyline_narrator import (
    AnthropicStorylineNarrator,
    NarrativeSection,
    SectionedNarrativeResult,
    _parse_sectioned_response,
)

if TYPE_CHECKING:
    import pytest

# ── Fake Anthropic responses (mirrors test_storyline_generation.py) ──


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeResponse:
    content: list[Any] = field(default_factory=list)
    usage: _FakeUsage = field(default_factory=_FakeUsage)


class _FakeAnthropicClient:
    """Records the most recent request and returns a canned response."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    @property
    def messages(self) -> _FakeAnthropicClient:
        return self

    def create(self, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        self.last_kwargs = kwargs
        return self._response


def _words(n: int) -> str:
    """Return a string of ``n`` whitespace-separated filler words."""
    return " ".join(["lorem"] * n)


# ── Section parser ──────────────────────────────────────────────


class TestSectionParser:
    def test_two_sections_parsed_with_titles(self) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": "## First Days\nAtlas started school.",
             "citations": []},
            {"type": "text", "text": "## Reading\nAtlas read a book.",
             "citations": []},
        ])
        sections = _parse_sectioned_response(response, document_to_entry={})
        assert [s.title for s in sections] == ["First Days", "Reading"]
        # Heading marker is stripped; only the prose remains as a text segment.
        assert sections[0].segments == [
            {"kind": "text", "text": "Atlas started school."}
        ]
        assert sections[1].segments == [
            {"kind": "text", "text": "Atlas read a book."}
        ]

    def test_section_citations_map_to_entry_id(self) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": "## Running\nAtlas ran with his father.",
             "citations": []},
            {
                "type": "text",
                "text": "He kept pace.",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "Atlas ran with me",
                        "document_index": 0,
                        "start_char_index": 0,
                        "end_char_index": 17,
                    }
                ],
            },
            {"type": "text", "text": "## Reading\nLater he read.", "citations": []},
            {
                "type": "text",
                "text": "A chapter book.",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "first chapter book",
                        "document_index": 1,
                        "start_char_index": 0,
                        "end_char_index": 18,
                    }
                ],
            },
        ])
        sections = _parse_sectioned_response(
            response,
            document_to_entry={0: 42, 1: 43},
            document_to_date={0: "2026-02-15", 1: "2026-02-22"},
        )
        assert len(sections) == 2
        # Section 0 cites entry 42, section 1 cites entry 43.
        assert sections[0].source_entry_ids == [42]
        assert sections[0].citation_count == 1
        assert sections[1].source_entry_ids == [43]
        assert sections[1].citation_count == 1
        cite0 = next(s for s in sections[0].segments if s["kind"] == "citation")
        assert cite0 == {
            "kind": "citation",
            "entry_id": 42,
            "quote": "Atlas ran with me",
            "entry_date": "2026-02-15",
        }
        cite1 = next(s for s in sections[1].segments if s["kind"] == "citation")
        assert cite1["entry_id"] == 43
        assert cite1["entry_date"] == "2026-02-22"

    def test_word_count_from_text_only_excludes_cited_text(self) -> None:
        # Prose has 5 words; the cited_text has many words but must not count.
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "## Title\none two three four five",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": _words(50),
                        "document_index": 0,
                        "start_char_index": 0,
                        "end_char_index": 5,
                    }
                ],
            },
        ])
        sections = _parse_sectioned_response(
            response, document_to_entry={0: 1},
        )
        assert len(sections) == 1
        # "one two three four five" → 5 words; cited_text excluded.
        assert sections[0].word_count == 5

    def test_out_of_band_section_still_returned_and_logged(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A 10-word section is below the [180, 240] band.
        response = _FakeResponse(content=[
            {"type": "text", "text": f"## Tiny\n{_words(10)}", "citations": []},
        ])
        with caplog.at_level("WARNING"):
            sections = _parse_sectioned_response(response, document_to_entry={})
        assert len(sections) == 1
        assert sections[0].word_count == 10
        assert any("word" in m.lower() for m in caplog.messages)

    def test_in_band_section_does_not_warn(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": f"## OK\n{_words(200)}", "citations": []},
        ])
        with caplog.at_level("WARNING"):
            sections = _parse_sectioned_response(response, document_to_entry={})
        assert sections[0].word_count == 200
        assert not any("word" in m.lower() for m in caplog.messages)

    def test_heading_with_no_following_prose_is_tolerated(self) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": "## Empty Section", "citations": []},
            {"type": "text", "text": "## Has Prose\nSome words here.",
             "citations": []},
        ])
        sections = _parse_sectioned_response(response, document_to_entry={})
        assert [s.title for s in sections] == ["Empty Section", "Has Prose"]
        # Empty section has no segments and zero word count — no crash.
        assert sections[0].segments == []
        assert sections[0].word_count == 0
        assert sections[1].word_count == 3

    def test_preamble_before_first_heading_attaches_to_leading_section(
        self,
    ) -> None:
        # Text before the first heading should not be lost; it becomes an
        # implicit leading section with an empty title.
        response = _FakeResponse(content=[
            {"type": "text", "text": "Some stray preamble.", "citations": []},
            {"type": "text", "text": "## Real Section\nReal prose.",
             "citations": []},
        ])
        sections = _parse_sectioned_response(response, document_to_entry={})
        assert len(sections) == 2
        assert sections[0].title == ""
        assert sections[0].segments == [
            {"kind": "text", "text": "Some stray preamble."}
        ]
        assert sections[1].title == "Real Section"

    def test_multiline_heading_block_keeps_remainder_as_prose(self) -> None:
        # First line is the heading; the rest of the block is prose.
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "## A Title\nFirst paragraph.\nSecond paragraph.",
                "citations": [],
            },
        ])
        sections = _parse_sectioned_response(response, document_to_entry={})
        assert sections[0].title == "A Title"
        assert sections[0].segments == [
            {"kind": "text", "text": "First paragraph.\nSecond paragraph."}
        ]

    def test_no_headings_yields_single_untitled_section(self) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": "Just prose, no headings.", "citations": []},
        ])
        sections = _parse_sectioned_response(response, document_to_entry={})
        assert len(sections) == 1
        assert sections[0].title == ""
        assert sections[0].segments == [
            {"kind": "text", "text": "Just prose, no headings."}
        ]


# ── End-to-end through generate_sectioned_narrative ─────────────


class TestGenerateSectionedNarrative:
    def test_end_to_end_two_sections(self) -> None:
        excerpts = [
            DatedEntryExcerpt(
                entry_id=42, entry_date="2026-02-15",
                final_text="Atlas ran at school today.", quotes=[],
            ),
            DatedEntryExcerpt(
                entry_id=43, entry_date="2026-02-22",
                final_text="Atlas read his first chapter book.", quotes=[],
            ),
        ]
        canned = _FakeResponse(content=[
            {
                "type": "text",
                "text": "## Running\nAtlas ran at school.",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "Atlas ran at school today.",
                    "document_index": 0,
                    "start_char_index": 0,
                    "end_char_index": 26,
                }],
            },
            {
                "type": "text",
                "text": "## Reading\nHe read a chapter book.",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "Atlas read his first chapter book.",
                    "document_index": 1,
                    "start_char_index": 0,
                    "end_char_index": 34,
                }],
            },
        ])
        client = _FakeAnthropicClient(canned)
        narrator = AnthropicStorylineNarrator(
            api_key="x", model="claude-opus-4-7", client=client,
        )
        result = narrator.generate_sectioned_narrative(
            excerpts=excerpts, storyline_name="Atlas",
            storyline_description="The author's son",
        )
        assert isinstance(result, SectionedNarrativeResult)
        assert [s.title for s in result.sections] == ["Running", "Reading"]
        assert result.sections[0].source_entry_ids == [42]
        assert result.sections[1].source_entry_ids == [43]
        assert result.sections[0].segments[-1]["entry_date"] == "2026-02-15"
        assert result.sections[1].segments[-1]["entry_date"] == "2026-02-22"
        assert result.model_used == "claude-opus-4-7"
        assert result.raw_usage is not None
        # Aggregate convenience fields.
        assert result.citation_count == 2
        assert result.source_entry_ids == [42, 43]
        # Sectioning prompt was sent, not the flat one.
        system = client.last_kwargs["system"][0]["text"]
        assert "##" in system

    def test_empty_excerpts_short_circuits(self) -> None:
        narrator = AnthropicStorylineNarrator(
            api_key="x", client=_FakeAnthropicClient(_FakeResponse()),
        )
        result = narrator.generate_sectioned_narrative(
            excerpts=[], storyline_name="Atlas",
        )
        assert result.sections == []
        assert result.citation_count == 0

    def test_api_failure_returns_empty_sectioned_result(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        excerpts = [
            DatedEntryExcerpt(
                entry_id=1, entry_date="2026-01-01",
                final_text="x", quotes=[],
            ),
        ]

        class _Boom:
            @property
            def messages(self) -> Any:  # noqa: ANN401
                return self

            def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
                raise RuntimeError("boom")

        narrator = AnthropicStorylineNarrator(api_key="x", client=_Boom())
        with caplog.at_level("ERROR"):
            result = narrator.generate_sectioned_narrative(
                excerpts=excerpts, storyline_name="x",
            )
        assert result.sections == []
        assert isinstance(result, SectionedNarrativeResult)


class TestNarrativeSectionDataclass:
    def test_defaults(self) -> None:
        section = NarrativeSection(title="t")
        assert section.title == "t"
        assert section.segments == []
        assert section.source_entry_ids == []
        assert section.citation_count == 0
        assert section.word_count == 0
