# Conversations Retrieval Robustness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `/conversations` reply path answer aggregate, temporal, and trend questions correctly (not from an 8-entry sample) while keeping today's strong lookup retrieval, strict grounding, and bounded cost.

**Architecture:** A new four-way intent classifier (Haiku, mirrors `providers/query_classifier.py`) routes each reply to a handler: `lookup` keeps today's hybrid path (now with adaptive passage count, matched-chunk truncation, and one bounded re-retrieval); `aggregate`/`temporal`/`trend` use the structured methods already on `QueryService`. Every new path falls back to `lookup` on any failure, so the floor is today's behavior. `services/conversations.py` becomes a small package.

**Tech Stack:** Python 3.13, uv, pytest, anthropic SDK, SQLite, ChromaDB.

**Spec:** `docs/superpowers/specs/2026-06-17-conversations-retrieval-robustness-design.md`

---

## File Structure

**Create:**
- `src/journal/providers/intent_classifier.py` — `IntentResult`, `IntentClassifier` Protocol, `HeuristicIntentClassifier`, `AnthropicIntentClassifier`, `build_intent_classifier`.
- `src/journal/services/conversations/__init__.py` — re-exports `ConversationService`, `ConversationNotFoundError`.
- `src/journal/services/conversations/service.py` — `ConversationService` (classify → dispatch → persist).
- `src/journal/services/conversations/passages.py` — `window_passage`, `select_passages`, `build_citations`.
- `src/journal/services/conversations/handlers.py` — `ReplyOutcome`, `LookupHandler`, `AggregateHandler`, `TemporalHandler`, `TrendHandler`.
- `tests/test_providers/test_intent_classifier.py`
- `tests/test_services/test_conversation_passages.py`
- `tests/test_services/test_conversation_handlers.py`

**Modify:**
- `src/journal/providers/answerer.py` — `continue_conversation` gains optional `context_note` and `retrieve` params (Protocol + `NoopAnswerer` + `AnthropicAnswerer`).
- `src/journal/mcp_server/bootstrap.py:775` — build the intent classifier + handlers, inject into `ConversationService`.
- `tests/test_services/test_conversations.py` — update for the new dispatch (kept passing throughout).

**Delete:**
- `src/journal/services/conversations.py` — content moves into the package in Task 1.

**Unchanged:** `services/query.py`, `services/hybrid.py`, the rerank/embedding providers, `services/answer.py` (the single-shot `/api/search/answer` path is not touched).

---

## Task 1: Move `conversations.py` into a package (no behavior change)

**Files:**
- Create: `src/journal/services/conversations/__init__.py`
- Create: `src/journal/services/conversations/service.py`
- Delete: `src/journal/services/conversations.py`

- [ ] **Step 1: Create the package by moving the module with git**

```bash
cd /Users/john/projects/journal/server
mkdir -p src/journal/services/conversations
git mv src/journal/services/conversations.py src/journal/services/conversations/service.py
```

- [ ] **Step 2: Add `__init__.py` that re-exports the public API**

Create `src/journal/services/conversations/__init__.py`:

```python
"""Conversation service package — start a thread, then reply with routing."""

from journal.services.conversations.service import (
    ConversationNotFoundError,
    ConversationService,
)

__all__ = ["ConversationNotFoundError", "ConversationService"]
```

- [ ] **Step 3: Run the existing conversation tests to confirm the move is transparent**

Run: `uv run pytest tests/test_services/test_conversations.py tests/test_bootstrap_conversations.py tests/test_api_conversations.py -q`
Expected: PASS (imports resolve via the new `__init__.py`; no behavior changed).

- [ ] **Step 4: Confirm no other importers broke**

Run: `uv run pytest -q -m "not integration"`
Expected: PASS (same count as before this task).

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/services/conversations tests
git commit -m "refactor: make conversations service a package (no behavior change)"
```

---

## Task 2: `window_passage` — matched-chunk truncation (weakness #4)

Centers the truncation window on the matched span instead of taking the first N chars.

**Files:**
- Create: `src/journal/services/conversations/passages.py`
- Test: `tests/test_services/test_conversation_passages.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_services/test_conversation_passages.py`:

```python
"""Passage selection + truncation helpers for conversation replies."""

from __future__ import annotations

from journal.models import ChunkMatch, SearchResult
from journal.services.conversations.passages import window_passage


def _result(text: str, *, chunks=None, snippet=None) -> SearchResult:
    return SearchResult(
        entry_id=1, entry_date="2026-01-01", text=text, score=1.0,
        matching_chunks=chunks or [], snippet=snippet,
    )


def test_window_centers_on_matching_chunk() -> None:
    text = "A" * 1000 + "TARGET" + "B" * 1000
    chunk = ChunkMatch(text="TARGET", score=0.9, chunk_index=1,
                       char_start=1000, char_end=1006)
    out = window_passage(_result(text, chunks=[chunk]), max_chars=100)
    assert "TARGET" in out
    assert len(out) <= 100


def test_window_falls_back_to_head_when_no_offsets() -> None:
    text = "C" * 500
    out = window_passage(_result(text), max_chars=100)
    assert out == "C" * 100


def test_window_returns_short_text_unchanged() -> None:
    out = window_passage(_result("short"), max_chars=100)
    assert out == "short"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: FAIL with `ModuleNotFoundError: ... passages`.

- [ ] **Step 3: Write minimal implementation**

Create `src/journal/services/conversations/passages.py`:

```python
"""Passage selection, truncation, and citation helpers for replies.

Pure functions — no I/O. `window_passage` centers an entry's truncation
window on the span that actually matched the query (matched-chunk
truncation) instead of taking the first N chars. `select_passages`
adapts how many passages to keep to the rerank-score distribution.
`build_citations` resolves cited entry ids back to preview snippets.
"""

from __future__ import annotations

from journal.models import SearchResult
from journal.providers.answerer import AnswerPassage

#: FTS5 snippet() wraps matched terms with these control characters.
_FTS_MARK_START = "\x02"
_FTS_MARK_END = "\x03"


def window_passage(result: SearchResult, max_chars: int) -> str:
    """Return up to `max_chars` of `result.text`, centered on the match.

    Locates the matched span via (1) the top dense `matching_chunks`
    offset, else (2) the FTS5 `snippet` control-char position, else (3)
    falls back to head truncation. The window is clamped to the text
    bounds. Always returns at most `max_chars` characters.
    """
    text = result.text
    if len(text) <= max_chars:
        return text

    center = _match_center(result, text)
    if center is None:
        return text[:max_chars]

    half = max_chars // 2
    start = max(0, min(center - half, len(text) - max_chars))
    return text[start : start + max_chars]


def _match_center(result: SearchResult, text: str) -> int | None:
    """Character index to center the window on, or None for head-truncate."""
    for chunk in result.matching_chunks:
        if chunk.char_start is not None and chunk.char_end is not None:
            return (chunk.char_start + chunk.char_end) // 2
    if result.snippet:
        marked = result.snippet.find(_FTS_MARK_START)
        if marked >= 0:
            term = result.snippet[marked + 1 :].split(_FTS_MARK_END, 1)[0]
            pos = text.find(term) if term else -1
            if pos >= 0:
                return pos + len(term) // 2
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/passages.py tests/test_services/test_conversation_passages.py
git commit -m "feat: matched-chunk truncation for conversation passages"
```

