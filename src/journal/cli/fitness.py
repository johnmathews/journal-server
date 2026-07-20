"""W11 — fitness CLI subcommands.

Four flat argparse subcommands wired into :mod:`journal.cli`:

- ``journal fitness-reauth-strava`` — print the authorize URL, run a
  one-shot HTTP listener on the host/port from ``STRAVA_REDIRECT_URI``,
  exchange the received code for tokens, persist into
  ``fitness_auth_state`` with ``auth_status="ok"``.
- ``journal fitness-reauth-garmin`` — log into Garmin Connect via the
  configured username/password (or prompts), handle the optional MFA
  callback from stdin, persist the resulting token blob into
  ``fitness_auth_state.extra_state_json``.
- ``journal fitness-sync [--source strava|garmin|both] [--since YYYY-MM-DD]``
  — run fetch + normalize inline, mirroring the shape of
  ``extract-entities`` and ``backfill-mood`` (no JobRunner is constructed
  in the short-lived CLI process; the long-running server still routes
  through JobRunner via REST/MCP).
- ``journal fitness-status`` — print the same per-source snapshot the
  ``GET /api/fitness/sync/status`` endpoint returns.

Plan-drift corrections vs. ``docs/fitness-tier-plan.md`` §W11 are recorded
in ``journal/260509-fitness-w11-cli-reauth.md``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from getpass import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from garminconnect import Garmin

from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.providers.garmin import GarminConnectGarminProvider
from journal.providers.strava import (
    StravalibStravaProvider,
    Tokens,
    exchange_code,
)
from journal.services.fitness.backfill import (
    BackfillBlocked,
    BackfillResult,
    backfill_garmin,
    backfill_strava,
)
from journal.services.fitness.credentials import (
    CredentialDecryptError,
    CredentialKeyInvalid,
    decrypt_credential,
    encrypt_credential,
)
from journal.services.fitness.fetch import (
    GarminFetchService,
    StravaFetchService,
)
from journal.services.fitness.normalize import normalize_garmin, normalize_strava

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from journal.config import Config


log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exit_strava_disabled(*, hint: str | None = None) -> None:
    """Print the W1 strava-mothball refusal and exit non-zero.

    Strava is mothballed (roadmap D8, Strava API paywall 2026-06-30);
    every Strava-touching CLI path refuses unless STRAVA_ENABLED=true.
    """
    message = "Error: Strava integration is disabled (STRAVA_ENABLED=false)."
    if hint:
        message = f"{message} {hint}"
    print(message, file=sys.stderr)
    sys.exit(1)


# ── Strava OAuth ─────────────────────────────────────────────────────


def _build_strava_authorize_url(*, client_id: str, redirect_uri: str) -> str:
    """Construct the authorize URL the operator pastes into a browser."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
    }
    return f"https://www.strava.com/oauth/authorize?{urlencode(params)}"


