# Search Answer Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `POST /api/search/answer` endpoint that synthesizes a grounded, cited answer from the existing hybrid-retrieval top-N, surfaced on demand above the search results list in the webapp.

**Architecture:** A new `Answerer` provider (Anthropic, Sonnet 4.6) behind a Protocol mirrors `providers/reranker.py`. A thin `AnswerService` reuses the existing `QueryService.search_entries` for retrieval, feeds dated passages to the answerer, and resolves cited entry ids back to entries. A new POST route exposes it. The webapp adds an "Answer this" button + answer panel; the results list endpoint is untouched.

**Tech Stack:** Python 3.13, `uv`, pytest, Anthropic SDK (`claude-sonnet-4-6`); Vue 3 + TypeScript, Pinia, Vitest.

**Spec:** `docs/superpowers/specs/2026-06-16-search-answer-synthesis-design.md`

**Grounding decision (from spec):** strict — answer only from supplied passages; if not covered, `answered=false` with a fixed message; never guess. On LLM error/malformed output the answerer **raises** (no silent degrade) and the route returns `502`.

**Implementation note on structured output:** rather than depend on a specific SDK `output_config` feature, `AnthropicAnswerer` follows the **proven house pattern in `providers/reranker.py`** — instruct strict JSON in the prompt and parse leniently (first `{` … last `}`). This keeps it consistent with the existing codebase and SDK-version-independent.

---

## File Structure

**Server (`server/`):**
- Create: `src/journal/providers/answerer.py` — `Answerer` Protocol, `AnswerPassage`/`AnswerResult` dataclasses, `AnswerUnavailable`, `AnthropicAnswerer`, `NoopAnswerer`, `build_answerer`.
- Create: `src/journal/services/answer.py` — `AnswerCitation`/`AnswerResponse` dataclasses, `AnswerService`.
- Modify: `src/journal/config.py` — `answer_provider`, `answer_model`, `answer_context_entries`.
- Modify: `src/journal/service_registry.py` — add `answer` key.
- Modify: `src/journal/mcp_server/bootstrap.py` — build answerer + `AnswerService`, register in `_services`.
- Modify: `src/journal/api/search.py` — `POST /api/search/answer`.
- Modify: `src/journal/api/settings.py` — surface answer config (optional, see Task 6).
- Modify: `docs/search.md` — Answer synthesis section.
- Test: `tests/test_providers/test_answerer.py`, `tests/test_services/test_answer.py`, `tests/test_api.py` (`TestSearchAnswer`).

**Webapp (`webapp/`):**
- Modify: `src/types/search.ts` — `AnswerRequestParams`, `AnswerCitation`, `AnswerResponse`.
- Modify: `src/api/search.ts` — `answerQuestion()`.
- Modify: `src/stores/search.ts` — answer state + `runAnswer()` + clear-on-search.
- Modify: `src/views/SearchView.vue` — "Answer this" button + answer panel.
- Modify: `docs/development.md` (or nearest active doc) — brief note.
- Test: `src/api/__tests__/search.test.ts`, `src/stores/__tests__/search.test.ts`, `src/views/__tests__/SearchView.test.ts`.

Work the server tasks (1–6) first — the webapp tasks (7–11) consume the endpoint. All server commands run from `server/`; webapp commands from `webapp/`.

---

## Task 1: `Answerer` provider

**Files:**
- Create: `src/journal/providers/answerer.py`
- Test: `tests/test_providers/test_answerer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_providers/test_answerer.py`:

```python
"""Tests for the answer-synthesis provider."""

import anthropic
import pytest

from journal.providers.answerer import (
    NO_MATCH_MESSAGE,
    AnswerPassage,
    AnswerUnavailable,
    AnthropicAnswerer,
    NoopAnswerer,
    build_answerer,
)


class _FakeMessages:
    def __init__(self, raw: str | None = None, exc: Exception | None = None):
        self._raw = raw
        self._exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc

        class _Block:
            text = self._raw

        class _Resp:
            content = [_Block()]

        return _Resp()


def _answerer(raw: str | None = None, exc: Exception | None = None) -> AnthropicAnswerer:
    a = AnthropicAnswerer(api_key="test", model="claude-sonnet-4-6")
    a._client.messages = _FakeMessages(raw=raw, exc=exc)  # type: ignore[assignment]
    return a


PASSAGES = [
    AnswerPassage(entry_id=42, entry_date="2026-02-14", text="My lower back started hurting."),
    AnswerPassage(entry_id=7, entry_date="2026-03-01", text="Back still sore after the gym."),
]


def test_parses_answer_and_filters_invented_ids():
    raw = '{"answer": "Your back pain began on 2026-02-14.", "answered": true, "cited_entry_ids": [42, 999]}'
    result = _answerer(raw=raw).answer("when did my back start hurting?", PASSAGES)
    assert result.answered is True
    assert "2026-02-14" in result.answer
    # 999 was never a candidate — it must be dropped.
    assert result.cited_entry_ids == [42]


def test_answered_false_passthrough():
    raw = f'{{"answer": "{NO_MATCH_MESSAGE}", "answered": false, "cited_entry_ids": []}}'
    result = _answerer(raw=raw).answer("did I go to Mars?", PASSAGES)
    assert result.answered is False
    assert result.cited_entry_ids == []


def test_malformed_output_raises():
    with pytest.raises(AnswerUnavailable):
        _answerer(raw="not json at all").answer("q", PASSAGES)


def test_api_error_raises():
    exc = anthropic.APIError("boom", request=None, body=None)
    with pytest.raises(AnswerUnavailable):
        _answerer(exc=exc).answer("q", PASSAGES)


def test_noop_answerer_is_not_answered():
    result = NoopAnswerer().answer("q", PASSAGES)
    assert result.answered is False
    assert result.cited_entry_ids == []


def test_build_answerer_selects_adapter():
    assert isinstance(build_answerer("none"), NoopAnswerer)
    assert isinstance(
        build_answerer("anthropic", anthropic_api_key="k", model="claude-sonnet-4-6"),
        AnthropicAnswerer,
    )
    with pytest.raises(ValueError):
        build_answerer("anthropic", anthropic_api_key="")
    with pytest.raises(ValueError):
        build_answerer("bogus")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_providers/test_answerer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'journal.providers.answerer'`.

