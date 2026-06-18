"""Tests for the W11 fitness CLI subcommands.

Covers the four flat argparse subcommands wired into ``cli/__init__.py``:

- ``journal fitness-reauth-strava`` — OAuth one-shot flow.
- ``journal fitness-reauth-garmin`` — username/password + optional MFA.
- ``journal fitness-sync`` — inline fetch + normalize per source.
- ``journal fitness-status`` — per-source auth + last-runs print.

Tests follow the existing flat-file convention (``tests/test_cli.py`` and
``tests/test_api_fitness.py``). The CLI is driven via ``cli.main()`` with
constructed ``sys.argv`` lists; network seams (``_oauth_listener``,
``exchange_code``, ``GarminConnectGarminProvider``, the per-source sync
helper) are patched at the module level.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest

from journal.cli import main
from journal.db.connection import get_connection
from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.providers.strava import Tokens

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fitness_env(tmp_path, monkeypatch):
    """Point the CLI at a fresh temp DB and stub all required env vars.

    The CLI reads its DB path and provider credentials via ``load_config``
    (env-driven). Tests inject everything through monkeypatched env vars
    so each invocation gets a clean DB and known credentials.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("STRAVA_REDIRECT_URI", "http://localhost:8400/strava/callback")
    # No GARMIN_USERNAME / GARMIN_PASSWORD — those config fields were removed
    # in W6. The CLI takes --username and reads the password via getpass;
    # tests patch getpass directly.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    conn = get_connection(db_path)
    run_migrations(conn)
    conn.close()
    return db_path


def _read_state(db_path, *, source: str) -> FitnessAuthState | None:
    factory = ConnectionFactory(db_path)
    return FitnessRepository(factory).get_auth_state(user_id=1, source=source)


def _open_repo(db_path):
    """Return (conn, FitnessRepository) for direct setup/inspect in tests."""
    factory = ConnectionFactory(db_path)
    return factory.get(), FitnessRepository(factory)


