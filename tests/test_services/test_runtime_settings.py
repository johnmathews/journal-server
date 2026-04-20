"""Tests for the RuntimeSettings service."""

from unittest.mock import MagicMock

import pytest

from journal.services.runtime_settings import (
    SETTING_DEFS_BY_KEY,
    RuntimeSettings,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.preprocess_images = True
    config.ocr_dual_pass = False
    config.enable_mood_scoring = True
    config.ocr_provider = "anthropic"
    config.registration_enabled = False
    return config


@pytest.fixture
def settings(db_conn, mock_config):
    return RuntimeSettings(db_conn, mock_config)


class TestLoad:
    def test_seeds_from_config_defaults(self, settings, mock_config):
        assert settings.get("preprocess_images") is True
        assert settings.get("ocr_dual_pass") is False
        assert settings.get("enable_mood_scoring") is True
        assert settings.get("ocr_provider") == "anthropic"
        assert settings.get("registration_enabled") is False

    def test_persists_seeds_to_db(self, db_conn, settings):
        rows = db_conn.execute("SELECT key FROM runtime_settings").fetchall()
        keys = {r[0] for r in rows}
        assert "preprocess_images" in keys
        assert "ocr_dual_pass" in keys

    def test_loads_existing_db_values_over_config(self, db_conn, mock_config):
        """If DB already has a value, it takes precedence over Config."""
        db_conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) "
            "VALUES ('ocr_dual_pass', 'true', '2026-01-01T00:00:00Z')"
        )
        db_conn.commit()
        s = RuntimeSettings(db_conn, mock_config)
        assert s.get("ocr_dual_pass") is True  # DB says true, Config says false


class TestGetSet:
    def test_get_unknown_key_raises(self, settings):
        with pytest.raises(KeyError, match="Unknown"):
            settings.get("nonexistent")

    def test_set_and_get(self, settings):
        settings.set("ocr_dual_pass", True)
        assert settings.get("ocr_dual_pass") is True

    def test_set_persists_to_db(self, db_conn, settings):
        settings.set("preprocess_images", False)
        row = db_conn.execute(
            "SELECT value FROM runtime_settings WHERE key = 'preprocess_images'"
        ).fetchone()
        assert row[0] == "false"

    def test_set_unknown_key_raises(self, settings):
        with pytest.raises(KeyError, match="Unknown"):
            settings.set("bogus", True)

    def test_set_bool_rejects_non_bool(self, settings):
        with pytest.raises(ValueError, match="boolean"):
            settings.set("ocr_dual_pass", "yes")

    def test_set_string_rejects_non_string(self, settings):
        with pytest.raises(ValueError, match="string"):
            settings.set("ocr_provider", 42)

    def test_set_string_rejects_invalid_choice(self, settings):
        with pytest.raises(ValueError, match="must be one of"):
            settings.set("ocr_provider", "openai")

    def test_set_string_accepts_valid_choice(self, settings):
        settings.set("ocr_provider", "gemini")
        assert settings.get("ocr_provider") == "gemini"


class TestOnChange:
    def test_callback_fires_on_change(self, db_conn, mock_config):
        callback = MagicMock()
        s = RuntimeSettings(db_conn, mock_config, on_change=callback)
        s.set("ocr_dual_pass", True)
        callback.assert_called_once_with("ocr_dual_pass", True)

    def test_callback_not_fired_when_value_unchanged(self, db_conn, mock_config):
        callback = MagicMock()
        s = RuntimeSettings(db_conn, mock_config, on_change=callback)
        s.set("preprocess_images", True)  # same as default
        callback.assert_not_called()

    def test_no_callback_no_error(self, db_conn, mock_config):
        s = RuntimeSettings(db_conn, mock_config, on_change=None)
        s.set("ocr_dual_pass", True)  # should not raise


class TestGetAll:
    def test_returns_all_settings(self, settings):
        result = settings.get_all()
        keys = {s["key"] for s in result}
        for sdef in SETTING_DEFS_BY_KEY.values():
            assert sdef.key in keys

    def test_includes_metadata(self, settings):
        result = settings.get_all()
        by_key = {s["key"]: s for s in result}
        ocr = by_key["ocr_provider"]
        assert ocr["type"] == "string"
        assert ocr["label"] == "OCR Provider"
        assert "choices" in ocr
        assert "anthropic" in ocr["choices"]

    def test_bool_setting_has_no_choices(self, settings):
        result = settings.get_all()
        by_key = {s["key"]: s for s in result}
        assert "choices" not in by_key["preprocess_images"]