def _make_oauth_handler(
    callback_path: str, code_holder: list[str],
) -> type[BaseHTTPRequestHandler]:
    """Return a one-shot ``BaseHTTPRequestHandler`` subclass that captures
    the ``code`` query param when the callback path is hit. Appending to
    ``code_holder`` is the signal back to :func:`_oauth_listener`.
    """

    class _StravaOAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib spelling
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            codes = params.get("code", [])
            if codes:
                code_holder.append(codes[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"Strava authorization received. You can close this tab.",
                )
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Strava callback missing 'code' parameter.")

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return  # silence the default access log

    return _StravaOAuthHandler


def _oauth_listener(
    *,
    host: str,
    port: int,
    callback_path: str,
    server_factory: Any = HTTPServer,
) -> str:
    """Block on a one-shot HTTP listener; return the captured ``code``.

    Exposed at module level (not nested) so tests can patch it directly
    with a synthetic return value rather than spinning up a real socket.
    Raises :class:`RuntimeError` if the callback fires without a ``code``
    parameter (e.g. user denied the authorisation prompt).
    """
    code_holder: list[str] = []
    handler_cls = _make_oauth_handler(callback_path, code_holder)
    server = server_factory((host, port), handler_cls)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if not code_holder:
        raise RuntimeError(
            "Strava OAuth callback did not include a 'code' parameter — "
            "did the user reject access?",
        )
    return code_holder[0]


def cmd_fitness_reauth_strava(args: argparse.Namespace, config: Config) -> None:
    """Run the Strava OAuth flow and persist the resulting tokens.

    Two code paths share the same DB write. When ``--code`` is supplied
    the listener is skipped entirely and the code is exchanged inline —
    the primary path for headless deploys where the server already
    owns ``STRAVA_REDIRECT_URI``'s port (see ``docs/fitness-operations.md``
    §2e). Without ``--code`` the command prints the authorize URL and
    blocks on a one-shot HTTP listener bound to the redirect URI's
    host/port, the classic laptop/dev bootstrap flow (§2d).

    The operator-driven re-auth declares ``auth_status="ok"`` and clears
    ``auth_broken_since`` because the operator has just fixed the auth
    interactively. ``extra_state``, ``last_refresh_at``, and
    ``created_at`` are read-then-merged from the existing row to
    preserve fields the fetch service maintains independently.
    """
    if not config.strava_enabled:
        _exit_strava_disabled(
            hint="Set STRAVA_ENABLED=true to revive the integration.",
        )
    if not config.strava_client_id or not config.strava_client_secret:
        print(
            "Error: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set "
            "in the environment.",
            file=sys.stderr,
        )
        sys.exit(1)

    code = getattr(args, "code", None)
    if code is not None:
        print("Exchanging code via --code (skipping OAuth listener)...")
    else:
        parsed = urlparse(config.strava_redirect_uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8400
        callback_path = parsed.path or "/strava/callback"

        auth_url = _build_strava_authorize_url(
            client_id=config.strava_client_id,
            redirect_uri=config.strava_redirect_uri,
        )
        print("Open this URL in your browser to authorise Strava:")
        print(f"  {auth_url}")
        print(
            f"Listening for callback on http://{host}:{port}{callback_path} ...",
        )

        try:
            code = _oauth_listener(
                host=host, port=port, callback_path=callback_path,
            )
        except KeyboardInterrupt:
            print("\nCancelled — no tokens written.", file=sys.stderr)
            sys.exit(130)

    try:
        tokens, athlete_id = exchange_code(
            client_id=config.strava_client_id,
            client_secret=config.strava_client_secret,
            code=code,
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream error
        print(
            f"Error: Strava token exchange failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id = args.user_id
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = FitnessRepository(db_factory)
    existing = repo.get_auth_state(user_id=user_id, source="strava")
    extra = dict(existing.extra_state) if existing else {}
    # Capture the upstream athlete id (D8) so a later reconnect with a
    # different Strava account is detected. The CLI path is operator-driven,
    # but the captured field is checked in the W3 webapp endpoint too.
    if athlete_id is not None:
        extra["upstream_user_id"] = athlete_id
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="strava",
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_expires_at=tokens["token_expires_at"],
            extra_state=extra,
            last_successful_login_at=_now_iso(),
            last_refresh_at=existing.last_refresh_at if existing else None,
            auth_status="ok",
            auth_broken_since=None,
            created_at=existing.created_at if existing else "",
        ),
    )
    print("Strava re-auth complete — tokens persisted.")


# ── Garmin login ─────────────────────────────────────────────────────


def _stdin_mfa_prompt() -> str:
    """Read a 6-digit MFA code from stdin via :func:`input`."""
    return input("Garmin MFA code: ").strip()


def cmd_fitness_reauth_garmin(args: argparse.Namespace, config: Config) -> None:
    """Run garminconnect login and persist the resulting token blob.

    Username comes from ``--username`` (required, no env-var fallback);
    password is read from stdin via :func:`getpass.getpass`. MFA, when
    Garmin asks for it, is read from stdin via :func:`_stdin_mfa_prompt`.
    The token blob produced by ``client.dumps`` is mirrored into
    ``fitness_auth_state.extra_state_json`` so subsequent syncs boot
    from the DB row, not the filesystem cache (D4 in the integration
    plan).

    Operator-only fallback path for the per-user webapp connect flow
    (W2). Same operator-driven semantics as
    :func:`cmd_fitness_reauth_strava`: ``auth_status`` is forced to
    ``"ok"`` and ``auth_broken_since`` cleared on success.
    """
    username = args.username.strip()
    password = getpass("Garmin password: ")
    if not username or not password:
        print(
            "Error: --username and a non-empty password are required.",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id = args.user_id
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = FitnessRepository(db_factory)

    persisted: list[str] = []

    def _persist(blob: str) -> None:
        persisted.append(blob)

    provider = GarminConnectGarminProvider(
        username=username,
        password=password,
        persist_tokens=_persist,
    )

    try:
        provider.login(mfa_callback=_stdin_mfa_prompt)
    except KeyboardInterrupt:
        print("\nCancelled — no tokens written.", file=sys.stderr)
        sys.exit(130)

    if not persisted:
        print(
            "Error: Garmin login completed but no token blob was emitted.",
            file=sys.stderr,
        )
        sys.exit(1)

    # W5 saved credentials: encrypt right after the successful login, while
    # the plaintext is still in scope. Key unset → feature off, nothing
    # credential-shaped is written (pre-W5 behavior).
    enc_password: str | None = None
    if config.fitness_credential_key:
        enc_password = encrypt_credential(
            password, key=config.fitness_credential_key,
        )

    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    extra = dict(existing.extra_state) if existing else {}
    extra["tokens_blob"] = persisted[-1]
    if enc_password:
        extra["garmin_username"] = username
        extra["enc_password"] = enc_password
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="garmin",
            access_token=existing.access_token if existing else None,
            refresh_token=existing.refresh_token if existing else None,
            token_expires_at=existing.token_expires_at if existing else None,
            extra_state=extra,
            last_successful_login_at=_now_iso(),
            last_refresh_at=existing.last_refresh_at if existing else None,
            auth_status="ok",
            auth_broken_since=None,
            created_at=existing.created_at if existing else "",
        ),
    )
    if enc_password:
        print(
            "Garmin re-auth complete — token blob and encrypted "
            "credentials persisted.",
        )
    else:
        print("Garmin re-auth complete — token blob persisted.")


# ── Garmin token mint / import (split-IP recovery) ───────────────────
#
# When Garmin's Cloudflare bot defenses flag the server's egress IP, a
# fresh username/password login from the server fails (429 / bot
# challenge) even though the credentials are correct. These two commands
# split the recovery so the network-bound login runs somewhere unflagged:
#
#   1. ``fitness-garmin-mint-token`` — run on a laptop / unflagged network.
#      Logs into Garmin, prints a portable JSON envelope to stdout. Does
#      NOT touch the database, so it needs no prod DB access.
#   2. ``fitness-garmin-import-token`` — run on the server. Reads the
#      envelope from stdin/file and writes the token blob into
#      ``fitness_auth_state`` (auth_status='ok'). No network login.
#
# A garth OAuth1 token is valid ~1 year, so a single mint+import keeps the
# daily sync running without the server ever doing a fresh SSO login.


def _mint_garmin_token_envelope(
    *,
    username: str,
    password: str,
    client_factory: Callable[..., Any] | None = None,
    mfa_prompt: Callable[[], str] = _stdin_mfa_prompt,
) -> dict[str, Any]:
    """Log into Garmin and return a portable token envelope.

    Pure network + in-memory: constructs a ``garminconnect.Garmin``,
    logs in (prompting for MFA via ``mfa_prompt`` when Garmin asks),
    reads the upstream account identity, and captures the token blob.
    Never opens the database — safe to run from any machine/network.

    The returned envelope carries ``upstream_user_id`` (for the D8
    different-account guard on import) and ``tokens_blob`` (the value the
    fetch service boots from).
    """
    factory = client_factory or Garmin
    client = factory(
        email=username, password=password, prompt_mfa=mfa_prompt,
    )
    client.login()
    profile = client.get_user_profile()
    upstream: str | None = None
    if isinstance(profile, dict):
        raw = profile.get("displayName") or profile.get("userName")
        if isinstance(raw, str) and raw:
            upstream = raw
    return {
        "source": "garmin",
        "upstream_user_id": upstream or username,
        "tokens_blob": client.client.dumps(),
        "minted_at": _now_iso(),
    }


def cmd_fitness_garmin_mint_token(args: argparse.Namespace, config: Config) -> None:
    """Mint a Garmin token envelope (no DB writes) — run on an unflagged IP.

    ``--username`` is required; the password is read from stdin via
    getpass (never env vars), matching ``fitness-reauth-garmin``. The JSON
    envelope goes to ``--output`` (a path, or ``-`` for stdout, the
    default); all human-readable progress goes to stderr so the stdout
    envelope can be piped straight into ``fitness-garmin-import-token``.
    """
    del config  # intentionally unused — minting never touches the DB
    username = args.username.strip()
    password = getpass("Garmin password: ")
    if not username or not password:
        print(
            "Error: --username and a non-empty password are required.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        envelope = _mint_garmin_token_envelope(username=username, password=password)
    except KeyboardInterrupt:
        print("\nCancelled — no token minted.", file=sys.stderr)
        sys.exit(130)

    payload = json.dumps(envelope, indent=2)
    destination = getattr(args, "output", None) or "-"
    if destination == "-":
        print(
            f"Garmin token minted (upstream={envelope['upstream_user_id']}). "
            "Pipe the JSON below into 'journal fitness-garmin-import-token' "
            "on the server.",
            file=sys.stderr,
        )
        print(payload)
    else:
        Path(destination).write_text(payload + "\n", encoding="utf-8")
        print(
            f"Garmin token envelope written to {destination} "
            f"(upstream={envelope['upstream_user_id']}).",
            file=sys.stderr,
        )


def _import_garmin_token_envelope(
    repo: FitnessRepository,
    *,
    user_id: int,
    envelope: dict[str, Any],
    client_factory: Callable[..., Any] | None = None,
) -> str:
    """Persist a minted envelope into ``fitness_auth_state`` (auth_status='ok').

    Validates the blob is well-formed and loadable by the SDK (offline)
    before writing, warns on stderr if it belongs to a different upstream
    account than the one already stored (D8), then upserts with the same
    operator-driven semantics as :func:`cmd_fitness_reauth_garmin`.
    Returns the ``upstream_user_id`` written.
    """
    blob = envelope.get("tokens_blob")
    upstream = envelope.get("upstream_user_id")
    if not isinstance(blob, str) or not blob:
        raise ValueError("envelope missing a non-empty 'tokens_blob'")
    if not isinstance(upstream, str) or not upstream:
        raise ValueError("envelope missing a non-empty 'upstream_user_id'")

    # Offline sanity check: the blob must be JSON and load into a fresh SDK
    # client. Catches a truncated/corrupted paste before it reaches the DB.
    factory = client_factory or Garmin
    try:
        json.loads(blob)
        factory(email="", password="").client.loads(blob)
    except Exception as exc:  # noqa: BLE001 — surface a clean operator error
        raise ValueError(f"token blob failed to load into the SDK: {exc}") from exc

    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    if existing is not None:
        stored = (existing.extra_state or {}).get("upstream_user_id")
        if stored and stored != upstream:
            print(
                f"Warning: importing a different Garmin account "
                f"(stored={stored}, incoming={upstream}).",
                file=sys.stderr,
            )
    extra = dict(existing.extra_state) if existing else {}
    extra["tokens_blob"] = blob
    extra["upstream_user_id"] = upstream
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="garmin",
            access_token=existing.access_token if existing else None,
            refresh_token=existing.refresh_token if existing else None,
            token_expires_at=existing.token_expires_at if existing else None,
            extra_state=extra,
            last_successful_login_at=_now_iso(),
            last_refresh_at=existing.last_refresh_at if existing else None,
            auth_status="ok",
            auth_broken_since=None,
            created_at=existing.created_at if existing else "",
        ),
    )
    return upstream


