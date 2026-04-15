"""Tests for CLI interface."""

import sys

import pytest

from journal.cli import main


def test_cli_help(capsys):
    """Test that CLI shows help without errors."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "Journal Analysis Tool" in captured.out


def test_cli_requires_command(capsys):
    """Test that CLI requires a subcommand."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal"]
        main()
    assert exc_info.value.code != 0


def test_cli_ingest_multi_help(capsys):
    """Test that ingest-multi subcommand shows help."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "ingest-multi", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "ingest-multi" in captured.out
    assert "files" in captured.out
    assert "--date" in captured.out


def test_cli_backfill_chunks_help(capsys):
    """Test that backfill-chunks subcommand shows help."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "backfill-chunks", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "backfill-chunks" in captured.out


def test_cli_rechunk_help(capsys):
    """Test that rechunk subcommand shows help with the --dry-run flag."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "rechunk", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "rechunk" in captured.out
    assert "--dry-run" in captured.out


def test_cli_eval_chunking_help(capsys):
    """Test that eval-chunking subcommand shows help with the --json flag."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "eval-chunking", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "eval-chunking" in captured.out
    assert "--json" in captured.out


def test_cli_all_commands_registered(capsys):
    """Test that all expected commands appear in help output."""
    with pytest.raises(SystemExit):
        sys.argv = ["journal", "--help"]
        main()
    captured = capsys.readouterr()
    for cmd in (
        "ingest",
        "ingest-multi",
        "search",
        "list",
        "stats",
        "health",
        "backfill-chunks",
        "backfill-mood",
        "rechunk",
        "eval-chunking",
    ):
        assert cmd in captured.out, f"Command '{cmd}' not found in help output"


def test_cli_health_help(capsys):
    """`journal health --help` documents the --compact flag."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "health", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "health" in captured.out
    assert "--compact" in captured.out


def test_cmd_health_emits_json_without_a_running_server(
    tmp_path, monkeypatch, capsys
):
    """`cmd_health` should build services locally, run the checks,
    and print a JSON payload — without needing the MCP server or
    any live providers. ChromaDB is mocked out because the CLI
    still constructs a real ChromaVectorStore."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_health
    from journal.config import Config

    db_path = tmp_path / "cli_health.db"
    config = Config(
        db_path=db_path,
        anthropic_api_key="a" * 40,
        openai_api_key="o" * 40,
    )

    # Patch the ChromaVectorStore constructor used by cmd_health to
    # return a MagicMock whose `count()` returns 0 — avoids needing
    # a running ChromaDB container.
    fake_store = MagicMock()
    fake_store.count.return_value = 0
    with patch("journal.cli.ChromaVectorStore", return_value=fake_store):
        args = MagicMock(compact=False)
        cmd_health(args, config)

    captured = capsys.readouterr()
    # The output is pretty-printed JSON.
    import json

    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert "ingestion" in payload
    assert "checks" in payload
    # Four checks: sqlite, chromadb, anthropic, openai.
    assert len(payload["checks"]) == 4


def test_cli_backfill_mood_help(capsys):
    """`journal backfill-mood --help` documents all flags."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "backfill-mood", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "backfill-mood" in captured.out
    for flag in ("--force", "--prune-retired", "--dry-run", "--start-date"):
        assert flag in captured.out


def test_cmd_backfill_mood_dry_run(tmp_path, capsys):
    """`cmd_backfill_mood --dry-run` exercises the full code path
    (load dimensions, build services, run backfill) without
    calling the scorer or writing to the DB."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_backfill_mood
    from journal.config import Config
    from journal.db.connection import get_connection
    from journal.db.migrations import run_migrations
    from journal.db.repository import SQLiteEntryRepository

    # Seed a real DB with two entries.
    db_path = tmp_path / "mood_cli.db"
    conn = get_connection(db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)
    repo.create_entry("2026-04-01", "photo", "first", 1)
    repo.create_entry("2026-04-02", "photo", "second", 1)
    conn.close()

    # Point the config at a minimal valid mood-dimensions file.
    dims_path = tmp_path / "dims.toml"
    dims_path.write_text(
        """
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
negative_pole = "sadness"
scale_type = "bipolar"
notes = "notes"
"""
    )

    config = Config(
        db_path=db_path,
        anthropic_api_key="a" * 40,
        mood_dimensions_path=dims_path,
    )

    # Patch the scorer constructor so no real Anthropic client is built.
    with patch(
        "journal.providers.mood_scorer.AnthropicMoodScorer"
    ) as mock_cls:
        mock_cls.return_value = MagicMock()
        cmd_backfill_mood(
            MagicMock(
                force=False,
                prune_retired=False,
                dry_run=True,
                start_date=None,
                end_date=None,
            ),
            config,
        )

    out = capsys.readouterr().out
    assert "dry-run" in out.lower() or "Dry run" in out
    assert "Scored:" in out


def test_cmd_health_compact_mode(tmp_path, capsys):
    """`--compact` emits single-line JSON for piping."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_health
    from journal.config import Config

    config = Config(
        db_path=tmp_path / "compact.db",
        anthropic_api_key="a" * 40,
        openai_api_key="o" * 40,
    )
    fake_store = MagicMock()
    fake_store.count.return_value = 0
    with patch("journal.cli.ChromaVectorStore", return_value=fake_store):
        cmd_health(MagicMock(compact=True), config)

    out = capsys.readouterr().out.strip()
    # One line, no pretty-print whitespace.
    assert "\n" not in out
    import json

    payload = json.loads(out)
    assert payload["status"] == "ok"
