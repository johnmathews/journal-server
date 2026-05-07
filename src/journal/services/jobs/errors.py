"""Error classification helpers for the jobs subsystem.

``friendly_error`` maps known external-service exceptions to short
strings suitable for display in the webapp. ``is_transient`` is the
predicate for the retry path: exceptions matching the same patterns get
retried with exponential backoff (3 min, 6 min, 12 min, 24 min, 48 min)
before being marked failed.
"""

from __future__ import annotations


def friendly_error(exc: Exception) -> str:
    """Map known external-service exceptions to user-friendly messages.

    The raw exception is already logged by the caller; this produces a
    short message suitable for display in the webapp UI.
    """
    msg = str(exc)
    # Google Gemini API errors
    if "503" in msg and ("UNAVAILABLE" in msg or "high demand" in msg):
        return "OCR service overloaded"
    if "429" in msg and "RESOURCE_EXHAUSTED" in msg:
        return "Google API rate limit exceeded"
    if "404" in msg and "is not found for API version" in msg:
        return (
            "The configured OCR model was not found. "
            "Check the OCR_MODEL setting."
        )
    # OpenAI API errors
    if "openai" in msg.lower() and ("rate_limit" in msg.lower() or "429" in msg):
        return "OpenAI rate limit exceeded"
    # Anthropic API errors
    if "overloaded" in msg.lower() and ("anthropic" in msg.lower() or "529" in msg):
        return "Anthropic API overloaded"
    # Fall through — return the raw message for unexpected errors
    return msg


def is_transient(exc: Exception) -> bool:
    """Return True if the exception looks like a temporary API issue
    worth retrying.
    """
    msg = str(exc)
    if "503" in msg and ("UNAVAILABLE" in msg or "high demand" in msg):
        return True
    if "429" in msg and ("RESOURCE_EXHAUSTED" in msg or "rate_limit" in msg.lower()):
        return True
    return "overloaded" in msg.lower() and ("529" in msg or "anthropic" in msg.lower())


# Retry schedule: 3 min, 6 min, 12 min, 24 min, 48 min (exponential
# backoff with a cap so a stuck overload doesn't pin a worker for hours).
RETRY_DELAYS_SECONDS = [180, 360, 720, 1440, 2880]
