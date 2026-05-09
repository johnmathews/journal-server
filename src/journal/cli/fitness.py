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

import logging
import sys
from datetime import UTC, datetime
from getpass import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from journal.db.connection import get_connection
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.providers.garmin import GarminConnectGarminProvider
from journal.providers.strava import (
    StravalibStravaProvider,
    Tokens,
    exchange_code,
)
from journal.services.fitness.fetch import (
    GarminFetchService,
    StravaFetchService,
)
from journal.services.fitness.normalize import normalize_garmin, normalize_strava

if TYPE_CHECKING:
    import argparse

    from journal.config import Config


log = logging.getLogger(__name__)

_DEFAULT_USER_ID = 1


# ── Helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    The operator-driven re-auth differs from the bootstrap-time persist
    closure on one point: re-auth declares ``auth_status="ok"`` and
    clears ``auth_broken_since`` because the operator has just fixed
    the auth interactively. ``extra_state``, ``last_refresh_at``, and
    ``created_at`` are read-then-merged from the existing row to
    preserve fields the fetch service maintains independently.
    """
    if not config.strava_client_id or not config.strava_client_secret:
        print(
            "Error: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set "
            "in the environment.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    print(f"Listening for callback on http://{host}:{port}{callback_path} ...")

    try:
        code = _oauth_listener(
            host=host, port=port, callback_path=callback_path,
        )
    except KeyboardInterrupt:
        print("\nCancelled — no tokens written.", file=sys.stderr)
        sys.exit(130)

    tokens = exchange_code(
        client_id=config.strava_client_id,
        client_secret=config.strava_client_secret,
        code=code,
    )

    user_id = args.user_id
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = FitnessRepository(conn)
    existing = repo.get_auth_state(user_id=user_id, source="strava")
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="strava",
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_expires_at=tokens["token_expires_at"],
            extra_state=dict(existing.extra_state) if existing else {},
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

    Username/password come from the env (``GARMIN_USERNAME`` /
    ``GARMIN_PASSWORD``), falling back to interactive prompts. MFA, when
    Garmin asks for it, is read from stdin via :func:`_stdin_mfa_prompt`.
    The token blob produced by ``client.dumps`` is mirrored into
    ``fitness_auth_state.extra_state_json`` so subsequent syncs boot
    from the DB row, not the filesystem cache (D4 in the integration
    plan).

    Same operator-driven semantics as :func:`cmd_fitness_reauth_strava`:
    ``auth_status`` is forced to ``"ok"`` and ``auth_broken_since``
    cleared on success.
    """
    username = config.garmin_username or input("Garmin username: ").strip()
    password = config.garmin_password or getpass("Garmin password: ")
    if not username or not password:
        print(
            "Error: GARMIN_USERNAME and GARMIN_PASSWORD required "
            "(env or prompt).",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id = args.user_id
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = FitnessRepository(conn)

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

    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    extra = dict(existing.extra_state) if existing else {}
    extra["tokens_blob"] = persisted[-1]
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
    print("Garmin re-auth complete — token blob persisted.")


# ── fitness-sync ─────────────────────────────────────────────────────


class _NoopFitnessNotifier:
    """No-op :class:`FitnessNotifier` for short-lived CLI runs.

    The interactive operator already sees the result on stdout; firing
    Pushover from a one-shot CLI invocation would cross channels
    confusingly. The fetch service still records ``fitness_sync_runs``
    rows — the notifier is only consulted on auth-broken / threshold
    paths to fan out alerts.
    """

    def notify_fitness_auth_broken(self, user_id: int, source: str) -> None:
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
    """Build the ``provider_factory`` callback for Garmin sync."""

    def _factory(auth: FitnessAuthState) -> GarminConnectGarminProvider:
        user_id = auth.user_id
        tokens_blob = (
            auth.extra_state.get("tokens_blob") if auth.extra_state else None
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
            username=config.garmin_username,
            password=config.garmin_password,
            tokens_blob=tokens_blob,
            persist_tokens=_persist,
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

    conn = get_connection(config.db_path)
    run_migrations(conn)
    fitness_repo = FitnessRepository(conn)

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


# ── fitness-status ───────────────────────────────────────────────────


def cmd_fitness_status(args: argparse.Namespace, config: Config) -> None:
    """Print per-source auth + last-runs snapshot.

    Shape mirrors :func:`journal.api.fitness._per_source_status` so the
    CLI and the webapp surface the same payload. Sources with neither
    an auth row nor any sync runs are omitted; if every source is
    unconfigured, prints a helpful message and exits 0.
    """
    user_id = args.user_id
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = FitnessRepository(conn)

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