def cmd_fitness_garmin_import_token(args: argparse.Namespace, config: Config) -> None:
    """Import a minted Garmin token envelope into the DB — run on the server.

    Reads the JSON envelope from ``--input`` (a path, or ``-`` for stdin,
    the default) and persists it for ``--user-id``. No network login —
    the daily sync boots from the stored blob.
    """
    source = getattr(args, "input", None) or "-"
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: input is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(envelope, dict):
        print("Error: envelope must be a JSON object.", file=sys.stderr)
        sys.exit(1)

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = FitnessRepository(db_factory)
    try:
        upstream = _import_garmin_token_envelope(
            repo, user_id=args.user_id, envelope=envelope,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Garmin token imported for user_id={args.user_id} "
        f"(upstream={upstream}); auth_status set to ok.",
    )


# ── fitness-sync ─────────────────────────────────────────────────────


class _NoopFitnessNotifier:
    """No-op :class:`FitnessNotifier` for short-lived CLI runs.

    The interactive operator already sees the result on stdout; firing
    Pushover from a one-shot CLI invocation would cross channels
    confusingly. The fetch service still records ``fitness_sync_runs``
    rows — the notifier is only consulted on auth-broken / threshold
    paths to fan out alerts.
    """

    def notify_fitness_auth_broken(
        self, user_id: int, source: str, *, recovery_attempted: bool = False,
    ) -> None:
        return

    def notify_fitness_sync_failure(
        self, user_id: int, source: str, attempts: int,
    ) -> None:
        return


