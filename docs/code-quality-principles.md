# Code Quality Principles — For Humans and Agents

**Status:** standing rules. **Last updated:** 2026-05-09. **Supersedes:** none.
This is a reference doc, not a plan — no kill criteria, no work units. Edit
when a new convention is adopted or an old one is retired.

A standing reference for what "good code" means in this project. Captured 2026-05-07 alongside a concrete codebase review (kept in Claude auto-memory under `review_codebase_2026-05-07`).

The headline idea: **most of what makes code good for an agent also makes it good for a human, but more sharply.** Agents are an unusually strict reader — they have no memory across sessions, no tribal knowledge, and a hard context-window budget. Optimising for agent-comprehensibility is a forcing function for the kind of clarity that humans enjoy too.

---

## Shared foundation (humans and agents both need)

These are the non-negotiables. They help anyone reasoning about the code.

1. **Strong seams.** Protocol/interface boundaries between layers so each piece can be understood, tested, and replaced independently. (This repo already does this well via `EntityStore`, `ExtractionProvider`, `EmbeddingsProvider`, etc.)
2. **Small, single-purpose modules.** A 3000-line file isn't bad because it's "ugly" — it's bad because nobody can hold it in working memory. Humans scroll; agents burn context. Both lose the thread.
3. **Behaviour-focused tests.** Tests act as executable specs. They tell a reader what the code is supposed to do — far more useful than docstrings that drift.
4. **Honest naming.** A function called `resolve_entity` should resolve an entity, not also re-embed it as a side effect. Misleading names cost readers the same regardless of species.
5. **Locality of behaviour.** Related code lives together. Cross-cutting state and "spooky action at a distance" hurt everyone.
6. **Comments that explain *why*, not *what*.** The "why two thread pools" docstring on `JobRunner`
   (`services/jobs/runner.py`) is the gold standard — it explains why storyline jobs get a dedicated single-worker
   pool (ingestion priority + same-storyline race avoidance) rather than just restating that two pools exist.

---

## What an agent needs that a human doesn't

1. **Discoverability over familiarity.** A human builds a mental model over weeks. An agent has no such memory across sessions — it relies on grep, file names, and module structure. Names and directory layout must be **immediately obvious from the outside**, with no tribal knowledge required. `utils.py` and `helpers.py` are anti-patterns: an agent must read them to know what's in them.
2. **Bounded context windows.** Humans skim; agents pay token cost. A 3000-line file forces an agent to either read it whole (expensive) or guess where the relevant section is (unreliable). Files in the 100–500 line range are dramatically easier to work with.
3. **Explicit interfaces over implicit conventions.** Humans absorb "we always do X here" by reading enough code. Agents don't generalise the same way — they benefit from typed signatures, Protocols, and contracts that are machine-checkable.
4. **No implicit state.** Per-call scratch attributes on `self` (e.g. `_current_candidate_ids`) are the #1 thing agents get wrong. They reason about each method in isolation and miss invariants stored on the instance. Pass state as parameters.
5. **No cross-module reach-ins.** When module A calls `b._private_helper()`, neither side's interface advertises the dependency. Agents miss this; so do humans, just less often.
6. **Self-contained tests.** Tests that require understanding fixture inheritance across three `conftest.py` files are hard to extend correctly. Tests that show their full setup inline (or with one obvious fake) are far easier to mimic.
7. **No reliance on out-of-band knowledge.** If correctness depends on "you must also update the migration AND the seed script AND the type stubs," that has to be written down somewhere the agent will encounter — `CLAUDE.md`, a checklist, or better, code that fails loudly if you forget. Humans get this from teammates and code review; agents won't.
8. **Predictable file layout.** "Routes for resource X live in `api/x.py`, service in `services/x.py`, tests in `tests/services/test_x.py`" — this regularity lets an agent jump to the right file on the first try.
9. **Repeatability of patterns.** Three files that do similar things should look similar. Humans tolerate stylistic variation; agents do better with an obvious template to copy.

---

## What a human needs that an agent doesn't

1. **A narrative.** Humans need to know *why this codebase exists* and how it evolved. The `journal/` dated entries and `docs/` files are for humans — agents can read them, but they're not strictly necessary for an agent to make a correct local change.
2. **Aesthetic consistency.** Indentation style, blank-line conventions, comment voice — humans get fatigued by inconsistency in a way agents don't.
3. **Onboarding ramps.** README, "getting started" docs, a sane `make dev` command. An agent doesn't onboard — it parachutes in and leaves.
4. **Tooling ergonomics.** Fast test feedback loops, good error messages, hot reload, REPL-friendly code. Agents tolerate slow loops better than humans do.
5. **Reasoning about *change over time*.** Humans think "is this the right abstraction for where this is heading in 6 months?" Agents do best when asked to make the smallest correct local change — and can be hurt by speculative future-proofing.
6. **Cognitive load management.** Humans get tired, frustrated, distracted. Code that respects this — short functions, clear failure modes, no surprising behaviour at 4pm — matters. Agents don't get tired the same way.

