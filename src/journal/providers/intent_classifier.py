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
        user = (
            question
            if not context
            else f"Conversation so far:\n{context}\n\nLatest message: {question}"
        )
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
