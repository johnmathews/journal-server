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

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
    ) -> NarrativeResult:
        self.calls.append((len(excerpts), storyline_name))
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
        user_id=user_id, entity_id=entity.id, name="Atlas",
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
            user_id=user_id, entity_id=entity.id, name="Atlas",
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
            user_id=user_id, entity_id=entity.id, name="Atlas",
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
            user_id=user_id, entity_id=entity.id, name="Atlas",
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
