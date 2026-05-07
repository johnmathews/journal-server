# auth_api.py split — item-6 exception batch 2 (Item 3)

Picked up `docs/refactor-item-6-exceptions-plan.md` § Item 3, the
final outstanding item-6 exception. The batch-1 work landed on
2026-05-07 (Item 1: `api/entities` → `entities + entity_merge` and
Item 2: `entity_extraction/service.py` reclassified as
acknowledged-permanent).

## Standing-facts re-verification before starting

- HEAD: `02fa834` (item-6 exceptions batch 1 journal). Tree clean.
- `wc -l src/journal/auth_api.py` → 840 (matches plan).
- `grep -rE 'patch\("journal\.auth_api\.|monkeypatch\.setattr\("journal\.auth_api\.' tests/`
  → zero hits, so plan's optional commit C (test patch retargets)
  was a no-op and dropped from the start.

## What the file was

840 lines, two registration functions:

- `register_auth_routes` — 14 user-facing routes (login, logout,
  GET/PATCH /me, register, the public auth-config flag, four
  password/email-recovery routes, `POST/GET /api/auth/api-keys`
  with internal method dispatch, `DELETE /api/auth/api-keys/{id}`).
- `register_admin_routes` — 6 admin-gated routes (list/update users,
  4 dynamic-reload endpoints).

Three module-level helpers (`_services_or_503`, `_user_to_dict`,
`_api_key_info_to_dict`) plus two nested API-key helpers.

## Result

`src/journal/auth_api.py` (840 lines) → `src/journal/auth_api/`:

| File | Lines | Responsibility |
|---|---:|---|
| `__init__.py` | 62 | Composes `register_auth_routes` from the four user-facing cluster registers; re-exports `register_admin_routes` and the two `_*_to_dict` helpers (the helper re-exports preserve test imports). |
| `_shared.py` | 65 | `_services_or_503`, `_user_to_dict`, `_api_key_info_to_dict`. |
| `core.py` | 134 | Login, logout, GET /me, public auth-config flag (Cluster A). |
| `account.py` | 355 | Register, verify-email, forgot-password, verify-reset-token, reset-password, resend-verification (Cluster B). |
| `profile.py` | 81 | PATCH /me only (Cluster C — sized for growth). |
| `api_keys.py` | 140 | API key CRUD (Cluster D). |
| `admin.py` | 236 | Admin user management + dynamic reloads (Clusters E + F). |

Largest file is `account.py` at 355 lines, well under the 400-line
target (acceptance #1). Total grew 28% (vs plan's 18% estimate)
from per-file imports + module docstrings.

## Commit shape (2 commits, dropped C)

1. **Commit A** (`d1992a4`) — `git mv auth_api.py auth_api/_legacy.py`
   + new `__init__.py` re-exporting all four symbols
   (`register_auth_routes`, `register_admin_routes`,
   `_user_to_dict`, `_api_key_info_to_dict`). Caught early: my
   pre-flight grep checked for `patch(...)` patterns but missed
   `from journal.auth_api import _user_to_dict` in
   `tests/test_auth_api.py`; added the helper re-exports to keep
   commit A truly behavior-free. 1796 unit + 8 integration green.
2. **Commit B** (`bb00065`) — Carve `_legacy.py` into the six
   purpose-named modules above, delete `_legacy.py`, repoint
   `__init__.py` re-exports. 18 of 20 route handler bodies
   byte-identical to predecessors (verified by AST-extracted
   per-function diff); the two exceptions are `auth_register` and
   `auth_config`, which had inline `from journal.api import
   _runtime_get` imports inside their bodies that I hoisted to
   module level (plan decision #9, "investigate before papering
   over"). The function-level `from journal.services.reload import
   ...` at the top of `register_admin_routes` was hoisted similarly.
3. **Commit C** dropped — pre-flight grep showed zero
   `patch("journal.auth_api...")` sites.

## Inline-import hoist verdict

Both inline-import sites turned out to be safe to hoist:

- `journal.api` does NOT import from `journal.auth_api` (verified by
  `grep -rE 'from journal\.auth_api|import journal\.auth_api'
  src/journal/api/` → empty).
- `journal.services.reload` imports only `journal.providers.*`, no
  cycle risk.

The original inline imports were defensive but, on inspection,
unnecessary. Hoisting trims four lines from handler bodies and
removes the "why is this import inside the function?" question for
future readers. Keeping commit B's "byte-identical" claim narrow:
**every other** handler body is unchanged.

## Acceptance criteria check

All 9 of the plan's Item 3 acceptance criteria pass:

1. ✅ All `auth_api/` files under 400 lines (target). Largest 355.
2. ✅ `uv run pytest -q -m 'not integration'` → 1796 passed.
3. ✅ `CHROMA_PORT=8401 uv run pytest -m integration -q` → 8 passed.
4. ✅ `uv run ruff check src/ tests/` → All checks passed.
5. ✅ `python -c "from journal.auth_api import register_auth_routes,
   register_admin_routes"` succeeds.
6. ✅ Reach-in gates: api/ = 0, tests/ = 37 (unchanged from baseline,
   re-verified with the project's actual gate commands from
   `docs/refactor-round-3.md` § Standing facts).
7. ✅ Per-handler byte-identity verified by AST extractor + diff.
   18/20 handlers byte-identical; 2 carry the documented hoist.
8. ✅ Manual smoke test: registered `smoketest@local.dev` via the
   webapp at localhost:5173 (backend localhost:8400, ChromaDB
   localhost:8401). Cycled login → me → logout → login. All four
   API responses 200/201. Backend log lines cite the new module
   paths (`journal.auth_api.account` for register, `journal.auth_api.core`
   for login/me/logout). Stopped backend + webapp after the test.
9. ✅ `auth_api.py` removed from the top-10 file size list. Top
   slot is now `services/entity_extraction/service.py` at 808 (the
   one we deliberately don't split).

## Standing-facts updates (in this commit)

`docs/refactor-round-3.md` updated in three places:

1. The "Acknowledged item-6 exceptions still in place" table —
   `auth_api.py` row marked RESOLVED, mirroring the api/entities
   format from batch 1.
2. The "My pick for the next session" list — auth_api split
   removed from the natural-follow-ups list, with a note that all
   three item-6 exceptions are now dispositioned.
3. The top-10 file-sizes table — `auth_api.py` removed,
   `providers/extraction.py` (560) becomes the bottom of the
   top-10. Added a note that the largest `auth_api/` file
   (`account.py` at 355) does not make the top-10.

## What this batch closes

After this commit, the round-3 refactor is fully closed end-to-end.
The standing-facts table's "acknowledged-but-pending" bucket is now
empty: every item-6 exception is either split (Items 1, 3) or
acknowledged-permanent (Item 2 — the `entity_extraction/service.py`
orchestrator). The reach-in gates are unchanged. The next refactor
session will start from a clean baseline; recommended pick (per
the plan and the round-3 doc) is the item-3 residual cleanup —
only worth touching if a specific cluster of the 37 reach-ins
surfaces real friction during unrelated work.
