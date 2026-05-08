# Item-6 exceptions — umbrella planning round

**Status:** active. **Last updated:** 2026-05-07. **Supersedes:** none.
Tracked under Round-3 § B in [`refactor-round-3.md`](./refactor-round-3.md).

The three remaining size-cap exceptions documented in
`docs/refactor-round-3.md` § B and the round-3 standing facts table:

1. `api/entities.py` (717 lines)
2. `services/entity_extraction/service.py` (808 lines)
3. `auth_api.py` (840 lines)

Each gets a section below. **Item 2's headline conclusion is "do
not split" — see § Item 2 for the reasoning.** Items 1 and 3 are
real refactor candidates with concrete proposals.

This is a read-only planning doc. Bring the proposals back for
sign-off before extracting.

---

## Standing facts (verified 2026-05-07)

- HEAD: `dbedd67` ("Docs: round-3 doc — drop closed follow-ups
  from 'what next' list").
- Tests: 1796 unit + 8 integration = 1804 total. (Was 1799 before
  the legacy entity-method deletion shaved 3.)
- Reach-in gates: api `0`, tests `37` (unchanged).
- File sizes (sources of truth: re-run
  `find src/journal -name '*.py' -exec wc -l {} + | sort -rn | head -10`):

  | File | Lines | Notes |
  |---|---:|---|
  | `auth_api.py` | 840 | Item-6 exception. Largest in repo. |
  | `services/entity_extraction/service.py` | 808 | Item-6 exception. |
  | `providers/transcription.py` | 778 | Within range. |
  | `providers/ocr.py` | 753 | Within range. |
  | `services/notifications.py` | 744 | Acknowledged. |
  | `api/entities.py` | 717 | Item-6 exception. |
  | `cli/_seed_samples.py` | 679 | Pure data. |

---

## Sequencing recommendation

1. **Item 1 first** — `api/entities.py`. Smallest, has a prior
   sketch in `journal/260507-api-py-split-unit-1a.md`, two-file
   split with no shared-helper friction (every helper is already
   in `api/_shared.py`). Estimated extraction time: ~1.5 hours.
   Best warm-up.
2. **Item 3 second** — `auth_api.py`. Bigger, no prior planning,
   security-sensitive surface. The two-file pattern from Item 1
   doesn't fit; this needs a 4-file package conversion plus a
   shared-helpers module. Estimated extraction time: ~3 hours.
3. **Item 2 — do nothing.** See § Item 2 below: the 808-line
   orchestrator is the design, the prior journal sketch already
   considered and rejected further splitting, and this independent
   re-analysis confirms that conclusion. Item 2's file should be
   moved off the item-6 candidates list and into a dedicated
   "acknowledged-permanent exceptions" sub-table.

If only one of Items 1 and 3 lands, **Item 1 has the higher
value-to-effort ratio.** Item 3's security-sensitive nature
means the extraction needs careful per-cluster code review
rather than the mechanical AST-extraction approach that worked
for the repository split.

---

## Item 1: `api/entities.py` split

### What the file is doing

A 717-line FastMCP route module under `src/journal/api/`. 16
route handlers grouped into two clear conceptual clusters that
the file's own inline comments (lines 210, 384, 495, 601) already
delineate. Module-level docstring explicitly notes "A future
split into `entities.py` (CRUD) + `entity_merge.py` (merge,
candidates, quarantine, aliases) is parked until growth pressure
forces it." The split shape is already on file.

No helpers, no Pydantic models, no module-level state — every
helper is already in `api/_shared.py` (`_entity_detail`,
`_entity_summary`, `_mention_dict`, `_relationship_dict`).

### Cluster mapping

#### Cluster A → `entities.py` (CRUD + read sub-resources, 9 routes)

| Method | Path | Lines | Type | Services |
|---|---|---:|---|---|
| GET | `/api/entities` | 60–99 | R | entity_store |
| GET | `/api/entities/{id}` | 101–120 | R | entity_store |
| GET | `/api/entities/{id}/mentions` | 122–172 | R | entity_store, query_svc |
| GET | `/api/entities/{id}/relationships` | 174–208 | R | entity_store |
| PATCH | `/api/entities/{id}` | 212–299 | W | entity_store, job_runner |
| DELETE | `/api/entities/{id}` | 301–321 | W | entity_store |
| GET | `/api/entities/aliases/lookup` | 386–414 | R | entity_store |
| POST | `/api/entities/{id}/aliases` | 416–462 | W | entity_store |
| DELETE | `/api/entities/{id}/aliases/{alias}` | 464–493 | W | entity_store |

**Subtotal: 9 routes, ~320 lines of route code.**

#### Cluster B → `entity_merge.py` (merge / dedup / quarantine, 7 routes)

| Method | Path | Lines | Type | Services |
|---|---|---:|---|---|
| POST | `/api/entities/merge` | 323–382 | W | entity_store |
| GET | `/api/entities/merge-candidates` | 603–642 | R | entity_store |
| PATCH | `/api/entities/merge-candidates/{id}` | 644–691 | W | entity_store |
| GET | `/api/entities/{id}/merge-history` | 693–717 | R | entity_store |
| GET | `/api/entities/quarantined` | 497–515 | R | entity_store |
| POST | `/api/entities/{id}/quarantine` | 517–564 | W | entity_store |
| POST | `/api/entities/{id}/release-quarantine` | 566–599 | W | entity_store |

**Subtotal: 7 routes, ~270 lines of route code.**

#### Service-dependency cleanliness

- Cluster A: `entity_store` + `query_svc` (one route) + `job_runner` (one route, optional).
- Cluster B: `entity_store` only.
- Zero cross-cluster handler-to-handler calls.
- Zero module-local helpers; both clusters import from `_shared.py`.

### Decisions to surface for sign-off

#### 1. File names follow the existing api/ package convention

`api/entities.py` (kept as-is, shrunk) + `api/entity_merge.py`
(new). Underscore in `entity_merge` is consistent with no current
api/ file using it, but it's a multi-word concept; the sibling
`api/_shared.py` is the only underscore-prefixed module and that
underscore signals "private". `entity_merge` (no leading
underscore) is the right shape.

#### 2. Two `register_*_routes()` functions, called from `api/__init__.py`

Mirrors the existing pattern. `api/__init__.py`'s
`register_api_routes()` already calls one
`register_entities_routes()`; after the split it calls both
`register_entities_routes()` and `register_entity_merge_routes()`.

```python
# api/__init__.py — additions only
from journal.api.entity_merge import register_entity_merge_routes

def register_api_routes(...):
    ...
    register_entities_routes(mcp, services_getter)
    register_entity_merge_routes(mcp, services_getter)
```

The public surface (`from journal.api import register_api_routes`)
is unchanged.

#### 3. Where does `entity_merge_history` go?

The route `GET /api/entities/{id}/merge-history` is shaped like a
read on an entity but the data it returns is merge-audit only.
**Recommendation:** put it in `entity_merge.py`. It's a read but
it sits next to merge-candidate logic and the merge-execution
route — keeping the merge-audit story in one file beats keeping
"reads on /entities/{id}/*" in one file. If a future agent wants
"all reads on a single entity", grep is the right tool, not file
layout.

#### 4. Where do alias mutations go?

Alias add/delete (POST/DELETE under `/api/entities/{id}/aliases/...`)
are sub-resource writes. **Recommendation:** keep them in
`entities.py`. Aliases are entity metadata — the alias-lookup
route is also there, and grouping all alias touchpoints in one
file simplifies "did changing aliases break this?" investigation.

The Cluster B clustering does not own aliases despite some
aliases being created during merge: the merge route writes
aliases as a side-effect of consolidation, but the user-facing
mental model "aliases are entity metadata" trumps the
implementation detail.

#### 5. Test patch retargets

Identical to the repository split: verify by grep before
extracting that no test does `patch("journal.api.entities.X")`.
If clean, no retargets needed and the third commit can be
dropped.

#### 6. No `_legacy.py` shell

Unlike `mcp_server` and `db/repository`, this is a within-package
move (we're staying under `api/`), not a file → package
conversion. Two-commit shape is sufficient:

- **Commit A**: create `api/entity_merge.py`, move 7 routes to it,
  remove them from `api/entities.py`, register both functions in
  `api/__init__.py`. Run full suite. No re-export work because
  no caller imports from `api/entities` directly — they all go
  through `register_api_routes`.
- **Commit B** (only if needed): test patch retargets. Expected
  to be a no-op.

### Acceptance criteria

1. `wc -l src/journal/api/entities.py src/journal/api/entity_merge.py`
   shows both under 400 lines (target ~320 + ~270).
2. `uv run pytest -q -m 'not integration'` passes (1796 unit).
3. `uv run pytest -m integration -q` passes (8 integration).
4. `uv run ruff check src/ tests/` passes.
5. `python -c "from journal.api import register_api_routes"`
   succeeds.
6. Reach-in gates: api `0`, tests `37`.
7. Both new file headers carry their own module docstring
   describing the cluster ownership and the routing rule that
   put each route there.
8. `api/__init__.py` registers both new functions in the same
   `register_api_routes` body, in the order
   `entities → entity_merge`.

---

## Item 2: `services/entity_extraction/service.py` — recommend no split

### Headline

**Do not split this file further.** Move it from "item-6
exceptions" (which implies "split eventually") to a separate
"acknowledged-permanent" sub-table in `refactor-round-3.md`'s
standing-facts.

### Why this section exists

When I asked the user "what's next?" after the repository split
landed, this file appeared in the natural follow-ups list because
it's an item-6 exception over the size cap. Item-6 exceptions
are by definition "no action unless forced", but the file's
appearance prompted a fresh look.

The fresh look (cluster mapping, instance-state usage, dependency
graph, cross-cluster edges) independently arrives at the same
conclusion as the prior journal sketch
(`journal/260507-unit-2-entity-extraction-split.md`): the
orchestrator is the right size for what it does.

### What the file is

`EntityExtractionService` is the orchestrator that turns "raw
entry text" into "deduplicated entities + relationships persisted
to the entity store". 808 lines, 9 methods, 10 instance fields,
1 module-level helper.

The file is **already the result of a split.** The original was
1187 lines; the round-2 work pulled `signature.py` (224),
`matching.py` (136), and `sanity.py` (123) out and left the
`service.py` orchestrator at 808.

### Why further splitting doesn't pay

#### 1. The orchestrator is glue, not duplication

`extract_from_entry` (lines 222–515, ~300 lines) is the main
orchestration method. It calls `extract_entities`, manages
idempotency, loops over extracted entities and relationships,
runs the sanity sweep, prunes orphans, and marks the entry
extracted. Pulling any of this into a sibling module just turns
sequential glue code into stateful glue code with extra import
hops.

#### 2. `_resolve_entity` cannot be cleanly extracted

The 132-line `_resolve_entity` (lines 622–753) is a multi-stage
decision tree (signature → LLM-asserted → exact-canonical →
alias → embedding-similarity → create). It reads 4 instance
fields directly (`_store`, `_embeddings`, `_threshold`,
`_llm_match_min_cosine`) and calls 8 store methods + 3 imported
helpers. The journal sketch already noted the only way to
extract it as a free function would be a 14-arg signature or an
`ExtractionContext` dataclass that bundles those fields. Both
options "move lines, not eliminate them."

#### 3. Mixin-on-self doesn't help here

The pattern that worked for `db/repository/` and `entitystore/`
— mixins composing into the host class via MRO — assumed the
methods being separated were data-access primitives that only
shared the connection. `EntityExtractionService` is the
opposite: the orchestrator IS the business logic, the helpers
are already in sibling modules, and what remains is
densely-coupled integration code where every method needs ~3–5
instance fields.

#### 4. Two leaf methods *could* be extracted, but the gain is small

- `build_known_entity_candidates` (70 lines): touches 4 fields,
  could become a free function `build_known_entity_candidates(
  store, embeddings, entry_text, user_id, top_k, threshold)`.
- `reembed_entity_for_description` (44 lines): touches 2 fields,
  same shape.

Total bulk movable this way: ~115 lines, dropping `service.py`
from 808 to ~700. That puts it at the top of the "tolerated"
range alongside `api/entities.py` (717), but the cost is two
new module imports and two refactored call sites for a 13%
reduction in file size. Not worth it.

### Recommendation

1. **Do not extract anything from this file in the current
   round.** It's already in the second-best shape it can be in
   without an architectural rewrite.
2. **Reclassify in `refactor-round-3.md`.** Move
   `services/entity_extraction/service.py` from the "item-6
   exception" list (which implies "future split candidate") to a
   new "acknowledged-permanent" sub-table that says explicitly
   "this size is by design; do not propose splitting again
   without an architectural change". The reclassification is
   a documentation-only commit.
3. **If the file ever crosses ~1000 lines**, that's the trigger
   to revisit — but only with a redesign of the
   `_resolve_entity` decision tree as a state machine, not as
   another mechanical split.

This is the kind of "don't do it" planning output the team
benefits from most. Saves a future session that would otherwise
walk into the same wall.

---

## Item 3: `auth_api.py` split

### What the file is doing

840 lines, 20 route handlers, 6 module-level helpers. Currently
sits at the package root (`src/journal/auth_api.py`), not under
`api/` — historical artefact from before the api/ package split.
Two registration functions today: `register_auth_routes()`
(user-facing, 14 routes) and `register_admin_routes()`
(admin-gated, 6 routes).

Three cross-cutting helpers used by every route or close to it:
- `_services_or_503()` — used by all 21 call sites; service-
  availability guard.
- `_user_to_dict()` — used by 12 routes; user-response
  serialiser.
- `_api_key_info_to_dict()` — used by 2 routes; API-key
  serialiser.

The helpers' ubiquity is the central design constraint: any
split that doesn't address them will produce duplicated helpers
across the new files. A `_shared.py` (or `_helpers.py`) module
is non-optional.

### Cluster mapping

| Cluster | Routes | Lines | Services |
|---|---:|---:|---|
| **A. Session & current user** (login, logout, me-read, public config) | 4 | ~115 | AuthService, get_authenticated_user |
| **B. Account lifecycle** (register, email-verify, forgot-password, verify-reset-token, reset-password, resend-verification) | 6 | ~330 | AuthService, EmailService (optional), Config, SQLiteUserRepository |
| **C. Profile** (PATCH /me) | 1 | ~45 | SQLiteUserRepository |
| **D. API keys** (POST/GET /api-keys, DELETE /api-keys/{id}) | 3 | ~100 | AuthService |
| **E. Admin: user management** (list, update is_admin/is_active) | 2 | ~95 | SQLiteUserRepository |
| **F. Admin: dynamic reloads** (4 reload endpoints) | 4 | ~95 | services.reload helpers |

### Proposed package shape

Convert `auth_api.py` to a package — same approach as
`mcp_server/` and `db/repository/`.

```
src/journal/auth_api/
  __init__.py        ~30  facade — re-exports register_auth_routes
                          and register_admin_routes
  _shared.py        ~110  _services_or_503, _user_to_dict,
                          _api_key_info_to_dict, anything else
                          referenced by ≥2 route modules
  core.py           ~140  Cluster A: login, logout, me-read,
                          auth-config (the public registration-
                          flag endpoint). The security audit
                          surface stays here, intact.
  account.py        ~340  Cluster B: register, email-verify,
                          forgot-password, verify-reset-token,
                          reset-password, resend-verification.
                          Owns the registration toggle, email
                          dispatch (best-effort), and password-
                          reset token flow. Largest cluster.
  profile.py         ~55  Cluster C: PATCH /me. Tiny but its own
                          file because it's the only profile-
                          mutation route and growth here is
                          plausible (avatar, locale, timezone).
  api_keys.py       ~110  Cluster D: API key CRUD.
  admin.py          ~210  Clusters E + F: admin user management
                          + dynamic reload endpoints. Both
                          share is_admin gating; not worth
                          splitting further unless one cluster
                          grows.
```

Total ~995 lines (vs current 840 = ~18% growth from per-file
imports + module docstrings + the helpers module + facade).
Largest resulting file is `account.py` at ~340 lines, well
under the 500-line target.

### Decisions to surface for sign-off

#### 1. Package conversion vs sibling files

`auth_api.py` lives at the package root. Three options:

1. **Convert to `auth_api/` package** (recommended). Mirrors
   `mcp_server/` and `db/repository/`. Re-export surface in
   `__init__.py` keeps every existing
   `from journal.auth_api import register_auth_routes,
   register_admin_routes` working untouched.
2. **Split into sibling files**:
   `auth_api.py` + `auth_api_account.py` + `auth_api_keys.py` +
   `auth_api_admin.py` + `auth_api_helpers.py` at
   `src/journal/`. Adds five top-level modules to the package
   root for one feature; no other feature in this codebase
   uses that pattern.
3. **Move the whole feature into `api/`** as
   `api/auth.py` etc. Architecturally cleanest — the api/
   package was created to hold HTTP route modules — but the
   cross-cutting impact is large: every importer changes, the
   `register_*` registration call sites move, and the `auth`
   module already lives at `journal.auth` which would create a
   confusing two-module situation.

**Recommendation:** option 1. Same pattern as the two recent
splits, lowest disruption.

#### 2. Helpers module name: `_shared.py` vs `_helpers.py`

`api/_shared.py` is the existing precedent. **Recommendation:**
`auth_api/_shared.py` to match.

#### 3. Where does the public `auth-config` route go?

`GET /api/auth/config` returns `{"registration_enabled": ...}`
to anonymous callers (no auth required). Two natural homes:
- **`core.py`** — it's a session/auth endpoint, even though
  it's anonymous.
- **`account.py`** — the registration-enabled flag is read
  before the register endpoint is called.

**Recommendation:** `core.py`. It's a single-route concern
about authentication infrastructure, not specifically about
registration.

#### 4. Profile in its own file or folded into core?

`PATCH /api/auth/me` is the only profile-mutation route today
(~45 lines). Two options:
- **Its own `profile.py`** (~55 lines). Trivially small but
  positioned for growth (avatar upload, locale, timezone, theme,
  ...).
- **Fold into `core.py`** alongside the `GET /me` read. Cluster
  by URL path, not by intent.

**Recommendation:** `profile.py`. The principles doc explicitly
calls out that "where do I add the new GET on entities?" should
be answerable on first grep. Putting `me-update` in
`profile.py` makes "where do profile mutations live?" answerable
on first grep too, and the file size cost is minimal. If profile
never grows, the file stays small — that's fine.

#### 5. Two admin clusters (E + F) folded into one `admin.py`?

Cluster E (user management) and Cluster F (dynamic reloads) are
both admin-gated but address different concerns: E mutates user
records, F reloads file-backed config. Combined ~190 lines.

**Recommendation:** fold both into `admin.py`. They share the
admin gating pattern, the file stays under 250 lines, and
splitting further is "cluster of two routes" territory — over-
fragmentation. If reload endpoints grow into their own subsystem
(rate-limited, audit-logged, etc.) split then.

#### 6. The `_services_or_503` ubiquity is a feature, not a bug

Every route in this file calls `_services_or_503()` first thing.
Three options:

1. **Keep in `_shared.py` and import everywhere** (recommended).
   One source, every route module imports.
2. **Wrap as a decorator** so each route reads
   `@requires_services` and the body assumes services are
   available. Cleaner per-route but more magic; new pattern not
   used elsewhere in the api/ package.
3. **Move into `journal.api._shared`** since that already has
   resource-helper precedent. Cross-package coupling — both
   `api/` and `auth_api/` would import the same helper.

**Recommendation:** option 1. Match `api/_shared.py`'s pattern
exactly. No new abstractions.

#### 7. Test patch retargets

Same drill as the previous splits:

```bash
grep -rE 'patch\("journal\.auth_api\.(.+)"\)|monkeypatch\.setattr\("journal\.auth_api\.' tests/
```

Verify before extracting. The auth surface is more likely than
the repository was to have specific patches (e.g., mocking
`AuthService.create_session` to test session-cookie flow, or
patching `_user_to_dict` for response shape assertions). If
hits exist, the third commit retargets them; if zero, drop the
third commit.

#### 8. Security-sensitive review

Unlike the repository split (mechanical AST extraction), the
auth_api split touches the security boundary. Three guards:

1. **Per-cluster code review.** Each new module gets reviewed
   for "did anything change?" before commit B closes. Specific
   focus: session cookie issuance + clearing, password handling,
   token validation, admin gating.
2. **Diff verification step in commit B.** Run
   `git diff <pre-split>..HEAD -- src/journal/auth_api/` and
   confirm every body is byte-for-byte identical to its
   predecessor. The split moves code; the only edits are the
   import paths and the registration-function bodies. Any other
   diff is a bug.
3. **Manual smoke test.** After commit B, exercise login →
   me → logout in the dev environment (browser, not just curl).
   The pre-push hook runs the suite, but a real cookie roundtrip
   catches "the session cookie helper got mis-imported" errors
   that unit tests with mocks can miss.

#### 9. Inline imports inside route handlers

Lines 227 and 309 do `from journal.api import _runtime_get`
inside the route body. This was likely a circular-import workaround.

**Recommendation:** during the split, hoist these to top-level
imports if possible, or document why they're inline. Don't paper
over a circular import — investigate.

If hoisting introduces a circular import, the circular-import
direction tells us something architectural worth recording.

#### 10. Commit shape (3 commits, mirroring repository split)

1. **Commit A** — package shell with `_legacy.py`. `auth_api.py`
   → `auth_api/_legacy.py` (git rename). New `auth_api/__init__.py`
   re-exporting `register_auth_routes` and `register_admin_routes`
   from `_legacy`. Run full suite — must be green. **No code
   changes**, only file moves and the facade.
2. **Commit B** — carve `_legacy.py` into `_shared.py`,
   `core.py`, `account.py`, `profile.py`, `api_keys.py`,
   `admin.py`. Update `__init__.py` re-exports to point at the
   real modules. Delete `_legacy.py`. Run full suite. The 7
   route handler bodies move byte-for-byte; only their import
   paths change.
3. **Commit C** — test patch retargets, if any. If commit B's
   suite passes cleanly, drop commit C.

### Acceptance criteria

1. `find src/journal/auth_api -name '*.py' -exec wc -l {} + |
   sort -rn` shows every file under 400 lines (target).
2. `uv run pytest -q -m 'not integration'` passes (1796 unit).
3. `uv run pytest -m integration -q` passes (8 integration).
4. `uv run ruff check src/ tests/` passes.
5. `python -c "from journal.auth_api import register_auth_routes,
   register_admin_routes"` succeeds.
6. Reach-in gates: api `0`, tests `37` (unchanged).
7. `git diff <pre-split>..HEAD -- src/journal/auth_api/` shows
   only structural moves — every route handler body is
   byte-identical to its predecessor (verified by reading the
   diff).
8. Manual login → me → logout smoke test against a local dev
   server succeeds.
9. `auth_api.py` is removed from the repo's top-10 file size
   list. Largest auth_api/ file is `account.py` (~340 lines).

---

## What this plan does NOT do

1. **Does not split** `services/entity_extraction/service.py`.
   See § Item 2 — recommendation is to reclassify it as
   acknowledged-permanent and remove it from the item-6
   candidates list.
2. **Does not address** the within-range files that grew
   recently (`services/notifications.py` at 744 from item-3
   part E, `providers/transcription.py` at 778, `providers/ocr.py`
   at 753). These are within the soft-cap; touching them is out
   of scope for this round.
3. **Does not introduce** Pydantic models or schema-based
   request validation in `auth_api`. The hand-rolled validation
   pattern carries over byte-for-byte; standardising on
   Pydantic is a separate refactor.
4. **Does not change** the `_services_or_503` /
   `get_authenticated_user` patterns. Same shape pre and post
   split.
5. **Does not move** `journal.auth` (the cookie/middleware
   module) — only `journal.auth_api` (the HTTP route module).
6. **Does not address** the round-2 item 1.1 cross-call
   shared-connection race or the transaction-pattern question
   (already closed in the repository package).

---

## Sessions

Per the standing process note ("plan first, then extract"):

1. **This planning round** — committed on its own as a docs
   change. No code touched. Brings all three proposals back for
   sign-off.
2. **Item 1 extraction** — `api/entities.py` split. Two commits
   (A: create `entity_merge.py`, move 7 routes; B optional: test
   patch retargets). Estimated 1.5 hours including re-running
   the suite after each commit.
3. **Item 2 — documentation-only commit.** Reclassify
   `services/entity_extraction/service.py` in
   `refactor-round-3.md` from "item-6 exception" to
   "acknowledged-permanent". One commit, ~10 minutes. Could be
   bundled into the Item 1 extraction PR as a follow-up.
4. **Item 3 extraction** — `auth_api.py` split. Three commits
   (A: package shell; B: carve; C: optional test retargets).
   Estimated 3 hours including the security-sensitive code
   review at commit B and the manual smoke test.

If batched in one session: Item 1 + Item 2 first (~2 hours),
land + push + watch CI, then Item 3 in a separate worktree (~3
hours) so the security-sensitive surface gets clean attention.

If only one item ships: **Item 1**. Smallest, lowest risk,
clearest payoff.
