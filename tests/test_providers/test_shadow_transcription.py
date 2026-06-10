"""Tests for ShadowTranscriptionProvider and the _word_diff helper."""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import pytest

from journal.models import TranscriptionResult
from journal.providers.transcription import (
    ShadowTranscriptionProvider,
    TranscriptionProvider,
    _word_diff,
)

LOGGER_NAME = "journal.providers.transcription"


def _result(text: str, uncertain_spans: list[tuple[int, int]] | None = None) -> TranscriptionResult:
    return TranscriptionResult(text=text, uncertain_spans=uncertain_spans or [])


def _find_diff_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    for record in caplog.records:
        if record.getMessage() == "transcription_shadow_diff":
            return record
    raise AssertionError("transcription_shadow_diff log record not found")


# ---------------------------------------------------------------------------
# Wrapper behaviour
# ---------------------------------------------------------------------------


def test_implements_protocol() -> None:
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    assert isinstance(wrapper, TranscriptionProvider)


def test_returns_primary_unchanged() -> None:
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("primary")
    shadow.transcribe.return_value = _result("shadow")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    result = wrapper.transcribe(b"audio", "audio/mp3", "en")

    assert result.text == "primary"


def test_logs_diff_when_both_succeed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("alpha beta gamma")
    shadow.transcribe.return_value = _result("alpha BETA gamma")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    diffs = record.diffs  # type: ignore[attr-defined]
    assert isinstance(diffs, list)
    assert len(diffs) == 1
    assert diffs[0]["op"] == "replace"
    assert diffs[0]["primary"] == "beta"
    assert diffs[0]["shadow"] == "BETA"


def test_diff_log_excludes_full_transcripts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("primary text content")
    shadow.transcribe.return_value = _result("shadow text content")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    assert not hasattr(record, "primary_text")
    assert not hasattr(record, "shadow_text")


def test_diff_log_includes_similarity_ratio(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("alpha beta gamma")
    shadow.transcribe.return_value = _result("alpha BETA gamma")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    similarity = record.similarity_ratio  # type: ignore[attr-defined]
    assert isinstance(similarity, float)
    assert 0.0 <= similarity <= 1.0
    # Round to 3 decimals → at most 3 fractional digits.
    assert round(similarity, 3) == similarity


def test_diff_log_includes_uncertain_span_counts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result(
        "hello world foo", uncertain_spans=[(0, 5), (6, 11)],
    )
    shadow.transcribe.return_value = _result(
        "hello world foo", uncertain_spans=[(0, 5)],
    )

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    assert record.primary_uncertain_count == 2  # type: ignore[attr-defined]
    assert record.shadow_uncertain_count == 1  # type: ignore[attr-defined]


def test_no_diff_when_identical_transcripts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("alpha beta gamma")
    shadow.transcribe.return_value = _result("alpha beta gamma")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    assert record.diffs == []  # type: ignore[attr-defined]
    assert record.similarity_ratio == 1.0  # type: ignore[attr-defined]


def test_shadow_failure_returns_primary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("primary text")
    shadow.transcribe.side_effect = RuntimeError("shadow boom")

    wrapper = ShadowTranscriptionProvider(
        primary=primary, shadow=shadow, shadow_label="gemini",
    )
    result = wrapper.transcribe(b"audio", "audio/mp3", "en")

    assert result.text == "primary text"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gemini" in r.getMessage() for r in warnings)


def test_shadow_failure_does_not_log_diff(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("primary text")
    shadow.transcribe.side_effect = RuntimeError("shadow boom")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    diff_records = [
        r for r in caplog.records if r.getMessage() == "transcription_shadow_diff"
    ]
    assert diff_records == []


def test_primary_failure_propagates() -> None:
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.side_effect = RuntimeError("primary boom")
    shadow.transcribe.return_value = _result("shadow text")

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    with pytest.raises(RuntimeError, match="primary boom"):
        wrapper.transcribe(b"audio", "audio/mp3", "en")


def test_runs_in_parallel() -> None:
    """Primary and shadow must execute concurrently.

    Each provider blocks on a shared two-party barrier inside its
    transcribe call, so the barrier only releases when both calls are
    in flight at the same time. Sequential execution would leave the
    first caller stranded until the barrier timeout, raising
    BrokenBarrierError and failing the test — no wall-clock timing
    involved, so the assertion is immune to loaded CI runners.
    """
    barrier = threading.Barrier(2, timeout=5)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)

    def primary_side(*args: object, **kwargs: object) -> TranscriptionResult:
        barrier.wait()
        return _result("primary")

    def shadow_side(*args: object, **kwargs: object) -> TranscriptionResult:
        barrier.wait()
        return _result("shadow")

    primary.transcribe.side_effect = primary_side
    shadow.transcribe.side_effect = shadow_side

    wrapper = ShadowTranscriptionProvider(primary=primary, shadow=shadow)
    result = wrapper.transcribe(b"audio", "audio/mp3", "en")

    assert result.text == "primary"
    assert not barrier.broken


def test_shadow_label_appears_in_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    primary = MagicMock(spec=TranscriptionProvider)
    shadow = MagicMock(spec=TranscriptionProvider)
    primary.transcribe.return_value = _result("primary")
    shadow.transcribe.return_value = _result("primary")

    wrapper = ShadowTranscriptionProvider(
        primary=primary, shadow=shadow, shadow_label="gemini",
    )
    wrapper.transcribe(b"audio", "audio/mp3", "en")

    record = _find_diff_record(caplog)
    assert record.shadow_label == "gemini"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _word_diff helper
# ---------------------------------------------------------------------------


def test_word_diff_replace() -> None:
    diffs = _word_diff("alpha beta gamma", "alpha BETA gamma")
    assert len(diffs) == 1
    assert diffs[0]["op"] == "replace"
    assert diffs[0]["primary"] == "beta"
    assert diffs[0]["shadow"] == "BETA"


def test_word_diff_insert() -> None:
    diffs = _word_diff("alpha gamma", "alpha beta gamma")
    assert len(diffs) == 1
    assert diffs[0]["op"] == "insert"
    assert diffs[0]["primary"] == ""
    assert diffs[0]["shadow"] == "beta"


def test_word_diff_delete() -> None:
    diffs = _word_diff("alpha beta gamma", "alpha gamma")
    assert len(diffs) == 1
    assert diffs[0]["op"] == "delete"
    assert diffs[0]["primary"] == "beta"
    assert diffs[0]["shadow"] == ""


def test_word_diff_identical_returns_empty() -> None:
    assert _word_diff("alpha beta gamma", "alpha beta gamma") == []
