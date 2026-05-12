# Multi-entity storylines — server side

**Date:** 2026-05-12. **Branch:** `eng-multi-entity-storylines`.
**Companion:** `webapp/journal/260512-multi-entity-storylines.md`.
**Plan:** `.engineering-team/plan-multi-entity-storylines.md`.

## What shipped

A storyline can now be anchored on 1..15 entities. Multi-entity is
treated as a semantic-equals set: an entry that mentions any anchor
contributes once; the narrator sees a single unioned corpus.

End-to-end:

- New `storyline_entities` join table; legacy `storylines.entity_id`
  column dropped. Application-level dedup replaces the old DB-level
  `UNIQUE(user_id, entity_id, name)`.
- `SQLiteStorylineRepository` carved: `find_by_entity` deleted;
  replaced by `find_by_anchor_set` (exact set match on
  `(user_id, name, anchor_ids)`) and `list_storylines_with_anchor`
  (any-anchor membership for the classifier). New anchor CRUD:
  `list_anchors`, `set_anchors`, `add_anchor`, `remove_anchor`.
- Generation service unions excerpts across anchors with
  `entry_id`-level dedup. FTS fallback runs per-anchor and unions.
- Extension classifier triggers `yes` if any anchor entity-id appears
  in extracted mentions or if any anchor's canonical name appears in
  the entry text.
- `POST /api/storylines` takes `entity_ids: list[int]`.
- New `PUT /api/storylines/{id}/anchors` for set-replacement.
- `journal_create_storyline` MCP tool takes `entity_ids: list[int]`.
- New `journal_set_storyline_anchors` MCP tool.
- Responses include `anchors: [{id, canonical_name}, ...]` everywhere
  the old `entity_id` field appeared.

## Migration design — what bit us

Migration 0028 is a table rebuild (drop column + drop the UNIQUE
constraint that referenced it). Three things tripped me up before the
shape was right:

1. **DROP COLUMN refuses while an index references the column.**
   `idx_storylines_entity` is one; the auto-index from
   `UNIQUE(user_id, entity_id, name)` is the other. The auto-index
   can only be removed by rebuilding the table — SQLite doesn't let
   you drop autoindexes directly. So the migration does the full
   create-new / copy / drop-old / rename rebuild dance.
2. **`PRAGMA foreign_keys = OFF` is a no-op inside a transaction.**
   First version ran the entire migration inside a `BEGIN/COMMIT`
   wrapper with FK-off at the top; `DROP TABLE storylines` then
   triggered the implicit `DELETE FROM` which cascade-deleted the
   freshly-backfilled `storyline_entities` rows. Fix: PRAGMA
   foreign_keys = OFF outside the transaction, then BEGIN, rebuild,
   COMMIT, PRAGMA foreign_keys = ON.
3. **`executescript` autocommits per statement and leaves any
   trailing BEGIN dangling on failure.** Extended
   `_executescript_idempotent` to roll back any open transaction
   when a migration fails, so the next attempt starts cleanly.

The fitness migrations' `test_idempotent_rerun_from_pre_fitness_baseline`
was scoped down: it used to roll user_version to 22 and re-run all
subsequent migrations including 0028. After 0028 ran once,
`storylines.entity_id` is gone, so the migration's own backfill
SELECT fails to parse on the re-run. The test now only re-runs the
fitness migrations (23-25); 0028's own re-runnability is covered by
the explicit dirty-fixture test in `TestStorylineEntitiesMigration`.

## Dedup semantics

`find_by_anchor_set(user_id, entity_ids, name)` returns a storyline
iff:

- `user_id` matches
- `name.strip()` matches exactly
- The anchor set is *exactly* equal (no subset, no superset, no
  extras)

Different anchor sets with the same name = different storylines.
Same set with different names = different storylines. This is what
the user asked for; the DB no longer enforces it (no UNIQUE on
`(user, name)` or `(user, entity_id, name)`), so the route layer is
the single source of truth for "is this a duplicate." 409 returned on
hit; 201 on miss.

## Anchor ordering decision

Anchors are presented as semantic equals in prompts and API output —
no "primary" or "secondary." Internally we sort by `entity_id ASC`
for determinism (same input → same prompt → same output). The user
asked: "if entity X has 3 mentions and entity Y has 10 mentions, I
expect to see more about Y in the storyline than X, that's only
natural." Right: narrative weight emerges from data volume, not from
an explicit weighting in the prompt template.

## What I did NOT touch

- Narrator and glue prompts. The original architecture passes
  `storyline_name` + `storyline_description` + the excerpts (as
  Anthropic Citations documents). The storyline name already
  communicates the multi-entity framing (e.g. "Atlas and Vienna
  together"); the excerpts naturally contain the entity names. No
  prompt template change needed.
- Anchor edit UX in the webapp. The MCP tool and REST endpoint exist
  so Claude and scripted clients can manage anchors today; the
  webapp follow-up plan will design a proper UI.

## Test counts

- Server: 2437 passed (was 2429). Coverage: server unchanged
  threshold-wise.
- Migration tests: 6 new in `TestStorylineEntitiesMigration` (fresh DB,
  prod-shaped backfill, dirty-fixture re-run, cascade delete, PK
  duplicate rejection, version check).
- Repository tests: 6 new in `TestStorylineCRUD` (multi-anchor create,
  dedup repeated ids, reject empty, exact-set find), 7 new in
  `TestAnchors` (set_anchors / add / remove / list_with_anchor /
  status filter / cascade).
- API tests: 6 new (multi-anchor create, anchors list, validation,
  PUT /anchors success / 404 / empty rejection).
- MCP tests: 6 new in `TestSetStorylineAnchors`, plus
  `journal_create_storyline` rewired for `entity_ids` list shape.

## Prod cleanup note

3 experimental storylines exist on prod today. They carry valid
`entity_id`s and the migration will backfill them cleanly into
`storyline_entities` before dropping the column. No manual step
needed. If the user wants a fresh slate post-deploy, `DELETE FROM
storylines WHERE ...;` cascades through both the panels and the
anchors.