---

## Task 3: `select_passages` — adaptive passage count (weakness #3)

**Files:**
- Modify: `src/journal/services/conversations/passages.py`
- Test: `tests/test_services/test_conversation_passages.py`

- [ ] **Step 1: Write the failing test (append)**

Append to `tests/test_services/test_conversation_passages.py`:

```python
from journal.services.conversations.passages import select_passages


def _scored(entry_id: int, score: float) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date="2026-01-01", text="t" * 50,
        score=score, matching_chunks=[], snippet=None,
    )


def test_select_keeps_floor_when_one_strong_result() -> None:
    results = [_scored(1, 0.9)] + [_scored(i, 0.05) for i in range(2, 10)]
    out = select_passages(results, max_chars=800, floor=3, ceiling=15, band=0.5)
    # one dominant score -> only the floor survives the band cut
    assert [p.entry_id for p in out] == [1, 2, 3]


def test_select_clamps_to_ceiling_when_many_close() -> None:
    results = [_scored(i, 0.9) for i in range(1, 30)]
    out = select_passages(results, max_chars=800, floor=3, ceiling=15, band=0.5)
    assert len(out) == 15


def test_select_returns_answer_passages_with_windowed_text() -> None:
    out = select_passages([_scored(1, 0.9)], max_chars=10, floor=1, ceiling=5,
                          band=0.5)
    assert isinstance(out[0], AnswerPassage)
    assert out[0].entry_id == 1
    assert len(out[0].text) <= 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: FAIL with `ImportError: cannot import name 'select_passages'`.

- [ ] **Step 3: Write minimal implementation (append to passages.py)**

```python
def select_passages(
    results: list[SearchResult],
    *,
    max_chars: int,
    floor: int = 3,
    ceiling: int = 15,
    band: float = 0.5,
) -> list[AnswerPassage]:
    """Pick an adaptive number of passages from ranked `results`.

    `results` must be ordered by rerank score descending. Keeps every
    result whose score is within `band` (relative to the top score) of
    the top, then clamps the count to `[floor, ceiling]`. Each kept
    result is truncated with `window_passage`.
    """
    if not results:
        return []
    top = results[0].score
    cutoff = top * (1.0 - band) if top > 0 else 0.0
    kept = [r for r in results if r.score >= cutoff]
    n = max(floor, min(len(kept), ceiling))
    n = min(n, len(results))
    chosen = results[:n]
    return [
        AnswerPassage(
            entry_id=r.entry_id,
            entry_date=r.entry_date,
            text=window_passage(r, max_chars),
        )
        for r in chosen
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/passages.py tests/test_services/test_conversation_passages.py
git commit -m "feat: adaptive passage count for conversation lookups"
```

---

## Task 4: `build_citations` — shared citation resolver

**Files:**
- Modify: `src/journal/services/conversations/passages.py`
- Test: `tests/test_services/test_conversation_passages.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from journal.services.conversations.passages import build_citations


def test_build_citations_resolves_known_ids_and_drops_unknown() -> None:
    by_id = {7: ("2026-03-01", "Back better now, much less pain today.")}
    cites = build_citations([7, 999], by_id, snippet_chars=10)
    assert len(cites) == 1
    assert cites[0]["entry_id"] == 7
    assert cites[0]["entry_date"] == "2026-03-01"
    assert cites[0]["snippet"] == "Back bette"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_citations'`.

- [ ] **Step 3: Write minimal implementation (append to passages.py)**

```python
def build_citations(
    cited_entry_ids: list[int],
    by_id: dict[int, tuple[str, str]],
    *,
    snippet_chars: int = 160,
) -> list[dict]:
    """Resolve cited entry ids to citation dicts, dropping unknown ids.

    `by_id` maps entry_id -> (entry_date, text). Preserves the order of
    `cited_entry_ids`. Mirrors the citation shape the conversation repo
    persists.
    """
    out: list[dict] = []
    for eid in cited_entry_ids:
        if eid not in by_id:
            continue
        date, text = by_id[eid]
        out.append(
            {
                "entry_id": eid,
                "entry_date": date,
                "snippet": text[:snippet_chars].strip(),
            }
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_passages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/passages.py tests/test_services/test_conversation_passages.py
git commit -m "feat: shared citation resolver for conversation replies"
```

---

## Task 5: Intent classifier provider (weaknesses #1, #2, #6)

Mirrors `providers/query_classifier.py`: Protocol + heuristic fallback + Anthropic adapter + builder.

**Files:**
- Create: `src/journal/providers/intent_classifier.py`
- Test: `tests/test_providers/test_intent_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_providers/test_intent_classifier.py`:

```python
"""Intent classifier — heuristic fallback + Anthropic JSON parsing."""

from __future__ import annotations

from journal.providers.intent_classifier import (
    HeuristicIntentClassifier,
    IntentResult,
    build_intent_classifier,
)


def test_heuristic_aggregate() -> None:
    r = HeuristicIntentClassifier().classify("how many times did I mention my back?")
    assert r.intent == "aggregate"


def test_heuristic_temporal() -> None:
    r = HeuristicIntentClassifier().classify("when did the back pain start?")
    assert r.intent == "temporal"


def test_heuristic_trend() -> None:
    r = HeuristicIntentClassifier().classify("have I gotten happier this year?")
    assert r.intent == "trend"


def test_heuristic_defaults_to_lookup() -> None:
    r = HeuristicIntentClassifier().classify("what did I say about Vienna?")
    assert r.intent == "lookup"
    # search_query defaults to the question itself
    assert r.search_query == "what did I say about Vienna?"


def test_anthropic_parse_via_builder_falls_back_to_heuristic_on_blank() -> None:
    clf = build_intent_classifier("none")
    assert isinstance(clf.classify("anything"), IntentResult)


def test_anthropic_parses_structured_json(monkeypatch) -> None:
    from journal.providers import intent_classifier as mod

    clf = mod.AnthropicIntentClassifier(api_key="x")

    class _Block:
        text = (
            '{"intent": "aggregate", "topic": "back", '
            '"start_date": null, "end_date": null, "dimension": null, '
            '"search_query": "back pain mentions"}'
        )

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(clf._client.messages, "create", lambda **_: _Resp())
    r = clf.classify("how many times did I mention my back?")
    assert r.intent == "aggregate"
    assert r.topic == "back"
    assert r.search_query == "back pain mentions"


def test_anthropic_malformed_falls_back_to_heuristic(monkeypatch) -> None:
    from journal.providers import intent_classifier as mod

    clf = mod.AnthropicIntentClassifier(api_key="x")

    class _Block:
        text = "not json"

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(clf._client.messages, "create", lambda **_: _Resp())
    r = clf.classify("when did my back start hurting?")
    assert r.intent == "temporal"  # heuristic rescued it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_intent_classifier.py -q`
Expected: FAIL with `ModuleNotFoundError: ... intent_classifier`.

- [ ] **Step 3: Write minimal implementation**

Create `src/journal/providers/intent_classifier.py`:

```python
"""Four-way intent classification for conversation replies.

Distinct from `query_classifier.py`, which is a binary question/search
gate for the single-shot answer endpoint. This classifier decides which
*retrieval shape* a conversation reply needs:

- `lookup`    — "what did I say about Vienna" → hybrid retrieval (default).
- `aggregate` — "how many times did I mention my back" → counts.
- `temporal`  — "when did the back pain start" → date-sorted retrieval.
- `trend`     — "have I gotten happier" → mood trends.

It also emits `search_query`: a standalone retrieval query that folds in
conversation context, replacing the crude "original + latest" concat
(spec weakness #6).

Mirrors the provider pattern in `query_classifier.py`:
- `HeuristicIntentClassifier` — offline regex rules; the fallback when
  the Anthropic adapter errors or returns unparseable output, so a
  classifier hiccup degrades to `lookup`, never blocks a reply.
- `AnthropicIntentClassifier` — one cheap Haiku JSON call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import anthropic

logger = logging.getLogger(__name__)

Intent = Literal["lookup", "aggregate", "temporal", "trend"]

_AGGREGATE = re.compile(r"\bhow (many|often)\b|\bhow much\b|\bcount\b", re.I)
_TEMPORAL = re.compile(r"\bwhen did\b|\bwhen was\b|\bfirst (time|mention)\b", re.I)
_TREND = re.compile(
    r"\b(trend|over time|gotten (more|less|better|worse|happier|sadder)|"
    r"have i (become|been)|mood)\b",
    re.I,
)


def _heuristic_intent(question: str) -> Intent:
    q = question.strip()
    if _TREND.search(q):
        return "trend"
    if _AGGREGATE.search(q):
        return "aggregate"
    if _TEMPORAL.search(q):
        return "temporal"
    return "lookup"


@dataclass(frozen=True)
class IntentResult:
    """Classified intent plus extracted retrieval parameters."""

    intent: Intent
    search_query: str
    topic: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    dimension: str | None = None


@runtime_checkable
class IntentClassifier(Protocol):
    def classify(self, question: str, context: str | None = None) -> IntentResult: ...


class HeuristicIntentClassifier:
    """Offline regex classifier; `search_query` is the question itself."""

    def classify(self, question: str, context: str | None = None) -> IntentResult:
        return IntentResult(
            intent=_heuristic_intent(question),
            search_query=question.strip(),
        )


_SYSTEM_PROMPT = (
    "You route a message in a conversation about a person's private "
    "journal. Decide which kind of retrieval answering it needs and "
    "extract parameters.\n\n"
    "Intents:\n"
    "- lookup: find entries about a topic ('what did I say about Vienna').\n"
    "- aggregate: count how many/often something occurs ('how many times "
    "did I mention my back').\n"
    "- temporal: when something started/stopped/first happened ('when did "
    "the back pain start').\n"
    "- trend: how something changed over time, esp. mood ('have I gotten "
    "happier this year').\n\n"
    "Output a single JSON object with exactly this shape:\n"
    "  {\n"
    '    "intent": "lookup|aggregate|temporal|trend",\n'
    '    "topic": "<noun phrase being asked about, or null>",\n'
    '    "start_date": "<YYYY-MM-DD or null>",\n'
    '    "end_date": "<YYYY-MM-DD or null>",\n'
    '    "dimension": "<mood dimension for trend, or null>",\n'
    '    "search_query": "<standalone retrieval query folding in the '
    'conversation so far>"\n'
    "  }\n\n"
    "Output the JSON object only. No prose, no markdown."
)


class AnthropicIntentClassifier:
    """Four-way intent classifier via an Anthropic Claude model (Haiku)."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 256,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def classify(self, question: str, context: str | None = None) -> IntentResult:
        if not question.strip():
            return IntentResult(intent="lookup", search_query=question.strip())
        user = question if not context else f"Conversation so far:\n{context}\n\nLatest message: {question}"
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as e:
            logger.warning("AnthropicIntentClassifier failed (%s); using heuristic", e)
            return HeuristicIntentClassifier().classify(question, context)

        parsed = self._parse(self._first_text(response))
        if parsed is None:
            logger.warning("AnthropicIntentClassifier unparseable; using heuristic")
            return HeuristicIntentClassifier().classify(question, context)
        return IntentResult(
            intent=parsed["intent"],
            search_query=parsed.get("search_query") or question.strip(),
            topic=parsed.get("topic"),
            start_date=parsed.get("start_date"),
            end_date=parsed.get("end_date"),
            dimension=parsed.get("dimension"),
        )

    @staticmethod
    def _first_text(response: object) -> str:
        content = getattr(response, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                return text
        return ""

    @staticmethod
    def _parse(raw: str) -> dict | None:
        if not raw:
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("intent") not in ("lookup", "aggregate", "temporal", "trend"):
            return None
        return parsed


def build_intent_classifier(
    name: str,
    *,
    anthropic_api_key: str = "",
    model: str = "claude-haiku-4-5",
) -> IntentClassifier:
    """Build an intent classifier by name. Unknown names raise (fail-fast)."""
    if name in ("none", "noop", "heuristic"):
        return HeuristicIntentClassifier()
    if name == "anthropic":
        if not anthropic_api_key:
            raise ValueError(
                "AnthropicIntentClassifier requires ANTHROPIC_API_KEY to be set"
            )
        return AnthropicIntentClassifier(api_key=anthropic_api_key, model=model)
    raise ValueError(
        f"Unknown intent classifier {name!r} — must be 'anthropic' or 'none'"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_providers/test_intent_classifier.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/intent_classifier.py tests/test_providers/test_intent_classifier.py
git commit -m "feat: four-way intent classifier for conversation routing"
```

---

## Task 6: Answerer gains `context_note` (for aggregate/trend computed facts)

`aggregate`/`trend` handlers need to feed the model a computed number (e.g. "you mentioned X in 40 entries") while still grounding citations in real entries. Add an optional note that gets prepended to the latest user turn.

**Files:**
- Modify: `src/journal/providers/answerer.py` (Protocol, `NoopAnswerer`, `AnthropicAnswerer.continue_conversation`)
- Test: `tests/test_providers/test_answerer.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Append to (or create) `tests/test_providers/test_answerer.py`:

```python
"""AnthropicAnswerer.continue_conversation context_note handling."""

from __future__ import annotations

from journal.providers.answerer import (
    AnswerPassage,
    AnthropicAnswerer,
    ConversationTurn,
)


def test_context_note_is_prepended_to_last_user_turn(monkeypatch) -> None:
    captured = {}

    class _Block:
        text = '{"answer": "ok", "answered": true, "cited_entry_ids": [1]}'

    class _Resp:
        content = [_Block()]

    a = AnthropicAnswerer(api_key="x")

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(a._client.messages, "create", _fake_create)

    history = [ConversationTurn(role="user", content="how many times?")]
    passages = [AnswerPassage(entry_id=1, entry_date="2026-01-01", text="back")]
    a.continue_conversation(history, passages, context_note="Computed: 40 entries.")

    last_user = captured["messages"][-1]["content"]
    assert "Computed: 40 entries." in last_user
    assert "back" in last_user  # passages still present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_answerer.py::test_context_note_is_prepended_to_last_user_turn -q`
Expected: FAIL with `TypeError: continue_conversation() got an unexpected keyword argument 'context_note'`.

- [ ] **Step 3: Update the Protocol and both adapters**

In `src/journal/providers/answerer.py`, change the `Answerer` Protocol method and `NoopAnswerer.continue_conversation` to accept the new keyword, and update `AnthropicAnswerer.continue_conversation`.

Protocol (replace the `continue_conversation` signature):

```python
    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
        *,
        context_note: str | None = None,
    ) -> AnswerResult: ...
```

`NoopAnswerer.continue_conversation` — add the same `*, context_note: str | None = None` parameter; body unchanged.

`AnthropicAnswerer.continue_conversation` — update the signature and the message assembly:

```python
    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
        *,
        context_note: str | None = None,
    ) -> AnswerResult:
        if not history:
            return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

        messages = [{"role": t.role, "content": t.content} for t in history]
        note_block = f"Computed facts: {context_note}\n\n" if context_note else ""
        passage_block = "\n".join(
            [*self._passage_lines(passages), "", "Output the JSON object now."]
        )
        messages[-1]["content"] = (
            f"{messages[-1]['content']}\n\n{note_block}{passage_block}"
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": _CONVERSATION_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
            )
        except anthropic.APIError as e:
            logger.warning("AnthropicAnswerer continue call failed: %s", e)
            raise AnswerUnavailable(str(e)) from e

        return self._result_from_response(response, {p.entry_id for p in passages})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers/test_answerer.py tests/test_services/test_conversations.py -q`
Expected: PASS (existing callers pass no `context_note`, so behavior is unchanged for them).

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/answerer.py tests/test_providers/test_answerer.py
git commit -m "feat: optional context_note on continue_conversation"
```

---

## Task 7: Bounded one-hop re-retrieval via `search_again` tool (weakness #5)

Give the answerer a single `search_again(query)` tool on the lookup path. When the model invokes it, run the caller-supplied `retrieve` callback once, feed results back, and let the model finalize. Capped at one extra hop.

**Files:**
- Modify: `src/journal/providers/answerer.py`
- Test: `tests/test_providers/test_answerer.py`

- [ ] **Step 1: Write the failing test (append)**

```python
import json


def test_search_again_runs_retrieve_once(monkeypatch) -> None:
    a = AnthropicAnswerer(api_key="x")
    calls = {"n": 0}

    class _ToolUse:
        type = "tool_use"
        id = "t1"
        name = "search_again"
        input = {"query": "reformulated"}

    class _FinalBlock:
        type = "text"
        text = '{"answer": "done", "answered": true, "cited_entry_ids": [2]}'

    class _ToolResp:
        stop_reason = "tool_use"
        content = [_ToolUse()]

    class _FinalResp:
        stop_reason = "end_turn"
        content = [_FinalBlock()]

    responses = [_ToolResp(), _FinalResp()]
    monkeypatch.setattr(
        a._client.messages, "create", lambda **_: responses.pop(0)
    )

    def _retrieve(query: str):
        calls["n"] += 1
        assert query == "reformulated"
        return [AnswerPassage(entry_id=2, entry_date="2026-02-02", text="more")]

    history = [ConversationTurn(role="user", content="tell me more")]
    result = a.continue_conversation(
        history,
        [AnswerPassage(entry_id=1, entry_date="2026-01-01", text="first")],
        retrieve=_retrieve,
    )
    assert calls["n"] == 1  # exactly one extra hop
    assert result.answer == "done"
    assert result.cited_entry_ids == [2]


def test_search_again_capped_at_one_hop(monkeypatch) -> None:
    a = AnthropicAnswerer(api_key="x")

    class _ToolUse:
        type = "tool_use"
        id = "t1"
        name = "search_again"
        input = {"query": "again"}

    class _ToolResp:
        stop_reason = "tool_use"
        content = [_ToolUse()]

    # The model keeps trying to call the tool; we must stop after one hop.
    monkeypatch.setattr(a._client.messages, "create", lambda **_: _ToolResp())

    seen = {"n": 0}

    def _retrieve(query: str):
        seen["n"] += 1
        return [AnswerPassage(entry_id=9, entry_date="2026-03-03", text="x")]

    result = a.continue_conversation(
        [ConversationTurn(role="user", content="more")],
        [AnswerPassage(entry_id=1, entry_date="2026-01-01", text="first")],
        retrieve=_retrieve,
    )
    assert seen["n"] == 1  # capped — not called a second time
    assert result.answered is False  # gave up gracefully
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_answerer.py -k search_again -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'retrieve'`.

- [ ] **Step 3: Implement the bounded tool loop**

In `AnthropicAnswerer`, add the tool definition near the system prompts:

```python
_SEARCH_AGAIN_TOOL = {
    "name": "search_again",
    "description": (
        "Retrieve more journal passages with a reformulated query. Use "
        "ONCE if the supplied passages are insufficient to answer the "
        "latest message. Returns dated passages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A standalone search query.",
            }
        },
        "required": ["query"],
    },
}
```

Update `continue_conversation` to thread an optional `retrieve` callback and run at most one hop. Replace the method body with:

```python
    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
        *,
        context_note: str | None = None,
        retrieve: "Callable[[str], list[AnswerPassage]] | None" = None,
    ) -> AnswerResult:
        if not history:
            return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

        messages = [{"role": t.role, "content": t.content} for t in history]
        note_block = f"Computed facts: {context_note}\n\n" if context_note else ""
        passage_block = "\n".join(
            [*self._passage_lines(passages), "", "Output the JSON object now."]
        )
        messages[-1]["content"] = (
            f"{messages[-1]['content']}\n\n{note_block}{passage_block}"
        )

        valid_ids = {p.entry_id for p in passages}
        tools = [_SEARCH_AGAIN_TOOL] if retrieve is not None else []
        hops_left = 1

        while True:
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    thinking={"type": "adaptive"},
                    system=[
                        {
                            "type": "text",
                            "text": _CONVERSATION_SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages,
                    tools=tools,
                )
            except anthropic.APIError as e:
                logger.warning("AnthropicAnswerer continue call failed: %s", e)
                raise AnswerUnavailable(str(e)) from e

            if getattr(response, "stop_reason", None) == "tool_use" and hops_left > 0:
                tool_use = self._first_tool_use(response)
                if tool_use is not None and retrieve is not None:
                    hops_left -= 1
                    new_passages = retrieve(tool_use.input.get("query", ""))
                    valid_ids.update(p.entry_id for p in new_passages)
                    messages.append(
                        {"role": "assistant", "content": response.content}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": "\n".join(
                                        self._passage_lines(new_passages)
                                    ),
                                }
                            ],
                        }
                    )
                    continue
                # No usable tool call — stop and treat as unanswered.
                return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

            if getattr(response, "stop_reason", None) == "tool_use":
                # Out of hops but the model still wants the tool — give up.
                return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

            return self._result_from_response(response, valid_ids)
