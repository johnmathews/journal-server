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
        "backfill-entity-embeddings",
        "rechunk",
        "eval-chunking",
        "extract-entities",
        "repair-entity-names",
        "bootstrap-storylines",
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
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.repository import SQLiteEntryRepository

    # Seed a real DB with two entries.
    db_path = tmp_path / "mood_cli.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    repo = SQLiteEntryRepository(factory)
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


def test_cmd_repair_entity_names_dry_run_proposes_fix(tmp_path, capsys):
    """`repair-entity-names` (dry-run) detects an LLM-clipped canonical
    name and prints a proposed repair without modifying the DB."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_repair_entity_names
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.repository import SQLiteEntryRepository
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "repair.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    repo = SQLiteEntryRepository(factory)
    store = SQLiteEntityStore(factory)

    entry = repo.create_entry("2026-04-25", "photo", "raw", 5, user_id=1)
    entity = store.create_entity(
        entity_type="organization",
        canonical_name="Nautilin",  # the clipped form
        description="",
        first_seen="2026-04-25",
        user_id=1,
    )
    store.create_mention(
        entity_id=entity.id,
        entry_id=entry.id,
        quote="Nautiline, the iOS app that connects to the music server",
        confidence=0.9,
        extraction_run_id="test-run",
    )
    conn.close()

    config = Config(db_path=db_path)
    cmd_repair_entity_names(MagicMock(apply=False), config)

    out = capsys.readouterr().out
    assert "'Nautilin' -> 'Nautiline'" in out
    assert "Dry-run only" in out

    # DB unchanged
    factory = ConnectionFactory(db_path)
    conn = factory.get()
    fresh = SQLiteEntityStore(factory).get_entity(entity.id)
    assert fresh is not None
    assert fresh.canonical_name == "Nautilin"
    conn.close()


def test_cmd_repair_entity_names_apply_updates_canonical_name(
    tmp_path, capsys,
):
    """`repair-entity-names --apply` actually updates the entity's
    canonical_name in the DB."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_repair_entity_names
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.repository import SQLiteEntryRepository
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "repair_apply.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    repo = SQLiteEntryRepository(factory)
    store = SQLiteEntityStore(factory)

    entry = repo.create_entry("2026-04-25", "photo", "raw", 5, user_id=1)
    entity = store.create_entity(
        entity_type="organization",
        canonical_name="Nautilin",
        description="",
        first_seen="2026-04-25",
        user_id=1,
    )
    store.create_mention(
        entity_id=entity.id,
        entry_id=entry.id,
        quote="We launched Nautiline last week.",
        confidence=0.9,
        extraction_run_id="test-run",
    )
    conn.close()

    config = Config(db_path=db_path)
    cmd_repair_entity_names(MagicMock(apply=True), config)

    out = capsys.readouterr().out
    assert "Applied 1/1" in out

    factory = ConnectionFactory(db_path)

    conn = factory.get()
    fresh = SQLiteEntityStore(factory).get_entity(entity.id)
    assert fresh is not None
    assert fresh.canonical_name == "Nautiline"
    conn.close()


