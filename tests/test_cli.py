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
        "backfill-chunks",
        "rechunk",
    ):
        assert cmd in captured.out, f"Command '{cmd}' not found in help output"