- [ ] **Step 3: Write the implementation**

Create `src/journal/providers/answerer.py`:

```python
"""Answer-synthesis Protocol and adapters.

The answerer turns a user's natural-language question plus a set of
retrieved journal passages into a short, grounded, cited answer. It is
the synthesis stage of the opt-in `POST /api/search/answer` endpoint —
distinct from search, which only ranks entries.

Adapters mirror `providers/reranker.py`:
- `NoopAnswerer` — returns `answered=False` with a "disabled" message.
  Used when `ANSWER_PROVIDER=none` and in unit tests that don't mock an
  LLM.
- `AnthropicAnswerer` — single-shot synthesis via Claude (Sonnet 4.6 by
  default). Strict grounding: answer only from the supplied passages; if
  they don't cover the question, return `answered=False` with the fixed
  no-match message. Output is strict JSON parsed leniently (the proven
  pattern from `reranker.py`); on API error or unparseable output it
  raises `AnswerUnavailable` rather than degrading to a guessed answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import anthropic

logger = logging.getLogger(__name__)

#: Fixed message returned when the journal doesn't cover the question.
NO_MATCH_MESSAGE = "I couldn't find anything about that in your journal."

#: Per-passage truncation — keeps the prompt bounded at N passages
#: (≈200 tokens each), matching the reranker's candidate cap.
_MAX_PASSAGE_CHARS = 800


@dataclass(frozen=True)
class AnswerPassage:
    """One retrieved entry offered to the answerer as grounding."""

    entry_id: int
    entry_date: str
    text: str


@dataclass(frozen=True)
class AnswerResult:
    """The synthesized answer plus the entry ids it cited."""

    answer: str
    answered: bool
    cited_entry_ids: list[int] = field(default_factory=list)


class AnswerUnavailable(Exception):
    """Raised when a grounded answer could not be produced.

    Covers API errors and malformed/unparseable model output. The route
    maps this to a 502 so the webapp degrades to "answer unavailable —
    see results below" without ever showing a fabricated answer.
    """


@runtime_checkable
class Answerer(Protocol):
    """Protocol for question answerers."""

    def answer(
        self, question: str, passages: list[AnswerPassage]
    ) -> AnswerResult: ...


class NoopAnswerer:
    """Identity answerer — always reports it could not answer.

    Used when answer synthesis is disabled (`ANSWER_PROVIDER=none`) and
    as the default in unit tests that exercise the service without an LLM.
    """

    def answer(
        self, question: str, passages: list[AnswerPassage]
    ) -> AnswerResult:
        return AnswerResult(
            answer="Answer synthesis is disabled.",
            answered=False,
            cited_entry_ids=[],
        )


_SYSTEM_PROMPT = (
    "You answer questions about a person's private journal. You are given "
    "the user's question and a numbered list of dated passages retrieved "
    "from their journal. Answer ONLY from these passages.\n\n"
    "Output a single JSON object with exactly this shape:\n"
    "  {\n"
    '    "answer": "<your answer, addressed to the journal author as \'you\'>",\n'
    '    "answered": <true|false>,\n'
    '    "cited_entry_ids": [<entry_id>, ...]\n'
    "  }\n\n"
    "Rules:\n"
    "- Ground every claim in the passages. Quote dates from the passages "
    "when relevant.\n"
    "- For 'when did X start' questions, identify the EARLIEST passage that "
    "evidences X and lead with its date.\n"
    "- `cited_entry_ids` lists the entry ids of the passages you actually "
    "used, most relevant first. Never invent an id.\n"
    "- If the passages do not contain enough to answer, set "
    f'"answered": false and "answer": "{NO_MATCH_MESSAGE}" and leave '
    "`cited_entry_ids` empty. Do NOT guess or use outside knowledge.\n"
    "- Output the JSON object only. No prose, no markdown."
)


class AnthropicAnswerer:
    """Answer synthesis via an Anthropic Claude model (Sonnet 4.6)."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def answer(
        self, question: str, passages: list[AnswerPassage]
    ) -> AnswerResult:
        if not passages:
            return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

        user_message = self._format_user_message(question, passages)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.APIError as e:
            logger.warning("AnthropicAnswerer call failed: %s", e)
            raise AnswerUnavailable(str(e)) from e

        raw = self._first_text(response)
        parsed = self._parse_response(raw)
        if parsed is None:
            logger.warning(
                "AnthropicAnswerer returned malformed output. "
                "Raw (first 200 chars): %r",
                (raw or "")[:200],
            )
            raise AnswerUnavailable("malformed answerer output")

        valid_ids = {p.entry_id for p in passages}
        cited = [
            int(eid)
            for eid in parsed["cited_entry_ids"]
            if isinstance(eid, int) and eid in valid_ids
        ]
        return AnswerResult(
            answer=str(parsed["answer"]),
            answered=bool(parsed["answered"]),
            cited_entry_ids=cited,
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
    def _format_user_message(
        question: str, passages: list[AnswerPassage]
    ) -> str:
        lines = [f"Question: {question}", "", "Passages:"]
        for p in passages:
            text = p.text
            if len(text) > _MAX_PASSAGE_CHARS:
                text = text[: _MAX_PASSAGE_CHARS - 1] + "…"
            lines.append(f"[entry_id={p.entry_id} date={p.entry_date}] {text}")
        lines.append("")
        lines.append("Output the JSON object now.")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(raw: str) -> dict | None:
        """Parse the model output; return the validated dict or None.

        Forgiving like the reranker: find the first `{` and last `}` and
        parse between them. Returns None if the shape is wrong so the
        caller raises `AnswerUnavailable`.
        """
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
        if not isinstance(parsed.get("answer"), str):
            return None
        if not isinstance(parsed.get("answered"), bool):
            return None
        if not isinstance(parsed.get("cited_entry_ids"), list):
            return None
        return parsed


def build_answerer(
    name: str,
    *,
    anthropic_api_key: str = "",
    model: str = "claude-sonnet-4-6",
) -> Answerer:
    """Build an answerer by name. Unknown names raise (fail-fast)."""
    if name in ("none", "noop"):
        return NoopAnswerer()
    if name == "anthropic":
        if not anthropic_api_key:
            raise ValueError(
                "AnthropicAnswerer requires ANTHROPIC_API_KEY to be set"
            )
        return AnthropicAnswerer(api_key=anthropic_api_key, model=model)
    raise ValueError(
        f"Unknown answerer {name!r} — must be 'anthropic' or 'none'"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_providers/test_answerer.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/journal/providers/answerer.py tests/test_providers/test_answerer.py
git add src/journal/providers/answerer.py tests/test_providers/test_answerer.py
git commit -m "feat(answerer): add answer-synthesis provider (Anthropic + Noop)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Config fields

**Files:**
- Modify: `src/journal/config.py`
- Test: `tests/test_config.py` (append; if absent, create)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (create the file with this content if it does not exist):

```python
from journal.config import Config