```

Add the helper:

```python
    @staticmethod
    def _first_tool_use(response: object):
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use":
                return block
        return None
```

Add the import at the top of the file:

```python
from collections.abc import Callable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers/test_answerer.py -q`
Expected: PASS (the `context_note` test still passes — `retrieve` defaults to `None`, so `tools=[]` and the loop returns on the first non-tool response).

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/answerer.py tests/test_providers/test_answerer.py
git commit -m "feat: bounded one-hop search_again tool on continue_conversation"
```

---

## Task 8: `ReplyOutcome` + `LookupHandler`

**Files:**
- Create: `src/journal/services/conversations/handlers.py`
- Test: `tests/test_services/test_conversation_handlers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_services/test_conversation_handlers.py`:

```python
"""Conversation intent handlers."""

from __future__ import annotations

from journal.models import Entry, SearchResult, TopicFrequency
from journal.providers.answerer import AnswerResult, ConversationTurn
from journal.providers.intent_classifier import IntentResult
from journal.services.conversations.handlers import (
    AggregateHandler,
    LookupHandler,
    ReplyOutcome,
    TemporalHandler,
    TrendHandler,
)


def _sr(entry_id: int, text: str, score: float = 1.0) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date="2026-03-01", text=text, score=score,
        matching_chunks=[], snippet=None,
    )


class _Query:
    def __init__(self, **returns):
        self.returns = returns
        self.calls: list[tuple[str, dict]] = []

    def search_entries(self, **kw):
        self.calls.append(("search_entries", kw))
        return self.returns.get("search_entries", [])

    def get_topic_frequency(self, topic, start_date=None, end_date=None, user_id=None):
        self.calls.append(("get_topic_frequency", {"topic": topic}))
        return self.returns["get_topic_frequency"]

    def get_mood_trends(self, start_date=None, end_date=None, granularity="week", user_id=None):
        self.calls.append(("get_mood_trends", {}))
        return self.returns.get("get_mood_trends", [])


class _Answerer:
    def __init__(self, result: AnswerResult):
        self._result = result
        self.last_kwargs = None

    def continue_conversation(self, history, passages, *, context_note=None, retrieve=None):
        self.last_kwargs = {
            "passages": passages,
            "context_note": context_note,
            "retrieve": retrieve,
        }
        return self._result


def _history() -> list[ConversationTurn]:
    return [ConversationTurn(role="user", content="follow up")]


def test_lookup_handler_passes_retrieve_and_resolves_citations() -> None:
    query = _Query(search_entries=[_sr(7, "Back better now.")])
    answerer = _Answerer(AnswerResult("Around March.", True, [7]))
    handler = LookupHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="lookup", search_query="back pain better")

    out = handler.handle(_history(), intent, user_id=1)

    assert isinstance(out, ReplyOutcome)
    assert out.answer == "Around March."
    assert out.citations[0]["entry_id"] == 7
    # lookup gives the answerer a one-hop retrieve callback
    assert answerer.last_kwargs["retrieve"] is not None
    # retrieval used the condensed search_query, larger candidate pool
    assert query.calls[0][1]["query"] == "back pain better"
    assert query.calls[0][1]["limit"] >= 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_lookup_handler_passes_retrieve_and_resolves_citations -q`