def _strava_provider_factory(
    config: Config, repo: FitnessRepository,
) -> Any:
    """Build the ``provider_factory`` callback for Strava sync.

    Same read-then-merge persist closure as the bootstrap path so a
    token refresh during a CLI sync preserves the auth_status /
    auth_broken_since columns the fetch service maintains.
    """

    def _factory(auth: FitnessAuthState) -> StravalibStravaProvider:
        user_id = auth.user_id

        def _persist(tokens: Tokens) -> None:
            existing = repo.get_auth_state(user_id=user_id, source="strava")
            repo.upsert_auth_state(
                FitnessAuthState(
                    user_id=user_id,
                    source="strava",
                    access_token=tokens["access_token"],
                    refresh_token=tokens["refresh_token"],
                    token_expires_at=tokens["token_expires_at"],
                    extra_state=dict(existing.extra_state) if existing else {},
                    last_successful_login_at=(
                        existing.last_successful_login_at if existing else None
                    ),
                    last_refresh_at=_now_iso(),
                    auth_status=existing.auth_status if existing else "unknown",
                    auth_broken_since=(
                        existing.auth_broken_since if existing else None
                    ),
                    created_at=existing.created_at if existing else "",
                ),
            )

        return StravalibStravaProvider(
            client_id=config.strava_client_id,
            client_secret=config.strava_client_secret,
            access_token=auth.access_token or "",
            refresh_token=auth.refresh_token or "",
            token_expires_at=auth.token_expires_at or "1970-01-01T00:00:00Z",
            persist_tokens=_persist,
        )

    return _factory