def test_cmd_repair_entity_names_skips_collision(tmp_path, capsys):
    """If the proposed repair would produce the canonical_name of an
    entity that already exists, skip with a warning rather than
    creating a duplicate row or violating uniqueness."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_repair_entity_names
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.repository import SQLiteEntryRepository
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "repair_collision.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    repo = SQLiteEntryRepository(factory)
    store = SQLiteEntityStore(factory)

    entry = repo.create_entry("2026-04-25", "photo", "raw", 5, user_id=1)

    # Already-correct entity exists.
    store.create_entity(
        entity_type="organization",
        canonical_name="Nautiline",
        description="",
        first_seen="2026-04-20",
        user_id=1,
    )
    # Stale clipped entity from a buggy older extraction.
    bad = store.create_entity(
        entity_type="organization",
        canonical_name="Nautilin",
        description="",
        first_seen="2026-04-25",
        user_id=1,
    )
    store.create_mention(
        entity_id=bad.id,
        entry_id=entry.id,
        quote="Nautiline shipped today",
        confidence=0.9,
        extraction_run_id="test-run",
    )
    conn.close()

    config = Config(db_path=db_path)
    cmd_repair_entity_names(MagicMock(apply=True), config)

    out = capsys.readouterr().out
    assert "would collide" in out
    # Should NOT have applied the colliding repair
    factory = ConnectionFactory(db_path)
    conn = factory.get()
    fresh = SQLiteEntityStore(factory).get_entity(bad.id)
    assert fresh is not None
    assert fresh.canonical_name == "Nautilin"  # unchanged
    conn.close()


def _seed_entity_with_raw_canonical(
    conn, entity_type: str, canonical_name: str, user_id: int = 1,
) -> int:
    """Insert an entity row directly via SQL, bypassing smart_title_case.

    Used to simulate pre-feature data where entities were stored with the raw
    LLM output, e.g. ``running`` lowercase, ``Easter picnic`` mixed-case.
    """
    cursor = conn.execute(
        "INSERT INTO entities"
        " (user_id, entity_type, canonical_name, description, first_seen)"
        " VALUES (?, ?, ?, '', '2026-04-01')",
        (user_id, entity_type, canonical_name),
    )
    conn.commit()
    entity_id = cursor.lastrowid
    assert entity_id is not None
    return entity_id


def test_cmd_renormalise_entity_casing_dry_run_lists_changes(tmp_path, capsys):
    """`renormalise-entity-casing` (dry-run) shows what would change without
    modifying the DB."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_renormalise_entity_casing
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "renorm.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()

    # Pre-feature data — names that should change after renormalisation.
    legacy_running = _seed_entity_with_raw_canonical(conn, "activity", "running")
    legacy_pages = _seed_entity_with_raw_canonical(conn, "activity", "morning pages")
    # Already-correct name — should NOT appear in dry-run output.
    correct = _seed_entity_with_raw_canonical(conn, "activity", "Frisbee")
    conn.close()

    config = Config(db_path=db_path)
    cmd_renormalise_entity_casing(MagicMock(apply=False), config)

    out = capsys.readouterr().out
    assert "'running' -> 'Running'" in out
    assert "'morning pages' -> 'Morning Pages'" in out
    # Already-correct rows should not be listed.
    assert "'Frisbee'" not in out
    assert "Dry-run only" in out

    # DB state must be unchanged.
    factory = ConnectionFactory(db_path)
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    assert store.get_entity(legacy_running).canonical_name == "running"
    assert store.get_entity(legacy_pages).canonical_name == "morning pages"
    assert store.get_entity(correct).canonical_name == "Frisbee"
    conn.close()


def test_cmd_renormalise_entity_casing_apply_updates_rows(tmp_path, capsys):
    """`renormalise-entity-casing --apply` writes the normalised values back."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_renormalise_entity_casing
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "renorm_apply.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    a = _seed_entity_with_raw_canonical(conn, "activity", "running")
    b = _seed_entity_with_raw_canonical(conn, "topic", "kubernetes")
    c = _seed_entity_with_raw_canonical(conn, "place", "the netherlands")
    conn.close()

    config = Config(db_path=db_path)
    cmd_renormalise_entity_casing(MagicMock(apply=True), config)

    out = capsys.readouterr().out
    assert "Applied" in out

    factory = ConnectionFactory(db_path)

    conn = factory.get()
    store = SQLiteEntityStore(factory)
    assert store.get_entity(a).canonical_name == "Running"
    assert store.get_entity(b).canonical_name == "Kubernetes"
    # Articles lowercased in non-leading positions via the algorithm.
    assert store.get_entity(c).canonical_name == "The Netherlands"
    conn.close()


def test_cmd_renormalise_entity_casing_uses_exceptions_toml(tmp_path, capsys):
    """The CLI must load the repo-shipped exceptions TOML so iOS/GitHub-style
    rules apply during the backfill."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_renormalise_entity_casing
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "renorm_exc.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    e1 = _seed_entity_with_raw_canonical(conn, "topic", "ios")
    e2 = _seed_entity_with_raw_canonical(conn, "topic", "github")
    e3 = _seed_entity_with_raw_canonical(conn, "topic", "javascript")
    conn.close()

    config = Config(db_path=db_path)
    cmd_renormalise_entity_casing(MagicMock(apply=True), config)

    factory = ConnectionFactory(db_path)

    conn = factory.get()
    store = SQLiteEntityStore(factory)
    assert store.get_entity(e1).canonical_name == "iOS"
    assert store.get_entity(e2).canonical_name == "GitHub"
    assert store.get_entity(e3).canonical_name == "JavaScript"
    conn.close()


