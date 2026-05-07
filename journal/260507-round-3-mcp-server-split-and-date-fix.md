# 2026-05-07 — Round 3 work: mcp_server.py split + heading-detector date fix

Two unrelated changes landed in the same worktree (`eng-round-3-and-date-fix`)
because the user requested them together. Logical commits per change.

## What landed

1. **heading_detector: keep date phrase in body** (`dc88968`).
   Reverted the `source_phrase` strip in `AnthropicHeadingDetector.detect`.
   The detector still resolves `heading_text` and `date_iso` (which drive the
   entry's filing date and title), but `body` now equals the input with only
   the heading-area leading whitespace dropped — the date phrase itself stays
   as the first line of the entry text. Voice and image ingestion paths
   inherit automatically since they consume `det.body` directly.
   `HeadingDetectionResult.to_text()` was test-only ceremony; deleted along
   with its three direct tests. 4 tests retargeted to assert the date phrase
   is preserved.

2. **Docs: refactor-follow-ups tidy** (`cd76a6f`). Round-3 Recommendation 1.
   Top-of-doc pointer to `refactor-round-3.md`; `~66` → `37` in the
   item-3 residual section and the standing-facts table.

3. **Docs: mcp_server.py planning round** (`32f72da`). Round-3
   Recommendation 2. New `docs/refactor-mcp-server-plan.md` with the
   proposed package shape, six surfaced decisions, and the acceptance
   criteria for the extraction itself. No code changes.

4. **mcp_server: convert to package — commit A** (`83f881d`). Pure
   relocation: `mcp_server.py` → `mcp_server/_legacy.py`, new
   `__init__.py` re-exporting every symbol callers touch, new
   `__main__.py` so `python -m journal.mcp_server` keeps working.
   Test patches in `tests/test_lifespan.py` retargeted to
   `journal.mcp_server._legacy.X` since re-exports do not share
   binding with their source module.

5. **mcp_server: split package into real modules — commit B**
   (`75368c3`). `_legacy.py` carved into `bootstrap.py` (475),
   `app.py` (26), `runserver.py` (93), `tools/_ctx.py` (46),
   `tools/queries.py` (233), `tools/ingestion.py` (312),
   `tools/entities.py` (186), `tools/jobs.py` (240). Test patches
   moved one final hop to `journal.mcp_server.bootstrap.X`.

The two refactor commits were intended to be three (A, B, C) per the
planning doc. C was "retarget test patches" — but since `_legacy.py`
stops existing the moment commit B lands, the test retarget had to
ship with B. The planning doc said this was a likely outcome
("if commit B passes the suite as-is — because re-export keeps the
binding alive at the test path — commit C is a no-op and can be
dropped"). What actually happened was the opposite: re-export did NOT
keep the binding alive at the test path even at commit A, so the
retarget had to ship in BOTH commit A (`_legacy`) and commit B
(`bootstrap`). One small mechanical edit in each.

## Decisions made

The six decisions from the planning doc all stood — none were revised
during execution:

1. `mcp = FastMCP(...)` lives in `app.py`; tools import it from there.
2. Test patches retarget to the originating module
   (`journal.mcp_server.bootstrap.X`).
3. `__init__.py` re-exports every name external callers use today.
   Listed explicitly with `__all__` for IDE / `from x import *` hygiene.
4. The 115-line on-change callback stays inline in `_init_services`.
   Extracting would require either a mutable services-dict factory or
   threading 4 captures through a fixed-signature callback — neither
   pays back without dedicated callback unit tests.
5. `python -m journal.mcp_server` keeps working via `__main__.py`. No
   pyproject.toml change.
6. Three planned commits collapsed to two. See above.

## Verification

Standing facts captured 2026-05-07 (post-split):

- 1799 unit + 8 integration = 1807 total. (Was 1808 pre-split: lost 1
  to the deletion of three `to_text()` tests, replaced with 2.)
- Reach-in residual unchanged: tests 37, api 0.
- File sizes top-10:

| File | Lines |
|---|---:|
| `db/repository.py` | 1603 |
| `auth_api.py` | 840 |
| `services/entity_extraction/service.py` | 808 |
| `providers/transcription.py` | 778 |
| `providers/ocr.py` | 753 |
| `services/notifications.py` | 744 |
| `api/entities.py` | 717 |
| `cli/_seed_samples.py` | 679 |
| `api/dashboard.py` | 609 |
| `mcp_server/bootstrap.py` | 475 |

`mcp_server.py` is gone from the top-10. The largest piece of the
former monolith (`bootstrap.py` at 475) is well under the 500-line
target and below the original "smell" threshold of 800.

- `python -c "from journal.mcp_server import main, lifespan, mcp,
  journal_ingest_text"` succeeds — re-export surface intact.
- `len(mcp._tool_manager._tools)` == 19 (6 queries + 6 ingestion + 4
  entities + 3 jobs).
- `ruff check src/ tests/` clean.

## Process notes worth remembering

- **Re-exports do not share binding with their source.** Both commits
  A and B needed test-patch retargets. The very common idiom
  `monkeypatch.setattr("journal.mcp_server.load_config", ...)` works
  ONLY when `_init_services` reads `load_config` from the same
  module's namespace. After splitting, the read site is in
  `bootstrap.py`, so the patch must target
  `journal.mcp_server.bootstrap.load_config`. The same applies to
  `mcp_module._services = None` reset fixtures: assigning through the
  facade resets the facade's binding, not the source module's.
- **The planning round was load-bearing.** The six decisions in
  `docs/refactor-mcp-server-plan.md` covered every question that came
  up during execution — most importantly, the re-export-doesn't-share-
  binding subtlety was anticipated. Skipping the planning round and
  jumping straight to extraction would have wasted a debugging cycle
  per surprise.
- **Combine commits when an intermediate state can't pass.** The plan
  proposed three commits because B and C were independent in spec.
  In practice, after B lands, the path patched in C's intermediate
  state (`_legacy`) no longer exists, so the suite can't pass between
  the two commits. Honest answer: ship them together.
