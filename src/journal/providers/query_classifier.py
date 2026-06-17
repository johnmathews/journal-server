"""Query intent classification for auto-triggered answer synthesis.

Before spending a (relatively expensive) Sonnet answer-synthesis call,
the search flow asks a cheap model whether the query is a natural-
language *question* the journal could answer ("when did my back start
hurting") versus a plain keyword/entity *search* the user wants to
browse matches for ("vienna", "back pain"). Only questions trigger the
synthesis step, so a keyword search never pays for an answer.

Mirrors the provider pattern in `reranker.py` / `answerer.py`:
- `HeuristicQueryClassifier` — no LLM; `?`-suffix or wh-word opener.
  Used when answer synthesis is disabled (`ANSWER_PROVIDER=none`) and as
  the fallback when the Anthropic classifier errors or returns something
  unparseable, so a classifier hiccup never blocks search.
- `AnthropicQueryClassifier` — one cheap Haiku call returning the single
  word QUESTION or SEARCH (~80 input + a few output tokens).
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

import anthropic

logger = logging.getLogger(__name__)

# The cheap, offline heuristic: ends with '?' or opens with a wh-word.
_WH_OPENER = re.compile(r"^(who|what|when|where|why|how)\b", re.IGNORECASE)


def _heuristic_is_question(query: str) -> bool:
    q = query.strip()
    if not q:
        return False
    return q.endswith("?") or bool(_WH_OPENER.match(q))


@runtime_checkable
class QueryClassifier(Protocol):
    """Protocol for question/search query classifiers."""

    def is_question(self, query: str) -> bool: ...


class HeuristicQueryClassifier:
    """Offline classifier — '?'-suffix or wh-word opener, no LLM call."""

    def is_question(self, query: str) -> bool:
        return _heuristic_is_question(query)


_SYSTEM_PROMPT = (
    "You classify a search query for a personal journal app. Decide "
    "whether the query is a natural-language QUESTION that an assistant "
    "could answer by reading the user's journal entries (e.g. 'when did "
    "my back start hurting', 'how often did I run in May', 'what did I "
    "say about Vienna'), or whether it is a plain keyword/entity SEARCH "
    "the user just wants to browse matching entries for (e.g. 'vienna', "
    "'back pain', 'atlas tennis').\n\n"
    "Reply with exactly one word: QUESTION or SEARCH. No punctuation, no "
    "explanation."
)


class AnthropicQueryClassifier:
    """Cheap question/search classifier via an Anthropic Claude model.

    On any failure (API error, unparseable reply) it falls back to the
    offline heuristic rather than raising — classification must never
    block the search results from rendering.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 5,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def is_question(self, query: str) -> bool:
        if not query.strip():
            return False
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
                messages=[{"role": "user", "content": query}],
            )
        except anthropic.APIError as e:
            logger.warning(
                "AnthropicQueryClassifier failed (%s); using heuristic", e
            )
            return _heuristic_is_question(query)

        raw = self._first_text(response).strip().upper()
        if raw.startswith("QUESTION"):
            return True
        if raw.startswith("SEARCH"):
            return False
        logger.warning(
            "AnthropicQueryClassifier returned %r; using heuristic", raw[:40]
        )
        return _heuristic_is_question(query)

    @staticmethod
    def _first_text(response: object) -> str:
        content = getattr(response, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                return text
        return ""


def build_query_classifier(
    name: str,
    *,
    anthropic_api_key: str = "",
    model: str = "claude-haiku-4-5",
) -> QueryClassifier:
    """Build a classifier by name. Unknown names raise (fail-fast)."""
    if name in ("none", "noop", "heuristic"):
        return HeuristicQueryClassifier()
    if name == "anthropic":
        if not anthropic_api_key:
            raise ValueError(
                "AnthropicQueryClassifier requires ANTHROPIC_API_KEY to be set"
            )
        return AnthropicQueryClassifier(api_key=anthropic_api_key, model=model)
    raise ValueError(
        f"Unknown query classifier {name!r} — must be 'anthropic' or 'none'"
    )