def test_cmd_renormalise_entity_casing_skips_collisions(tmp_path, capsys):
    """When the proposed normalised name collides with an existing entity of
    the same (user_id, entity_type), the CLI must skip rather than create a
    duplicate or violate uniqueness — and surface the collision in output so
    the operator can resolve it via the merge UI."""
    from unittest.mock import MagicMock

    from journal.cli import cmd_renormalise_entity_casing
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "renorm_coll.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    correct = _seed_entity_with_raw_canonical(conn, "activity", "Running")
    legacy = _seed_entity_with_raw_canonical(conn, "activity", "running")
    conn.close()

    config = Config(db_path=db_path)
    cmd_renormalise_entity_casing(MagicMock(apply=True), config)

    out = capsys.readouterr().out
    assert "would collide" in out
    # Neither entity should be merged or destroyed by the backfill itself.
    factory = ConnectionFactory(db_path)
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    assert store.get_entity(correct).canonical_name == "Running"
    assert store.get_entity(legacy).canonical_name == "running"
    conn.close()


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


def test_cmd_backfill_entity_embeddings_dry_run(tmp_path, capsys):
    """`backfill-entity-embeddings --dry-run` reports candidates and
    makes no embedding calls."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_backfill_entity_embeddings
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "reembed.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    # Two with descriptions, one without — only the two should count.
    store.create_entity("person", "Sarah", "my mother", "2026-01-01")
    store.create_entity("place", "Vienna", "city in Austria", "2026-01-01")
    store.create_entity("person", "Ghost", "", "2026-01-01")
    conn.close()

    config = Config(db_path=db_path, openai_api_key="sk-test")

    with patch("journal.cli.entities.OpenAIEmbeddingsProvider") as mock_cls:
        mock_cls.return_value = MagicMock()
        cmd_backfill_entity_embeddings(
            MagicMock(user_id=None, dry_run=True), config,
        )

    out = capsys.readouterr().out
    assert "Candidates with non-empty description: 2" in out
    assert "Dry run" in out
    # Embeddings client must not be called for an actual embed
    inst = mock_cls.return_value
    inst.embed_query.assert_not_called()


def test_cmd_backfill_entity_embeddings_writes_embeddings(
    tmp_path, capsys,
):
    """Without --dry-run, the command calls the embeddings API and
    persists the resulting vector via set_entity_embedding."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_backfill_entity_embeddings
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "reembed_apply.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    sarah = store.create_entity("person", "Sarah", "my mother", "2026-01-01")
    vienna = store.create_entity("place", "Vienna", "city", "2026-01-01")
    store.create_entity("person", "Ghost", "", "2026-01-01")
    conn.close()

    config = Config(db_path=db_path, openai_api_key="sk-test")

    fake_provider = MagicMock()
    fake_provider.embed_query = MagicMock(return_value=[0.5] * 4)
    with patch(
        "journal.cli.entities.OpenAIEmbeddingsProvider", return_value=fake_provider,
    ):
        cmd_backfill_entity_embeddings(
            MagicMock(user_id=None, dry_run=False), config,
        )

    out = capsys.readouterr().out
    assert "Re-embedded: 2" in out
    assert "Failed:      0" in out
    assert fake_provider.embed_query.call_count == 2

    # Verify the embeddings actually landed.
    factory = ConnectionFactory(db_path)
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    assert store.get_entity_embedding(sarah.id) == [0.5, 0.5, 0.5, 0.5]
    assert store.get_entity_embedding(vienna.id) == [0.5, 0.5, 0.5, 0.5]
    conn.close()