# ── Help-text tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "subcommand",
    [
        "fitness-reauth-strava",
        "fitness-reauth-garmin",
        "fitness-sync",
        "fitness-status",
        "fitness-audit",
    ],
)
def test_fitness_subcommand_help(capsys, subcommand):
    """Each subcommand exposes ``--help`` and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", subcommand, "--help"]
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert subcommand in captured.out


@pytest.mark.parametrize(
    "subcommand",
    [
        "fitness-reauth-strava",
        "fitness-reauth-garmin",
        "fitness-sync",
        "fitness-backfill",
        "fitness-status",
    ],
)
def test_fitness_subcommand_requires_user_id(capsys, subcommand):
    """W7 acceptance: every fitness-* subcommand requires --user-id.

    argparse exits 2 (non-zero) with an error message naming the missing
    argument when the flag is omitted. ``fitness-audit`` is excluded — it
    audits every user and does not take --user-id.
    """
    sys.argv = ["journal", subcommand]
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "--user-id" in captured.err


# ── Strava OAuth ─────────────────────────────────────────────────────


def test_fitness_reauth_strava_happy_path(fitness_env, capsys):
    """Listener returns code → exchange → upsert with auth_status='ok'."""
    fake_tokens = Tokens(
        access_token="acc_TOKEN",
        refresh_token="ref_TOKEN",
        token_expires_at="2026-06-01T00:00:00Z",
    )
    with patch("journal.cli.fitness._oauth_listener", return_value="TEST_CODE"), \
         patch(
             "journal.cli.fitness.exchange_code",
             return_value=(fake_tokens, "777777"),
         ) as mock_exchange:
        sys.argv = ["journal", "fitness-reauth-strava", "--user-id", "1"]
        main()

    assert mock_exchange.call_count == 1
    assert mock_exchange.call_args.kwargs["code"] == "TEST_CODE"
    assert mock_exchange.call_args.kwargs["client_id"] == "12345"

    state = _read_state(fitness_env, source="strava")
    assert state is not None
    assert state.access_token == "acc_TOKEN"
    assert state.refresh_token == "ref_TOKEN"
    assert state.token_expires_at == "2026-06-01T00:00:00Z"
    assert state.auth_status == "ok"
    assert state.auth_broken_since is None
    assert state.last_successful_login_at is not None
    # Athlete id captured from the W3 return-tuple shape (D8 retrofit).
    assert state.extra_state.get("upstream_user_id") == "777777"


def test_fitness_reauth_strava_user_cancellation(fitness_env, capsys):
    """KeyboardInterrupt during listener → no DB write, non-zero exit."""
    with patch(
        "journal.cli.fitness._oauth_listener", side_effect=KeyboardInterrupt,
    ), patch("journal.cli.fitness.exchange_code") as mock_exchange:
        sys.argv = ["journal", "fitness-reauth-strava", "--user-id", "1"]
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    mock_exchange.assert_not_called()
    assert _read_state(fitness_env, source="strava") is None


def test_fitness_reauth_strava_with_code_skips_listener(fitness_env):
    """W2 of the fitness multi-user final-mile plan: ``--code <code>``
    must bypass the OAuth listener entirely and exchange the code
    directly. The previous headless path required the inline-python
    recipe in fitness-operations.md §2e; this CLI flag is the clean
    replacement.
    """
    fake_tokens = Tokens(
        access_token="DIRECT_ACC",
        refresh_token="DIRECT_REF",
        token_expires_at="2026-07-01T00:00:00Z",
    )

    def _boom(*_a: object, **_kw: object) -> str:
        raise AssertionError(
            "_oauth_listener must not be invoked when --code is supplied",
        )

    with patch("journal.cli.fitness._oauth_listener", side_effect=_boom), \
         patch(
             "journal.cli.fitness.exchange_code",
             return_value=(fake_tokens, "888888"),
         ) as mock_exchange:
        sys.argv = [
            "journal", "fitness-reauth-strava",
            "--user-id", "1",
            "--code", "PASTED_CODE_FROM_BROWSER",
        ]
        main()

    assert mock_exchange.call_count == 1
    assert mock_exchange.call_args.kwargs["code"] == "PASTED_CODE_FROM_BROWSER"

    state = _read_state(fitness_env, source="strava")
    assert state is not None
    assert state.access_token == "DIRECT_ACC"
    assert state.refresh_token == "DIRECT_REF"
    assert state.auth_status == "ok"
    assert state.extra_state.get("upstream_user_id") == "888888"


def test_fitness_reauth_strava_with_invalid_code_exits_nonzero(
    fitness_env, capsys,
):
    """An invalid / expired code surfaces as a clear error on stderr and
    a non-zero exit, not an uncaught traceback. No DB row is written.
    """
    with patch(
        "journal.cli.fitness.exchange_code",
        side_effect=RuntimeError("invalid_grant: code already used"),
    ):
        sys.argv = [
            "journal", "fitness-reauth-strava",
            "--user-id", "1",
            "--code", "STALE_CODE",
        ]
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "Strava token exchange failed" in err
    assert "invalid_grant" in err
    assert _read_state(fitness_env, source="strava") is None


def test_fitness_reauth_strava_preserves_existing_extra_state(fitness_env):
    """Re-auth merges existing extra_state instead of clobbering it."""
    conn, repo = _open_repo(fitness_env)
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="strava",
            access_token="OLD", refresh_token="OLD",
            token_expires_at="2026-01-01T00:00:00Z",
            extra_state={"athlete_id": 9999},
            auth_status="broken",
            auth_broken_since="2026-04-01T00:00:00Z",
        ),
    )
    conn.close()

    fake_tokens = Tokens(
        access_token="NEW", refresh_token="NEW",
        token_expires_at="2026-06-01T00:00:00Z",
    )
    # Pass athlete_id=None so this test isolates extra_state preservation
    # from the upstream-id capture path covered above.
    with patch("journal.cli.fitness._oauth_listener", return_value="C"), \
         patch(
             "journal.cli.fitness.exchange_code",
             return_value=(fake_tokens, None),
         ):
        sys.argv = ["journal", "fitness-reauth-strava", "--user-id", "1"]
        main()

    state = _read_state(fitness_env, source="strava")
    assert state is not None
    assert state.access_token == "NEW"
    assert state.extra_state == {"athlete_id": 9999}
    # auth_broken_since is cleared because the operator just re-authed.
    assert state.auth_status == "ok"
    assert state.auth_broken_since is None


# ── Garmin login ─────────────────────────────────────────────────────


class _FakeGarminClientInternal:
    """Stand-in for ``garminconnect.Garmin.client``."""

    def __init__(self, dump_blob: str) -> None:
        self._dump_blob = dump_blob

    def loads(self, blob: str) -> None:
        return None

    def dumps(self) -> str:
        return self._dump_blob


class _FakeGarminClient:
    """Drop-in for the ``garminconnect.Garmin`` SDK class.

    Records whether the ``prompt_mfa`` callback fired and what value it
    returned, so the MFA-path test can assert end-to-end.
    """

    def __init__(self, *, email: str, password: str, prompt_mfa) -> None:
        self.email = email
        self.password = password
        self._prompt_mfa = prompt_mfa
        self.client = _FakeGarminClientInternal('{"oauth1": "FAKE_BLOB"}')
        self.mfa_received: list[str] = []
        self.login_calls = 0

    def login(self, tokenstore=None) -> None:
        self.login_calls += 1
        if self._prompt_mfa is not None and getattr(self, "_invoke_mfa", False):
            self.mfa_received.append(self._prompt_mfa())


def _patch_garmin_client(client_holder: list[_FakeGarminClient], *, invoke_mfa: bool):
    """Install a fake ``client_factory`` into the real provider class.

    The real :class:`GarminConnectGarminProvider` is used (so persist_tokens
    and ``client.dumps`` flow exercise the production code), but the
    underlying SDK is the fake class above.
    """
    from journal.providers.garmin import GarminConnectGarminProvider as _RealGarmin

    def factory(*, email: str, password: str, prompt_mfa) -> _FakeGarminClient:
        client = _FakeGarminClient(email=email, password=password, prompt_mfa=prompt_mfa)
        client._invoke_mfa = invoke_mfa
        client_holder.append(client)
        return client

    def patched_provider(**kwargs: Any) -> _RealGarmin:
        return _RealGarmin(client_factory=factory, **kwargs)

    return patch(
        "journal.cli.fitness.GarminConnectGarminProvider", side_effect=patched_provider,
    )


def test_fitness_reauth_garmin_non_mfa_happy_path(fitness_env, monkeypatch):
    """Login completes without invoking MFA; tokens blob persisted, auth_status='ok'.

    Username comes from --username; password from getpass (mocked).
    """
    monkeypatch.setattr(
        "journal.cli.fitness.getpass", lambda *a, **kw: "test_password",
    )

    clients: list[_FakeGarminClient] = []
    with _patch_garmin_client(clients, invoke_mfa=False):
        sys.argv = [
            "journal", "fitness-reauth-garmin",
            "--user-id", "1",
            "--username", "test_user@example.com",
        ]
        main()

    assert len(clients) == 1
    assert clients[0].login_calls == 1
    assert clients[0].mfa_received == []
    assert clients[0].email == "test_user@example.com"
    assert clients[0].password == "test_password"

    state = _read_state(fitness_env, source="garmin")
    assert state is not None
    assert state.extra_state.get("tokens_blob") == '{"oauth1": "FAKE_BLOB"}'
    assert state.auth_status == "ok"
    assert state.auth_broken_since is None


def test_fitness_reauth_garmin_mfa_happy_path(fitness_env, monkeypatch):
    """MFA callback fires; six-digit code from stdin feeds back into the SDK."""
    monkeypatch.setattr(
        "journal.cli.fitness.getpass", lambda *a, **kw: "test_password",
    )
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "123456")

    clients: list[_FakeGarminClient] = []
    with _patch_garmin_client(clients, invoke_mfa=True):
        sys.argv = [
            "journal", "fitness-reauth-garmin",
            "--user-id", "1",
            "--username", "test_user@example.com",
        ]
        main()

    assert clients[0].mfa_received == ["123456"]
    state = _read_state(fitness_env, source="garmin")
    assert state is not None
    assert state.auth_status == "ok"


def test_fitness_reauth_garmin_requires_username(fitness_env, monkeypatch):
    """W6 acceptance: --username is required; argparse refuses without it."""
    sys.argv = ["journal", "fitness-reauth-garmin", "--user-id", "1"]
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0


def test_fitness_reauth_garmin_no_env_var_fallback(fitness_env, monkeypatch, capsys):
    """W6 acceptance: setting GARMIN_USERNAME / GARMIN_PASSWORD in env has
    no effect — argparse still requires --username, and the password is
    always read via getpass."""
    monkeypatch.setenv("GARMIN_USERNAME", "leftover@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "leftover_pw")
    sys.argv = ["journal", "fitness-reauth-garmin", "--user-id", "1"]
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "--username" in captured.err


# ── fitness-sync ─────────────────────────────────────────────────────


def test_fitness_sync_source_both_runs_each(fitness_env, capsys):
    """``--source both`` invokes the per-source helper once per source."""
    calls: list[tuple[str, int]] = []

    def fake_run(*, source: str, fitness_repo, config, user_id: int, since):
        calls.append((source, user_id))
        return {
            "source": source,
            "status": "success",
            "run_id": 1,
            "rows_fetched": 5,
            "rows_normalized": 5,
        }

    with patch("journal.cli.fitness._run_one_source_sync", side_effect=fake_run):
        sys.argv = ["journal", "fitness-sync", "--source", "both", "--user-id", "1"]
        main()

    assert calls == [("strava", 1), ("garmin", 1)]
    captured = capsys.readouterr()
    assert "strava" in captured.out
    assert "garmin" in captured.out


def test_fitness_sync_default_source_is_both(fitness_env):
    """No ``--source`` flag → both sources sync (same default as REST)."""
    calls: list[str] = []

    def fake_run(*, source, **kwargs):
        calls.append(source)
        return {
            "source": source, "status": "success", "run_id": 1,
            "rows_fetched": 0, "rows_normalized": 0,
        }

    with patch("journal.cli.fitness._run_one_source_sync", side_effect=fake_run):
        sys.argv = ["journal", "fitness-sync", "--user-id", "1"]
        main()

    assert sorted(calls) == ["garmin", "strava"]


def test_fitness_sync_single_source(fitness_env):
    """``--source strava`` invokes only the Strava helper."""
    calls: list[str] = []

    def fake_run(*, source, **kwargs):
        calls.append(source)
        return {
            "source": source, "status": "success", "run_id": 1,
            "rows_fetched": 0, "rows_normalized": 0,
        }

    with patch("journal.cli.fitness._run_one_source_sync", side_effect=fake_run):
        sys.argv = ["journal", "fitness-sync", "--source", "strava", "--user-id", "1"]
        main()

    assert calls == ["strava"]


# ── fitness-status ───────────────────────────────────────────────────


def test_fitness_status_empty_db(fitness_env, capsys):
    """No auth_state rows → friendly message, exit 0."""
    sys.argv = ["journal", "fitness-status", "--user-id", "1"]
    main()  # Must not SystemExit non-zero.

    captured = capsys.readouterr()
    out = captured.out.lower()
    assert "no fitness sources" in out or "not configured" in out


def test_fitness_status_with_data(fitness_env, capsys):
    """Configured sources show auth_status + last-run row counts."""
    conn, repo = _open_repo(fitness_env)
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="strava",
            access_token="A", refresh_token="R",
            token_expires_at="2026-06-01T00:00:00Z",
            auth_status="ok",
        ),
    )
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="garmin",
            extra_state={"tokens_blob": "blob"},
            auth_status="broken",
            auth_broken_since="2026-05-01T00:00:00Z",
        ),
    )
    conn.close()

    sys.argv = ["journal", "fitness-status", "--user-id", "1"]
    main()

    out = capsys.readouterr().out
    assert "strava" in out
    assert "garmin" in out
    assert "ok" in out
    assert "broken" in out


# ── fitness-audit ────────────────────────────────────────────────────


def _add_user(conn, *, email: str, is_admin: int = 0) -> int:
    """Insert a user and return the new id."""
    cur = conn.execute(
        "INSERT INTO users (email, display_name, is_admin, email_verified) "
        "VALUES (?, ?, ?, 1)",
        (email, email.split("@")[0], is_admin),
    )
    conn.commit()
    return cur.lastrowid


def test_fitness_audit_clean_empty_db_exits_zero(fitness_env, capsys):
    """Migrated DB with no fitness rows → audit reports zero rows + PASS."""
    sys.argv = ["journal", "fitness-audit"]
    main()  # must not SystemExit non-zero

    out = capsys.readouterr().out
    assert "fitness_auth_state" in out
    assert "fitness_sync_runs" in out
    assert "fitness_activities" in out
    assert "fitness_daily" in out
    assert "fitness_raw_strava" in out
    assert "fitness_raw_garmin" in out
    assert "violations: 0" in out
    assert "PASS" in out


def test_fitness_audit_with_valid_rows_groups_per_user(fitness_env, capsys):
    """Rows owned by valid users → per-user breakdown shows their email."""
    conn, repo = _open_repo(fitness_env)
    user_2_id = _add_user(conn, email="user2@test.com")

    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=1, source="strava",
            access_token="A", refresh_token="R",
            token_expires_at="2026-06-01T00:00:00Z",
            auth_status="ok",
        ),
    )
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_2_id, source="strava",
            access_token="B", refresh_token="C",
            token_expires_at="2026-06-01T00:00:00Z",
            auth_status="ok",
        ),
    )
    conn.close()

    sys.argv = ["journal", "fitness-audit"]
    main()

    out = capsys.readouterr().out
    # Per-user breakdown shows both users' emails alongside their counts.
    assert "mthwsjc@gmail.com" in out or "user_id=1" in out
    assert "user2@test.com" in out
    assert "violations: 0" in out
    assert "PASS" in out


def test_fitness_audit_orphan_user_id_fails(fitness_env, capsys):
    """A row with user_id pointing at a deleted user is reported as a violation
    and the command exits non-zero.
    """
    conn = get_connection(fitness_env)
    # Foreign keys are enforced by default in this codebase, so disable them
    # for this fixture so we can simulate the data-integrity bug the audit is
    # meant to catch (a row whose user_id no longer resolves into users).
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO fitness_auth_state "
        "(user_id, source, access_token, refresh_token, token_expires_at, "
        " auth_status) "
        "VALUES (?, 'strava', 'A', 'R', '2026-06-01T00:00:00Z', 'ok')",
        (999,),  # no user with id=999 exists
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "fitness-audit"]
        main()
    assert exc_info.value.code != 0

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "fitness_auth_state" in out
    # The orphan user id is named in the violation listing.
    assert "999" in out


def test_fitness_audit_null_user_id_fails(fitness_env, capsys):
    """A row whose user_id is NULL is reported as a violation. Schema has
    NOT NULL, but the audit asserts it anyway as defense-in-depth — a future
    schema change or a raw-SQL insert could re-introduce a NULL.
    """
    conn = get_connection(fitness_env)
    # Build a per-table mirror without the NOT NULL constraint so we can
    # exercise the NULL branch without forging schema state. The audit reads
    # the same six tables by name, so insert directly into the real one
    # bypassing the constraint via a temporary trigger-free path.
    #
    # SQLite does not allow ALTER TABLE to drop NOT NULL, so we use the
    # rebuild-and-rename trick: copy schema without the NOT NULL, swap.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE fitness_sync_runs_tmp ("
        "    id              INTEGER PRIMARY KEY AUTOINCREMENT,"
        "    user_id         INTEGER,"
        "    source          TEXT NOT NULL,"
        "    started_at      TEXT NOT NULL,"
        "    finished_at     TEXT,"
        "    status          TEXT NOT NULL,"
        "    error_class     TEXT,"
        "    error_message   TEXT,"
        "    rows_fetched    INTEGER NOT NULL DEFAULT 0,"
        "    rows_normalized INTEGER NOT NULL DEFAULT 0,"
        "    notes_json      TEXT NOT NULL DEFAULT '{}'"
        ")",
    )
    conn.execute(
        "INSERT INTO fitness_sync_runs_tmp "
        "(user_id, source, started_at, status) "
        "VALUES (NULL, 'strava', '2026-05-10T00:00:00Z', 'success')",
    )
    conn.execute("DROP TABLE fitness_sync_runs")
    conn.execute("ALTER TABLE fitness_sync_runs_tmp RENAME TO fitness_sync_runs")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["journal", "fitness-audit"]
        main()
    assert exc_info.value.code != 0

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "fitness_sync_runs" in out
    assert "NULL" in out


# ── Garmin token mint / import (split-IP recovery) ───────────────────


class _FakeMintInternal:
    """Stand-in for ``garminconnect.Garmin.client`` for mint/import."""

    def __init__(self, dump_blob: str) -> None:
        self._dump_blob = dump_blob
        self.loaded: str | None = None

    def dumps(self) -> str:
        return self._dump_blob

    def loads(self, blob: str) -> None:
        # Mirror the SDK: reject anything that isn't the JSON we expect.
        import json as _json

        _json.loads(blob)
        self.loaded = blob


class _FakeMintGarmin:
    """Drop-in for ``garminconnect.Garmin`` exercising the mint flow."""

    def __init__(
        self, *, email: str = "", password: str = "", prompt_mfa=None,
    ) -> None:
        self.email = email
        self.password = password
        self._prompt_mfa = prompt_mfa
        self.client = _FakeMintInternal('{"oauth1": "MINTED_BLOB"}')
        self.login_calls = 0

    def login(self) -> None:
        self.login_calls += 1

    def get_user_profile(self) -> dict:
        return {"displayName": "alice.j", "fullName": "Alice J"}


def test_mint_garmin_token_envelope_shape() -> None:
    """The minted envelope carries source, upstream id, blob, and timestamp."""
    from journal.cli.fitness import _mint_garmin_token_envelope

    envelope = _mint_garmin_token_envelope(
        username="alice@example.com",
        password="pw",
        client_factory=_FakeMintGarmin,
        mfa_prompt=lambda: "000000",
    )
    assert envelope["source"] == "garmin"
    assert envelope["upstream_user_id"] == "alice.j"
    assert envelope["tokens_blob"] == '{"oauth1": "MINTED_BLOB"}'
    assert envelope["minted_at"].endswith("Z")


def test_import_garmin_token_envelope_persists_ok(fitness_env) -> None:
    """Importing a valid envelope writes the blob and flips auth_status='ok'."""
    from journal.cli.fitness import _import_garmin_token_envelope

    repo = FitnessRepository(ConnectionFactory(fitness_env))
    envelope = {
        "source": "garmin",
        "upstream_user_id": "alice.j",
        "tokens_blob": '{"oauth1": "MINTED_BLOB"}',
        "minted_at": "2026-06-19T00:00:00Z",
    }
    upstream = _import_garmin_token_envelope(
        repo, user_id=1, envelope=envelope, client_factory=_FakeMintGarmin,
    )
    assert upstream == "alice.j"
    state = _read_state(fitness_env, source="garmin")
    assert state is not None
    assert state.extra_state.get("tokens_blob") == '{"oauth1": "MINTED_BLOB"}'
    assert state.extra_state.get("upstream_user_id") == "alice.j"
    assert state.auth_status == "ok"
    assert state.auth_broken_since is None


def test_import_garmin_token_envelope_rejects_bad_blob(fitness_env) -> None:
    """A non-JSON / unloadable blob is rejected before touching the DB."""
    from journal.cli.fitness import _import_garmin_token_envelope

    repo = FitnessRepository(ConnectionFactory(fitness_env))
    envelope = {"source": "garmin", "upstream_user_id": "alice.j", "tokens_blob": "not-json"}
    with pytest.raises(ValueError, match="failed to load"):
        _import_garmin_token_envelope(
            repo, user_id=1, envelope=envelope, client_factory=_FakeMintGarmin,
        )
    assert _read_state(fitness_env, source="garmin") is None


def test_mint_then_import_roundtrip_via_main(fitness_env, monkeypatch, tmp_path) -> None:
    """End-to-end: mint to a file, import from it, DB row lands auth_status='ok'."""
    monkeypatch.setattr("journal.cli.fitness.getpass", lambda *a, **kw: "pw")
    monkeypatch.setattr("journal.cli.fitness.Garmin", _FakeMintGarmin)
    envelope_path = tmp_path / "garmin-token.json"

    sys.argv = [
        "journal", "fitness-garmin-mint-token",
        "--username", "alice@example.com",
        "--output", str(envelope_path),
    ]
    main()
    assert envelope_path.exists()

    sys.argv = [
        "journal", "fitness-garmin-import-token",
        "--user-id", "1",
        "--input", str(envelope_path),
    ]
    main()

    state = _read_state(fitness_env, source="garmin")
    assert state is not None
    assert state.auth_status == "ok"
    assert state.extra_state.get("tokens_blob") == '{"oauth1": "MINTED_BLOB"}'