def _garmin_provider_factory(
    config: Config, repo: FitnessRepository,
) -> Any:
    """Build the ``provider_factory`` callback for Garmin sync.

    Credentials are per-user from the DB (``tokens_blob`` on
    ``fitness_auth_state.extra_state_json``). No global Garmin
    username/password env vars exist; if a user has no token blob, the
    provider falls through to the network login with empty credentials
    and fails cleanly (the fetch service writes ``auth_status='broken'``).

    W6 of the strava-mothball / garmin-credentials plan: mirrors the
    bootstrap factory — when ``FITNESS_CREDENTIAL_KEY`` is set and the
    auth row carries saved (encrypted) credentials, they are decrypted
    and injected so the fetch service's unattended re-login also works
    from ``journal fitness-sync`` / ``fitness-backfill``. Decrypt
    failures degrade to the empty-credential provider with a warning.
    """

    def _factory(auth: FitnessAuthState) -> GarminConnectGarminProvider:
        user_id = auth.user_id
        extra = auth.extra_state or {}
        tokens_blob = extra.get("tokens_blob")

        username = ""
        password = ""
        saved_username = extra.get("garmin_username") or ""
        enc_password = extra.get("enc_password") or ""
        if config.fitness_credential_key and saved_username and enc_password:
            try:
                password = decrypt_credential(
                    enc_password, key=config.fitness_credential_key,
                )
                username = saved_username
            except (CredentialDecryptError, CredentialKeyInvalid) as exc:
                log.warning(
                    "Saved Garmin credentials for user %d could not be "
                    "decrypted (%s); unattended re-login disabled until "
                    "the user reconnects", user_id, exc,
                )

        def _persist(blob: str) -> None:
            existing = repo.get_auth_state(user_id=user_id, source="garmin")
            extra = dict(existing.extra_state) if existing else {}
            extra["tokens_blob"] = blob
            repo.upsert_auth_state(
                FitnessAuthState(
                    user_id=user_id,
                    source="garmin",
                    access_token=existing.access_token if existing else None,
                    refresh_token=existing.refresh_token if existing else None,
                    token_expires_at=(
                        existing.token_expires_at if existing else None
                    ),
                    extra_state=extra,
                    last_successful_login_at=(
                        existing.last_successful_login_at if existing else None
                    ),
                    last_refresh_at=_now_iso(),
                    auth_status=existing.auth_status if existing else "unknown",
                    auth_broken_since=(
                        existing.auth_broken_since if existing else None
                    ),
                    created_at=existing.created_at if existing else "",
                ),
            )

        return GarminConnectGarminProvider(
            username=username,
            password=password,
            tokens_blob=tokens_blob,
            persist_tokens=_persist,
            request_delay_s=config.fitness_garmin_request_delay_s,
        )

    return _factory


