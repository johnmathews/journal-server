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


@dataclass(frozen=True)
class ConversationTurn:
    """One turn of a conversation handed to the multi-turn answerer."""

    role: str       # "user" | "assistant"
    content: str


class AnswerUnavailable(Exception):  # noqa: N818
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

    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
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

    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
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
    '"answered": false and "answer": "' + NO_MATCH_MESSAGE + '" and leave '
    "`cited_entry_ids` empty. Do NOT guess or use outside knowledge.\n"
    "- Output the JSON object only. No prose, no markdown."
)

_CONVERSATION_SYSTEM_PROMPT = (
    "You are continuing a conversation about a person's private journal. "
    "You are given the conversation so far and a numbered list of dated "
    "passages retrieved from their journal for the latest message. Answer "
    "the latest user message ONLY from these passages and the conversation.\n\n"
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
    "- If the passages do not contain enough to answer the latest message, "
    'set "answered": false and "answer": "' + NO_MATCH_MESSAGE + '" and '
    "leave `cited_entry_ids` empty. Do NOT guess or use outside knowledge.\n"
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
                # Anthropic silently ignores cache_control on system blocks
                # below ~1024 tokens (2048 for Sonnet/Opus). The prompt is
                # under that threshold, so caching is currently a no-op —
                # cache_control is set anyway for forward compatibility and
                # mirrors the pattern used in reranker.py.
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.APIError as e:
            logger.warning("AnthropicAnswerer call failed: %s", e)
            raise AnswerUnavailable(str(e)) from e

        return self._result_from_response(response, {p.entry_id for p in passages})

    @staticmethod
    def _first_text(response: object) -> str:
        content = getattr(response, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                return text
        return ""

    @staticmethod
    def _passage_lines(passages: list[AnswerPassage]) -> list[str]:
        lines = ["Passages:"]
        for p in passages:
            text = p.text
            if len(text) > _MAX_PASSAGE_CHARS:
                text = text[: _MAX_PASSAGE_CHARS - 1] + "…"
            lines.append(f"[entry_id={p.entry_id} date={p.entry_date}] {text}")
        return lines

    @staticmethod
    def _format_user_message(
        question: str, passages: list[AnswerPassage]
    ) -> str:
        lines = [f"Question: {question}", ""]
        lines.extend(AnthropicAnswerer._passage_lines(passages))
        lines.append("")
        lines.append("Output the JSON object now.")
        return "\n".join(lines)

    def _result_from_response(
        self, response: object, valid_ids: set[int]
    ) -> AnswerResult:
        raw = self._first_text(response)
        parsed = self._parse_response(raw)
        if parsed is None:
            logger.warning(
                "AnthropicAnswerer returned malformed output. "
                "Raw (first 200 chars): %r",
                (raw or "")[:200],
            )
            raise AnswerUnavailable("malformed answerer output")
        cited: list[int] = []
        for eid in parsed["cited_entry_ids"]:
            try:
                eid_int = int(eid)
            except (TypeError, ValueError):
                continue
            if eid_int in valid_ids:
                cited.append(eid_int)
        return AnswerResult(
            answer=str(parsed["answer"]),
            answered=bool(parsed["answered"]),
            cited_entry_ids=cited,
        )

    def continue_conversation(
        self,
        history: list[ConversationTurn],
        passages: list[AnswerPassage],
    ) -> AnswerResult:
        if not history:
            return AnswerResult(answer=NO_MATCH_MESSAGE, answered=False)

        messages = [{"role": t.role, "content": t.content} for t in history]
        # Append the freshly-retrieved passages to the final user turn so
        # the model grounds the latest message against them.
        passage_block = "\n".join(
            [*self._passage_lines(passages), "", "Output the JSON object now."]
        )
        messages[-1]["content"] = (
            f"{messages[-1]['content']}\n\n{passage_block}"
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
