"""Tests for the transcription provider factory."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from journal.config import Config
from journal.providers.transcription import (
    _DEFAULT_TRANSCRIPTION_MODELS,
    GeminiTranscribeProvider,
    OpenAITranscribeProvider,
    RetryingTranscriptionProvider,
    ShadowTranscriptionProvider,
    _describe_stack,
    _resolve_model,
    build_transcription_provider,
)


def _clean_transcription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TRANSCRIPTION_PROVIDER",
        "TRANSCRIPTION_MODEL",
        "TRANSCRIPTION_FALLBACK_ENABLED",
        "TRANSCRIPTION_FALLBACK_MODEL",
        "TRANSCRIPTION_RETRY_MAX_ATTEMPTS",
        "TRANSCRIPTION_RETRY_BASE_DELAY",
        "TRANSCRIPTION_RETRY_MAX_DELAY",
        "TRANSCRIPTION_SHADOW_PROVIDER",
        "TRANSCRIPTION_SHADOW_MODEL",
        "TRANSCRIPTION_CONTEXT_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    # Disable context loading to keep tests deterministic — they don't need
    # to exercise the prompt builders.
    monkeypatch.setenv("TRANSCRIPTION_CONTEXT_ENABLED", "false")


def _build(config: Config):
    with (
        patch("journal.providers.transcription.openai.OpenAI"),
        patch("journal.providers.transcription.genai.Client"),
    ):
        return build_transcription_provider(config)


class TestDefaultConfig:
    def test_default_config_returns_openai_with_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        provider = _build(Config())

        assert isinstance(provider, RetryingTranscriptionProvider)
        assert isinstance(provider.primary, OpenAITranscribeProvider)
        assert provider.primary.model == "gpt-4o-transcribe"
        assert isinstance(provider.fallback, OpenAITranscribeProvider)
        assert provider.fallback.model == "whisper-1"

    def test_fallback_disabled_returns_bare_openai(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_ENABLED", "false")
        provider = _build(Config())

        assert isinstance(provider, OpenAITranscribeProvider)
        assert provider.model == "gpt-4o-transcribe"


class TestGeminiPrimary:
    def test_gemini_primary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "gemini")
        provider = _build(Config())

        assert isinstance(provider, RetryingTranscriptionProvider)
        assert isinstance(provider.primary, GeminiTranscribeProvider)
        assert provider.primary.model == "gemini-2.5-pro"
        assert isinstance(provider.fallback, OpenAITranscribeProvider)
        assert provider.fallback.model == "whisper-1"

    def test_gemini_with_explicit_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "gemini")
        monkeypatch.setenv("TRANSCRIPTION_MODEL", "gemini-2.5-flash")
        provider = _build(Config())

        assert isinstance(provider, RetryingTranscriptionProvider)
        assert isinstance(provider.primary, GeminiTranscribeProvider)
        assert provider.primary.model == "gemini-2.5-flash"


class TestSelfFallbackAvoidance:
    def test_openai_default_avoids_self_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "openai")
        monkeypatch.setenv("TRANSCRIPTION_MODEL", "whisper-1")
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_MODEL", "whisper-1")
        provider = _build(Config())

        # Identical primary + fallback config → no retry wrapper.
        assert isinstance(provider, OpenAITranscribeProvider)
        assert provider.model == "whisper-1"


class TestShadow:
    def test_shadow_enabled_wraps_in_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "gemini")
        provider = _build(Config())

        assert isinstance(provider, ShadowTranscriptionProvider)
        assert isinstance(provider.shadow, GeminiTranscribeProvider)
        assert provider.shadow.model == "gemini-2.5-pro"
        # Inside: retrying(openai/gpt-4o-transcribe, fb=whisper-1)
        assert isinstance(provider.primary, RetryingTranscriptionProvider)
        inner = provider.primary
        assert isinstance(inner.primary, OpenAITranscribeProvider)
        assert inner.primary.model == "gpt-4o-transcribe"

    def test_shadow_disabled_no_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "")
        provider = _build(Config())

        assert not isinstance(provider, ShadowTranscriptionProvider)


class TestValidation:
    def test_invalid_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "foo")
        with pytest.raises(ValueError, match="TRANSCRIPTION_PROVIDER"):
            Config()

    def test_invalid_shadow_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "foo")
        with pytest.raises(ValueError, match="TRANSCRIPTION_SHADOW_PROVIDER"):
            Config()


class TestModelResolution:
    def test_model_mismatch_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "gemini")
        monkeypatch.setenv("TRANSCRIPTION_MODEL", "gpt-4o-transcribe")

        with caplog.at_level(logging.INFO, logger="journal.providers.transcription"):
            provider = _build(Config())

        assert isinstance(provider, RetryingTranscriptionProvider)
        assert isinstance(provider.primary, GeminiTranscribeProvider)
        assert provider.primary.model == "gemini-2.5-pro"
        # An INFO record should mention the override.
        assert any(
            "not compatible with provider=gemini" in rec.getMessage()
            for rec in caplog.records
        )

    def test_resolve_model_openai_with_gemini_string_uses_default(self) -> None:
        assert _resolve_model("openai", "gemini-2.5-pro") == \
            _DEFAULT_TRANSCRIPTION_MODELS["openai"]

    def test_resolve_model_empty_uses_default(self) -> None:
        assert _resolve_model("openai", "") == _DEFAULT_TRANSCRIPTION_MODELS["openai"]
        assert _resolve_model("gemini", "") == _DEFAULT_TRANSCRIPTION_MODELS["gemini"]

    def test_resolve_model_compatible_passes_through(self) -> None:
        assert _resolve_model("openai", "gpt-4o-mini-transcribe") == \
            "gpt-4o-mini-transcribe"
        assert _resolve_model("gemini", "gemini-2.5-flash") == "gemini-2.5-flash"


class TestDescribeStack:
    def test_describe_stack_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)

        # Bare OpenAI.
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_ENABLED", "false")
        bare = _build(Config())
        assert _describe_stack(bare) == "openai/gpt-4o-transcribe"

        # Retrying with fallback.
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_ENABLED", "true")
        retry = _build(Config())
        assert _describe_stack(retry) == \
            "retrying(openai/gpt-4o-transcribe, fb=openai/whisper-1)"

        # Shadow.
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "gemini")
        shadow = _build(Config())
        assert _describe_stack(shadow) == (
            "shadow(retrying(openai/gpt-4o-transcribe, fb=openai/whisper-1), "
            "gemini/gemini-2.5-pro)"
        )