def _run_one_source_sync(
    *,
    source: str,
    fitness_repo: FitnessRepository,
    config: Config,
    user_id: int,
    since: datetime | None,
) -> dict[str, Any]:
    """Run fetch + normalize for one source inline.

    Decoupled from :func:`cmd_fitness_sync` so tests can patch this
    single seam and drive the dispatcher logic without standing up real
    fetch services.
    """
    notifier = _NoopFitnessNotifier()
    if source == "strava":
        fetch_service = StravaFetchService(
            repo=fitness_repo, notifier=notifier, config=config,
            provider_factory=_strava_provider_factory(config, fitness_repo),
        )
        fetch = fetch_service.run_sync(user_id=user_id, since=since)
        normalized = 0
        if fetch.status == "success":
            n = normalize_strava(fitness_repo, user_id=user_id)
            normalized = n.rows_normalized
        return {
            "source": "strava",
            "status": fetch.status,
            "run_id": fetch.run_id,
            "rows_fetched": fetch.rows_fetched,
            "rows_normalized": normalized,
        }
    if source == "garmin":
        fetch_service = GarminFetchService(
            repo=fitness_repo, notifier=notifier, config=config,
            provider_factory=_garmin_provider_factory(config, fitness_repo),
        )
        fetch = fetch_service.run_sync(user_id=user_id, since=since)
        normalized = 0
        if fetch.status == "success":
            n = normalize_garmin(fitness_repo, user_id=user_id)
            normalized = n.rows_normalized
        return {
            "source": "garmin",
            "status": fetch.status,
            "run_id": fetch.run_id,
            "rows_fetched": fetch.rows_fetched,
            "rows_normalized": normalized,
        }
    raise ValueError(f"Unknown source: {source}")


