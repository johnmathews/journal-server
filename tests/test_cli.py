"""Tests for CLI interface."""

import pytest

from journal.cli import main


def test_cli_help(capsys):
    """Test that CLI shows help without errors."""
    with pytest.raises(SystemExit) as exc_info:
        import sys
        sys.argv = ["journal", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "Journal Analysis Tool" in captured.out


def test_cli_requires_command(capsys):
    """Test that CLI requires a subcommand."""
    with pytest.raises(SystemExit) as exc_info:
        import sys
        sys.argv = ["journal"]
        main()
    assert exc_info.value.code != 0
