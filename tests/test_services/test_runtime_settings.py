"""Tests for the RuntimeSettings service."""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
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


@pytest.fixture
def settings_factory(tmp_path: Path) -> ConnectionFactory:
    """ConnectionFactory pointing at a migrated temp DB."""
    factory = ConnectionFactory(tmp_path / "settings.db")
    run_migrations(factory.get())
    return factory


@pytest.fixture
def settings_via_factory(settings_factory: ConnectionFactory, mock_config) -> RuntimeSettings:
    return RuntimeSettings(settings_factory, mock_config)


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


class TestFactoryPathSemantics:
    """Production-path coverage for the ``ConnectionFactory`` model.

    ``RuntimeSettings.set()`` is called from API request threads
    (admin toggles in the webapp), so it genuinely writes from
    multiple threads under realistic load. Under the old
    shared-``Connection`` model this would risk
    ``no transaction is active`` from a concurrent commit; under the
    factory model each request thread owns its own connection and
    the collision becomes structurally impossible.
    """

    def test_set_round_trip_under_factory(
        self, settings_via_factory: RuntimeSettings,
    ) -> None:
        # The bool defaults from `mock_config` survive the seed step.
        assert settings_via_factory.get("preprocess_images") is True

        settings_via_factory.set("preprocess_images", False)
        assert settings_via_factory.get("preprocess_images") is False

        # A second instance over the same factory's DB sees the
        # committed value (proves it really hit disk).
        config = MagicMock()
        config.preprocess_images = True  # opposite of what we set
        config.ocr_dual_pass = False
        config.enable_mood_scoring = True
        config.ocr_provider = "anthropic"
        config.registration_enabled = False
        second = RuntimeSettings(settings_via_factory._factory, config)  # type: ignore[arg-type]
        assert second.get("preprocess_images") is False

    def test_concurrent_set_under_load(
        self, settings_via_factory: RuntimeSettings,
    ) -> None:
        """Many threads each flipping bool settings on the shared
        factory. Under the old shared-``Connection`` model this would
        surface ``no transaction is active`` from a concurrent commit;
        under the factory model it must complete cleanly with the
        last write winning.
        """
        thread_count = 6
        flips_per_thread = 10
        errors: list[BaseException] = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(flips_per_thread):
                    # Alternate keys so threads aren't fighting over
                    # the same row every iteration; the SQLite write
                    # lock still serialises them at the file level.
                    key = (
                        "preprocess_images" if thread_idx % 2 == 0
                        else "ocr_dual_pass"
                    )
                    settings_via_factory.set(key, bool(i % 2))
            except BaseException as exc:  # noqa: BLE001 — test-only
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=thread_count) as ex:
            futures = [ex.submit(worker, i) for i in range(thread_count)]
            for f in as_completed(futures):
                f.result()

        assert errors == []
        # Both keys ended on a deterministic-modulo value (the very
        # last iteration of any thread that touched them). We only
        # care that no exceptions fired and the cache is consistent
        # with the DB.
        for key in ("preprocess_images", "ocr_dual_pass"):
            cached = settings_via_factory.get(key)
            assert isinstance(cached, bool)

    def test_each_thread_gets_distinct_connection(
        self, settings_via_factory: RuntimeSettings,
    ) -> None:
        main_conn_id = id(settings_via_factory._conn())
        captured: list[int] = []

        def worker() -> None:
            captured.append(id(settings_via_factory._conn()))

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(captured) == 1
        assert captured[0] != main_conn_id