Expected: FAIL with `ModuleNotFoundError: ... handlers`.

- [ ] **Step 3: Write minimal implementation**

Create `src/journal/services/conversations/handlers.py`:

```python
"""Per-intent handlers for conversation replies.

Each handler turns a classified `IntentResult` + conversation history
into a `ReplyOutcome` (answer text + resolved citations). Handlers
depend only on a `QueryService`-shaped object and an `Answerer`, so they
are unit-testable with stubs. The grounding contract is identical across
handlers — only the material fed to the answerer differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from journal.providers.answerer import AnswerPassage
from journal.services.conversations.passages import (
    build_citations,
    select_passages,
    window_passage,
)

if TYPE_CHECKING:
    from journal.providers.answerer import Answerer, ConversationTurn
    from journal.providers.intent_classifier import IntentResult
    from journal.services.query import QueryService

#: Candidate pool retrieved before adaptive selection trims it.
_CANDIDATE_POOL = 20
_PASSAGE_FLOOR = 3
_PASSAGE_CEILING = 15
_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class ReplyOutcome:
    """A handler's result: the answer plus persisted-shape citations."""

    answer: str
    answered: bool
    citations: list[dict]


class LookupHandler:
    """Today's hybrid path + adaptive count + one bounded re-retrieval."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        results = self._query.search_entries(
            query=intent.search_query,
            limit=_CANDIDATE_POOL,
            offset=0,
            user_id=user_id,
        )
        passages = select_passages(
            results,
            max_chars=self._passage_chars,
            floor=_PASSAGE_FLOOR,
            ceiling=_PASSAGE_CEILING,
        )
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}

        def _retrieve(query: str) -> list[AnswerPassage]:
            more = self._query.search_entries(
                query=query, limit=_CANDIDATE_POOL, offset=0, user_id=user_id
            )
            for r in more:
                by_id[r.entry_id] = (r.entry_date, r.text)
            return select_passages(
                more,
                max_chars=self._passage_chars,
                floor=_PASSAGE_FLOOR,
                ceiling=_PASSAGE_CEILING,
            )

        result = self._answerer.continue_conversation(
            history, passages, retrieve=_retrieve
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_lookup_handler_passes_retrieve_and_resolves_citations -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/handlers.py tests/test_services/test_conversation_handlers.py
git commit -m "feat: LookupHandler with adaptive passages + bounded re-retrieval"
```