def test_answer_config_defaults(monkeypatch):
    for var in ("ANSWER_PROVIDER", "ANSWER_MODEL", "ANSWER_CONTEXT_ENTRIES"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    assert cfg.answer_provider == "anthropic"
    assert cfg.answer_model == "claude-sonnet-4-6"
    assert cfg.answer_context_entries == 8


def test_answer_config_from_env(monkeypatch):
    monkeypatch.setenv("ANSWER_PROVIDER", "none")
    monkeypatch.setenv("ANSWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("ANSWER_CONTEXT_ENTRIES", "5")
    cfg = Config()
    assert cfg.answer_provider == "none"
    assert cfg.answer_model == "claude-haiku-4-5"
    assert cfg.answer_context_entries == 5
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -q -k answer`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'answer_provider'`.

- [ ] **Step 3: Add the config fields**

In `src/journal/config.py`, immediately after the `reranker_model` field (around line 366), add:

```python
    # ── Answer synthesis (POST /api/search/answer) ──────────────────
    # `anthropic` enables grounded answer synthesis over the hybrid
    # search top-N; `none` disables it (the endpoint returns an
    # answered=false "disabled" payload). `answer_context_entries` is
    # how many retrieved entries are fed to the answerer as grounding.
    answer_provider: str = field(
        default_factory=lambda: os.environ.get("ANSWER_PROVIDER", "anthropic")
    )
    answer_model: str = field(
        default_factory=lambda: os.environ.get("ANSWER_MODEL", "claude-sonnet-4-6")
    )
    answer_context_entries: int = field(
        default_factory=lambda: int(os.environ.get("ANSWER_CONTEXT_ENTRIES", "8"))
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py -q -k answer`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/journal/config.py tests/test_config.py
git commit -m "feat(config): add answer-synthesis settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `AnswerService`

**Files:**
- Create: `src/journal/services/answer.py`
- Test: `tests/test_services/test_answer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_services/test_answer.py`:

```python
"""Tests for the answer-synthesis service."""

from journal.models import SearchResult
from journal.providers.answerer import (
    NO_MATCH_MESSAGE,
    AnswerPassage,
    AnswerResult,
)
from journal.services.answer import AnswerService


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results
        self.calls: list[dict] = []

    def search_entries(self, **kwargs):
        self.calls.append(kwargs)
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult):
        self._result = result
        self.passages: list[AnswerPassage] | None = None

    def answer(self, question, passages):
        self.passages = passages
        return self._result


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


def test_no_results_short_circuits_without_calling_answerer():
    answerer = _FakeAnswerer(AnswerResult("should not be used", True, [1]))
    svc = AnswerService(_FakeQuery([]), answerer, model="claude-sonnet-4-6")
    resp = svc.answer_question("anything?")
    assert resp.answered is False
    assert resp.answer == NO_MATCH_MESSAGE
    assert resp.citations == []
    assert answerer.passages is None  # answerer never called


def test_builds_passages_and_resolves_citations():
    results = [
        _result(42, "2026-02-14", "My lower back started hurting today."),
        _result(7, "2026-03-01", "Back still sore."),
    ]
    answerer = _FakeAnswerer(
        AnswerResult("Your back pain began on 2026-02-14.", True, [42])
    )
    svc = AnswerService(
        _FakeQuery(results), answerer, model="claude-sonnet-4-6",
        context_entries=8,
    )
    resp = svc.answer_question("when did my back start hurting?")

    # retrieval used the question as the query at the configured limit
    assert svc._query.calls[0]["query"] == "when did my back start hurting?"
    assert svc._query.calls[0]["limit"] == 8
    # passages carry id + date + text
    assert [p.entry_id for p in answerer.passages] == [42, 7]
    # one citation, resolved to the matching entry's date + snippet
    assert resp.answered is True
    assert len(resp.citations) == 1
    assert resp.citations[0].entry_id == 42
    assert resp.citations[0].entry_date == "2026-02-14"
    assert "back" in resp.citations[0].snippet.lower()
    assert resp.model == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_services/test_answer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'journal.services.answer'`.

- [ ] **Step 3: Write the implementation**

Create `src/journal/services/answer.py`:

```python
"""Answer-synthesis service.

Orchestrates the opt-in `POST /api/search/answer` flow: reuse the hybrid
search to retrieve the top-N entries for the question, hand them to the
`Answerer` as dated passages, and resolve the cited entry ids back to
entries so the webapp can render clickable citations.

Retrieval reuses `QueryService.search_entries`, so the answer rides the
same BM25+dense+RRF+rerank pipeline (and result cache) as the list
endpoint — no separate retrieval path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from journal.providers.answerer import NO_MATCH_MESSAGE, AnswerPassage

if TYPE_CHECKING:
    from journal.providers.answerer import Answerer
    from journal.services.query import QueryService

#: Length of the citation preview snippet (chars of the entry text).
_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class AnswerCitation:
    entry_id: int
    entry_date: str
    snippet: str


@dataclass(frozen=True)
class AnswerResponse:
    question: str
    answer: str
    answered: bool
    citations: list[AnswerCitation]
    model: str


class AnswerService:
    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        model: str,
        context_entries: int = 8,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._model = model
        self._context_entries = context_entries
        self._passage_chars = passage_chars

    def answer_question(
        self,
        question: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> AnswerResponse:
        results = self._query.search_entries(
            query=question,
            start_date=start_date,
            end_date=end_date,
            limit=self._context_entries,
            offset=0,
            user_id=user_id,
        )
        if not results:
            return AnswerResponse(
                question=question,
                answer=NO_MATCH_MESSAGE,
                answered=False,
                citations=[],
                model=self._model,
            )

        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=r.text[: self._passage_chars],
            )
            for r in results
        ]
        result = self._answerer.answer(question, passages)

        by_id = {r.entry_id: r for r in results}
        citations = [
            AnswerCitation(
                entry_id=eid,
                entry_date=by_id[eid].entry_date,
                snippet=by_id[eid].text[:_SNIPPET_CHARS].strip(),
            )
            for eid in result.cited_entry_ids
            if eid in by_id
        ]
        return AnswerResponse(
            question=question,
            answer=result.answer,
            answered=result.answered,
            citations=citations,
            model=self._model,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_services/test_answer.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/journal/services/answer.py tests/test_services/test_answer.py
git add src/journal/services/answer.py tests/test_services/test_answer.py
git commit -m "feat(answer): add answer-synthesis service over hybrid retrieval

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire into the service registry and bootstrap

**Files:**
- Modify: `src/journal/service_registry.py`
- Modify: `src/journal/mcp_server/bootstrap.py`

- [ ] **Step 1: Add the `answer` key to `ServicesDict`**

In `src/journal/service_registry.py`, add to the `TYPE_CHECKING` imports:

```python
    from journal.services.answer import AnswerService
```

and add this entry to the `ServicesDict` body, right after the `query: QueryService` line:

```python
    answer: AnswerService
```

- [ ] **Step 2: Build the answerer in bootstrap**

In `src/journal/mcp_server/bootstrap.py`, add to the top-of-file imports next to `from journal.providers.reranker import build_reranker`:

```python
from journal.providers.answerer import build_answerer
from journal.services.answer import AnswerService
```

Immediately after the `reranker = build_reranker(...)` block (the call ending around line 293), add:

```python
    answerer = build_answerer(
        config.answer_provider,
        anthropic_api_key=config.anthropic_api_key,
        model=config.answer_model,
    )
    log.info(
        "  Answerer: provider=%s (%s)",
        config.answer_provider,
        config.answer_model if config.answer_provider != "none" else "n/a",
    )
```

- [ ] **Step 3: Construct `QueryService` as a named variable and register `AnswerService`**

In `src/journal/mcp_server/bootstrap.py`, the `_services` dict currently constructs `QueryService(...)` inline under the `"query"` key. Lift it to a named variable just **before** the `_services = {` line:

```python
    query_service = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
        stats=stats_collector,
        reranker=reranker,
        hybrid_config=HybridConfig(
            bm25_candidates=config.hybrid_bm25_candidates,
            dense_candidates=config.hybrid_dense_candidates,
            fusion_top_m=config.hybrid_fusion_top_m,
            rrf_k=config.hybrid_rrf_k,
        ),
    )
    answer_service = AnswerService(
        query_service,
        answerer,
        model=config.answer_model,
        context_entries=config.answer_context_entries,
    )
```

Then change the `"query"` entry in the `_services` dict to reference the variable, and add the `"answer"` entry right after it:

```python
        "query": query_service,
        "answer": answer_service,
```

(Delete the now-duplicated inline `QueryService(...)` literal that was under `"query"`.)

- [ ] **Step 4: Verify the server still boots and the unit suite passes**

Run: `uv run pytest -m "not integration" -q`
Expected: PASS (existing count + the new provider/service/config tests; no errors).

- [ ] **Step 5: Commit**

```bash
git add src/journal/service_registry.py src/journal/mcp_server/bootstrap.py
git commit -m "feat(bootstrap): wire AnswerService into the services container

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `POST /api/search/answer` route

**Files:**
- Modify: `src/journal/api/search.py`
- Test: `tests/test_api.py` (add `TestSearchAnswer`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_api.py`, add a new test class (place it after `class TestSearch`). It wires a fake `AnswerService` into the test `services` dict — match the project's existing fixture style for `client`/`services`/`repo` used by `TestSearch`:

```python
class TestSearchAnswer:
    def test_answer_returns_synthesized_payload(
        self, client: TestClient, services
    ) -> None:
        from journal.services.answer import AnswerCitation, AnswerResponse

        class _FakeAnswer:
            def answer_question(self, question, start_date=None, end_date=None, user_id=None):
                return AnswerResponse(
                    question=question,
                    answer="Your back pain began on 2026-02-14.",
                    answered=True,
                    citations=[AnswerCitation(42, "2026-02-14", "lower back hurting")],
                    model="claude-sonnet-4-6",
                )

        services["answer"] = _FakeAnswer()
        resp = client.post("/api/search/answer", json={"q": "when did my back start hurting?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answered"] is True
        assert body["citations"][0]["entry_id"] == 42
        assert body["model"] == "claude-sonnet-4-6"

    def test_answer_missing_query_returns_400(
        self, client: TestClient, services
    ) -> None:
        services["answer"] = object()  # never reached — validation precedes lookup use
        resp = client.post("/api/search/answer", json={"q": "   "})
        assert resp.status_code == 400
        assert resp.json()["error"] == "missing_query"

    def test_answer_unavailable_returns_502(
        self, client: TestClient, services
    ) -> None:
        from journal.providers.answerer import AnswerUnavailable

        class _Boom:
            def answer_question(self, *a, **k):
                raise AnswerUnavailable("boom")

        services["answer"] = _Boom()
        resp = client.post("/api/search/answer", json={"q": "anything?"})
        assert resp.status_code == 502
        assert resp.json()["error"] == "answer_unavailable"
```

> If `TestSearch` uses a different mechanism to inject services (e.g. a module-level `services` dict or a fixture name other than `services`), mirror exactly what `TestSearch` does — read the top of `tests/test_api.py` and the `TestSearch` setup before writing these, and adapt the injection line accordingly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py::TestSearchAnswer -q`
Expected: FAIL — 404 (route not registered) on the first test.

- [ ] **Step 3: Add the route**

In `src/journal/api/search.py`, add to the imports:

```python
from journal.providers.answerer import AnswerUnavailable
```

Inside `register_search_routes`, after the existing `search` function, add:

```python
    @mcp.custom_route(
        "/api/search/answer", methods=["POST"], name="api_search_answer"
    )
    @handler(services_getter, parse_json=True)
    def search_answer(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        """Synthesize a grounded, cited answer to a question.

        Body: ``{q: str, start_date?: ISO, end_date?: ISO}``. Reuses the
        hybrid search top-N as grounding, then asks the configured
        answerer for a strictly-grounded answer. Returns
        ``{question, answer, answered, citations[], model}``. ``answered``
        is false (with a fixed message) when the journal doesn't cover the
        question. 400 ``missing_query`` if ``q`` is empty; 502
        ``answer_unavailable`` if synthesis fails; 503 if not wired.
        """
        answer_svc = services.get("answer")
        if answer_svc is None:
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Answer synthesis is not configured.",
                },
                status_code=503,
            )

        user = get_authenticated_user(request)
        user_id = user.user_id

        q = (body.get("q") or "").strip()
        if not q:
            return JSONResponse(
                {
                    "error": "missing_query",
                    "message": "'q' field is required",
                },
                status_code=400,
            )

        start_date = body.get("start_date")
        end_date = body.get("end_date")

        try:
            result = answer_svc.answer_question(
                q, start_date=start_date, end_date=end_date, user_id=user_id
            )
        except AnswerUnavailable as e:
            log.info("POST /api/search/answer — answer unavailable for %r: %s", q, e)
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Could not generate an answer right now.",
                },
                status_code=502,
            )

        return JSONResponse(
            {
                "question": result.question,
                "answer": result.answer,
                "answered": result.answered,
                "citations": [
                    {
                        "entry_id": c.entry_id,
                        "entry_date": c.entry_date,
                        "snippet": c.snippet,
                    }
                    for c in result.citations
                ],
                "model": result.model,
            }
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py::TestSearchAnswer -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full unit suite and commit**

Run: `uv run pytest -m "not integration" -q`
Expected: PASS.

```bash
uv run ruff check src/journal/api/search.py tests/test_api.py
git add src/journal/api/search.py tests/test_api.py
git commit -m "feat(api): add POST /api/search/answer endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Server docs (+ optional settings exposure)

**Files:**
- Modify: `docs/search.md`
- Modify (optional): `src/journal/api/settings.py`

- [ ] **Step 1: Document the endpoint**

In `docs/search.md`, add a new top-level section after "## Errors":

```markdown
## Answer synthesis (opt-in)

`POST /api/search/answer` synthesizes a short, grounded, cited answer to a
natural-language question. It is opt-in — the webapp only calls it when the
user clicks "Answer this", so the per-query LLM cost is never paid on a plain
search.

**Body:** `{q: str, start_date?: ISO, end_date?: ISO}` (same bearer auth as
`/api/search`).

**Flow:** reuse the hybrid search top-`ANSWER_CONTEXT_ENTRIES` (default 8) as
grounding → ask the answerer (`claude-sonnet-4-6`, adaptive thinking) for a
strictly-grounded JSON answer → resolve cited ids back to entries.

**Grounding contract:** the answerer may only use the supplied passages. If
they don't cover the question it returns `answered: false` with the fixed
message *"I couldn't find anything about that in your journal."* — it never
guesses.

**Response:**

​```json
{
  "question": "when did my back start hurting?",
  "answer": "Your back pain first appears on 2026-02-14 …",
  "answered": true,
  "citations": [{"entry_id": 42, "entry_date": "2026-02-14", "snippet": "…"}],
  "model": "claude-sonnet-4-6"
}
​```

**Config:** `ANSWER_PROVIDER` (`anthropic`|`none`, default `anthropic`),
`ANSWER_MODEL` (default `claude-sonnet-4-6`), `ANSWER_CONTEXT_ENTRIES`
(default 8).

**Errors:** `400 missing_query`; `502 answer_unavailable` (synthesis failed —
the client should fall back to the results list); `503` if synthesis is not
wired.
```

(Replace the zero-width-space-prefixed code fences `​```json` / `​```` with plain ```` ``` ```` — they're escaped here only to nest inside this plan.)

- [ ] **Step 2 (optional): surface config under `/api/settings`**

If `src/journal/api/settings.py` has a `search` block reporting reranker/hybrid settings, add an `answer` sub-block reporting `provider`, `model`, `context_entries`. Mirror the existing block's shape exactly; if the settings route doesn't already expose a search block, skip this step.

- [ ] **Step 3: Commit**

```bash
git add docs/search.md
git commit -m "docs(search): document POST /api/search/answer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Webapp types

**Files:**
- Modify: `webapp/src/types/search.ts`

- [ ] **Step 1: Add the types**

Append to `webapp/src/types/search.ts`:

```typescript
/** Request body for the opt-in answer-synthesis endpoint. */
export interface AnswerRequestParams extends DateFilterParams {
  q: string
}

/** One cited entry backing a synthesized answer. */
export interface AnswerCitation {
  entry_id: number
  entry_date: string
  snippet: string
}

/**
 * Response from `POST /api/search/answer`. `answered` is false (with a
 * fixed "couldn't find" message in `answer`) when the journal doesn't
 * cover the question.
 */
export interface AnswerResponse {
  question: string
  answer: string
  answered: boolean
  citations: AnswerCitation[]
  model: string
}
```

- [ ] **Step 2: Type-check**

Run (from `webapp/`): `npm run build`
Expected: builds with no type errors.

- [ ] **Step 3: Commit**

```bash
git add src/types/search.ts
git commit -m "feat(search): add answer-synthesis response types

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Webapp API client method

**Files:**
- Modify: `webapp/src/api/search.ts`
- Test: `webapp/src/api/__tests__/search.test.ts`

- [ ] **Step 1: Write the failing test**

Add to `webapp/src/api/__tests__/search.test.ts` (mirror how the existing tests mock `apiFetch`/`fetch` in that file; the assertion below assumes a `fetch` mock — adapt to the file's existing mocking style):

```typescript
import { answerQuestion } from '@/api/search'

it('answerQuestion POSTs the question and date filters as JSON', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({
        question: 'q?',
        answer: 'a',
        answered: true,
        citations: [],
        model: 'claude-sonnet-4-6',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ),
  )

  const res = await answerQuestion({ q: 'q?', start_date: '2026-01-01' })

  expect(res.answered).toBe(true)
  const [url, init] = fetchMock.mock.calls[0]
  expect(url).toContain('/api/search/answer')
  expect(init?.method).toBe('POST')
  expect(JSON.parse(init?.body as string)).toEqual({
    q: 'q?',
    start_date: '2026-01-01',
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/api/__tests__/search.test.ts -t answerQuestion`
Expected: FAIL — `answerQuestion is not a function` / import error.

- [ ] **Step 3: Implement**

Add to `webapp/src/api/search.ts` (keep the existing `searchEntries`; add the import and the function):

```typescript
import type { AnswerRequestParams, AnswerResponse } from '@/types/search'

export function answerQuestion(
  params: AnswerRequestParams,
): Promise<AnswerResponse> {
  const body: Record<string, string> = { q: params.q }
  if (params.start_date) body.start_date = params.start_date
  if (params.end_date) body.end_date = params.end_date
  return apiFetch<AnswerResponse>('/api/search/answer', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
```

(If `apiFetch` and the `SearchResponse`/`SearchRequestParams` types are already imported at the top of `search.ts`, merge the new type import into the existing `import type { … } from '@/types/search'` line rather than adding a duplicate.)

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run src/api/__tests__/search.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/search.ts src/api/__tests__/search.test.ts
git commit -m "feat(search): add answerQuestion API client

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Webapp store — answer state + `runAnswer()`

**Files:**
- Modify: `webapp/src/stores/search.ts`
- Test: `webapp/src/stores/__tests__/search.test.ts`

- [ ] **Step 1: Write the failing tests**

Add to `webapp/src/stores/__tests__/search.test.ts` (mirror the file's existing pattern of mocking `@/api/search` and using `setActivePinia`):

```typescript
import { answerQuestion } from '@/api/search'
const mockAnswer = vi.mocked(answerQuestion)

// NOTE: ensure the vi.mock('@/api/search', …) factory at the top of this
// file also exports `answerQuestion: vi.fn()` alongside `searchEntries`.

it('runAnswer populates answer state on success', async () => {
  const store = useSearchStore()
  store.query = 'when did my back start hurting?'
  mockAnswer.mockResolvedValue({
    question: 'when did my back start hurting?',
    answer: 'Your back pain began on 2026-02-14.',
    answered: true,
    citations: [{ entry_id: 42, entry_date: '2026-02-14', snippet: 'lower back' }],
    model: 'claude-sonnet-4-6',
  })

  await store.runAnswer()

  expect(store.answer).toContain('2026-02-14')
  expect(store.answered).toBe(true)
  expect(store.answerCitations).toHaveLength(1)
  expect(store.answerError).toBeNull()
})

it('runAnswer surfaces a friendly error on failure', async () => {
  const { ApiRequestError } = await import('@/api/client')
  const store = useSearchStore()
  store.query = 'x'
  mockAnswer.mockRejectedValueOnce(new ApiRequestError(502, 'answer_unavailable', 'nope'))

  await store.runAnswer()

  expect(store.answerError).toBeTruthy()
  expect(store.answer).toBe('')
})

it('runAnswer does nothing for an empty query', async () => {
  const store = useSearchStore()
  store.query = '   '
  await store.runAnswer()
  expect(mockAnswer).not.toHaveBeenCalled()
})

it('running a new search clears any prior answer', async () => {
  const store = useSearchStore()
  store.answer = 'stale answer'
  store.answered = true
  store.answerCitations = [{ entry_id: 1, entry_date: '2026-01-01', snippet: 's' }]

  // searchEntries is already mocked in this file for runSearch tests
  await store.runSearch({ q: 'fresh' })

  expect(store.answer).toBe('')
  expect(store.answered).toBe(false)
  expect(store.answerCitations).toEqual([])
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/stores/__tests__/search.test.ts -t runAnswer`
Expected: FAIL — `store.runAnswer is not a function`.

- [ ] **Step 3: Implement the store changes**

In `webapp/src/stores/search.ts`:

(a) Extend the API import and add the answer types:

```typescript
import { searchEntries, answerQuestion } from '@/api/search'
import type {
  AnswerCitation,
  SearchRequestParams,
  SearchResultItem,
  SearchSort,
} from '@/types/search'
```

(b) Add answer state next to the result state refs:

```typescript
  // Answer-synthesis state (opt-in: populated only by runAnswer()).
  const answer = ref('')
  const answered = ref(false)
  const answerCitations = ref<AnswerCitation[]>([])
  const answerLoading = ref(false)
  const answerError = ref<string | null>(null)
```

(c) Add a private helper and call it whenever results change. Define it above `runSearch`:

```typescript
  function clearAnswer(): void {
    answer.value = ''
    answered.value = false
    answerCitations.value = []
    answerError.value = null
  }
```

Inside `runSearch`, add `clearAnswer()` as the first line of the function body (before the `partial` field assignments) so a stale answer never sits above fresh results.

(d) Add the `runAnswer` action after `runSearch`:

```typescript
  /**
   * Synthesize an answer to the current query. Opt-in — only called
   * when the user clicks "Answer this", since it spends an LLM call.
   */
  async function runAnswer(): Promise<void> {
    const trimmed = query.value.trim()
    if (!trimmed) return

    answerLoading.value = true
    answerError.value = null
    try {
      const params = { q: trimmed } as {
        q: string
        start_date?: string
        end_date?: string
      }
      if (startDate.value) params.start_date = startDate.value
      if (endDate.value) params.end_date = endDate.value

      const res = await answerQuestion(params)
      answer.value = res.answer
      answered.value = res.answered
      answerCitations.value = res.citations
    } catch (e) {
      if (e instanceof ApiRequestError) {
        answerError.value =
          'Answer unavailable — see the results below.'
      } else if (e instanceof Error) {
        answerError.value = e.message
      } else {
        answerError.value = 'Answer failed'
      }
      answer.value = ''
      answered.value = false
      answerCitations.value = []
    } finally {
      answerLoading.value = false
    }
  }
```

(e) In `reset()`, add `clearAnswer()` plus `answerLoading.value = false`.

(f) Add the new state and action to the store's returned object:

```typescript
    answer,
    answered,
    answerCitations,
    answerLoading,
    answerError,
    runAnswer,
```

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/stores/__tests__/search.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/stores/search.ts src/stores/__tests__/search.test.ts
git commit -m "feat(search): add runAnswer store action + answer state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Webapp view — "Answer this" button + answer panel

**Files:**
- Modify: `webapp/src/views/SearchView.vue`
- Test: `webapp/src/views/__tests__/SearchView.test.ts`

- [ ] **Step 1: Write the failing tests**

Add to `webapp/src/views/__tests__/SearchView.test.ts`:

```typescript
it('clicking Answer this calls runAnswer and renders the answer + citations', async () => {
  const { useSearchStore } = await import('@/stores/search')

  const wrapper = mountView()
  const store = useSearchStore()
  const spy = vi.spyOn(store, 'runAnswer').mockImplementation(async () => {
    store.answer = 'Your back pain began on 2026-02-14.'
    store.answered = true
    store.answerCitations = [
      { entry_id: 42, entry_date: '2026-02-14', snippet: 'lower back' },
    ]
  })

  await wrapper.find('[data-testid="search-query-input"]').setValue('when?')
  await wrapper.find('[data-testid="search-answer"]').trigger('click')
  await flushPromises()

  expect(spy).toHaveBeenCalled()
  const panel = wrapper.find('[data-testid="answer-panel"]')
  expect(panel.exists()).toBe(true)
  expect(panel.text()).toContain('2026-02-14')
  const cite = wrapper.find('[data-testid="answer-citation"]')
  expect(cite.attributes('href')).toContain('/entries/42')
})

it('shows the answer error when runAnswer fails', async () => {
  const { useSearchStore } = await import('@/stores/search')
  const wrapper = mountView()
  const store = useSearchStore()
  vi.spyOn(store, 'runAnswer').mockImplementation(async () => {
    store.answerError = 'Answer unavailable — see the results below.'
  })

  await wrapper.find('[data-testid="search-query-input"]').setValue('x')
  await wrapper.find('[data-testid="search-answer"]').trigger('click')
  await flushPromises()

  expect(wrapper.find('[data-testid="answer-error"]').text()).toContain(
    'Answer unavailable',
  )
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/views/__tests__/SearchView.test.ts -t "Answer this"`
Expected: FAIL — no element `[data-testid="search-answer"]`.

- [ ] **Step 3: Implement the view changes**

(a) In the `<script setup>` of `SearchView.vue`, add an answer handler beside the existing `submit()`:

```typescript
function onAnswer(): void {
  store.runAnswer()
}
```

(b) In the `<form>`, immediately after the existing Search `<button>` (the one with `data-testid="search-submit"`), add:

```html
      <button
        type="button"
        class="btn border-gray-200 dark:border-gray-700/60 text-gray-700 dark:text-gray-200 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center"
        data-testid="search-answer"
        :disabled="store.answerLoading || !queryInput.trim()"
        @click="onAnswer"
      >
        {{ store.answerLoading ? 'Thinking…' : 'Answer this' }}
      </button>
```

(c) Add the answer panel immediately **above** the results section (before the `<!-- Loading / error / empty states -->` block):

```html
    <!-- Answer panel (opt-in synthesis) -->
    <div
      v-if="store.answerLoading || store.answer || store.answerError"
      class="mb-4 rounded-md border border-gray-200 dark:border-gray-700/60 bg-white dark:bg-gray-800 p-4"
      data-testid="answer-panel"
    >
      <div
        v-if="store.answerLoading"
        class="text-sm text-gray-600 dark:text-gray-300"
      >
        Thinking…
      </div>
      <div
        v-else-if="store.answerError"
        class="text-sm text-red-600 dark:text-red-400"
        data-testid="answer-error"
      >
        {{ store.answerError }}
      </div>
      <template v-else>
        <p
          class="text-sm text-gray-800 dark:text-gray-100 whitespace-pre-wrap leading-relaxed"
          data-testid="answer-text"
        >
          {{ store.answer }}
        </p>
        <div
          v-if="store.answerCitations.length"
          class="mt-3 flex flex-wrap gap-2"
        >
          <RouterLink
            v-for="c in store.answerCitations"
            :key="c.entry_id"
            :to="`/entries/${c.entry_id}`"
            class="text-xs px-2 py-1 rounded bg-violet-50 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300 hover:bg-violet-100"
            data-testid="answer-citation"
          >
            {{ c.entry_date }}
          </RouterLink>
        </div>
      </template>
    </div>
```

(If `RouterLink` is not already used/imported in this file, the existing result links show how links are rendered — match that approach, e.g. an `<a>` with the same `href` shape the test asserts.)

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/views/__tests__/SearchView.test.ts`
Expected: PASS (all SearchView tests).

- [ ] **Step 5: Verify coverage, format, lint, build**

Run: `npm run format -- src/views/SearchView.vue src/stores/search.ts src/api/search.ts && npm run lint && npm run test:coverage && npm run build`
Expected: format clean, lint clean, coverage ≥85% on all metrics, build succeeds.

- [ ] **Step 6: Commit**

```bash
git add src/views/SearchView.vue src/views/__tests__/SearchView.test.ts
git commit -m "feat(search): add 'Answer this' button + answer panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Webapp docs + journal entries (both repos)

**Files:**
- Modify: `webapp/docs/development.md` (or the nearest active doc describing search)
- Create: `server/journal/260616-search-answer-synthesis.md`
- Create: `webapp/journal/260616-search-answer-panel.md`

- [ ] **Step 1: Webapp doc note**

Add a short subsection to the webapp's search/development doc: the Search page has an "Answer this" button that calls `POST /api/search/answer` and renders a synthesized, cited answer above the results; it's opt-in (one LLM call per click) and degrades to the results list on error.

- [ ] **Step 2: Journal entries**

Create `server/journal/260616-search-answer-synthesis.md` and `webapp/journal/260616-search-answer-panel.md` capturing: the opt-in design, the strict-grounding contract, the `Answerer` provider mirroring the reranker, reuse of hybrid retrieval, and the cost/latency profile.

- [ ] **Step 3: Commit (each repo)**

```bash
# server
git add docs journal && git commit -m "docs: journal entry for search answer synthesis

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
# webapp
git add docs journal && git commit -m "docs: journal entry for search answer panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (before push/deploy)

- [ ] Server: `uv run pytest -m "not integration" -q` → all pass; `uv run ruff check src/ tests/` → clean.
- [ ] Webapp: `npm run format:check && npm run lint && npm run test:coverage && npm run build` → all pass, coverage ≥85%.
- [ ] Push each repo to `main`, watch CI (`gh run watch`) until green.
- [ ] Deploy per runbook: `ssh media`, `cd /srv/media`, `docker compose pull journal-server journal-webapp && docker compose up -d journal-server journal-webapp`; confirm clean boot in `docker logs journal-server`.
- [ ] Smoke test: from the webapp, run a question search and click "Answer this"; confirm a cited answer renders and a nonsense question returns the "couldn't find" message.
```