def test_cmd_backfill_entity_embeddings_user_id_filter(
    tmp_path, capsys,
):
    """--user-id N restricts the backfill to one user."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_backfill_entity_embeddings
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "reembed_userscope.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    conn.execute(
        "INSERT INTO users (email, display_name, is_admin, email_verified) "
        "VALUES ('u2@test.com', 'User Two', 0, 1)"
    )
    conn.commit()
    store = SQLiteEntityStore(factory)
    store.create_entity("person", "Sarah", "user1 entity", "2026-01-01", user_id=1)
    store.create_entity("person", "Bob",   "user2 entity", "2026-01-01", user_id=2)
    conn.close()

    config = Config(db_path=db_path, openai_api_key="sk-test")
    fake_provider = MagicMock()
    fake_provider.embed_query = MagicMock(return_value=[0.1])
    with patch(
        "journal.cli.entities.OpenAIEmbeddingsProvider", return_value=fake_provider,
    ):
        cmd_backfill_entity_embeddings(
            MagicMock(user_id=2, dry_run=False), config,
        )

    out = capsys.readouterr().out
    assert "user 2" in out
    assert "Re-embedded: 1" in out
    assert fake_provider.embed_query.call_count == 1


def test_cmd_backfill_entity_embeddings_continues_on_per_row_failure(
    tmp_path, capsys,
):
    """A failure embedding one entity should not abort the rest."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_backfill_entity_embeddings
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.entitystore.store import SQLiteEntityStore

    db_path = tmp_path / "reembed_failure.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    conn = factory.get()
    store = SQLiteEntityStore(factory)
    store.create_entity("person", "Sarah", "first", "2026-01-01")
    store.create_entity("person", "Bob",   "second", "2026-01-01")
    conn.close()

    config = Config(db_path=db_path, openai_api_key="sk-test")
    fake_provider = MagicMock()
    fake_provider.embed_query = MagicMock(
        side_effect=[RuntimeError("rate limit"), [0.7]],
    )
    with patch(
        "journal.cli.entities.OpenAIEmbeddingsProvider", return_value=fake_provider,
    ):
        cmd_backfill_entity_embeddings(
            MagicMock(user_id=None, dry_run=False), config,
        )

    out = capsys.readouterr().out
    assert "Re-embedded: 1" in out


def _make_storyline(
    factory, *, user_id: int = 1, name: str = "Trip to Vienna", anchor: str = "Vienna",
):
    """Create one entity-anchored storyline (with its seq-1 draft
    chapter) for the ``bootstrap-storylines`` tests below."""
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.entitystore.store import SQLiteEntityStore

    entity_store = SQLiteEntityStore(factory)
    entity = entity_store.create_entity("place", anchor, "anchor", "2026-01-01")
    storyline_repo = SQLiteStorylineRepository(factory)
    return storyline_repo.create_storyline(user_id, [entity.id], name)


def test_cli_bootstrap_storylines_help(capsys):
    """`journal bootstrap-storylines --help` documents the new flags."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "bootstrap-storylines", "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    for flag in ("--user-id", "--storyline-id", "--mark-read", "--execute"):
        assert flag in captured.out


def test_cmd_bootstrap_storylines_dry_run_lists_candidates_no_engine(
    tmp_path, capsys,
):
    """Dry-run (no --execute) lists the user's storylines with their
    chapter/entry counts and never constructs the engine — no
    ANTHROPIC_API_KEY required, no LLM call possible."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_bootstrap_storylines
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations

    db_path = tmp_path / "bootstrap_dry.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    storyline = _make_storyline(factory)
    factory.get().close()

    # No anthropic_api_key at all — build_storyline_stack would raise if
    # it were ever called, so a passing dry run proves it wasn't.
    config = Config(db_path=db_path, anthropic_api_key="")

    with patch("journal.cli._services.build_storyline_stack") as mock_build:
        cmd_bootstrap_storylines(
            MagicMock(
                user_id=1, storyline_id=None, mark_read=False, execute=False,
            ),
            config,
        )
        mock_build.assert_not_called()

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert f"[{storyline.id}] {storyline.name}" in out
    assert "would bootstrap" in out
    # A freshly created storyline has its seeded seq-1 draft chapter and
    # no assigned entries yet.
    assert "1 chapter(s), 0 entries" in out


