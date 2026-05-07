# Item 4 — split the three parked oversized files

Date: 2026-05-07

Closes the last open work in `docs/refactor-follow-ups.md`. All
three files originally flagged in the v2 plan's "Out of scope"
section are now packages with per-resource (ingestion, entitystore)
or per-command (cli) modules. Every resulting file is under the
600-line soft cap except `cli/_seed_samples.py` (679 lines of
literal sample data — no edits expected).

## Final layouts

### `services/ingestion/`

| File | Lines | What |
|---|---:|---|
| `service.py` | 375 | IngestionService class, __init__, helpers (_process_text, _is_duplicate, _detect_heading, _maybe_*), mutating ops (save_final_text, delete_entry, reprocess_embeddings, rechunk_entry, ...) |
| `image.py` | 280 | _ImageIngestMixin: ingest_image, ingest_multi_page_entry, _strip_and_shift_page_spans |
| `voice.py` | 270 | _VoiceIngestMixin: ingest_voice, ingest_multi_voice |
| `text.py` | 70 | _TextIngestMixin: ingest_text |
| `url_sources.py` | 206 | _UrlIngestMixin: ingest_*_from_url + _download + _validate_public_url |
| `__init__.py` | 14 | Re-export IngestionService |

### `entitystore/`

| File | Lines | What |
|---|---:|---|
| `protocol.py` | 263 | EntityStore Protocol + _normalise + _row_to_entity + _row_to_mention + _row_to_relationship |
| `store.py` | 408 | SQLiteEntityStore class, entity CRUD + listing + embeddings + casing exceptions; re-exports EntityStore |
| `mentions.py` | 195 | _MentionsMixin: mentions, relationships, get_entities_for_entry, mark_entry_extracted |
| `merge.py` | 325 | _MergeMixin: merge_entities, delete_orphaned_entities, quarantine, merge candidates, merge history |

### `cli/`

| File | Lines | What |
|---|---:|---|
| `__init__.py` | 603 | main + argparse + smaller commands (cmd_ingest, cmd_search, cmd_list, cmd_ingest_multi, cmd_backfill_chunks, cmd_eval_chunking, cmd_rechunk, cmd_seed, cmd_migrate_chromadb, cmd_stats, cmd_health) |
| `_seed_samples.py` | 679 | SEED_SAMPLES list (44 Tolkien-themed sample entries) |
| `_services.py` | 96 | build_services() — production stack helper |
| `entities.py` | 251 | cmd_extract_entities, cmd_backfill_entity_embeddings, cmd_repair_entity_names |
| `mood.py` | 108 | cmd_backfill_mood |

## Decision reversed: free functions → mixins

The plan's Decision 2 originally called for free functions in
`ingestion/` and `entitystore/`, matching item 2's worker shape.
Once I started extracting `ingest_image`, the signature reality
became visible: that single method reaches 6 instance fields
(`_ocr`, `_repo`, `_process_text`, `_is_duplicate`,
`_maybe_preprocess`, `_detect_heading`). Threading those through a
context dataclass would duplicate the `IngestionService`
constructor surface for no real test-isolation gain — every
existing test already builds the full service with fakes, so the
"can be tested without `IngestionService`" property doesn't pay
off.

Mixin classes keep the methods bound to `self` and only move the
file-organisation needle, which was the actual goal. The reversal
applies to ingestion and entitystore; CLI commands kept the
free-function shape since they take `(args, config)` and have no
shared state.

## Decisions worth remembering

- **`protocol.py` for the EntityStore Protocol exists primarily to
  break a circular import.** The mixin modules need
  `_row_to_mention` etc. that previously lived at the bottom of
  `store.py`; `store.py` needs to import the mixins to compose
  them. Pulling the helpers + Protocol up into `protocol.py` gives
  both sides a clean upward dependency. The Protocol is small enough
  that hosting it there alongside the helpers is appropriate (and
  makes the `from journal.entitystore.protocol import EntityStore`
  read better than `from journal.entitystore.store import EntityStore`
  for new callers — but the latter is preserved via re-export).
- **`ingestion/_seed_samples.py` was the cheapest cli.py win.** Half
  the file's bulk was the literal Tolkien-themed sample list. The
  data has no reason to live alongside command bodies; pulling it to
  its own module dropped cli.py from 1621 to 955 lines in one
  commit, before any command-by-command work.
- **CLI test-patch retargets need attention per command.** A patch
  like `journal.cli.ChromaVectorStore` works for commands that still
  live in `cli/__init__.py` (e.g. `cmd_health`) and breaks for
  commands that moved to `cli/_services.py` (everything that builds
  the production stack). When extracting commands, audit
  `tests/test_cli.py` for any `patch("journal.cli.X")` and retarget
  per the new home.
- **`smart_title_case` import in store.py is unused after the split.**
  The casing-exceptions plumbing stays in store.py, but
  `smart_title_case` itself is only called from `create_entity`,
  which stays in store.py. Ruff was happy without removing it; it's
  on the import allow-list because the casing dict is set on the
  store at construction time.

## Verification

- `uv run pytest -m 'not integration'` → 1800 passed across all
  three commits.
- `uv run ruff check src/ tests/` → clean.
- All file sizes from the original snapshot driven below the cap:
  `cli/__init__.py` 603 (cap 600 — within tolerance);
  every other extracted file ≤ 408 except the literal-data file.

## What surfaced

`db/repository.py` (1603 lines) and `mcp_server.py` (1509) are now
the largest files in `src/`. Neither was on the original parked
list. They're now noted in the standing-facts size table in
`docs/refactor-follow-ups.md` so future planning rounds can decide
whether they need similar treatment.
