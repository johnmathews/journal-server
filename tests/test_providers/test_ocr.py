"""Tests for OCR providers (Anthropic, Gemini) and the factory."""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from journal.providers.ocr import (
    _DEFAULT_MODELS,
    CONTEXT_USAGE_INSTRUCTIONS,
    SYSTEM_PROMPT,
    UNCERTAIN_CLOSE,
    UNCERTAIN_OPEN,
    AnthropicOCRProvider,
    GeminiOCRProvider,
    OCRProvider,
    OCRResult,
    _build_cache_control,
    build_ocr_provider,
    load_context_files,
    parse_uncertain_markers,
)


class TestAnthropicOCRProvider:
    """Tests for AnthropicOCRProvider."""

    def _make_provider(
        self,
        context_dir: Path | None = None,
        cache_ttl: str = "5m",
    ) -> AnthropicOCRProvider:
        with patch("journal.providers.ocr.anthropic.Anthropic"):
            provider = AnthropicOCRProvider(
                api_key="test-key",
                model="claude-opus-4-6",
                max_tokens=4096,
                context_dir=context_dir,
                cache_ttl=cache_ttl,
            )
        return provider

    def test_implements_protocol(self) -> None:
        provider = self._make_provider()
        assert isinstance(provider, OCRProvider)

    def test_extract_returns_ocr_result(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Hello world from handwriting")]
        provider._client.messages.create.return_value = mock_message

        result = provider.extract(b"fake-image-data", "image/png")

        assert isinstance(result, OCRResult)
        assert result.text == "Hello world from handwriting"
        assert result.uncertain_spans == []
        provider._client.messages.create.assert_called_once()

    def test_extract_strips_sentinels_and_records_spans(self) -> None:
        provider = self._make_provider()
        raw = f"Today I met {UNCERTAIN_OPEN}Ritsya{UNCERTAIN_CLOSE} at the park."
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=raw)]
        provider._client.messages.create.return_value = mock_message

        result = provider.extract(b"data", "image/png")

        assert result.text == "Today I met Ritsya at the park."
        assert result.uncertain_spans == [(12, 18)]
        # Clean text must not contain sentinel characters.
        assert UNCERTAIN_OPEN not in result.text
        assert UNCERTAIN_CLOSE not in result.text

    def test_extract_text_wrapper_returns_clean_string(self) -> None:
        provider = self._make_provider()
        raw = f"plain {UNCERTAIN_OPEN}foo{UNCERTAIN_CLOSE} bar"
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=raw)]
        provider._client.messages.create.return_value = mock_message

        result = provider.extract_text(b"fake-image-data", "image/png")

        assert result == "plain foo bar"
        assert UNCERTAIN_OPEN not in result
        assert UNCERTAIN_CLOSE not in result

    def test_system_prompt_included_without_context(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message

        provider.extract_text(b"fake-image-data", "image/jpeg")

        call_kwargs = provider._client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert len(system) == 1
        # Without a context dir, the system block is the unchanged
        # SYSTEM_PROMPT — no glossary instructions appended.
        assert system[0]["text"] == SYSTEM_PROMPT
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_prompt_instructs_sentinel_usage(self) -> None:
        """SYSTEM_PROMPT must tell the model to wrap uncertain words
        in ⟪/⟫ sentinels — that instruction is what powers the whole
        uncertainty-tracking feature, so a regression here would
        silently disable it."""
        assert UNCERTAIN_OPEN in SYSTEM_PROMPT
        assert UNCERTAIN_CLOSE in SYSTEM_PROMPT
        # Smell-test a couple of phrases to catch accidental deletes.
        assert "uncertain" in SYSTEM_PROMPT.lower()
        assert "sparingly" in SYSTEM_PROMPT.lower()

    def test_image_is_base64_encoded(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message

        image_data = b"fake-image-data"
        provider.extract_text(image_data, "image/png")

        call_kwargs = provider._client.messages.create.call_args.kwargs
        messages = call_kwargs["messages"]
        image_block = messages[0]["content"][0]
        expected_b64 = base64.standard_b64encode(image_data).decode("utf-8")
        assert image_block["source"]["data"] == expected_b64
        assert image_block["source"]["media_type"] == "image/png"

    def test_context_dir_composes_into_system_text(
        self, tmp_path: Path
    ) -> None:
        context = tmp_path / "context"
        context.mkdir()
        (context / "people.md").write_text(
            "- Ritsya — daughter\n- Atlas — dog\n"
        )
        (context / "places.md").write_text(
            "- Vienna — first met Atlas here\n"
        )

        provider = self._make_provider(context_dir=context)
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message

        provider.extract_text(b"data", "image/png")

        system_text = (
            provider._client.messages.create.call_args.kwargs["system"][0]["text"]
        )
        # Start matches the original prompt.
        assert system_text.startswith(SYSTEM_PROMPT)
        # Hallucination-prevention instructions come next.
        assert CONTEXT_USAGE_INSTRUCTIONS.strip() in system_text
        # Both context files are present, in alphabetical order.
        people_idx = system_text.find("people")
        places_idx = system_text.find("places")
        assert people_idx != -1 and places_idx != -1
        assert people_idx < places_idx
        # Content from the files is verbatim in the system text.
        assert "Ritsya" in system_text
        assert "Atlas" in system_text
        assert "Vienna" in system_text

    def test_context_dir_missing_falls_back_to_system_prompt(
        self, tmp_path: Path
    ) -> None:
        # Point at a dir that doesn't exist — provider must fall back.
        missing = tmp_path / "nope"
        provider = self._make_provider(context_dir=missing)
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message
        provider.extract_text(b"data", "image/png")

        system_text = (
            provider._client.messages.create.call_args.kwargs["system"][0]["text"]
        )
        assert system_text == SYSTEM_PROMPT

    def test_context_dir_empty_falls_back_to_system_prompt(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        provider = self._make_provider(context_dir=empty)
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message
        provider.extract_text(b"data", "image/png")

        system_text = (
            provider._client.messages.create.call_args.kwargs["system"][0]["text"]
        )
        assert system_text == SYSTEM_PROMPT

    def test_cache_ttl_1h(self) -> None:
        provider = self._make_provider(cache_ttl="1h")
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message
        provider.extract_text(b"data", "image/png")

        cache_control = (
            provider._client.messages.create.call_args.kwargs["system"][0][
                "cache_control"
            ]
        )
        assert cache_control == {"type": "ephemeral", "ttl": "1h"}

    def test_invalid_cache_ttl_raises(self) -> None:
        with (
            patch("journal.providers.ocr.anthropic.Anthropic"),
            pytest.raises(ValueError, match="Invalid OCR context cache TTL"),
        ):
            AnthropicOCRProvider(
                api_key="test-key",
                model="claude-opus-4-6",
                max_tokens=4096,
                cache_ttl="30m",
            )

    def test_small_context_logs_cache_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A small glossary is well under the 4096-token cache minimum —
        # the provider should log a warning on init.
        context = tmp_path / "context"
        context.mkdir()
        (context / "people.md").write_text("- Ritsya\n")

        with caplog.at_level("WARNING", logger="journal.providers.ocr"):
            self._make_provider(context_dir=context)

        messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("cache minimum" in m for m in messages), (
            f"expected a cache-minimum warning, got: {messages}"
        )


class TestLoadContextFiles:
    def test_none_returns_empty(self) -> None:
        assert load_context_files(None) == ""

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_context_files(tmp_path / "missing") == ""

    def test_not_a_directory_returns_empty(self, tmp_path: Path) -> None:
        # A file, not a directory.
        p = tmp_path / "not-a-dir.md"
        p.write_text("content")
        assert load_context_files(p) == ""

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_context_files(empty) == ""

    def test_alphabetical_order_and_headers(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "zebra.md").write_text("z content")
        (d / "apple.md").write_text("a content")
        result = load_context_files(d)
        # Headers derived from filename stems.
        assert "# apple" in result
        assert "# zebra" in result
        # Alphabetical: apple before zebra.
        assert result.find("# apple") < result.find("# zebra")

    def test_underscores_and_dashes_become_spaces_in_heading(
        self, tmp_path: Path
    ) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "work_topics.md").write_text("a")
        (d / "family-names.md").write_text("b")
        result = load_context_files(d)
        assert "# work topics" in result
        assert "# family names" in result

    def test_empty_file_is_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "empty.md").write_text("")
        (d / "real.md").write_text("content")
        result = load_context_files(d)
        assert "# empty" not in result
        assert "# real" in result

    def test_non_md_files_ignored(self, tmp_path: Path) -> None:
        d = tmp_path / "ctx"
        d.mkdir()
        (d / "notes.txt").write_text("should be ignored")
        (d / "glossary.md").write_text("should be included")
        result = load_context_files(d)
        assert "should be ignored" not in result
        assert "should be included" in result


class TestBuildCacheControl:
    def test_5m_default(self) -> None:
        assert _build_cache_control("5m") == {"type": "ephemeral"}

    def test_1h_adds_ttl(self) -> None:
        assert _build_cache_control("1h") == {
            "type": "ephemeral",
            "ttl": "1h",
        }

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid OCR context cache TTL"):
            _build_cache_control("10m")


class TestParseUncertainMarkers:
    """Exhaustive coverage of the sentinel parser.

    Every behaviour documented in `parse_uncertain_markers`'s docstring
    has a test here. Anything we don't cover is a regression waiting to
    happen — the parser runs on every OCR response and if it silently
    misbehaves the webapp quietly stops highlighting things.
    """

    def _wrap(self, inner: str) -> str:
        return f"{UNCERTAIN_OPEN}{inner}{UNCERTAIN_CLOSE}"

    def test_plain_text_no_markers(self) -> None:
        clean, spans = parse_uncertain_markers("hello world")
        assert clean == "hello world"
        assert spans == []

    def test_empty_string(self) -> None:
        assert parse_uncertain_markers("") == ("", [])

    def test_single_word_span(self) -> None:
        raw = f"hello {self._wrap('world')}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "hello world"
        assert spans == [(6, 11)]

    def test_span_at_start(self) -> None:
        raw = f"{self._wrap('foo')} bar"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foo bar"
        assert spans == [(0, 3)]

    def test_span_at_end(self) -> None:
        raw = f"foo {self._wrap('bar')}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foo bar"
        assert spans == [(4, 7)]

    def test_multi_word_phrase_span(self) -> None:
        raw = f"I met {self._wrap('the dog')} today"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "I met the dog today"
        assert spans == [(6, 13)]

    def test_multiple_disjoint_spans(self) -> None:
        raw = f"{self._wrap('foo')} plain {self._wrap('bar baz')} end"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foo plain bar baz end"
        assert spans == [(0, 3), (10, 17)]

    def test_adjacent_spans(self) -> None:
        raw = f"{self._wrap('foo')}{self._wrap('bar')}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foobar"
        assert spans == [(0, 3), (3, 6)]

    def test_unmatched_open_is_dropped(self) -> None:
        raw = f"good {UNCERTAIN_OPEN}lost forever"
        clean, spans = parse_uncertain_markers(raw)
        # Text is preserved exactly — only the sentinel is dropped.
        assert clean == "good lost forever"
        assert spans == []

    def test_unmatched_close_is_dropped(self) -> None:
        raw = f"good {UNCERTAIN_CLOSE}still good"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "good still good"
        assert spans == []

    def test_nested_sentinels_collapse_to_outer(self) -> None:
        raw = f"{UNCERTAIN_OPEN}foo {UNCERTAIN_OPEN}bar{UNCERTAIN_CLOSE} baz{UNCERTAIN_CLOSE}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foo bar baz"
        assert spans == [(0, 11)]

    def test_empty_pair_is_dropped(self) -> None:
        raw = f"before {UNCERTAIN_OPEN}{UNCERTAIN_CLOSE} after"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "before  after"
        assert spans == []

    def test_whitespace_only_pair_is_dropped(self) -> None:
        raw = f"x {UNCERTAIN_OPEN}   {UNCERTAIN_CLOSE} y"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "x     y"
        assert spans == []

    def test_inner_whitespace_trimmed_from_span(self) -> None:
        raw = f"a {UNCERTAIN_OPEN}  foo  {UNCERTAIN_CLOSE} b"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "a   foo   b"
        # Span covers only "foo" — whitespace padding is trimmed out.
        (start, end), = spans
        assert clean[start:end] == "foo"

    def test_whitespace_inside_phrase_preserved(self) -> None:
        # Whitespace *between* non-space chars inside the span is part
        # of the span. Only leading/trailing whitespace is trimmed.
        raw = f"{UNCERTAIN_OPEN}foo bar{UNCERTAIN_CLOSE}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "foo bar"
        assert spans == [(0, 7)]

    def test_unicode_characters_counted_correctly(self) -> None:
        # Accented characters are single code points; spans are in
        # code-point (not byte) offsets.
        raw = f"caf{UNCERTAIN_OPEN}é{UNCERTAIN_CLOSE}"
        clean, spans = parse_uncertain_markers(raw)
        assert clean == "café"
        assert spans == [(3, 4)]

    def test_parser_never_raises_on_malformed_input(self) -> None:
        # Property-ish: any combination of sentinels and text should
        # parse without an exception.
        malformed = (
            f"{UNCERTAIN_CLOSE}{UNCERTAIN_OPEN}"
            f"{UNCERTAIN_OPEN}{UNCERTAIN_OPEN}"
            f"hello{UNCERTAIN_CLOSE}"
            f"{UNCERTAIN_OPEN}  {UNCERTAIN_CLOSE}"
            f"{UNCERTAIN_OPEN}"  # dangling
        )
        clean, spans = parse_uncertain_markers(malformed)
        # We don't assert the exact output, only that the parser
        # returns without exploding and emits valid Python values.
        assert isinstance(clean, str)
        assert isinstance(spans, list)
        assert all(
            isinstance(s, tuple) and len(s) == 2 and s[1] > s[0]
            for s in spans
        )

    def test_drops_are_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="journal.providers.ocr"):
            parse_uncertain_markers(f"bad {UNCERTAIN_CLOSE} close")
        warnings = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("sentinel parser dropped" in m for m in warnings)


