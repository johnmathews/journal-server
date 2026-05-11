"""Runtime settings — editable from the webapp without container restart.

Persisted in SQLite ``runtime_settings`` table.  On startup, missing keys
are populated from the frozen ``Config`` (env-var defaults).  Subsequent
reads come from an in-memory cache; writes go to both cache and DB.

Each setting has a *definition* that declares its type, label,
description, and (optionally) allowed values.  The definition list is
the single source of truth for what is editable at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from journal.db.factory import ConnectionFactory

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Setting definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # "bool" | "string"
    label: str
    description: str
    config_attr: str  # attribute name on the Config dataclass
    choices: list[str] | None = None  # for string-type settings


SETTING_DEFS: list[SettingDef] = [
    SettingDef(
        key="preprocess_images",
        type="bool",
        label="Image Preprocessing",
        description="Auto-rotate, crop to text area, downscale, and enhance contrast before OCR.",
        config_attr="preprocess_images",
    ),
    SettingDef(
        key="ocr_dual_pass",
        type="bool",
        label="Dual-Pass OCR",
        description="Run both Anthropic and Gemini on each page; flag disagreements as doubts.",
        config_attr="ocr_dual_pass",
    ),
    SettingDef(
        key="enable_mood_scoring",
        type="bool",
        label="Mood Scoring",
        description="Score mood dimensions for each ingested entry.",
        config_attr="enable_mood_scoring",
    ),
    SettingDef(
        key="ocr_provider",
        type="string",
        label="OCR Provider",
        description="Primary OCR provider (used in single-pass mode).",
        config_attr="ocr_provider",
        choices=["anthropic", "gemini"],
    ),
    SettingDef(
        key="registration_enabled",
        type="bool",
        label="User Registration",
        description="Allow new user sign-ups.",
        config_attr="registration_enabled",
    ),
    SettingDef(
        key="transcript_formatting",
        type="bool",
        label="Transcript Paragraph Formatting",
        description="Use LLM to add paragraph breaks to voice transcriptions.",
        config_attr="transcript_formatting",
    ),
    SettingDef(
        key="date_heading_detection",
        type="bool",
        label="Date Heading Detection",
        description=(
            "Lift a leading date in voice transcripts and OCR text into a "
            "markdown heading on the displayed entry text. Original raw "
            "text is preserved."
        ),
        config_attr="date_heading_detection",
    ),
    SettingDef(
        key="transcription_context_enabled",
        type="bool",
        label="Voice Transcription Context",
        description=(
            "Pass the OCR context files (people, places, glossary) to "
            "Whisper as a prompt to improve spelling of proper nouns in "
            "voice transcripts. Requires server restart to pick up edits "
            "to the context files."
        ),
        config_attr="transcription_context_enabled",
    ),
]

SETTING_DEFS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in SETTING_DEFS}


# ---------------------------------------------------------------------------
# RuntimeSettings
# ---------------------------------------------------------------------------


def _serialize(value: Any, sdef: SettingDef) -> str:
    if sdef.type == "bool":
        return "true" if value else "false"
    return str(value)


def _deserialize(raw: str, sdef: SettingDef) -> Any:
    if sdef.type == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


class RuntimeSettings:
    """In-memory + SQLite-backed runtime settings with change callbacks.

    Construction takes a :class:`ConnectionFactory` (used by production
    via ``mcp_server/bootstrap.py``). ``set()`` is called from API
    request threads (admin toggles in the webapp) and writes to the DB
    on every call; each request thread gets its own
    ``sqlite3.Connection`` from the factory so cross-thread
    implicit-transaction collisions are structurally impossible.
    """

    def __init__(
        self,
        factory: ConnectionFactory,
        config: Any,
        on_change: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._factory = factory
        self._config = config
        self._on_change = on_change
        self._cache: dict[str, Any] = {}
        self._load()

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    def _load(self) -> None:
        """Populate cache from DB, seeding from Config for missing keys."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT key, value FROM runtime_settings"
        ).fetchall()
        db_values = {row[0]: row[1] for row in rows}

        for sdef in SETTING_DEFS:
            if sdef.key in db_values:
                self._cache[sdef.key] = _deserialize(db_values[sdef.key], sdef)
            else:
                default = getattr(self._config, sdef.config_attr)
                self._cache[sdef.key] = default
                # Seed the DB so the value is visible even if never changed.
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_settings (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (sdef.key, _serialize(default, sdef), _now_iso()),
                )
        conn.commit()
        log.info("Runtime settings loaded: %s", self._cache)

    def get(self, key: str) -> Any:
        """Return the current value for *key*. Raises KeyError if unknown."""
        if key not in SETTING_DEFS_BY_KEY:
            raise KeyError(f"Unknown runtime setting: {key!r}")
        return self._cache[key]

    def set(self, key: str, value: Any) -> None:
        """Persist *value* for *key* and trigger side-effects."""
        sdef = SETTING_DEFS_BY_KEY.get(key)
        if sdef is None:
            raise KeyError(f"Unknown runtime setting: {key!r}")

        # Validate
        if sdef.type == "bool" and not isinstance(value, bool):
            raise ValueError(f"Setting {key!r} requires a boolean, got {type(value).__name__}")
        if sdef.type == "string" and not isinstance(value, str):
            raise ValueError(f"Setting {key!r} requires a string, got {type(value).__name__}")
        if sdef.choices and value not in sdef.choices:
            raise ValueError(
                f"Setting {key!r} must be one of {sdef.choices}, got {value!r}"
            )

        old = self._cache.get(key)
        self._cache[key] = value
        conn = self._conn()
        conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, _serialize(value, sdef), _now_iso()),
        )
        conn.commit()
        log.info("Runtime setting %s changed: %r → %r", key, old, value)

        if self._on_change and old != value:
            self._on_change(key, value)

    def get_all(self) -> list[dict[str, Any]]:
        """Return all editable settings with metadata for the API."""
        result = []
        for sdef in SETTING_DEFS:
            entry: dict[str, Any] = {
                "key": sdef.key,
                "type": sdef.type,
                "label": sdef.label,
                "description": sdef.description,
                "value": self._cache[sdef.key],
            }
            if sdef.choices:
                entry["choices"] = sdef.choices
            result.append(entry)
        return result


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
