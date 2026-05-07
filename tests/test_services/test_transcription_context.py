"""Tests for the Whisper transcription-context prompt builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from journal.services.transcription_context import (
    _FULL_CONTEXT_PREAMBLE,
    DEFAULT_MAX_TOKENS,
    _normalize_whitespace,
    _strip_markdown,
    _truncate_to_tokens,
    build_full_context_instruction,
    build_whisper_prompt,
)


class TestStripMarkdown:
    def test_strips_headings(self):
        assert _strip_markdown("# People\n\n# Places").strip() == "People\n\nPlaces"

    def test_strips_list_bullets(self):
        text = "- Alice\n- Bob\n* Carol\n+ Dave"
        assert _strip_markdown(text) == "Alice\nBob\nCarol\nDave"

    def test_strips_bold_keeps_inner(self):
        assert _strip_markdown("Meet **Adi** at the cafe.") == "Meet Adi at the cafe."

    def test_strips_italic_keeps_inner(self):
        assert _strip_markdown("Visit *Berlin* often.") == "Visit Berlin often."
        assert _strip_markdown("Visit _Berlin_ often.") == "Visit Berlin often."

    def test_strips_inline_code(self):
        assert _strip_markdown("Run `journal` daily.") == "Run journal daily."

    def test_strips_links_keeps_anchor_text(self):
        assert (
            _strip_markdown("See [docs](https://example.com) for more.")
            == "See docs for more."
        )

    def test_strips_images_entirely(self):
        assert _strip_markdown("![alt text](img.png)Caption.") == "Caption."

    def test_strips_horizontal_rule(self):
        text = "Foo\n---\nBar"
        assert "---" not in _strip_markdown(text)

    def test_combination(self):
        text = (
            "# People\n\n"
            "- **Adi** — close friend (also _Addy_)\n"
            "- **Dr. Patel** — physiotherapist\n\n"
            "# Places\n\n"
            "- **Hampstead Heath** — park\n"
        )
        result = _strip_markdown(text)
        assert "Adi" in result
        assert "Addy" in result
        assert "Dr. Patel" in result
        assert "Hampstead Heath" in result
        # Markdown markers are gone
        assert "**" not in result
        assert "# " not in result
        assert "- " not in result


class TestNormalizeWhitespace:
    def test_collapses_multiple_spaces(self):
        assert _normalize_whitespace("a   b  c") == "a b c"

    def test_collapses_newlines(self):
        assert _normalize_whitespace("a\n\nb\nc") == "a b c"

    def test_strips_edges(self):
        assert _normalize_whitespace("  hello world  ") == "hello world"

    def test_empty(self):
        assert _normalize_whitespace("") == ""
        assert _normalize_whitespace("   \n\n  ") == ""


class TestTruncateToTokens:
    def test_short_text_unchanged(self):
        text = "Alice Bob Carol"
        assert _truncate_to_tokens(text, max_tokens=200) == text

    def test_long_text_is_shortened(self):
        text = " ".join(["word"] * 500)
        result = _truncate_to_tokens(text, max_tokens=50)
        assert len(result) < len(text)

    def test_empty(self):
        assert _truncate_to_tokens("", max_tokens=10) == ""

    def test_token_count_is_under_limit(self):
        import tiktoken

        text = " ".join(f"name{i}" for i in range(500))
        result = _truncate_to_tokens(text, max_tokens=50)
        enc = tiktoken.get_encoding("o200k_base")
        assert len(enc.encode(result)) <= 50


class TestBuildWhisperPrompt:
    def test_none_dir_returns_empty(self):
        assert build_whisper_prompt(None) == ""

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert build_whisper_prompt(tmp_path / "does-not-exist") == ""

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        assert build_whisper_prompt(tmp_path) == ""

    def test_dir_with_only_non_markdown_returns_empty(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("Alice\nBob\n")
        assert build_whisper_prompt(tmp_path) == ""

    def test_single_file_loaded_and_stripped(self, tmp_path: Path):
        (tmp_path / "people.md").write_text(
            "# People\n\n- **Adi** — close friend\n- **Dr. Patel** — physio\n"
        )
        result = build_whisper_prompt(tmp_path)
        assert result
        assert "Adi" in result
        assert "Dr. Patel" in result
        # Markdown is gone
        assert "**" not in result
        assert "\n" not in result  # whitespace collapsed
        # Filename heading from load_context_files is also stripped
        assert "# people" not in result.lower()

    def test_multiple_files_concatenated(self, tmp_path: Path):
        (tmp_path / "people.md").write_text("- **Adi**\n- **Joe**")
        (tmp_path / "places.md").write_text("- **Berlin**\n- **London**")
        result = build_whisper_prompt(tmp_path)
        assert "Adi" in result
        assert "Joe" in result
        assert "Berlin" in result
        assert "London" in result

    def test_truncates_when_over_limit(self, tmp_path: Path):
        # Build a file far longer than 200 tokens.
        names = [f"Person{i}" for i in range(500)]
        (tmp_path / "huge.md").write_text("\n".join(f"- **{n}**" for n in names))

        result = build_whisper_prompt(tmp_path, max_tokens=50)
        assert result

        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        assert len(enc.encode(result)) <= 50

    def test_default_max_tokens_is_200(self):
        assert DEFAULT_MAX_TOKENS == 200

    def test_truly_empty_file_returns_empty(self, tmp_path: Path):
        # `load_context_files` skips files whose content strips to empty,
        # so the prompt is empty. (Files with only headings still produce
        # a stem-derived hint, which is intentional.)
        (tmp_path / "empty.md").write_text("\n\n   \n")
        assert build_whisper_prompt(tmp_path) == ""


class TestBuildFullContextInstruction:
    def test_none_dir_returns_empty(self):
        assert build_full_context_instruction(None) == ""

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert build_full_context_instruction(tmp_path / "does-not-exist") == ""

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        assert build_full_context_instruction(tmp_path) == ""

    def test_includes_preamble_when_context_present(self, tmp_path: Path):
        (tmp_path / "people.md").write_text("- **Adi** — close friend")
        result = build_full_context_instruction(tmp_path)
        assert _FULL_CONTEXT_PREAMBLE in result
        assert "Adi" in result

    def test_preserves_markdown_structure(self, tmp_path: Path):
        (tmp_path / "people.md").write_text(
            "- **Adi** — close friend\n- **Dr. Patel** — physio\n"
        )
        result = build_full_context_instruction(tmp_path)
        # Markdown markers preserved (unlike whisper prompt) — model reads as glossary.
        assert "**Adi**" in result
        assert "**Dr. Patel**" in result

    def test_no_truncation_for_long_context(self, tmp_path: Path):
        # Build a file far longer than 200 tokens to confirm no truncation occurs.
        names = [f"Person{i}" for i in range(500)]
        long_content = "\n".join(f"- **{n}**" for n in names)
        (tmp_path / "huge.md").write_text(long_content)

        result = build_full_context_instruction(tmp_path)
        # Every name must survive (whisper prompt would truncate).
        for n in names:
            assert n in result

    def test_multiple_files_concatenated(self, tmp_path: Path):
        (tmp_path / "people.md").write_text("- **Adi**")
        (tmp_path / "places.md").write_text("- **Berlin**")
        result = build_full_context_instruction(tmp_path)
        assert "Adi" in result
        assert "Berlin" in result

    def test_anti_hallucination_language_present(self):
        # The preamble must explicitly tell the model not to invent words.
        assert "do not invent" in _FULL_CONTEXT_PREAMBLE.lower()


class TestWhisperPromptForwarding:
    """Verify the OpenAI provider passes the context prompt to the API."""

    def _build_provider(self, context_prompt: str):
        """Construct an OpenAITranscribeProvider with the SDK call layer
        replaced by an in-line fake. Replaces an earlier pattern that
        used ``__new__`` to bypass ``__init__`` and then poked
        ``provider._client`` / ``_model`` / ``_context_prompt`` directly.
        """
        from unittest.mock import patch

        from journal.providers.transcription import OpenAITranscribeProvider

        captured_kwargs: dict = {}

        class _FakeTranscriptions:
            def create(self, **kwargs):
                captured_kwargs.update(kwargs)
                return type("R", (), {"text": "hello", "logprobs": None})()

        class _FakeClient:
            audio = type("A", (), {"transcriptions": _FakeTranscriptions()})()

        with patch(
            "journal.providers.transcription.openai.OpenAI",
            return_value=_FakeClient(),
        ):
            provider = OpenAITranscribeProvider(
                api_key="test-key",
                model="gpt-4o-transcribe",
                confidence_threshold=-0.5,
                context_prompt=context_prompt,
            )
        return provider, captured_kwargs

    def test_prompt_forwarded_when_present(self, monkeypatch):
        provider, captured_kwargs = self._build_provider(
            "Adi Dr. Patel Hampstead Heath",
        )

        provider.transcribe(b"fake audio", "audio/mp3", "en")

        assert captured_kwargs.get("prompt") == "Adi Dr. Patel Hampstead Heath"

    def test_prompt_omitted_when_blank(self):
        provider, captured_kwargs = self._build_provider("")

        provider.transcribe(b"fake audio", "audio/mp3", "en")

        assert "prompt" not in captured_kwargs

    @pytest.fixture(autouse=True)
    def _quiet_logs(self, caplog):
        caplog.set_level("ERROR", logger="journal.providers.transcription")
