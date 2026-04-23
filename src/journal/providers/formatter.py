"""Transcript paragraph formatter — inserts paragraph breaks via LLM."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a transcript formatter. Your ONLY job is to insert paragraph breaks
(blank lines) into voice transcriptions where natural topic shifts, pauses,
or logical boundaries occur.

Rules:
- Do NOT change, add, or remove any words.
- Only insert blank lines (\\n\\n) between paragraphs.
- The output must contain exactly the same words in the same order as the input.
- If the text is already well-paragraphed or too short to need breaks, return it unchanged.
"""


@runtime_checkable
class FormatterProtocol(Protocol):
    """Protocol for transcript formatting providers."""

    def format_paragraphs(self, text: str) -> str: ...


class AnthropicFormatter:
    """Paragraph formatter using the Anthropic API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 8192,
    ) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def format_paragraphs(self, text: str) -> str:
        """Add paragraph breaks to *text* without changing any words.

        Returns the original *text* unchanged when:
        - the input is blank,
        - the LLM alters the word sequence, or
        - the API call fails for any reason.
        """
        if not text.strip():
            return text

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            formatted = response.content[0].text
        except Exception:
            log.warning("Formatter API call failed — returning original text", exc_info=True)
            return text

        # Safety check: the LLM must not change any words.
        if text.split() != formatted.split():
            log.warning(
                "Formatter changed words (model=%s, original=%d words, "
                "result=%d words) — returning original text",
                self._model,
                len(text.split()),
                len(formatted.split()),
            )
            return text

        return formatted