---

## The "agent test" as a design heuristic

Three questions to ask of any module:

1. *Could a competent agent, dropped into this repo cold, find the right file in one grep and make the change correctly?*
2. *If I deleted all the tribal knowledge, would the code still be navigable?*
3. *Does each file fit comfortably in a single context window (~500 lines)?*

If yes to all three, it's almost certainly a pleasant codebase for humans too. The agent test is a stricter version of the human test — passing it gives both.

---

## Routing rules for `src/journal/api/`

The HTTP layer follows two rules in this order:

1. **Default — primary resource (URL prefix root).** A route under `/api/<resource>/...` lives in
   `api/<resource>.py`. This makes "where do I add the new GET on entities?" answerable on first
   grep. Cross-resource handlers (e.g. `/api/entries/{id}/entities`) place by **URL prefix root**
   (entries here), and call across services as needed.
2. **Override — responsibility (write/job creation).** Routes whose primary effect is to create a
   job or perform a long-running write live in a write module, regardless of URL prefix.
   Rationale: these routes share a dependency cluster (`IngestionService`, `JobRunner`, OCR /
   transcription / extraction providers) that read/CRUD handlers never touch, and bundling them
   with their resource's reads pushes single files past the readable-context budget. The
   override family was originally a single file (`api/ingestion.py`); when it outgrew the
   ~800-line size rule it was split into three siblings — same category, one rule:
   - `api/ingestion.py` — `/api/entries/ingest/*`, `/api/entities/extract`, `/api/mood/backfill`.
   - `api/storylines_write.py` — `POST /api/storylines`, `POST .../regenerate`,
     `DELETE /api/storylines/{id}`, `PUT .../anchors` (reads stay in `api/storylines.py`).
   - `api/fitness_jobs.py` — `POST /api/fitness/sync/{source}`,
     `POST /api/fitness/backfill/{source}` (reads stay in `api/fitness.py`; the Garmin/Strava
     auth flows are plain URL-resource modules, `api/fitness_garmin.py` /
     `api/fitness_strava.py`, not part of the override).

   Adding a new "kick off a job" route? Put it in the matching write module (or `ingestion.py`
   if no sibling matches) and add a one-line comment at the call site noting which URL it serves.

The override is the first explicit deviation from URL-prefix purity in this codebase. It must
stay narrow: "write/job creation" is the only currently-recognised category, and splitting the
family by resource (as above) is a size-rule split, not a new category. New deviation
categories require updating this section **and** `api/_shared.py`'s docstring in the same
commit (the v2 refactor plan that originally captured the rule,
`archive/code-quality-refactor-plan.md`, is closed and only kept as a historical record).

---

## Anti-patterns to actively avoid

- **God files.** Anything over ~800 lines is a smell; over ~1500 is a problem.
- **Implicit state.** Per-call attributes on a service object that callers don't set explicitly. If a method's behaviour depends on `self._foo`, `_foo` should either be a constructor arg or a method parameter.
- **Cross-module private reach.** `module_a` calling `module_b._helper()`. Either promote it to public API or restructure so the call doesn't need to happen.
- **Vague names.** `utils`, `helpers`, `manager`, `handler` without a noun. `data`, `info`, `process` as variable names.
- **Tests that bind to implementation.** Reaching into `service._client.messages.create` couples tests to a private structure. Prefer fakes at the Protocol seam.
- **Comments that restate the code.** `# increment counter` above `counter += 1`. Delete on sight.
- **Speculative abstraction.** "We might need this to be pluggable later." Three concrete uses before you abstract.

---

## Concise, not terse

A line worth holding onto. The two failure modes:

- **Verbose:** redundant comments, defensive code for cases that can't happen, three helper layers when one inline expression would do, tests that re-explain what the assertion already says.
- **Terse:** single-letter names, dense one-liners that pack four operations, no comments where a *why* is genuinely non-obvious, magic numbers without context.

Concise means: every token earns its place, and nothing more is there. A reader (human or agent) shouldn't have to do detective work to understand intent — and shouldn't have to wade through ceremony to find the substance.