---

## Task 9: `AggregateHandler` (weakness #1)

**Files:**
- Modify: `src/journal/services/conversations/handlers.py`
- Test: `tests/test_services/test_conversation_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_aggregate_handler_injects_count_note_and_cites_entries() -> None:
    entries = [Entry(id=7, entry_date="2026-02-14", raw_text="my back hurt",
                     final_text="my back hurt")]
    tf = TopicFrequency(topic="back", count=40, entries=entries)
    query = _Query(get_topic_frequency=tf)
    answerer = _Answerer(AnswerResult("You mentioned it 40 times.", True, [7]))
    handler = AggregateHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="aggregate", search_query="back",
                          topic="back")

    out = handler.handle(_history(), intent, user_id=1)

    assert query.calls[0][0] == "get_topic_frequency"
    assert "40" in answerer.last_kwargs["context_note"]
    assert out.citations[0]["entry_id"] == 7
    assert out.answer == "You mentioned it 40 times."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_aggregate_handler_injects_count_note_and_cites_entries -q`
Expected: FAIL with `ImportError: cannot import name 'AggregateHandler'`.

- [ ] **Step 3: Write minimal implementation (append to handlers.py)**

```python
def _entry_text(entry) -> str:
    return entry.final_text or entry.raw_text or ""


class AggregateHandler:
    """Count/frequency questions — answer leads with the number."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        topic = intent.topic or intent.search_query
        freq = self._query.get_topic_frequency(
            topic,
            start_date=intent.start_date,
            end_date=intent.end_date,
            user_id=user_id,
        )
        note = (
            f"The phrase/topic '{freq.topic}' appears in {freq.count} "
            f"journal entries"
            + (
                f" between {intent.start_date} and {intent.end_date}."
                if intent.start_date and intent.end_date
                else "."
            )
        )
        passages = [
            AnswerPassage(
                entry_id=e.id,
                entry_date=e.entry_date,
                text=_entry_text(e)[: self._passage_chars],
            )
            for e in freq.entries
        ]
        by_id = {e.id: (e.entry_date, _entry_text(e)) for e in freq.entries}
        result = self._answerer.continue_conversation(
            history, passages, context_note=note
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_aggregate_handler_injects_count_note_and_cites_entries -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/handlers.py tests/test_services/test_conversation_handlers.py
git commit -m "feat: AggregateHandler for count/frequency conversation questions"
```

