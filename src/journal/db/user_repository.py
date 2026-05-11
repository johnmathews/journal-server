"""Repository interface and SQLite implementation for users, sessions, and API keys."""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from journal.db.factory import ConnectionFactory
from journal.models import ApiKeyInfo, User

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        is_active=bool(row["is_active"]),
        email_verified=bool(row["email_verified"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@runtime_checkable
class UserRepository(Protocol):
    # Users
    def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str | None = None,
        is_admin: bool = False,
    ) -> User: ...

    def get_user_by_id(self, user_id: int) -> User | None: ...

    def get_user_by_email(self, email: str) -> User | None: ...

    def get_password_hash(self, user_id: int) -> str | None: ...

    def update_user(self, user_id: int, **fields: Any) -> User | None: ...

    def list_users(self) -> list[User]: ...

    def increment_failed_logins(self, user_id: int) -> None: ...

    def reset_failed_logins(self, user_id: int) -> None: ...

    def lock_user(self, user_id: int, until: str) -> None: ...

    def get_lock_status(self, user_id: int) -> str | None: ...

    # Sessions
    def create_session(
        self,
        session_id: str,
        user_id: int,
        expires_at: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> None: ...

    def get_session(self, session_id: str) -> dict | None: ...

    def update_session_last_seen(self, session_id: str) -> None: ...

    def delete_session(self, session_id: str) -> None: ...

    def delete_user_sessions(self, user_id: int) -> int: ...

    def cleanup_expired_sessions(self) -> int: ...

    # API Keys
    def create_api_key(
        self,
        user_id: int,
        key_prefix: str,
        key_hash: str,
        name: str,
        expires_at: str | None = None,
    ) -> int: ...

    def get_api_key_by_hash(self, key_hash: str) -> dict | None: ...

    def list_api_keys(self, user_id: int) -> list[ApiKeyInfo]: ...

    def revoke_api_key(self, key_id: int, user_id: int) -> bool: ...

    def update_api_key_last_used(self, key_id: int) -> None: ...

    # Admin queries
    def get_user_stats(self) -> list[dict]: ...

    # Preferences
    def get_preferences(self, user_id: int) -> dict[str, Any]: ...

    def get_preference(self, user_id: int, key: str) -> Any | None: ...

    def set_preference(self, user_id: int, key: str, value: Any) -> None: ...

    def delete_preference(self, user_id: int, key: str) -> bool: ...


class SQLiteUserRepository:
    """SQLite-backed repository for users, sessions, and API keys.

    Construction accepts either a :class:`ConnectionFactory` (preferred,
    used by production via ``mcp_server/bootstrap.py``) or a bare
    ``sqlite3.Connection`` (legacy, retained for tests that haven't been
    migrated to the factory model yet — see
    ``docs/sqlite-per-thread-connections-plan.md`` W3).

    On the **factory** path each thread that calls a method gets its
    own ``sqlite3.Connection`` via ``threading.local`` inside the
    factory, so the shared-state commit race documented in
    ``docs/sqlite-threading.md`` is structurally impossible. The
    ``_lock`` is a no-op on this path.

    On the **legacy connection** path the same ``Connection`` instance
    is shared across threads. The per-method ``threading.Lock``
    serialises ``execute`` + ``commit`` pairs within this repo.
    """

    def __init__(
        self,
        factory_or_conn: ConnectionFactory | sqlite3.Connection,
    ) -> None:
        if isinstance(factory_or_conn, ConnectionFactory):
            self._factory: ConnectionFactory | None = factory_or_conn
            self._direct_conn: sqlite3.Connection | None = None
        else:
            self._factory = None
            self._direct_conn = factory_or_conn
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        """Return the connection for the current call.

        Factory path: returns this thread's connection (lazily opened
        on first use). Legacy path: returns the single shared
        connection passed at construction.
        """
        if self._factory is not None:
            return self._factory.get()
        assert self._direct_conn is not None
        return self._direct_conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection for the current thread.

        On the factory path this returns the *calling* thread's
        connection (committed rows are visible via WAL). On the
        legacy path this returns the single shared connection.
        """
        return self._conn()

    # ── Users ───────────────────────────────────────────────────────────

    def create_user(
        self,
        email: str,
        display_name: str,
        password_hash: str | None = None,
        is_admin: bool = False,
    ) -> User:
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "INSERT INTO users (email, display_name, password_hash, is_admin, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (email, display_name, password_hash, int(is_admin), now, now),
            )
            conn.commit()
            user_id = cursor.lastrowid
        log.info("Created user %d (%s)", user_id, email)
        user = self.get_user_by_id(user_id)  # type: ignore[arg-type]
        assert user is not None
        return user

    def get_user_by_id(self, user_id: int) -> User | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> User | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_password_hash(self, user_id: int) -> str | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return row["password_hash"]

    def update_user(self, user_id: int, **fields: Any) -> User | None:
        if not fields:
            return self.get_user_by_id(user_id)

        # Allowlist of columns that can be updated
        allowed = {
            "email",
            "display_name",
            "password_hash",
            "is_admin",
            "is_active",
            "email_verified",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Cannot update fields: {invalid}")

        # Convert booleans to int for SQLite storage
        params: list[Any] = []
        set_clauses: list[str] = []
        for col, val in fields.items():
            set_clauses.append(f"{col} = ?")
            if isinstance(val, bool):
                params.append(int(val))
            else:
                params.append(val)
        set_clauses.append("updated_at = ?")
        params.append(_now_iso())
        params.append(user_id)

        sql = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = ?"
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(sql, params)
            conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_user_by_id(user_id)

    def list_users(self) -> list[User]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def increment_failed_logins(self, user_id: int) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE users SET failed_login_attempts = failed_login_attempts + 1 "
                "WHERE id = ?",
                (user_id,),
            )
            conn.commit()

    def reset_failed_logins(self, user_id: int) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE users SET failed_login_attempts = 0, locked_until = NULL "
                "WHERE id = ?",
                (user_id,),
            )
            conn.commit()

    def lock_user(self, user_id: int, until: str) -> None:
        """Conditionally lock a user if their failed attempts meet the threshold.

        Only sets ``locked_until`` when ``failed_login_attempts >= 5``.
        """
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "UPDATE users SET locked_until = ? "
                "WHERE id = ? AND failed_login_attempts >= 5",
                (until, user_id),
            )
            conn.commit()
        if cursor.rowcount > 0:
            log.warning("Locked user %d until %s", user_id, until)

    def get_lock_status(self, user_id: int) -> str | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT locked_until FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return row["locked_until"]

    # ── Sessions ────────────────────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        user_id: int,
        expires_at: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                "INSERT INTO user_sessions (id, user_id, created_at, expires_at, "
                "last_seen_at, user_agent, ip_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, now, expires_at, now, user_agent, ip_address),
            )
            conn.commit()
        log.info("Created session for user %d", user_id)

    def get_session(self, session_id: str) -> dict | None:
        """Return session data joined with user info, or None if expired/missing."""
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT s.*, u.email, u.display_name, u.is_admin, u.is_active, "
                "u.email_verified "
                "FROM user_sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.id = ? AND s.expires_at > datetime('now')",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def update_session_last_seen(self, session_id: str) -> None:
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()

    def delete_session(self, session_id: str) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "DELETE FROM user_sessions WHERE id = ?", (session_id,)
            )
            conn.commit()

    def delete_user_sessions(self, user_id: int) -> int:
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM user_sessions WHERE user_id = ?", (user_id,)
            )
            conn.commit()
        count = cursor.rowcount
        if count:
            log.info("Deleted %d session(s) for user %d", count, user_id)
        return count

    def cleanup_expired_sessions(self) -> int:
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM user_sessions WHERE expires_at <= datetime('now')"
            )
            conn.commit()
        count = cursor.rowcount
        if count:
            log.info("Cleaned up %d expired session(s)", count)
        return count

    # ── API Keys ────────────────────────────────────────────────────────

    def create_api_key(
        self,
        user_id: int,
        key_prefix: str,
        key_hash: str,
        name: str,
        expires_at: str | None = None,
    ) -> int:
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "INSERT INTO api_keys (user_id, key_prefix, key_hash, name, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, key_prefix, key_hash, name, expires_at),
            )
            conn.commit()
            key_id = cursor.lastrowid
        log.info("Created API key %d for user %d", key_id, user_id)
        return key_id  # type: ignore[return-value]

    def get_api_key_by_hash(self, key_hash: str) -> dict | None:
        """Return API key data joined with user info, or None if revoked/expired/missing."""
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT k.id AS key_id, k.user_id, k.key_prefix, k.name, "
                "k.created_at, k.expires_at, k.last_used_at, k.revoked_at, "
                "u.email, u.display_name, u.is_admin, u.is_active, u.email_verified "
                "FROM api_keys k "
                "JOIN users u ON u.id = k.user_id "
                "WHERE k.key_hash = ? "
                "AND k.revoked_at IS NULL "
                "AND (k.expires_at IS NULL OR k.expires_at > datetime('now'))",
                (key_hash,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_api_keys(self, user_id: int) -> list[ApiKeyInfo]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, user_id, key_prefix, name, created_at, expires_at, "
                "last_used_at, revoked_at "
                "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            ApiKeyInfo(
                id=r["id"],
                user_id=r["user_id"],
                key_prefix=r["key_prefix"],
                name=r["name"],
                created_at=r["created_at"],
                expires_at=r["expires_at"],
                last_used_at=r["last_used_at"],
                revoked_at=r["revoked_at"],
            )
            for r in rows
        ]

    def revoke_api_key(self, key_id: int, user_id: int) -> bool:
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "UPDATE api_keys SET revoked_at = ? "
                "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (now, key_id, user_id),
            )
            conn.commit()
        if cursor.rowcount > 0:
            log.info("Revoked API key %d for user %d", key_id, user_id)
            return True
        return False

    def update_api_key_last_used(self, key_id: int) -> None:
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (now, key_id),
            )
            conn.commit()

    # ── Admin queries ───────────────────────────────────────────────────

    def get_user_stats(self) -> list[dict]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.is_admin, u.is_active, "
                "u.email_verified, u.created_at, "
                "COALESCE(es.entry_count, 0) AS entry_count, "
                "COALESCE(es.total_words, 0) AS total_words, "
                "es.last_entry_at, "
                "COALESCE(js.job_count, 0) AS job_count "
                "FROM users u "
                "LEFT JOIN ("
                "  SELECT user_id, COUNT(*) AS entry_count, "
                "  SUM(word_count) AS total_words, MAX(created_at) AS last_entry_at "
                "  FROM entries GROUP BY user_id"
                ") es ON es.user_id = u.id "
                "LEFT JOIN ("
                "  SELECT user_id, COUNT(*) AS job_count FROM jobs GROUP BY user_id"
                ") js ON js.user_id = u.id "
                "ORDER BY u.created_at DESC"
            ).fetchall()

            # Compute per-user cost estimates from job type breakdown.
            # Approximate per-job costs (USD) based on typical token usage:
            #   ingest_images/ingest_audio: ~$0.02 (OCR/transcription + embedding)
            #   entity_extraction: ~$0.03 (Claude Opus prompt)
            #   mood_score_entry: ~$0.005 (Claude Sonnet prompt)
            #   mood_backfill: ~$0.005 per entry scored (estimate from job count)
            #   reprocess_embeddings: ~$0.01 (OpenAI embedding calls)
            cost_per_type = {
                "ingest_images": 0.02,
                "ingest_audio": 0.02,
                "entity_extraction": 0.03,
                "mood_score_entry": 0.005,
                "mood_backfill": 0.005,
                "reprocess_embeddings": 0.01,
            }

            cost_rows = conn.execute(
                "SELECT j.user_id, j.type, COUNT(*) AS cnt, "
                "MAX(j.created_at) AS last_job_at "
                "FROM jobs j "
                "GROUP BY j.user_id, j.type"
            ).fetchall()

            # Also get this-week job costs
            week_cost_rows = conn.execute(
                "SELECT j.user_id, j.type, COUNT(*) AS cnt "
                "FROM jobs j "
                "WHERE j.created_at >= date('now', '-7 days') "
                "GROUP BY j.user_id, j.type"
            ).fetchall()

        # Build per-user cost maps
        user_costs: dict[int, float] = {}
        for cr in cost_rows:
            uid = cr["user_id"]
            rate = cost_per_type.get(cr["type"], 0.01)
            user_costs[uid] = user_costs.get(uid, 0.0) + cr["cnt"] * rate

        user_week_costs: dict[int, float] = {}
        for cr in week_cost_rows:
            uid = cr["user_id"]
            rate = cost_per_type.get(cr["type"], 0.01)
            user_week_costs[uid] = user_week_costs.get(uid, 0.0) + cr["cnt"] * rate

        result = []
        for r in rows:
            d = dict(r)
            d["cost_estimate"] = round(user_costs.get(d["id"], 0.0), 2)
            d["cost_this_week"] = round(user_week_costs.get(d["id"], 0.0), 2)
            result.append(d)
        return result

    # ── Preferences ────────────────────────────────────────────────────

    def get_preferences(self, user_id: int) -> dict[str, Any]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                "SELECT key, value FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def get_preference(self, user_id: int, key: str) -> Any | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def set_preference(self, user_id: int, key: str, value: Any) -> None:
        now = _now_iso()
        encoded = json.dumps(value)
        conn = self._conn()
        with self._lock:
            conn.execute(
                "INSERT INTO user_preferences (user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, key) DO UPDATE SET value = ?, updated_at = ?",
                (user_id, key, encoded, now, encoded, now),
            )
            conn.commit()

    def delete_preference(self, user_id: int, key: str) -> bool:
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM user_preferences WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            conn.commit()
        return cursor.rowcount > 0
