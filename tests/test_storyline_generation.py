"""Tests for the storyline generation layer (W4).

* Narrator response parser: turns Anthropic's content+citations blocks
  into our `Segment` shape, using the per-request document index to
  recover entry IDs. (Each entry is its own ``source="text"`` document,
  so ``document_index`` maps to entry_id; ``cited_text`` is a short
  sentence-level excerpt rather than the whole wrapped entry.)
* Glue response parser: tolerant JSON-array decoder + deterministic
  fallback when the LLM call fails or the response is malformed.
* `StorylineGenerationService` end-to-end with fakes injected for
  providers, exercising the FTS fallback and verifying both panels
  are persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.models import DatedEntryExcerpt, Entry
from journal.providers.storyline_glue import (
    AnthropicStorylineGlue,
    GlueResult,
    _describe_gap,
    _fallback_transitions,
    _parse_transitions,
)
from journal.providers.storyline_narrator import (
    AnthropicStorylineNarrator,
    NarrativeResult,
    _parse_narrative_response,
)
from journal.services.storylines.service import (
    StorylineGenerationService,
    _extract_snippet,
)

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory


# ── Fake Anthropic responses ────────────────────────────────────


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


# ── Narrator parser ─────────────────────────────────────────────


class TestNarratorParser:
    def test_text_block_without_citations_becomes_text_segment(self) -> None:
        response = _FakeResponse(content=[
            {"type": "text", "text": "Atlas is the author's son.", "citations": []},
        ])
        segments = _parse_narrative_response(response, document_to_entry={})
        assert segments == [{"kind": "text", "text": "Atlas is the author's son."}]

    def test_text_block_with_citations_emits_text_then_citations(self) -> None:
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "Atlas ran with his father.",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "Atlas ran with me",
                        "document_index": 0,
                        "document_title": "Entry 42 (2026-02-15)",
                        "start_char_index": 0,
                        "end_char_index": 17,
                    }
                ],
            }
        ])
        segments = _parse_narrative_response(
            response, document_to_entry={0: 42, 1: 43},
        )
        assert segments == [
            {"kind": "text", "text": "Atlas ran with his father."},
            {"kind": "citation", "entry_id": 42, "quote": "Atlas ran with me"},
        ]

    def test_unknown_document_index_is_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "Some claim.",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "stuff",
                        "document_index": 99,
                        "start_char_index": 0,
                        "end_char_index": 5,
                    }
                ],
            }
        ])
        with caplog.at_level("WARNING"):
            segments = _parse_narrative_response(response, document_to_entry={0: 1})
        # Text still rendered, citation dropped
        kinds = [s["kind"] for s in segments]
        assert kinds == ["text"]
        assert any("not in document_to_entry" in m for m in caplog.messages)

    def test_parser_stamps_entry_date_when_document_to_date_supplied(self) -> None:
        """The narrator parser must thread the source entry's ISO date
        onto each citation segment. Otherwise the webapp's
        absolute-date eyebrow logic in StorylineNarrative.vue has
        nothing to work with."""
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "He ran.",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "I ran today",
                        "document_index": 0,
                        "start_char_index": 0,
                        "end_char_index": 11,
                    }
                ],
            }
        ])
        segments = _parse_narrative_response(
            response,
            document_to_entry={0: 42},
            document_to_date={0: "2026-02-15"},
        )
        citations = [s for s in segments if s["kind"] == "citation"]
        assert citations == [
            {
                "kind": "citation",
                "entry_id": 42,
                "quote": "I ran today",
                "entry_date": "2026-02-15",
            }
        ]

    def test_parser_omits_entry_date_when_no_date_map(self) -> None:
        """Backward compat: callers that don't supply document_to_date
        get citation segments without entry_date — webapp falls back
        gracefully."""
        response = _FakeResponse(content=[
            {
                "type": "text",
                "text": "x",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "y",
                        "document_index": 0,
                        "start_char_index": 0,
                        "end_char_index": 1,
                    }
                ],
            }
        ])
        segments = _parse_narrative_response(response, document_to_entry={0: 1})
        citation = next(s for s in segments if s["kind"] == "citation")
        assert "entry_date" not in citation

    def test_full_narrator_call_stamps_entry_dates_end_to_end(self) -> None:
        """End-to-end through generate_narrative: confirm the parallel
        document_to_date map is built from the excerpts and threaded
        all the way through to the final NarrativeResult segments."""
        excerpts = [
            DatedEntryExcerpt(
                entry_id=10, entry_date="2026-01-05",
                final_text="text 1", quotes=[],
            ),
            DatedEntryExcerpt(
                entry_id=20, entry_date="2026-02-22",
                final_text="text 2", quotes=[],
            ),
        ]
        canned = _FakeResponse(content=[
            {
                "type": "text",
                "text": "claim A",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "cite A",
                    "document_index": 0,
                    "start_char_index": 0,
                    "end_char_index": 6,
                }],
            },
            {
                "type": "text",
                "text": "claim B",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "cite B",
                    "document_index": 1,
                    "start_char_index": 0,
                    "end_char_index": 6,
                }],
            },
        ])
        narrator = AnthropicStorylineNarrator(
            api_key="x", client=_FakeAnthropicClient(canned),
        )
        result = narrator.generate_narrative(
            excerpts=excerpts, storyline_name="x",
        )
        citations = [s for s in result.segments if s["kind"] == "citation"]
        assert citations[0]["entry_date"] == "2026-01-05"
        assert citations[1]["entry_date"] == "2026-02-22"

    def test_non_text_blocks_are_ignored(self) -> None:
        response = _FakeResponse(content=[
            {"type": "tool_use", "name": "foo"},  # not text — ignore
            {"type": "text", "text": "ok", "citations": []},
        ])
        segments = _parse_narrative_response(response, document_to_entry={})
        assert segments == [{"kind": "text", "text": "ok"}]

    def test_empty_excerpts_short_circuits(self) -> None:
        narrator = AnthropicStorylineNarrator(
            api_key="x", client=_FakeAnthropicClient(_FakeResponse()),
        )
        result = narrator.generate_narrative(
            excerpts=[], storyline_name="Atlas",
        )
        assert result.segments == []
        assert result.citation_count == 0

    def test_end_to_end_round_trip(self) -> None:
        # Two excerpts → two source="text" documents → response cites both
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
            {"type": "text", "text": "The author writes about his son. "},
            {
                "type": "text",
                "text": "Atlas ran at school",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "Atlas ran at school today.",
                    "document_index": 0,
                    "document_title": "Entry 42 (2026-02-15)",
                    "start_char_index": 0,
                    "end_char_index": 26,
                }],
            },
            {
                "type": "text",
                "text": " and read his first chapter book.",
                "citations": [{
                    "type": "char_location",
                    "cited_text": "Atlas read his first chapter book.",
                    "document_index": 1,
                    "document_title": "Entry 43 (2026-02-22)",
                    "start_char_index": 0,
                    "end_char_index": 34,
                }],
            },
        ])
        client = _FakeAnthropicClient(canned)
        narrator = AnthropicStorylineNarrator(
            api_key="x", model="claude-opus-4-7", client=client,
        )
        result = narrator.generate_narrative(
            excerpts=excerpts, storyline_name="Atlas",
            storyline_description="The author's 7-year-old son",
        )
        # Three text segments + two citation segments, in order
        kinds = [s["kind"] for s in result.segments]
        assert kinds == ["text", "text", "citation", "text", "citation"]
        # source_entry_ids dedupes and preserves order
        assert result.source_entry_ids == [42, 43]
        assert result.citation_count == 2
        assert result.model_used == "claude-opus-4-7"
        # Citation quotes are the short cited_text from the API, not bloated.
        citations = [s for s in result.segments if s["kind"] == "citation"]
        assert citations[0]["quote"] == "Atlas ran at school today."
        assert citations[1]["quote"] == "Atlas read his first chapter book."
        for citation in citations:
            assert len(citation["quote"]) < 200  # sentence-length, not entry-length
        # Request shape: cache_control on system; N source="text" documents;
        # cache_control on the LAST document only (caches the whole corpus
        # up to and including the newest entry — single breakpoint).
        assert client.last_kwargs is not None
        system_block = client.last_kwargs["system"][0]
        assert system_block["cache_control"] == {"type": "ephemeral"}
        content_blocks = client.last_kwargs["messages"][0]["content"]
        # Two documents + the user_query text block
        document_blocks = [b for b in content_blocks if b.get("type") == "document"]
        assert len(document_blocks) == 2
        # All documents are source="text" with citations enabled
        for doc in document_blocks:
            assert doc["type"] == "document"
            assert doc["source"]["type"] == "text"
            assert doc["source"]["media_type"] == "text/plain"
            assert doc["citations"] == {"enabled": True}
        # Per-entry payload: source.data is the entry's final_text; title
        # carries entry_id/date so the model can reason about which entry
        # it's reading without that metadata showing up inside cited_text.
        assert document_blocks[0]["source"]["data"] == "Atlas ran at school today."
        assert document_blocks[0]["title"] == "Entry 42 (2026-02-15)"
        assert document_blocks[1]["source"]["data"] == (
            "Atlas read his first chapter book."
        )
        assert document_blocks[1]["title"] == "Entry 43 (2026-02-22)"
        # Cache breakpoint: only the LAST document gets cache_control.
        assert "cache_control" not in document_blocks[0]
        assert document_blocks[-1]["cache_control"] == {"type": "ephemeral"}

    def test_api_failure_returns_empty_result(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _BlowingClient:
            messages = None  # populated below

            class _M:
                def create(self, **_: Any) -> Any:  # noqa: ANN401
                    raise RuntimeError("network sad")
            messages = _M()  # type: ignore[assignment]

        narrator = AnthropicStorylineNarrator(
            api_key="x", client=_BlowingClient(),
        )
        excerpts = [
            DatedEntryExcerpt(
                entry_id=1, entry_date="2026-02-15",
                final_text="x", quotes=[],
            ),
        ]
        with caplog.at_level("ERROR"):
            result = narrator.generate_narrative(
                excerpts=excerpts, storyline_name="X",
            )
        assert result.segments == []
        assert result.citation_count == 0


# ── Glue parser ─────────────────────────────────────────────────


class TestGlueParser:
    def test_describe_gap_buckets(self) -> None:
        assert _describe_gap(0) == "Later the same day:"
        assert _describe_gap(1) == "The next day:"
        assert _describe_gap(4) == "4 days later:"
        assert _describe_gap(8) == "A week later:"
        assert _describe_gap(21) == "3 weeks later:"
        assert _describe_gap(45) == "A month later:"
        assert _describe_gap(95) == "3 months later:"

    def test_fallback_transitions_match_pair_count(self) -> None:
        excerpts = [
            DatedEntryExcerpt(entry_id=1, entry_date="2026-02-15", final_text=""),
            DatedEntryExcerpt(entry_id=2, entry_date="2026-02-22", final_text=""),
            DatedEntryExcerpt(entry_id=3, entry_date="2026-03-20", final_text=""),
        ]
        out = _fallback_transitions(excerpts)
        assert len(out) == 2

    @pytest.mark.parametrize(
        "raw,expected_len",
        [
            ('["one:", "two:", "three:"]', 3),
            ("```json\n[\"a:\", \"b:\"]\n```", 2),
            ('Sure, here you go: ["x:", "y:"]', 2),
        ],
    )
    def test_parse_transitions_accepts_variants(
        self, raw: str, expected_len: int,
    ) -> None:
        out = _parse_transitions(raw, expected=expected_len)
        assert out is not None
        assert len(out) == expected_len

    @pytest.mark.parametrize(
        "raw",
        [
            "[\"only-one:\"]",  # wrong length
            "not json at all",
            '{"object": "not array"}',
            '["mixed", 7]',  # wrong types
        ],
    )
    def test_parse_transitions_rejects_bad(self, raw: str) -> None:
        assert _parse_transitions(raw, expected=2) is None

    def test_glue_provider_uses_fallback_on_failure(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _Boom:
            class _M:
                def create(self, **_: Any) -> Any:  # noqa: ANN401
                    raise RuntimeError("nope")
            messages = _M()
        glue = AnthropicStorylineGlue(api_key="x", client=_Boom())
        excerpts = [
            DatedEntryExcerpt(entry_id=1, entry_date="2026-02-15", final_text=""),
            DatedEntryExcerpt(entry_id=2, entry_date="2026-02-22", final_text=""),
        ]
        with caplog.at_level("ERROR"):
            result = glue.generate_transitions(excerpts)
        assert len(result.transitions) == 1
        assert result.transitions[0] == "A week later:"

    def test_glue_provider_parses_canned_response(self) -> None:
        canned = _FakeResponse(content=[
            {"type": "text", "text": '["The next day:", "A week later:"]'},
        ])
        client = _FakeAnthropicClient(canned)
        glue = AnthropicStorylineGlue(
            api_key="x", model="claude-haiku-4-5", client=client,
        )
        excerpts = [
            DatedEntryExcerpt(entry_id=1, entry_date="2026-02-15", final_text=""),
            DatedEntryExcerpt(entry_id=2, entry_date="2026-02-16", final_text=""),
            DatedEntryExcerpt(entry_id=3, entry_date="2026-02-23", final_text=""),
        ]
        result = glue.generate_transitions(excerpts)
        assert result.transitions == ["The next day:", "A week later:"]
        assert result.model_used == "claude-haiku-4-5"


# ── Snippet helper ──────────────────────────────────────────────


class TestSnippetExtraction:
    def test_extract_snippet_centers_on_match(self) -> None:
        body = "x" * 200 + " Atlas " + "y" * 200
        snip = _extract_snippet(body, "Atlas")
        assert "Atlas" in snip
        # Snippet truncated on both ends with ellipsis
        assert snip.startswith("…")
        assert snip.endswith("…")

    def test_extract_snippet_falls_back_to_prefix_when_absent(self) -> None:
        body = "no mention of the surface form here"
        snip = _extract_snippet(body, "Atlas")
        assert snip == body

    def test_extract_snippet_empty_body(self) -> None:
        assert _extract_snippet("", "Atlas") == ""


# ── Generation service ─────────────────────────────────────────


class _FakeNarrator:
    def __init__(self, segments: list[dict[str, Any]]) -> None:
        self._segments = segments
        self.model = "claude-opus-4-7-fake"
        self.calls: list[tuple[int, str]] = []
        self.last_prior_narrative: str | None = None
        self.last_kwargs: dict[str, Any] = {}

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
        prior_narrative: str | None = None,
    ) -> NarrativeResult:
        self.calls.append((len(excerpts), storyline_name))
        self.last_prior_narrative = prior_narrative
        self.last_kwargs = {
            "excerpts": list(excerpts),
            "storyline_name": storyline_name,
            "storyline_description": storyline_description,
            "prior_narrative": prior_narrative,
        }
        source_ids: list[int] = []
        seen: set[int] = set()
        citation_count = 0
        for seg in self._segments:
            if seg.get("kind") == "citation":
                citation_count += 1
                eid = int(seg.get("entry_id", 0))
                if eid and eid not in seen:
                    seen.add(eid)
                    source_ids.append(eid)
        return NarrativeResult(
            segments=list(self._segments),
            source_entry_ids=source_ids,
            citation_count=citation_count,
            model_used=self.model,
        )


class _FakeGlue:
    def __init__(self) -> None:
        self.model = "claude-haiku-4-5-fake"
        self.calls: int = 0

    def generate_transitions(
        self, excerpts: list[DatedEntryExcerpt],
    ) -> GlueResult:
        self.calls += 1
        if len(excerpts) < 2:
            return GlueResult(model_used=self.model)
        return GlueResult(
            transitions=[f"step {i}:" for i in range(len(excerpts) - 1)],
            model_used=self.model,
        )


@pytest.fixture
def seeded_storyline(
    factory: ConnectionFactory,
) -> tuple[SQLiteStorylineRepository, SQLiteEntityStore, int, int, int]:
    """Returns (storyline_repo, entity_store, user_id, entity_id, storyline_id)
    with one Atlas-person entity and two dated mentions."""
    conn = factory.get()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        ("u@x.test", "x", "U"),
    )
    user_id = cur.lastrowid
    conn.commit()
    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="person", canonical_name="Atlas",
        description="", first_seen="2026-02-15", user_id=user_id,
    )
    # Two dated entries with entity mentions
    for entry_date, quote in [
        ("2026-02-15", "Atlas read his first chapter book."),
        ("2026-03-15", "Atlas ran with me to the park."),
    ]:
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entry_date, "text", quote, quote, len(quote.split()), user_id),
        )
        entry_id = cur.lastrowid
        conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity.id, entry_id, quote, 0.95, "run-1"),
        )
    conn.commit()
    repo = SQLiteStorylineRepository(factory)
    storyline = repo.create_storyline(
        user_id=user_id, entity_ids=[entity.id], name="Atlas",
        start_date="2026-01-01", end_date="2026-12-31",
    )
    return repo, store, user_id, entity.id, storyline.id


class _FakeEntryRepo:
    """Minimal EntryRepository fake for FTS fallback testing.

    Signature mirrors `_SearchMixin.search_text` exactly — returns
    `list[Entry]`, no `limit` kwarg, all date/user kwargs match the
    real call. Mismatches here let a deploy-blocking bug through
    once already; integration tests against the real repo guard
    against regression.
    """

    def __init__(self) -> None:
        self.entries: dict[int, Entry] = {}
        self.search_hits: list[Entry] = []

    def search_text(
        self,
        query: str,  # noqa: ARG002
        start_date: str | None = None,  # noqa: ARG002
        end_date: str | None = None,  # noqa: ARG002
        user_id: int | None = None,  # noqa: ARG002
    ) -> list[Entry]:
        return list(self.search_hits)

    def get_entry(self, entry_id: int) -> Entry | None:
        return self.entries.get(entry_id)


class TestGenerationService:
    def test_regenerates_both_panels(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        repo, store, _user, _entity, storyline_id = seeded_storyline
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "The author writes about his son. "},
            {"kind": "citation", "entry_id": 1, "quote": "Atlas read."},
            {"kind": "text", "text": " A month later: "},
            {"kind": "citation", "entry_id": 2, "quote": "Atlas ran."},
        ])
        glue = _FakeGlue()
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=glue,
        )
        result = svc.regenerate(storyline_id)
        assert result.entry_count == 2
        assert result.entity_mention_count == 2
        assert result.fts_fallback_count == 0
        assert result.narrative_citation_count == 2
        assert glue.calls == 1
        # Both panels persisted
        narrative_panel = repo.get_panel(storyline_id, "narrative")
        curation_panel = repo.get_panel(storyline_id, "curation")
        assert narrative_panel is not None
        assert curation_panel is not None
        # Curation panel: lede + 2 citations + 1 transition
        assert curation_panel.citation_count == 2
        # last_generated_at recorded
        refreshed = repo.get_storyline(storyline_id)
        assert refreshed is not None
        assert refreshed.last_generated_at is not None

    def test_empty_narrator_result_preserves_existing_narrative(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        """Regression: a transient narrator failure (Anthropic outage,
        rate limit, etc.) used to wipe the previously good narrative
        panel because the service unconditionally upserted whatever
        the narrator returned. The narrator catches Exception
        internally and returns an empty NarrativeResult, so the empty
        segments would overwrite the persisted panel. The fix: when
        the corpus is non-empty but the narrator came back empty,
        leave the existing panel alone and surface the failure as a
        warning.
        """
        repo, store, _user, _entity, storyline_id = seeded_storyline

        # First regen — produces a real narrative.
        good_narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "The author writes about his son. "},
            {"kind": "citation", "entry_id": 1, "quote": "Atlas read."},
        ])
        svc_good = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=good_narrator,
            glue=_FakeGlue(),
        )
        svc_good.regenerate(storyline_id)
        original_panel = repo.get_panel(storyline_id, "narrative")
        assert original_panel is not None
        assert len(original_panel.segments) == 2

        # Second regen — narrator silently fails (returns no segments).
        # The existing narrative panel must NOT be overwritten.
        empty_narrator = _FakeNarrator(segments=[])
        svc_empty = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=empty_narrator,
            glue=_FakeGlue(),
        )
        result = svc_empty.regenerate(storyline_id)
        assert result.warnings
        assert any("preserved" in w.lower() for w in result.warnings)

        preserved = repo.get_panel(storyline_id, "narrative")
        assert preserved is not None
        # Same segments as before — not wiped.
        assert preserved.segments == original_panel.segments
        assert preserved.citation_count == original_panel.citation_count

    def test_citation_segments_include_entry_date(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        """Curation panel citations must carry the source entry's ISO
        date so the webapp's absolute-date toggle has data to work
        with. This is the field-level smoke test for the
        relative/absolute toggle feature."""
        repo, store, _user, _entity, storyline_id = seeded_storyline
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[
                {"kind": "text", "text": "intro"},
                {"kind": "citation", "entry_id": 1, "quote": "q"},
            ]),
            glue=_FakeGlue(),
        )
        svc.regenerate(storyline_id)

        curation = repo.get_panel(storyline_id, "curation")
        assert curation is not None
        citation_segs = [s for s in curation.segments if s["kind"] == "citation"]
        assert citation_segs, "curation panel should have citation segments"
        for seg in citation_segs:
            assert "entry_date" in seg, (
                "every curation citation must carry entry_date"
            )
            assert isinstance(seg["entry_date"], str)
            # ISO YYYY-MM-DD
            assert len(seg["entry_date"]) == 10
            assert seg["entry_date"][4] == "-" and seg["entry_date"][7] == "-"

    def test_embedder_receives_text_only_and_capped_input(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        """Regression for production bug: embedder was passed prose
        plus every citation's `quote`. With source="content" documents,
        `quote` was the whole wrapped entry; the narrator now uses
        source="text" so quotes are sentence-length — but text-only-
        plus-cap remains defence-in-depth against a future provider
        change or a corpus where a single sentence is unusually long.
        """
        repo, store, _user, _entity, storyline_id = seeded_storyline
        huge_quote = "X" * 60_000  # would blow the 8192-token limit
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "A concise narrative summary."},
            {"kind": "citation", "entry_id": 1, "quote": huge_quote},
            {"kind": "text", "text": "Another concise observation."},
        ])
        received: list[str] = []

        def embedder(text: str) -> list[float]:
            received.append(text)
            return [0.0]

        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
            embedder=embedder,
        )
        svc.regenerate(storyline_id)
        assert len(received) == 1
        embedded = received[0]
        # No citation quotes leaked in
        assert "X" * 1000 not in embedded
        assert "concise narrative summary" in embedded
        assert "concise observation" in embedded
        # Well below the 8192-token ceiling
        assert len(embedded) <= 32_000

    def test_embedder_input_is_truncated_when_prose_too_long(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        """If a future narrator emits >32k chars of prose, the
        embedder still gets a capped input rather than a 400."""
        repo, store, _user, _entity, storyline_id = seeded_storyline
        long_prose = "lorem ipsum " * 10_000  # ~120k chars
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": long_prose},
        ])
        received: list[str] = []

        def embedder(text: str) -> list[float]:
            received.append(text)
            return [0.0]

        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
            embedder=embedder,
        )
        svc.regenerate(storyline_id)
        assert len(received) == 1
        assert len(received[0]) <= 32_000

    def test_summary_embedding_persisted_when_embedder_given(
        self,
        seeded_storyline: tuple[Any, Any, int, int, int],
    ) -> None:
        repo, store, _user, _entity, storyline_id = seeded_storyline
        calls: list[str] = []

        def embedder(text: str) -> list[float]:
            calls.append(text)
            return [float(len(text)), 0.0, 0.0]

        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Hello world."},
            {"kind": "citation", "entry_id": 1, "quote": "cited"},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
            embedder=embedder,
        )
        svc.regenerate(storyline_id)
        refreshed = repo.get_storyline(storyline_id)
        assert refreshed is not None
        assert refreshed.summary_embedding is not None
        assert len(refreshed.summary_embedding) == 3
        assert len(calls) == 1
        # Embed input is the synthesised prose only — not citation quotes.
        # (See _join_narrative_text docstring for rationale.)
        assert calls[0] == "Hello world."

    def test_empty_window_persists_empty_panels(
        self,
        factory: ConnectionFactory,
    ) -> None:
        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name)"
            " VALUES (?, ?, ?)", ("e@x.test", "x", "E"),
        )
        user_id = cur.lastrowid
        conn.commit()
        store = SQLiteEntityStore(factory)
        entity = store.create_entity(
            entity_type="person", canonical_name="Atlas",
            description="", first_seen="2030-01-01", user_id=user_id,
        )
        repo = SQLiteStorylineRepository(factory)
        storyline = repo.create_storyline(
            user_id=user_id, entity_ids=[entity.id], name="Atlas",
            start_date="2030-01-01", end_date="2030-12-31",
        )
        narrator = _FakeNarrator(segments=[])
        glue = _FakeGlue()
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=glue,
        )
        result = svc.regenerate(storyline.id)
        assert result.entry_count == 0
        assert result.warnings
        assert "No entries found" in result.warnings[0]
        # Narrator/glue not called when there's nothing to generate
        assert narrator.calls == []
        assert glue.calls == 0

    def test_missing_storyline_raises(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo = SQLiteStorylineRepository(factory)
        store = SQLiteEntityStore(factory)
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[]),
            glue=_FakeGlue(),
        )
        with pytest.raises(ValueError, match="not found"):
            svc.regenerate(99999)

    def test_fts_fallback_against_real_repository(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """Regression for production bug: FTS fallback called
        ``EntryRepository.search_text(..., limit=50)`` but the real
        method on ``_SearchMixin`` doesn't accept a ``limit`` kwarg
        (and returns ``list[Entry]``, not ``list[SearchResult]``).
        Caught only after deploy because the fake-repo unit test had
        a permissive signature. This test wires the *real* repository
        so the integration is exercised end-to-end.
        """
        from journal.db.repository import SQLiteEntryRepository

        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name)"
            " VALUES (?, ?, ?)", ("ftsreal@x.test", "x", "F"),
        )
        user_id = cur.lastrowid
        conn.commit()
        store = SQLiteEntityStore(factory)
        entity = store.create_entity(
            entity_type="person", canonical_name="Atlas",
            description="", first_seen="2026-02-15", user_id=user_id,
        )
        # No entity mentions at all — FTS fallback path must fire.
        body = "I picked up Atlas from school today. He ran ahead."
        conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-03-01", "text", body, body, len(body.split()), user_id),
        )
        conn.commit()

        repo = SQLiteStorylineRepository(factory)
        storyline = repo.create_storyline(
            user_id=user_id, entity_ids=[entity.id], name="Atlas",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Atlas at school."},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=SQLiteEntryRepository(factory),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
            fts_fallback_threshold=3,
        )
        # Before the fix this raised TypeError on `limit=50`.
        result = svc.regenerate(storyline.id)
        assert result.fts_fallback_count == 1
        assert result.entry_count == 1

    def test_fts_fallback_when_mentions_are_sparse(
        self,
        factory: ConnectionFactory,
    ) -> None:
        # Seed user + entity + ONE mention. Threshold is 3, so FTS
        # fallback should fire and pull in extra entries.
        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name)"
            " VALUES (?, ?, ?)", ("f@x.test", "x", "F"),
        )
        user_id = cur.lastrowid
        conn.commit()
        store = SQLiteEntityStore(factory)
        entity = store.create_entity(
            entity_type="person", canonical_name="Atlas",
            description="", first_seen="2026-02-15", user_id=user_id,
        )
        # One real entity mention
        body1 = "Atlas read a book this evening."
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-02-15", "text", body1, body1, 6, user_id),
        )
        entry1_id = cur.lastrowid
        conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity.id, entry1_id, body1, 0.95, "run-1"),
        )
        # Two FTS-only entries (no entity mentions). Pretend the
        # extractor missed "Atlas" pronominal references.
        body2 = "My son was very tired today, he fell asleep on the sofa."
        body3 = "I picked up Atlas from school. He ran ahead."
        for entry_date, body in [("2026-02-20", body2), ("2026-03-01", body3)]:
            cur = conn.execute(
                "INSERT INTO entries"
                " (entry_date, source_type, raw_text, final_text,"
                "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                (entry_date, "text", body, body, len(body.split()), user_id),
            )
        conn.commit()

        fake_entry_repo = _FakeEntryRepo()
        # Pretend FTS returns the two non-extracted entries
        rows = conn.execute(
            "SELECT id, entry_date, final_text, raw_text FROM entries"
            " WHERE final_text LIKE '%Atlas%' AND id != ?", (entry1_id,),
        ).fetchall()
        for row in rows:
            entry = Entry(
                id=row["id"], entry_date=row["entry_date"],
                source_type="text", raw_text=row["raw_text"],
                final_text=row["final_text"], word_count=10, user_id=user_id,
            )
            fake_entry_repo.entries[row["id"]] = entry
            fake_entry_repo.search_hits.append(entry)

        repo = SQLiteStorylineRepository(factory)
        storyline = repo.create_storyline(
            user_id=user_id, entity_ids=[entity.id], name="Atlas",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Atlas's week."},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=fake_entry_repo,
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
            fts_fallback_threshold=3,
        )
        result = svc.regenerate(storyline.id)
        # 1 entity mention + (at most 2) FTS rows; one might be skipped
        # by the surface-form match. Whichever, at least 2 entries
        # should now be in scope.
        assert result.entry_count >= 2
        assert result.fts_fallback_count >= 1


# ── W6: append-mode regeneration ───────────────────────────────


def _seed_storyline_with_history(
    factory: ConnectionFactory,
    *,
    user_email: str = "append@x.test",
) -> tuple[
    SQLiteStorylineRepository,
    SQLiteEntityStore,
    int,
    int,
    int,
]:
    """Seed a storyline with three early entries + entity mentions
    and run an initial replace generation so the storyline has
    populated panels + last_generated_at set.

    Date choice: we use far-future dates (2099-…) so that the
    initial replace's ``last_generated_at`` (set to wall-clock NOW
    at run time) is comfortably BEFORE the future entries. Append
    mode requires the new run's start_date to be on or after
    last_generated_at, which means the new entries must be in
    the future relative to test execution time. 2099 gives us 70+
    years of headroom — the test stays correct until the year 2099,
    which is well past any reasonable codebase lifetime."""
    conn = factory.get()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        (user_email, "x", "U"),
    )
    user_id = cur.lastrowid
    conn.commit()
    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="person", canonical_name="Atlas",
        description="", first_seen="2099-01-10", user_id=user_id,
    )
    # Three early-window entries (2099-01) — included by the initial run.
    for entry_date, quote in [
        ("2099-01-10", "Atlas read his first chapter book."),
        ("2099-01-15", "Atlas asked about the stars."),
        ("2099-01-20", "Atlas drew a map of the garden."),
    ]:
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entry_date, "text", quote, quote, len(quote.split()), user_id),
        )
        entry_id = cur.lastrowid
        conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity.id, entry_id, quote, 0.95, "run-1"),
        )
    # Two future-window entries (2099-03) — used for the append run.
    for entry_date, quote in [
        ("2099-03-05", "Atlas ran a kilometre with me."),
        ("2099-03-12", "Atlas wrote a short story."),
    ]:
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entry_date, "text", quote, quote, len(quote.split()), user_id),
        )
        entry_id = cur.lastrowid
        conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity.id, entry_id, quote, 0.95, "run-2"),
        )
    conn.commit()
    repo = SQLiteStorylineRepository(factory)
    storyline = repo.create_storyline(
        user_id=user_id, entity_ids=[entity.id], name="Atlas",
        start_date="2099-01-01", end_date="2099-01-31",
    )
    return repo, store, user_id, entity.id, storyline.id


class TestAppendMode:
    def test_append_happy_path_extends_curation_and_narrative(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(factory)

        # First do a replace run to populate the panels.
        initial_narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Atlas's early month: "},
            {"kind": "citation", "entry_id": 1, "quote": "Atlas read."},
            {"kind": "citation", "entry_id": 2, "quote": "Atlas asked."},
            {"kind": "citation", "entry_id": 3, "quote": "Atlas drew."},
        ])
        svc_initial = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=initial_narrator,
            glue=_FakeGlue(),
        )
        svc_initial.regenerate(sid)
        curation_before = repo.get_panel(sid, "curation")
        narrative_before = repo.get_panel(sid, "narrative")
        assert curation_before is not None
        assert narrative_before is not None
        assert curation_before.citation_count == 3
        narrative_before_segments_count = len(narrative_before.segments)

        # Now append-run for March.
        append_narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Two months on, "},
            {"kind": "citation", "entry_id": 4, "quote": "Atlas ran."},
            {"kind": "citation", "entry_id": 5, "quote": "Atlas wrote."},
        ])
        svc_append = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=append_narrator,
            glue=_FakeGlue(),
        )
        result = svc_append.regenerate(
            sid,
            start_date="2099-03-01",
            end_date="2099-03-31",
            mode="append",
        )
        # Counts reflect only the new run.
        assert result.entry_count == 2
        assert result.entity_mention_count == 2
        assert result.narrative_citation_count == 2
        assert result.curation_citation_count == 2

        # Panels grew.
        curation_after = repo.get_panel(sid, "curation")
        narrative_after = repo.get_panel(sid, "narrative")
        assert curation_after is not None
        assert narrative_after is not None
        assert curation_after.citation_count == 5  # 3 + 2
        # New citations live at the END of the merged panel.
        citation_ids = [
            s["entry_id"]
            for s in curation_after.segments
            if s.get("kind") == "citation"
        ]
        assert citation_ids[-2:] == [4, 5]
        # Narrative grew.
        assert len(narrative_after.segments) > narrative_before_segments_count
        # source_entry_ids merged, no dupes, originals preserved first.
        assert narrative_after.source_entry_ids[:3] == narrative_before.source_entry_ids
        assert 4 in narrative_after.source_entry_ids
        assert 5 in narrative_after.source_entry_ids

    def test_append_passes_prior_narrative_to_narrator(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """The narrator receives the existing narrative prose as
        ``prior_narrative`` so it can produce a continuation rather
        than re-stating ground already covered."""
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="prior@x.test",
        )
        initial_narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "The early chapter described his curiosity."},
            {"kind": "citation", "entry_id": 1, "quote": "q"},
        ])
        StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=initial_narrator,
            glue=_FakeGlue(),
        ).regenerate(sid)

        append_narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Later: "},
            {"kind": "citation", "entry_id": 4, "quote": "q4"},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=append_narrator,
            glue=_FakeGlue(),
        )
        svc.regenerate(
            sid, start_date="2099-03-01", end_date="2099-03-31",
            mode="append",
        )
        assert append_narrator.last_prior_narrative is not None
        assert "early chapter" in append_narrator.last_prior_narrative

    def test_append_rejects_start_before_last_generated_at(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="early@x.test",
        )
        # Replace first to set last_generated_at.
        StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[
                {"kind": "text", "text": "x"},
                {"kind": "citation", "entry_id": 1, "quote": "q"},
            ]),
            glue=_FakeGlue(),
        ).regenerate(sid)

        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[]),
            glue=_FakeGlue(),
        )
        # last_generated_at is "now" (set by the initial replace),
        # so 2020-01-01 is well before it.
        with pytest.raises(ValueError, match="on or after"):
            svc.regenerate(
                sid, start_date="2020-01-01", mode="append",
            )

    def test_append_rejects_when_never_generated(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="never@x.test",
        )
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[]),
            glue=_FakeGlue(),
        )
        # No initial regen → last_generated_at is None.
        with pytest.raises(ValueError, match="previously-generated"):
            svc.regenerate(
                sid, start_date="2099-03-01", mode="append",
            )

    def test_append_rejects_without_start_date(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="nostart@x.test",
        )
        StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[
                {"kind": "text", "text": "x"},
                {"kind": "citation", "entry_id": 1, "quote": "q"},
            ]),
            glue=_FakeGlue(),
        ).regenerate(sid)

        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[]),
            glue=_FakeGlue(),
        )
        with pytest.raises(ValueError, match="explicit start_date"):
            svc.regenerate(sid, mode="append")

    def test_replace_mode_still_works(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """The default mode must remain replace; passing ``mode=replace``
        explicitly must produce identical behavior to omitting it."""
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="replace@x.test",
        )
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Replaced narrative."},
            {"kind": "citation", "entry_id": 1, "quote": "q"},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
        )
        result = svc.regenerate(sid, mode="replace")
        assert result.entry_count == 3  # initial Jan window: 3 entries
        # Append-only marker should NOT have been passed
        assert narrator.last_prior_narrative is None

    def test_date_range_override_on_replace_changes_window(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """Passing start_date/end_date in replace mode overrides
        whatever is on the storyline row (which here was Jan)."""
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="override@x.test",
        )
        narrator = _FakeNarrator(segments=[
            {"kind": "text", "text": "Override window."},
            {"kind": "citation", "entry_id": 4, "quote": "q"},
        ])
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=narrator,
            glue=_FakeGlue(),
        )
        # Without override: 3 entries (Jan). With override: 2 (March).
        result = svc.regenerate(
            sid, start_date="2099-03-01", end_date="2099-03-31",
        )
        assert result.entry_count == 2

    def test_invalid_mode_rejected(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="badmode@x.test",
        )
        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[]),
            glue=_FakeGlue(),
        )
        with pytest.raises(ValueError, match="Invalid mode"):
            svc.regenerate(sid, mode="bogus")  # type: ignore[arg-type]

    def test_append_with_no_new_entries_preserves_panels(
        self,
        factory: ConnectionFactory,
    ) -> None:
        repo, store, _u, _e, sid = _seed_storyline_with_history(
            factory, user_email="empty@x.test",
        )
        StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[
                {"kind": "text", "text": "x"},
                {"kind": "citation", "entry_id": 1, "quote": "q"},
            ]),
            glue=_FakeGlue(),
        ).regenerate(sid)
        before = repo.get_panel(sid, "narrative")
        assert before is not None

        svc = StorylineGenerationService(
            entity_store=store,
            entry_repository=_FakeEntryRepo(),
            storyline_repository=repo,
            narrator=_FakeNarrator(segments=[
                {"kind": "text", "text": "should not be persisted"},
            ]),
            glue=_FakeGlue(),
        )
        # April has no entries.
        result = svc.regenerate(
            sid, start_date="2099-04-01", end_date="2099-04-30",
            mode="append",
        )
        assert result.entry_count == 0
        assert result.warnings
        after = repo.get_panel(sid, "narrative")
        assert after is not None
        assert after.segments == before.segments