---

## Task 10: `TemporalHandler` (weakness #2)

**Files:**
- Modify: `src/journal/services/conversations/handlers.py`
- Test: `tests/test_services/test_conversation_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_temporal_handler_sorts_ascending_and_cites() -> None:
    query = _Query(search_entries=[_sr(7, "First back pain.")])
    answerer = _Answerer(AnswerResult("It started 2026-03-01.", True, [7]))
    handler = TemporalHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="temporal", search_query="back pain start")

    out = handler.handle(_history(), intent, user_id=1)

    # date_asc guarantees the earliest evidencing entry is present
    assert query.calls[0][1]["sort"] == "date_asc"
    assert out.citations[0]["entry_id"] == 7
    assert out.answer == "It started 2026-03-01."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_temporal_handler_sorts_ascending_and_cites -q`
Expected: FAIL with `ImportError: cannot import name 'TemporalHandler'`.

- [ ] **Step 3: Write minimal implementation (append to handlers.py)**

```python
class TemporalHandler:
    """When-did-X questions — retrieve date-ascending so the earliest wins."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        results = self._query.search_entries(
            query=intent.search_query,
            start_date=intent.start_date,
            end_date=intent.end_date,
            limit=_PASSAGE_CEILING,
            offset=0,
            user_id=user_id,
            sort="date_asc",
        )
        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=window_passage(r, self._passage_chars),
            )
            for r in results
        ]
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}
        result = self._answerer.continue_conversation(history, passages)
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_temporal_handler_sorts_ascending_and_cites -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/handlers.py tests/test_services/test_conversation_handlers.py
git commit -m "feat: TemporalHandler for when-did-X conversation questions"
```

---

## Task 11: `TrendHandler` (weakness #1, trend shape)

**Files:**
- Modify: `src/journal/services/conversations/handlers.py`
- Test: `tests/test_services/test_conversation_handlers.py`

- [ ] **Step 1: Write the failing test (append)**

```python
from journal.models import MoodTrend


def test_trend_handler_summarizes_series_into_note() -> None:
    trends = [
        MoodTrend(period="2026-W01", dimension="happiness", avg_score=0.3,
                  entry_count=4),
        MoodTrend(period="2026-W20", dimension="happiness", avg_score=0.7,
                  entry_count=5),
    ]
    query = _Query(get_mood_trends=trends,
                   search_entries=[_sr(7, "Felt great.")])
    answerer = _Answerer(AnswerResult("You've trended happier.", True, [7]))
    handler = TrendHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="trend", search_query="happiness over time",
                          dimension="happiness")

    out = handler.handle(_history(), intent, user_id=1)

    assert query.calls[0][0] == "get_mood_trends"
    assert "happiness" in answerer.last_kwargs["context_note"]
    assert "0.3" in answerer.last_kwargs["context_note"]
    assert out.answer == "You've trended happier."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py::test_trend_handler_summarizes_series_into_note -q`