def cmd_fitness_sync(args: argparse.Namespace, config: Config) -> None:
    """Run a fitness sync inline (fetch + normalize) for the requested source(s).

    Mirrors how ``extract-entities`` and ``backfill-mood`` work: build
    services, call them synchronously, print the result. No JobRunner is
    constructed in the CLI process — the long-running server still routes
    its scheduled / on-demand syncs through JobRunner via REST and MCP.
    The ``fitness_sync_runs`` row is recorded by the fetch service either
    way.
    """
    # W1 strava-mothball: "both" is rejected (not degraded to garmin) so
    # the operator gets an explicit signal rather than silently-partial
    # output.
    if args.source in ("strava", "both") and not config.strava_enabled:
        _exit_strava_disabled(hint="Use --source garmin.")

    sources: list[str] = (
        ["strava", "garmin"] if args.source == "both" else [args.source]
    )

    user_id = args.user_id

    since: datetime | None = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            print(
                f"Error: --since must be ISO date YYYY-MM-DD, got {args.since!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    fitness_repo = FitnessRepository(db_factory)

    any_failed = False
    for source in sources:
        print(f"\nSyncing {source}...")
        result = _run_one_source_sync(
            source=source, fitness_repo=fitness_repo, config=config,
            user_id=user_id, since=since,
        )
        status = result["status"]
        marker = "ok" if status == "success" else "fail"
        print(
            f"  [{marker}] {source}: status={status} "
            f"run_id={result['run_id']} "
            f"fetched={result['rows_fetched']} "
            f"normalized={result['rows_normalized']}",
        )
        if status not in ("success", "running"):
            any_failed = True

    if any_failed:
        sys.exit(1)


# ── fitness-backfill ─────────────────────────────────────────────────


def _run_one_source_backfill(
    *,
    source: str,
    fitness_repo: FitnessRepository,
    config: Config,
    user_id: int,
    start: str,
    end: str,
) -> BackfillResult:
    """Construct the right fetch service and drive its backfill.

    Decoupled from :func:`cmd_fitness_backfill` so the dispatcher logic
    stays patch-friendly: tests can substitute this single seam without
    standing up real fetch services. Mirrors the
    :func:`_run_one_source_sync` helper for ``fitness-sync``.
    """
    notifier = _NoopFitnessNotifier()
    if source == "strava":
        fetch_service = StravaFetchService(
            repo=fitness_repo, notifier=notifier, config=config,
            provider_factory=_strava_provider_factory(config, fitness_repo),
        )
        return backfill_strava(
            user_id=user_id,
            repo=fitness_repo,
            fetch_service=fetch_service,
            start=start,
            end=end,
        )
    if source == "garmin":
        fetch_service = GarminFetchService(
            repo=fitness_repo, notifier=notifier, config=config,
            provider_factory=_garmin_provider_factory(config, fitness_repo),
        )
        return backfill_garmin(
            user_id=user_id,
            repo=fitness_repo,
            fetch_service=fetch_service,
            start=start,
            end=end,
        )
    raise ValueError(f"Unknown source: {source}")


def cmd_fitness_backfill(args: argparse.Namespace, config: Config) -> None:
    """Run a historical backfill (W13) for the requested source(s).

    Like ``fitness-sync``, runs synchronously inline — no JobRunner is
    constructed. Each 30-day window opens a ``fitness_sync_runs`` row
    via the W6 fetch service so ``journal fitness-status`` (and the
    ``/api/health`` block) reflect progress in real time.

    Exits non-zero if any source backfill is aborted (transient streak
    cliff, auth_broken) or blocked (a routine sync was already in
    flight). The operator's recovery path is encoded in the printed
    ``aborted_reason`` string.
    """
    # W1 strava-mothball: mirror cmd_fitness_sync — reject strava/both
    # outright with a clear message.
    if args.source in ("strava", "both") and not config.strava_enabled:
        _exit_strava_disabled(hint="Use --source garmin.")

    sources: list[str] = (
        ["strava", "garmin"] if args.source == "both" else [args.source]
    )

    today_iso = datetime.now(UTC).date().isoformat()
    end = args.end or today_iso

    user_id = args.user_id

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    fitness_repo = FitnessRepository(db_factory)

    any_failed = False
    for source in sources:
        print(f"\nBackfilling {source} from {args.start} to {end}...")
        try:
            result = _run_one_source_backfill(
                source=source, fitness_repo=fitness_repo, config=config,
                user_id=user_id, start=args.start, end=end,
            )
        except BackfillBlocked as exc:
            print(f"  [blocked] {source}: {exc}", file=sys.stderr)
            any_failed = True
            continue

        marker = "ok" if result.final_status == "complete" else "fail"
        print(
            f"  [{marker}] {source}: status={result.final_status} "
            f"windows={result.windows_succeeded}/{result.windows_attempted} "
            f"fetched={result.rows_fetched} "
            f"normalized={result.rows_normalized}",
        )
        if result.aborted_reason:
            print(f"    reason: {result.aborted_reason}")
        if result.final_status not in ("complete", "no_windows"):
            any_failed = True

    if any_failed:
        sys.exit(1)


# ── fitness-status ───────────────────────────────────────────────────


_FITNESS_TABLES: tuple[str, ...] = (
    "fitness_auth_state",
    "fitness_sync_runs",
    "fitness_activities",
    "fitness_daily",
    "fitness_raw_strava",
    "fitness_raw_garmin",
)


def cmd_fitness_audit(args: argparse.Namespace, config: Config) -> None:
    """Audit per-user data isolation across every fitness table.

    For each of the six fitness tables, reports total row count, per-user
    breakdown (joined with ``users.email`` so the operator can read the
    output without a DB lookup), and any violations: rows with
    ``user_id IS NULL`` or ``user_id`` pointing at a non-existent user
    (FK orphan). Exits 0 on a clean audit, 1 if any violations are found.

    Used as the W1 pre-flight check for the multi-user rollout (see
    ``docs/fitness-multiuser-plan.md``) and as the W14 verification gate
    after user 2 starts populating their own rows.
    """
    db_factory = ConnectionFactory(config.db_path)
    conn = db_factory.get()
    run_migrations(conn)

    print(f"fitness data audit (db: {config.db_path})")
    print("=" * 60)

    violations: list[str] = []
    snapshot: dict[str, int] = {}

    for table in _FITNESS_TABLES:
        total_row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        total = total_row[0] if total_row is not None else 0
        snapshot[table] = total

        print(f"\n[{table}] rows={total}")

        if total == 0:
            continue

        # Per-user breakdown joined with users.email. LEFT JOIN so orphans
        # (user_id with no matching users row) still appear, with email NULL.
        breakdown = conn.execute(
            f"""
            SELECT t.user_id, u.email, COUNT(*) AS n
            FROM {table} AS t
            LEFT JOIN users AS u ON u.id = t.user_id
            GROUP BY t.user_id
            ORDER BY t.user_id IS NULL DESC, t.user_id
            """,
        ).fetchall()

        for row in breakdown:
            user_id, email, count = row[0], row[1], row[2]
            if user_id is None:
                label = "user_id=NULL"
                violations.append(
                    f"{table}: {count} row(s) with user_id IS NULL",
                )
            elif email is None:
                label = f"user_id={user_id} (orphan: no matching users row)"
                violations.append(
                    f"{table}: {count} row(s) with orphan user_id={user_id} "
                    "(referenced user no longer exists)",
                )
            else:
                label = f"user_id={user_id} ({email})"
            print(f"  {label:<60} rows={count}")

    print()
    print("=" * 60)
    print(f"violations: {len(violations)}")
    if violations:
        for v in violations:
            print(f"  - {v}")
        print("result: FAIL")
        sys.exit(1)
    else:
        print("result: PASS")


def cmd_fitness_status(args: argparse.Namespace, config: Config) -> None:
    """Print per-source auth + last-runs snapshot.

    Shape mirrors :func:`journal.api.fitness._per_source_status` so the
    CLI and the webapp surface the same payload. Sources with neither
    an auth row nor any sync runs are omitted; if every source is
    unconfigured, prints a helpful message and exits 0.
    """
    user_id = args.user_id
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = FitnessRepository(db_factory)

    rows: list[dict[str, Any]] = []
    for source in ("strava", "garmin"):
        auth = repo.get_auth_state(user_id=user_id, source=source)
        last_runs = repo.list_recent_sync_runs(
            user_id=user_id, source=source, limit=10,
        )
        if auth is None and not last_runs:
            continue
        last_success = repo.last_successful_sync_at(
            user_id=user_id, source=source,
        )
        rows.append({
            "source": source,
            "auth_status": auth.auth_status if auth is not None else "unknown",
            "auth_broken_since": (
                auth.auth_broken_since if auth is not None else None
            ),
            "last_success_at": last_success,
            "last_runs": last_runs,
        })

    if not rows:
        print("No fitness sources configured for this user.")
        print(
            "Run `journal fitness-reauth-strava` or "
            "`journal fitness-reauth-garmin` to set one up.",
        )
        return

    for row in rows:
        print(f"\n[{row['source']}]")
        print(f"  auth_status:       {row['auth_status']}")
        print(f"  auth_broken_since: {row['auth_broken_since'] or '-'}")
        print(f"  last_success_at:   {row['last_success_at'] or '-'}")
        if row["last_runs"]:
            print(f"  recent runs ({len(row['last_runs'])}):")
            for run in row["last_runs"]:
                print(
                    f"    {run.started_at} -> {run.status:<20} "
                    f"fetched={run.rows_fetched} "
                    f"normalized={run.rows_normalized}",
                )