class TestGeminiOCRProvider:
    """Tests for GeminiOCRProvider."""

    def _make_provider(
        self,
        context_dir: Path | None = None,
    ) -> GeminiOCRProvider:
        with patch("journal.providers.ocr.genai.Client"):
            provider = GeminiOCRProvider(
                api_key="test-google-key",
                model="gemini-2.5-pro",
                context_dir=context_dir,
            )
        return provider

    def test_implements_protocol(self) -> None:
        provider = self._make_provider()
        assert isinstance(provider, OCRProvider)

    def test_extract_returns_ocr_result(self) -> None:
        provider = self._make_provider()
        mock_response = MagicMock()
        mock_response.text = "Hello world from handwriting"
        provider._client.models.generate_content.return_value = mock_response

        result = provider.extract(b"fake-image-data", "image/png")

        assert isinstance(result, OCRResult)
        assert result.text == "Hello world from handwriting"
        assert result.uncertain_spans == []
        provider._client.models.generate_content.assert_called_once()

    def test_extract_strips_sentinels_and_records_spans(self) -> None:
        provider = self._make_provider()
        raw = f"Today I met {UNCERTAIN_OPEN}Ritsya{UNCERTAIN_CLOSE} at the park."
        mock_response = MagicMock()
        mock_response.text = raw
        provider._client.models.generate_content.return_value = mock_response

        result = provider.extract(b"data", "image/png")

        assert result.text == "Today I met Ritsya at the park."
        assert result.uncertain_spans == [(12, 18)]

    def test_system_prompt_passed_to_gemini(self) -> None:
        provider = self._make_provider()
        mock_response = MagicMock()
        mock_response.text = "extracted"
        provider._client.models.generate_content.return_value = mock_response

        provider.extract(b"data", "image/jpeg")

        call_kwargs = provider._client.models.generate_content.call_args.kwargs
        assert call_kwargs["config"].system_instruction == SYSTEM_PROMPT

    def test_extract_text_wrapper(self) -> None:
        provider = self._make_provider()
        mock_response = MagicMock()
        mock_response.text = "plain text"
        provider._client.models.generate_content.return_value = mock_response

        assert provider.extract_text(b"data", "image/png") == "plain text"

    def test_context_dir_composes_into_system_text(
        self, tmp_path: Path
    ) -> None:
        context = tmp_path / "context"
        context.mkdir()
        (context / "people.md").write_text(
            "- Ritsya — daughter\n- Atlas — dog\n"
        )
        (context / "places.md").write_text(
            "- Vienna — first met Atlas here\n"
        )

        provider = self._make_provider(context_dir=context)
        mock_response = MagicMock()
        mock_response.text = "extracted"
        provider._client.models.generate_content.return_value = mock_response

        provider.extract(b"data", "image/png")

        call_kwargs = provider._client.models.generate_content.call_args.kwargs
        system_text = call_kwargs["config"].system_instruction
        assert system_text.startswith(SYSTEM_PROMPT)
        assert CONTEXT_USAGE_INSTRUCTIONS.strip() in system_text
        # Both context files present, in alphabetical order.
        people_idx = system_text.find("people")
        places_idx = system_text.find("places")
        assert people_idx != -1 and places_idx != -1
        assert people_idx < places_idx
        # Content from the files is verbatim.
        assert "Ritsya" in system_text
        assert "Atlas" in system_text
        assert "Vienna" in system_text

    def test_context_dir_missing_falls_back_to_system_prompt(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope"
        provider = self._make_provider(context_dir=missing)
        mock_response = MagicMock()
        mock_response.text = "extracted"
        provider._client.models.generate_content.return_value = mock_response
        provider.extract(b"data", "image/png")

        call_kwargs = provider._client.models.generate_content.call_args.kwargs
        assert call_kwargs["config"].system_instruction == SYSTEM_PROMPT

    def test_context_dir_empty_falls_back_to_system_prompt(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        provider = self._make_provider(context_dir=empty)
        mock_response = MagicMock()
        mock_response.text = "extracted"
        provider._client.models.generate_content.return_value = mock_response
        provider.extract(b"data", "image/png")

        call_kwargs = provider._client.models.generate_content.call_args.kwargs
        assert call_kwargs["config"].system_instruction == SYSTEM_PROMPT


class TestBuildOcrProvider:
    """Tests for the build_ocr_provider factory."""

    def _make_config(self, **overrides: str) -> MagicMock:
        defaults = {
            "ocr_provider": "anthropic",
            "anthropic_api_key": "test-anthropic-key",
            "google_api_key": "test-google-key",
            "ocr_model": "",
            "ocr_max_tokens": 4096,
            "ocr_context_dir": None,
            "ocr_context_cache_ttl": "5m",
        }
        defaults.update(overrides)
        config = MagicMock()
        for k, v in defaults.items():
            setattr(config, k, v)
        return config

    @patch("journal.providers.ocr.anthropic.Anthropic")
    def test_builds_anthropic_provider(self, _mock_anthropic: MagicMock) -> None:
        config = self._make_config(ocr_provider="anthropic")
        provider = build_ocr_provider(config)
        assert isinstance(provider, AnthropicOCRProvider)

    @patch("journal.providers.ocr.genai.Client")
    def test_builds_gemini_provider(self, _mock_genai: MagicMock) -> None:
        config = self._make_config(ocr_provider="gemini")
        provider = build_ocr_provider(config)
        assert isinstance(provider, GeminiOCRProvider)

    @patch("journal.providers.ocr.genai.Client")
    def test_builds_gemini_with_context_dir(
        self, _mock_genai: MagicMock, tmp_path: Path
    ) -> None:
        context = tmp_path / "context"
        context.mkdir()
        (context / "people.md").write_text("- Ritsya\n")
        config = self._make_config(ocr_provider="gemini")
        config.ocr_context_dir = context
        provider = build_ocr_provider(config)
        assert isinstance(provider, GeminiOCRProvider)
        assert "Ritsya" in provider._system_text

    @patch("journal.providers.ocr.anthropic.Anthropic")
    def test_default_model_anthropic(self, _mock: MagicMock) -> None:
        config = self._make_config(ocr_provider="anthropic", ocr_model="")
        provider = build_ocr_provider(config)
        assert provider._model == _DEFAULT_MODELS["anthropic"]

    @patch("journal.providers.ocr.genai.Client")
    def test_default_model_gemini(self, _mock: MagicMock) -> None:
        config = self._make_config(ocr_provider="gemini", ocr_model="")
        provider = build_ocr_provider(config)
        assert provider._model == _DEFAULT_MODELS["gemini"]

    @patch("journal.providers.ocr.anthropic.Anthropic")
    def test_explicit_model_override(self, _mock: MagicMock) -> None:
        config = self._make_config(
            ocr_provider="anthropic", ocr_model="claude-sonnet-4-5"
        )
        provider = build_ocr_provider(config)
        assert provider._model == "claude-sonnet-4-5"

    def test_unknown_provider_raises(self) -> None:
        config = self._make_config(ocr_provider="openai")
        with pytest.raises(ValueError, match="Unknown OCR provider"):
            build_ocr_provider(config)