Expected: FAIL with `ImportError: cannot import name 'TrendHandler'`.

- [ ] **Step 3: Write minimal implementation (append to handlers.py)**

```python
class TrendHandler:
    """Change-over-time / mood questions — summarize the series as a note."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        trends = self._query.get_mood_trends(
            start_date=intent.start_date,
            end_date=intent.end_date,
            user_id=user_id,
        )
        relevant = (
            [t for t in trends if t.dimension == intent.dimension]
            if intent.dimension
            else trends
        )
        series = ", ".join(f"{t.period}={t.avg_score:.2f}" for t in relevant)
        note = (
            f"Mood trend for '{intent.dimension or 'overall'}' "
            f"(period=avg_score): {series}."
            if series
            else "No mood-trend data is available for this period."
        )
        results = self._query.search_entries(
            query=intent.search_query,
            limit=_PASSAGE_FLOOR,
            offset=0,
            user_id=user_id,
        )
        passages = select_passages(
            results,
            max_chars=self._passage_chars,
            floor=_PASSAGE_FLOOR,
            ceiling=_PASSAGE_FLOOR,
        )
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}
        result = self._answerer.continue_conversation(
            history, passages, context_note=note
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_services/test_conversation_handlers.py -q`
Expected: PASS (all handler tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/handlers.py tests/test_services/test_conversation_handlers.py
git commit -m "feat: TrendHandler for change-over-time conversation questions"
```

---

## Task 12: Wire dispatch + fallback into `ConversationService.reply`

**Files:**
- Modify: `src/journal/services/conversations/service.py`
- Test: `tests/test_services/test_conversations.py`

- [ ] **Step 1: Write the failing tests (append to test_conversations.py)**

```python
from journal.providers.intent_classifier import IntentResult


class _FakeClassifier:
    def __init__(self, result: IntentResult):
        self._result = result
        self.seen: list[str] = []

    def classify(self, question, context=None):
        self.seen.append(question)
        return self._result


class _RecordingHandler:
    def __init__(self, outcome):
        self._outcome = outcome
        self.called = False

    def handle(self, history, intent, user_id):
        self.called = True
        return self._outcome


def test_reply_routes_to_handler_for_classified_intent(tmp_path: Path) -> None:
    from journal.services.conversations.handlers import ReplyOutcome

    repo = _repo(tmp_path)
    classifier = _FakeClassifier(IntentResult(intent="aggregate", search_query="back"))
    handler = _RecordingHandler(
        ReplyOutcome(answer="42 times.", answered=True,
                     citations=[{"entry_id": 7, "entry_date": "2026-02-14",
                                 "snippet": "back"}])
    )
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer(),
                   classifier=classifier, handlers={"aggregate": handler})
    cid = _seed(svc)

    msg = svc.reply(USER_A, cid, "how many times did I mention my back?")

    assert handler.called
    assert msg.content == "42 times."
    assert msg.citations[0]["entry_id"] == 7


def test_reply_falls_back_to_lookup_when_handler_errors(tmp_path: Path) -> None:
    from journal.services.conversations.handlers import ReplyOutcome

    repo = _repo(tmp_path)
    classifier = _FakeClassifier(IntentResult(intent="aggregate", search_query="back"))

    class _Boom:
        def handle(self, history, intent, user_id):
            raise RuntimeError("aggregate path broke")

    lookup = _RecordingHandler(
        ReplyOutcome(answer="fallback answer", answered=True, citations=[])
    )
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer(),
                   classifier=classifier,
                   handlers={"aggregate": _Boom(), "lookup": lookup})
    cid = _seed(svc)

    msg = svc.reply(USER_A, cid, "how many times?")

    assert lookup.called  # degraded to lookup, did not raise
    assert msg.content == "fallback answer"
```

Update the existing `_service` helper in this file to accept the new dependencies (keep the old behavior when not supplied by building real defaults):

```python
def _service(repo, query, answerer, **kw) -> ConversationService:
    from journal.providers.intent_classifier import HeuristicIntentClassifier
    from journal.services.conversations.handlers import LookupHandler

    classifier = kw.pop("classifier", HeuristicIntentClassifier())
    handlers = kw.pop("handlers", None) or {
        "lookup": LookupHandler(query, answerer, passage_chars=800),
    }
    return ConversationService(
        repository=repo,
        query_service=query,
        answerer=answerer,
        classifier=classifier,
        handlers=handlers,
        model=kw.pop("model", "claude-sonnet-4-6"),
    )
```

> Note: `test_reply_combines_query_and_persists_both_turns` asserted the old
> `original + "\n" + follow-up` query string. With the heuristic classifier the
> lookup `search_query` is just the follow-up message. Update that assertion to
> `query.calls[0]["query"] == "and when did it get better?"` and
> `query.calls[0]["limit"] == 20` (the candidate pool), then keep the rest.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services/test_conversations.py -q`
Expected: FAIL — `ConversationService.__init__` does not yet accept `classifier`/`handlers`.

- [ ] **Step 3: Rewrite `ConversationService` to classify + dispatch**

Replace the body of `src/journal/services/conversations/service.py`'s `ConversationService` with classification + dispatch. Keep `start`, `list`, `get`, `delete` unchanged. New `__init__` and `reply`:

```python
class ConversationService:
    def __init__(
        self,
        *,
        repository: SQLiteConversationRepository,
        query_service: QueryService,
        answerer: Answerer,
        classifier: IntentClassifier,
        handlers: dict[str, object],
        model: str,
    ) -> None:
        self._repo = repository
        self._query = query_service
        self._answerer = answerer
        self._classifier = classifier
        self._handlers = handlers
        self._model = model

    def reply(
        self,
        user_id: int,
        conversation_id: int,
        message: str,
    ) -> ConversationMessage:
        conv = self._repo.get(conversation_id, user_id)
        if conv is None:
            raise ConversationNotFoundError(
                f"conversation {conversation_id} not found"
            )

        history = [
            ConversationTurn(role=m.role, content=m.content) for m in conv.messages
        ]
        history.append(ConversationTurn(role="user", content=message))
        context = "\n".join(f"{m.role}: {m.content}" for m in conv.messages)

        intent = self._classifier.classify(message, context=context)
        outcome = self._dispatch(intent, history, user_id)

        added = self._repo.add_messages(
            conversation_id,
            user_id,
            [
                {"role": "user", "content": message, "citations": []},
                {
                    "role": "assistant",
                    "content": outcome.answer,
                    "citations": outcome.citations,
                },
            ],
        )
        return added[-1]

    def _dispatch(self, intent, history, user_id):
        """Route to the intent's handler; fall back to lookup on any error.

        AnswerUnavailable propagates (the caller maps it to 502 and
        persists nothing); every other handler error degrades to the
        lookup path so a routing bug never breaks chat.
        """
        handler = self._handlers.get(intent.intent) or self._handlers["lookup"]
        try:
            return handler.handle(history, intent, user_id)
        except AnswerUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — deliberate degrade-to-lookup
            log.warning(
                "handler %r failed (%s); falling back to lookup",
                intent.intent, e,
            )
            lookup_intent = replace(intent, intent="lookup")
            return self._handlers["lookup"].handle(history, lookup_intent, user_id)
```

