"""Per-job LLM token-usage capture, scoped via a ``contextvars`` collector.

Every LLM call in the codebase currently discards the token counts the
provider SDK returns. This module is the single seam that captures them
and attributes them to the *job* that triggered the call — without
threading a collector object through every service and provider.

The crux is a :class:`contextvars.ContextVar`. The job runner opens a
:func:`usage_scope` on the worker thread; each provider adapter calls
:func:`record` (or one of the SDK-shaped ``record_*`` helpers) right
after its API call. Because the collector rides on a contextvar it is
visible to:

* the whole synchronous call stack on the worker thread, and
* any child thread the adapter spawns **via**
  ``contextvars.copy_context().run(...)`` — which is how the OCR
  dual-pass and transcription-shadow fan-outs propagate the scope into
  their ``ThreadPoolExecutor(max_workers=2)`` sub-threads. Those two
  fan-outs are the heaviest token consumers, so a thread-local would
  miss exactly the calls that matter most.

Off a job (the request path — answer synthesis, reranking, etc.) there
is no active scope, so :func:`record` is a cheap no-op. That is
deliberate: W2 attributes tokens to *jobs* only.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class UsageCollector:
    """Thread-safe accumulator of per-model input/output token counts.

    A single collector is shared across the worker thread and any child
    threads it fans out to, so ``add`` is guarded by a lock. ``per_model``
    maps a model id to ``{"input_tokens": int, "output_tokens": int}``.
    """

    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Add token counts for ``model`` (lock-guarded, additive)."""
        with self._lock:
            bucket = self.per_model.setdefault(
                model, {"input_tokens": 0, "output_tokens": 0},
            )
            bucket["input_tokens"] += int(input_tokens)
            bucket["output_tokens"] += int(output_tokens)

    @property
    def totals(self) -> tuple[int, int]:
        """Return ``(total_input_tokens, total_output_tokens)`` across models."""
        with self._lock:
            input_total = sum(
                bucket["input_tokens"] for bucket in self.per_model.values()
            )
            output_total = sum(
                bucket["output_tokens"] for bucket in self.per_model.values()
            )
        return input_total, output_total


_current: contextvars.ContextVar[UsageCollector | None] = contextvars.ContextVar(
    "journal_usage_collector", default=None,
)


def record(model: str, input_tokens: int, output_tokens: int) -> None:
    """Attribute token counts to the active :func:`usage_scope`.

    A NO-OP when no scope is active — this is what excludes request-path
    LLM calls (answerer, reranker, classifiers) from job attribution.
    """
    collector = _current.get()
    if collector is None:
        return
    collector.add(model, input_tokens, output_tokens)


@contextlib.contextmanager
def usage_scope() -> Iterator[UsageCollector]:
    """Bind a fresh :class:`UsageCollector` to the contextvar for the block.

    Yields the collector so the caller can read ``collector.totals`` after
    the wrapped work finishes. The contextvar is reset on exit, so nested
    and sequential scopes never leak into one another.
    """
    collector = UsageCollector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def _attr(obj: Any, name: str, default: int = 0) -> int:  # noqa: ANN401
    """Read an integer token count, tolerating missing/None attributes.

    Mirrors the defensive ``_extract_usage`` helper in
    ``providers/storyline_narrator.py``: SDK usage objects occasionally
    omit a field or set it to ``None`` (e.g. Gemini's
    ``candidates_token_count`` on an empty completion), and tests inject
    partial fakes.
    """
    value = getattr(obj, name, default)
    return value if value is not None else default


def record_anthropic(model: str, message: Any) -> None:  # noqa: ANN401
    """Record usage from an Anthropic ``messages.create`` response.

    Reads ``message.usage.input_tokens`` / ``output_tokens``. Tolerates a
    missing or ``None`` ``usage`` attribute.
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return
    record(model, _attr(usage, "input_tokens"), _attr(usage, "output_tokens"))


def record_gemini(model: str, response: Any) -> None:  # noqa: ANN401
    """Record usage from a Gemini ``generate_content`` response.

    Reads ``response.usage_metadata.prompt_token_count`` /
    ``candidates_token_count``. Tolerates a missing or ``None``
    ``usage_metadata`` attribute.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    record(
        model,
        _attr(usage, "prompt_token_count"),
        _attr(usage, "candidates_token_count"),
    )


def record_openai(model: str, response: Any) -> None:  # noqa: ANN401
    """Record usage from an OpenAI response (chat / embeddings / audio).

    Reads ``response.usage.prompt_tokens`` / ``completion_tokens``.
    Embeddings responses have no completion, so ``completion_tokens`` is
    absent and output defaults to 0. Tolerates a missing or ``None``
    ``usage`` attribute (some audio-transcription responses omit it).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    record(
        model,
        _attr(usage, "prompt_tokens"),
        _attr(usage, "completion_tokens"),
    )