def test_cmd_bootstrap_storylines_execute_calls_engine_per_storyline(
    tmp_path, capsys,
):
    """--execute calls engine.bootstrap once per storyline, forwarding
    --mark-read."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_bootstrap_storylines
    from journal.cli._services import StorylineStack
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.services.storylines.engine import UpdateResult

    db_path = tmp_path / "bootstrap_execute.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    s1 = _make_storyline(factory, name="Trip to Vienna", anchor="Vienna")
    s2 = _make_storyline(factory, name="New Job", anchor="Acme Corp")

    config = Config(db_path=db_path, anthropic_api_key="sk-ant-test")
    # Reuse the same factory the fixtures were created with — this test's
    # thread-local connection must stay open for the repository below.
    storyline_repository = SQLiteStorylineRepository(factory)
    fake_engine = MagicMock()
    fake_engine.bootstrap.side_effect = [
        UpdateResult(storyline_id=s1.id, chapter_count=3),
        UpdateResult(storyline_id=s2.id, chapter_count=2),
    ]
    fake_stack = StorylineStack(
        entry_repository=MagicMock(),
        storyline_repository=storyline_repository,
        engine=fake_engine,
    )

    with patch(
        "journal.cli._services.build_storyline_stack", return_value=fake_stack,
    ):
        cmd_bootstrap_storylines(
            MagicMock(
                user_id=1, storyline_id=None, mark_read=True, execute=True,
            ),
            config,
        )

    assert fake_engine.bootstrap.call_count == 2
    called_ids = {c.args[0] for c in fake_engine.bootstrap.call_args_list}
    assert called_ids == {s1.id, s2.id}
    for c in fake_engine.bootstrap.call_args_list:
        assert c.kwargs["mark_read"] is True

    out = capsys.readouterr().out
    assert "EXECUTED" in out
    assert "3 chapter(s)" in out
    assert "2 chapter(s)" in out


def test_cmd_bootstrap_storylines_execute_continues_after_one_failure(
    tmp_path, capsys,
):
    """One storyline's ``engine.bootstrap`` raising must not abort the
    sweep — the rest still get processed — but the command must exit
    non-zero so a cron/CI caller notices the failure."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_bootstrap_storylines
    from journal.cli._services import StorylineStack
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.services.storylines.engine import UpdateResult

    db_path = tmp_path / "bootstrap_partial_failure.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    s1 = _make_storyline(factory, name="Trip to Vienna", anchor="Vienna")
    s2 = _make_storyline(factory, name="New Job", anchor="Acme Corp")

    config = Config(db_path=db_path, anthropic_api_key="sk-ant-test")
    storyline_repository = SQLiteStorylineRepository(factory)
    fake_engine = MagicMock()
    fake_engine.bootstrap.side_effect = [
        RuntimeError("judge API down"),
        UpdateResult(storyline_id=s2.id, chapter_count=2),
    ]
    fake_stack = StorylineStack(
        entry_repository=MagicMock(),
        storyline_repository=storyline_repository,
        engine=fake_engine,
    )

    with (
        patch(
            "journal.cli._services.build_storyline_stack", return_value=fake_stack,
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_bootstrap_storylines(
            MagicMock(
                user_id=1, storyline_id=None, mark_read=False, execute=True,
            ),
            config,
        )

    assert exc_info.value.code != 0
    # Both storylines were attempted despite the first one raising.
    assert fake_engine.bootstrap.call_count == 2
    called_ids = {c.args[0] for c in fake_engine.bootstrap.call_args_list}
    assert called_ids == {s1.id, s2.id}

    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "judge API down" in out
    assert "2 chapter(s)" in out  # s2 still printed its normal summary


def test_cmd_bootstrap_storylines_storyline_id_restricts_to_one(
    tmp_path, capsys,
):
    """--storyline-id restricts both dry-run listing and --execute to a
    single storyline."""
    from unittest.mock import MagicMock, patch

    from journal.cli import cmd_bootstrap_storylines
    from journal.cli._services import StorylineStack
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.migrations import run_migrations
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.services.storylines.engine import UpdateResult

    db_path = tmp_path / "bootstrap_single.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    _s1 = _make_storyline(factory, name="Trip to Vienna", anchor="Vienna")
    s2 = _make_storyline(factory, name="New Job", anchor="Acme Corp")

    config = Config(db_path=db_path, anthropic_api_key="sk-ant-test")
    storyline_repository = SQLiteStorylineRepository(factory)
    fake_engine = MagicMock()
    fake_engine.bootstrap.return_value = UpdateResult(
        storyline_id=s2.id, chapter_count=1,
    )
    fake_stack = StorylineStack(
        entry_repository=MagicMock(),
        storyline_repository=storyline_repository,
        engine=fake_engine,
    )

    with patch(
        "journal.cli._services.build_storyline_stack", return_value=fake_stack,
    ):
        cmd_bootstrap_storylines(
            MagicMock(
                user_id=1, storyline_id=s2.id, mark_read=False, execute=True,
            ),
            config,
        )

    fake_engine.bootstrap.assert_called_once_with(s2.id, mark_read=False)

    out = capsys.readouterr().out
    assert "New Job" in out
    assert "Trip to Vienna" not in out