Add the imports at the top of `service.py`:

```python
import logging
from dataclasses import replace

from journal.providers.answerer import AnswerUnavailable, ConversationTurn

log = logging.getLogger(__name__)
```

Wrap the `ConversationNotFound` add-messages race handling as before (keep the existing `try/except ConversationNotFound` around `add_messages`).

> Note: the `AnswerUnavailable`-persists-nothing test still holds because the
> exception propagates out of `_dispatch` before `add_messages` runs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_services/test_conversations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/conversations/service.py tests/test_services/test_conversations.py
git commit -m "feat: classify + dispatch conversation replies with lookup fallback"
```

---

## Task 13: Bootstrap wiring

**Files:**
- Modify: `src/journal/mcp_server/bootstrap.py`
- Test: `tests/test_bootstrap_conversations.py`

- [ ] **Step 1: Write/extend the failing test**

Append to `tests/test_bootstrap_conversations.py` a check that the built service has a classifier and a `lookup` handler. (Match the file's existing bootstrap-invocation style; if it builds services via the real `build_services`, assert on the returned `conversation` service attributes.)

```python
def test_bootstrap_conversation_service_has_router(built_services) -> None:
    svc = built_services["conversation"]
    assert svc._classifier is not None
    assert "lookup" in svc._handlers
    assert "aggregate" in svc._handlers
```

> If `test_bootstrap_conversations.py` has no `built_services` fixture, reuse
> the construction helper already used by the other tests in that file rather
> than adding a new fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bootstrap_conversations.py -q`
Expected: FAIL (`ConversationService` constructed without `classifier`/`handlers`).

- [ ] **Step 3: Update bootstrap construction**

Add the import near the other provider imports (top of `bootstrap.py`, by line 37):

```python
from journal.providers.intent_classifier import build_intent_classifier
from journal.services.conversations.handlers import (
    AggregateHandler,
    LookupHandler,
    TemporalHandler,
    TrendHandler,
)
```

Replace the `ConversationService(...)` block at `bootstrap.py:775` with:

```python
    intent_classifier = build_intent_classifier(
        config.answer_provider,
        anthropic_api_key=config.anthropic_api_key,
        model=config.answer_classifier_model,
    )
    conversation_handlers = {
        "lookup": LookupHandler(query_service, answerer, passage_chars=800),
        "aggregate": AggregateHandler(query_service, answerer, passage_chars=800),
        "temporal": TemporalHandler(query_service, answerer, passage_chars=800),
        "trend": TrendHandler(query_service, answerer, passage_chars=800),
    }
    conversation_service = ConversationService(
        repository=conversation_repository,
        query_service=query_service,
        answerer=answerer,
        classifier=intent_classifier,
        handlers=conversation_handlers,
        model=config.answer_model,
    )
```

> `passage_chars=800` matches the existing answerer truncation budget
> (`_MAX_PASSAGE_CHARS` in `answerer.py`); it is a char budget, not the
> `answer_context_entries` count.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bootstrap_conversations.py tests/test_api_conversations.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole unit suite + lint**

Run: `uv run pytest -q -m "not integration" && uv run ruff check src/ tests/`
Expected: PASS, no lint errors. (Integration tests covered separately — bring up Chroma and run the full suite once before the final push.)

- [ ] **Step 6: Commit**

```bash
git add src/journal/mcp_server/bootstrap.py tests/test_bootstrap_conversations.py
git commit -m "feat: wire intent classifier + handlers into conversation service"
```

---

## Task 14: Docs + journal entry

**Files:**
- Modify: `docs/` — the conversations/architecture doc that describes the reply flow (find with `grep -rl "conversation" docs/`); if none exists, add a short `docs/conversations.md`.
- Create: `journal/260617-conversations-retrieval-robustness.md`
- Modify: the spec header `Status:` line.

- [ ] **Step 1: Update the architecture doc**

Document the new flow: classify → dispatch (`lookup`/`aggregate`/`temporal`/`trend`) → fallback to lookup on error; the adaptive passage count `[3,15]`, matched-chunk truncation, and the one-hop `search_again`. Keep it short.

- [ ] **Step 2: Write the journal entry**

Create `journal/260617-conversations-retrieval-robustness.md` capturing: the six weaknesses, why approach C, the new components, and the tunable knobs (candidate pool 20, band 0.5, floor/ceiling 3/15, one re-retrieval hop).

- [ ] **Step 3: Flip the spec status**

In `docs/superpowers/specs/2026-06-17-conversations-retrieval-robustness-design.md`, change the status line to:
`**Status:** implemented 2026-06-17.`

- [ ] **Step 4: Commit**

```bash
git add docs journal
git commit -m "docs: document conversation retrieval routing + journal entry"
```

---

## Self-Review Notes

- **Spec coverage:** #1 aggregate → Task 9; #2 temporal → Task 10; #3 adaptive count → Task 3; #4 truncation → Task 2; #5 carry-forward → Task 7; #6 multi-turn query → Task 5 (`search_query`) + Task 12 (context-aware classify). Strengths preserved: lookup path unchanged in shape (Task 8), grounding contract reused (all handlers call `continue_conversation`), fallback-to-lookup (Task 12), bounded cost (candidate pool + `[3,15]` clamp + one hop).
- **Trend handler** also covers the spec's `trend` intent (Task 11).
- **Fallback guard:** `_dispatch` re-raises `AnswerUnavailable` (preserves the "persist nothing on LLM failure" contract that `test_reply_unavailable_persists_nothing` checks) and degrades every other error to lookup.
- **Known follow-up for the implementer:** confirm `get_topic_frequency` counts *entries containing the topic* (matches the `context_note` wording in Task 9). If it counts raw mentions instead, adjust the note wording — do not change the method.
