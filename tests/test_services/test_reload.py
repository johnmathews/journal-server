"""Tests for the reload helpers in `journal.services.reload`.

These exercise the three operator-triggered reloads (OCR context,
transcription context, mood dimensions). Each test verifies:

1. The relevant attribute on the running service is rebound to a fresh
   object (i.e. the swap actually happened).
2. A pre-reload reference held by an in-flight caller is unaffected
   (i.e. attribute writes do not mutate the old object).
3. After editing the file-backed config, the new object reflects the
   edit while the old object still reflects the pre-edit state.

The helpers operate on a `services` dict shaped like the one built in
`journal.mcp_server._init_services` (the keys we care about here are
`ingestion` and `job_runner`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from journal.config import Config
from journal.providers.ocr import build_ocr_provider
from journal.providers.transcription import build_transcription_provider
from journal.services.reload import (
    reload_entity_casing_exceptions,
    reload_mood_dimensions,
    reload_ocr_provider,
    reload_transcription_provider,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def context_dir(tmp_path: Path) -> Path:
    """A populated OCR context directory. Tests may overwrite or add files."""
    ctx = tmp_path / "ocr-context"
    ctx.mkdir()
    (ctx / "people.md").write_text("Alice Example — close friend\n", encoding="utf-8")
    return ctx


@pytest.fixture
def mood_path(tmp_path: Path) -> Path:
    """A valid two-dimension mood-dimensions TOML file."""
    p = tmp_path / "mood-dimensions.toml"
    p.write_text(
        '[[dimension]]\n'
        'name = "joy_sadness"\n'
        'positive_pole = "joy"\n'
        'negative_pole = "sadness"\n'
        'scale_type = "bipolar"\n'
        'notes = "Score how joyful or sad the entry feels."\n'
        '\n'
        '[[dimension]]\n'
        'name = "agency"\n'
        'positive_pole = "in control"\n'
        'negative_pole = "powerless"\n'
        'scale_type = "bipolar"\n'
        'notes = "Score how agentic the writer feels."\n',
        encoding="utf-8",
    )
    return p


@pytest.fixture
def base_config(tmp_path: Path, context_dir: Path, mood_path: Path) -> Config:
    """A Config that exercises the OCR + transcription + mood paths.

    Uses fake API keys — the providers' constructors store credentials
    but do not make network calls, so this is safe.
    """
    return Config(
        db_path=tmp_path / "test_reload.db",
        anthropic_api_key="test-anthropic-key",
        openai_api_key="test-openai-key",
        ocr_provider="anthropic",
        ocr_dual_pass=False,
        ocr_context_dir=context_dir,
        transcription_provider="openai",
        transcription_model="whisper-1",
        transcription_fallback_enabled=False,
        enable_mood_scoring=True,
        mood_dimensions_path=mood_path,
        secret_key="test-secret-key-for-tokens",
    )


@pytest.fixture
def services(base_config: Config) -> dict[str, Any]:
    """A minimal services dict mirroring the live shape.

    Only the attributes the reload helpers touch are real; the rest is
    stubbed so we can construct without standing up the whole stack.
    """
    ocr = build_ocr_provider(base_config)
    transcription = build_transcription_provider(base_config)

    from journal.providers.mood_scorer import AnthropicMoodScorer
    from journal.services.mood_dimensions import load_mood_dimensions
    from journal.services.mood_scoring import MoodScoringService

    dims = load_mood_dimensions(base_config.mood_dimensions_path)
    repo = MagicMock()
    mood_service = MoodScoringService(
        scorer=AnthropicMoodScorer(
            api_key=base_config.anthropic_api_key,
            model=base_config.mood_scorer_model,
            max_tokens=base_config.mood_scorer_max_tokens,
        ),
        repository=repo,
        dimensions=dims,
    )

    ingestion = MagicMock()
    ingestion._ocr = ocr
    ingestion._transcription = transcription
    ingestion._mood_scoring = mood_service

    job_runner = MagicMock()
    job_runner._mood_scoring = mood_service

    return {"ingestion": ingestion, "job_runner": job_runner}


# ---------------------------------------------------------------------------
# OCR reload
# ---------------------------------------------------------------------------


class TestReloadOcrProvider:
    def test_swaps_reference(self, services: dict[str, Any], base_config: Config) -> None:
        old_ocr = services["ingestion"]._ocr
        reload_ocr_provider(services, base_config)
        assert services["ingestion"]._ocr is not old_ocr

    def test_picks_up_new_context_files(
        self, services: dict[str, Any], base_config: Config, context_dir: Path
    ) -> None:
        old_ocr = services["ingestion"]._ocr
        assert "Alice Example" in old_ocr._system_text

        # Operator edits the context file on disk.
        (context_dir / "people.md").write_text(
            "Bob Newcomer — coworker\n", encoding="utf-8"
        )

        reload_ocr_provider(services, base_config)
        new_ocr = services["ingestion"]._ocr

        # The fresh provider sees the edit.
        assert "Bob Newcomer" in new_ocr._system_text
        assert "Alice Example" not in new_ocr._system_text

        # The pre-reload reference (held by an in-flight request) does
        # not — Python attribute writes don't mutate the old object.
        assert "Alice Example" in old_ocr._system_text
        assert "Bob Newcomer" not in old_ocr._system_text

    def test_returns_summary(self, services: dict[str, Any], base_config: Config) -> None:
        summary = reload_ocr_provider(services, base_config)

        assert summary["reloaded"] == "ocr-context"
        assert summary["provider"] == "anthropic"
        assert summary["dual_pass"] is False
        assert summary["context_files"] == 1
        assert summary["context_chars"] > 0
        assert "reloaded_at" in summary
        # Round-trippable ISO-8601-ish.
        assert "T" in summary["reloaded_at"]

    def test_summary_reflects_dual_pass_setting(
        self, services: dict[str, Any], base_config: Config
    ) -> None:
        cfg = replace(
            base_config,
            ocr_dual_pass=True,
            google_api_key="test-google-key",
        )
        summary = reload_ocr_provider(services, cfg)
        assert summary["dual_pass"] is True


# ---------------------------------------------------------------------------
# Transcription reload
# ---------------------------------------------------------------------------


class TestReloadTranscriptionProvider:
    def test_swaps_reference(self, services: dict[str, Any], base_config: Config) -> None:
        old = services["ingestion"]._transcription
        reload_transcription_provider(services, base_config)
        assert services["ingestion"]._transcription is not old

    def test_picks_up_new_context_files(
        self, services: dict[str, Any], base_config: Config, context_dir: Path
    ) -> None:
        old = services["ingestion"]._transcription
        old_prompt = old._context_prompt
        assert "Alice Example" in old_prompt

        (context_dir / "people.md").write_text(
            "Bob Newcomer — coworker\n", encoding="utf-8"
        )
        reload_transcription_provider(services, base_config)
        new = services["ingestion"]._transcription

        assert "Bob Newcomer" in new._context_prompt
        # In-flight reference is untouched.
        assert "Alice Example" in old._context_prompt

    def test_returns_summary(self, services: dict[str, Any], base_config: Config) -> None:
        summary = reload_transcription_provider(services, base_config)

        assert summary["reloaded"] == "transcription-context"
        assert "stack" in summary
        assert summary["context_files"] == 1
        assert summary["context_chars"] > 0
        assert "reloaded_at" in summary


# ---------------------------------------------------------------------------
# Mood dimensions reload
# ---------------------------------------------------------------------------


class TestReloadMoodDimensions:
    def test_swaps_both_references(
        self, services: dict[str, Any], base_config: Config
    ) -> None:
        old_ingestion = services["ingestion"]._mood_scoring
        old_runner = services["job_runner"]._mood_scoring
        # Sanity: they start as the same instance.
        assert old_ingestion is old_runner

        reload_mood_dimensions(services, base_config)

        new_ingestion = services["ingestion"]._mood_scoring
        new_runner = services["job_runner"]._mood_scoring

        assert new_ingestion is not old_ingestion
        assert new_runner is not old_runner
        # Both services should see the same fresh instance (avoid drift).
        assert new_ingestion is new_runner

    def test_picks_up_new_dimensions(
        self, services: dict[str, Any], base_config: Config, mood_path: Path
    ) -> None:
        old = services["ingestion"]._mood_scoring
        assert {d.name for d in old.dimensions} == {"joy_sadness", "agency"}

        # Operator adds a third dimension.
        mood_path.write_text(
            mood_path.read_text(encoding="utf-8")
            + '\n[[dimension]]\n'
            'name = "energy"\n'
            'positive_pole = "energetic"\n'
            'negative_pole = "drained"\n'
            'scale_type = "bipolar"\n'
            'notes = "Score how energetic the writer feels."\n',
            encoding="utf-8",
        )

        reload_mood_dimensions(services, base_config)
        new = services["ingestion"]._mood_scoring

        assert {d.name for d in new.dimensions} == {
            "joy_sadness",
            "agency",
            "energy",
        }
        # In-flight reference unaffected.
        assert {d.name for d in old.dimensions} == {"joy_sadness", "agency"}

    def test_returns_summary(self, services: dict[str, Any], base_config: Config) -> None:
        summary = reload_mood_dimensions(services, base_config)

        assert summary["reloaded"] == "mood-dimensions"
        assert summary["dimension_count"] == 2
        assert set(summary["dimensions"]) == {"joy_sadness", "agency"}
        assert "reloaded_at" in summary

    def test_raises_when_mood_scoring_disabled(
        self, services: dict[str, Any], base_config: Config
    ) -> None:
        cfg = replace(base_config, enable_mood_scoring=False)
        with pytest.raises(RuntimeError, match="mood scoring is disabled"):
            reload_mood_dimensions(services, cfg)


# ---------------------------------------------------------------------------
# Entity-casing exceptions reload
# ---------------------------------------------------------------------------


@pytest.fixture
def entity_casing_path(tmp_path: Path) -> Path:
    """A small entity-casing exceptions TOML for reload tests."""
    p = tmp_path / "entity-casing-exceptions.toml"
    p.write_text(
        '[meta]\n'
        'version = "test"\n'
        '\n'
        '[exceptions]\n'
        '"ios" = "iOS"\n'
        '"nasa" = "NASA"\n',
        encoding="utf-8",
    )
    return p


class TestReloadEntityCasingExceptions:
    def _build_services(
        self, casing_path: Path, db_path: Path
    ) -> dict[str, Any]:
        """Construct an isolated services dict with a real SQLite-backed store."""
        from journal.db.connection import get_connection
        from journal.db.migrations import run_migrations
        from journal.entitystore.store import SQLiteEntityStore
        from journal.services.entity_naming import (
            load_entity_casing_exceptions,
        )

        conn = get_connection(db_path)
        run_migrations(conn)
        exceptions = load_entity_casing_exceptions(casing_path)
        store = SQLiteEntityStore(conn, casing_exceptions=exceptions)
        return {
            "entity_store": store,
            "entity_casing_exceptions": exceptions,
        }

    def test_swaps_exceptions_table(
        self, entity_casing_path: Path, base_config: Config, tmp_path: Path
    ) -> None:
        cfg = replace(
            base_config, entity_casing_exceptions_path=entity_casing_path
        )
        services = self._build_services(entity_casing_path, tmp_path / "casing-test.db")
        store = services["entity_store"]

        # Sanity: the seeded exceptions are in effect.
        e1 = store.create_entity("topic", "ios", "", "2026-01-01")
        assert e1.canonical_name == "iOS"

        # Operator edits the file to add a new entry (and remove an old one).
        entity_casing_path.write_text(
            '[exceptions]\n'
            '"hp" = "HP"\n',
            encoding="utf-8",
        )
        summary = reload_entity_casing_exceptions(services, cfg)

        # New exception is honoured; old one no longer applied.
        e2 = store.create_entity("organization", "hp", "", "2026-01-01")
        assert e2.canonical_name == "HP"
        e3 = store.create_entity("topic", "ios", "", "2026-01-02")
        # 'iOS' is no longer in the table — but mid-word uppercase fallback
        # only fires for input that has uppercase chars; raw 'ios' falls
        # through to plain title case.
        assert e3.canonical_name == "Ios"

        # Summary reflects new state.
        assert summary["reloaded"] == "entity-casing"
        assert summary["exception_count"] == 1
        assert summary["path"] == str(entity_casing_path)
        assert "reloaded_at" in summary

    def test_returns_summary_for_initial_load(
        self, entity_casing_path: Path, base_config: Config, tmp_path: Path
    ) -> None:
        cfg = replace(
            base_config, entity_casing_exceptions_path=entity_casing_path
        )
        services = self._build_services(entity_casing_path, tmp_path / "casing-test.db")
        summary = reload_entity_casing_exceptions(services, cfg)
        assert summary["reloaded"] == "entity-casing"
        assert summary["exception_count"] == 2
        assert "reloaded_at" in summary

    def test_missing_file_logs_and_returns_empty(
        self, tmp_path: Path, base_config: Config
    ) -> None:
        casing_path = tmp_path / "missing.toml"
        cfg = replace(
            base_config, entity_casing_exceptions_path=casing_path
        )
        # Build services with a nonexistent file path — empty initial table.
        services = self._build_services(
            tmp_path / "nope.toml", tmp_path / "casing-test.db"
        )
        summary = reload_entity_casing_exceptions(services, cfg)
        assert summary["exception_count"] == 0

    def test_services_dict_updated(
        self, entity_casing_path: Path, base_config: Config, tmp_path: Path
    ) -> None:
        cfg = replace(
            base_config, entity_casing_exceptions_path=entity_casing_path
        )
        services = self._build_services(entity_casing_path, tmp_path / "casing-test.db")
        # Edit and reload.
        entity_casing_path.write_text(
            '[exceptions]\n'
            '"klm" = "KLM"\n',
            encoding="utf-8",
        )
        reload_entity_casing_exceptions(services, cfg)
        assert services["entity_casing_exceptions"] == {"klm": "KLM"}
