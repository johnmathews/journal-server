"""Tests for ``StorylineExtensionClassifier`` (Task 7).

Ported from ``tests/test_storyline_jobs.py::TestExtensionClassifier``
(the pre-redesign home of these tests) and updated for two Task 7
fixes:

* Stage 2 (surface form) now word-boundary matches instead of doing a
  bare substring test — a short anchor name like "Ana" must not fire
  on "banana".
* Stage 2.5 (embedding fallback) now reads the storyline's *draft
  chapter* embedding via ``storyline_repository.get_draft(...)``
  instead of a ``Storyline.summary_embedding`` attribute that no
  longer exists post-Task-3.

The entity-overlap (stage 1) and no-match (stage 3) tests carried over
unchanged since that behaviour didn't move. The old fixture's use of
``storyline_repo.update_summary_embedding(...)`` (a removed method) is
replaced with a direct SQL write to ``storyline_chapters
.draft_embedding_json`` on the auto-created draft chapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.models import Entry
from journal.providers.storyline_extension_decider import ExtensionDecision
from journal.services.storylines.extension import StorylineExtensionClassifier

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory


# ── Fakes ───────────────────────────────────────────────────────


@dataclass
class _CannedDecider:
    verdict: ExtensionDecision
    calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "haiku-fake"

    def decide(self, **kwargs: Any) -> ExtensionDecision:  # noqa: ANN401
        self.calls.append(kwargs)
        return self.verdict


class _MiniEntryRepo:
    """Just enough of ``EntryRepository`` for the classifier: get_entry."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_entry(self, eid: int) -> Entry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE id = ?", (eid,),
        ).fetchone()
        if row is None:
            return None
        return Entry(
            id=row["id"], entry_date=row["entry_date"],
            source_type=row["source_type"],
            raw_text=row["raw_text"],
            final_text=row["final_text"] or "",
            word_count=row["word_count"] or 0,
            user_id=row["user_id"] or 0,
        )

    def search_text(self, **_: Any) -> list[Any]:  # noqa: ANN401
        return []


def _insert_entry(
    conn: sqlite3.Connection, *, entry_date: str, text: str, user_id: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text,"
        "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (entry_date, "text", text, text, len(text.split()), user_id),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _set_draft_embedding(
    conn: sqlite3.Connection, storyline_id: int, embedding: list[float] | None,
) -> None:
    conn.execute(
        "UPDATE storyline_chapters SET draft_embedding_json = ?"
        " WHERE storyline_id = ? AND state = 'draft'",
        (json.dumps(embedding) if embedding is not None else None, storyline_id),
    )
    conn.commit()


# ── classifier_env fixture ─────────────────────────────────────


@pytest.fixture
def classifier_env(factory: ConnectionFactory) -> dict[str, Any]:
    """Seed a user, an "Ana" anchor entity, and a storyline anchored on
    it. Callers add entries and build a classifier per test via the
    helpers below."""
    conn = factory.get()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)", ("ext@x.test", "x", "E"),
    )
    user_id = cur.lastrowid
    conn.commit()
    assert user_id is not None

    entity_store = SQLiteEntityStore(factory)
    entity = entity_store.create_entity(
        entity_type="person", canonical_name="Ana",
        description="", first_seen="2026-02-15", user_id=user_id,
    )
    storyline_repo = SQLiteStorylineRepository(factory)
    storyline = storyline_repo.create_storyline(
        user_id=user_id, entity_ids=[entity.id], name="Ana",
    )

    decider = _CannedDecider(
        verdict=ExtensionDecision(
            decision="yes", reasoning="Matches.", model_used="haiku-fake",
        ),
    )
    entry_repo = _MiniEntryRepo(conn)

    return {
        "conn": conn,
        "user_id": user_id,
        "entity": entity,
        "storyline": storyline,
        "storyline_repo": storyline_repo,
        "entity_store": entity_store,
        "entry_repo": entry_repo,
        "decider": decider,
    }


def _make_classifier(
    env: dict[str, Any], *, embedder: Any = None, relevance_threshold: float = 0.5,  # noqa: ANN401
) -> StorylineExtensionClassifier:
    return StorylineExtensionClassifier(
        entity_store=env["entity_store"],
        entry_repository=env["entry_repo"],
        storyline_repository=env["storyline_repo"],
        decider=env["decider"],
        embedder=embedder,
        relevance_threshold=relevance_threshold,
    )


# ── stage 1: entity overlap ─────────────────────────────────────


class TestEntityOverlap:
    def test_entity_overlap_yields_yes_without_llm(
        self, classifier_env: dict[str, Any],
    ) -> None:
        env = classifier_env
        body = "I saw Ana at the park today."
        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-01", text=body, user_id=env["user_id"],
        )
        env["conn"].execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (env["entity"].id, entry_id, body, 0.95, "r-1"),
        )
        env["conn"].commit()

        classifier = _make_classifier(env)
        results = classifier.classify_for_entry(entry_id=entry_id, user_id=env["user_id"])

        assert len(results) == 1
        assert results[0].decision == "yes"
        assert results[0].stage == "entity_overlap"
        assert env["decider"].calls == []

        # last_extension_check_at recorded
        s = env["storyline_repo"].get_storyline(env["storyline"].id)
        assert s is not None
        assert s.last_extension_check_at is not None


# ── stage 2: surface form (word boundary) ───────────────────────


class TestSurfaceForm:
    def test_surface_form_requires_word_boundary(
        self, classifier_env: dict[str, Any],
    ) -> None:
        env = classifier_env

        # "banana" contains "ana" (lowercased "Ana") as a substring but
        # not as a whole word — must NOT escalate to the decider.
        no_match_id = _insert_entry(
            env["conn"], entry_date="2026-03-02",
            text="we ate a banana", user_id=env["user_id"],
        )
        classifier = _make_classifier(env)
        results = classifier.classify_for_entry(
            entry_id=no_match_id, user_id=env["user_id"],
        )
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert env["decider"].calls == []

        # "Ana called" contains "Ana" as a whole word — must escalate.
        match_id = _insert_entry(
            env["conn"], entry_date="2026-03-03",
            text="Ana called", user_id=env["user_id"],
        )
        results = classifier.classify_for_entry(
            entry_id=match_id, user_id=env["user_id"],
        )
        assert results[0].stage == "surface_form_llm"
        assert results[0].decision == "yes"  # canned verdict
        assert len(env["decider"].calls) == 1

    def test_surface_form_escapes_special_regex_chars(
        self, classifier_env: dict[str, Any],
    ) -> None:
        """A canonical name containing regex metacharacters (a period, a
        parenthesis) must be treated literally, not as a regex — an
        unescaped ``(M.D.)`` would either crash ``re.compile`` (unbalanced
        group) or silently match unintended text. Name doesn't *start* or
        *end* with punctuation so the ``\\b`` boundaries land on word
        characters, isolating this test to the escaping behaviour rather
        than \\b's separate (pre-existing, brief-specified) edge case with
        punctuation-adjacent boundaries."""
        env = classifier_env
        entity2 = env["entity_store"].create_entity(
            entity_type="person", canonical_name="Dr. Smith (M.D.) Jones",
            description="", first_seen="2026-02-16", user_id=env["user_id"],
        )
        storyline2 = env["storyline_repo"].create_storyline(
            user_id=env["user_id"], entity_ids=[entity2.id], name="Dr. Smith",
        )

        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-04",
            text="Saw Dr. Smith (M.D.) Jones at the clinic.", user_id=env["user_id"],
        )
        classifier = _make_classifier(env)
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        matching = [r for r in results if r.storyline_id == storyline2.id]
        assert len(matching) == 1
        assert matching[0].stage == "surface_form_llm"


# ── stage 3: no match short-circuit ─────────────────────────────


class TestNoMatch:
    def test_no_match_short_circuits(self, classifier_env: dict[str, Any]) -> None:
        env = classifier_env
        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-05",
            text="Today I baked sourdough bread.", user_id=env["user_id"],
        )
        classifier = _make_classifier(env)
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert env["decider"].calls == []


# ── stage 2.5: embedding fallback (draft chapter embedding) ─────


class TestEmbeddingStage:
    def test_embedding_stage_reads_draft_embedding(
        self, classifier_env: dict[str, Any],
    ) -> None:
        env = classifier_env
        _set_draft_embedding(env["conn"], env["storyline"].id, [1.0, 0.0, 0.0])

        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-06",
            text="Today I baked sourdough bread.", user_id=env["user_id"],
        )
        classifier = _make_classifier(
            env, embedder=lambda _text: [1.0, 0.0, 0.0],  # cosine 1.0
        )
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        assert results[0].stage == "embedding_llm"
        assert results[0].decision == "yes"  # canned verdict
        assert len(env["decider"].calls) == 1

    def test_embedding_stage_below_threshold_stays_no(
        self, classifier_env: dict[str, Any],
    ) -> None:
        env = classifier_env
        _set_draft_embedding(env["conn"], env["storyline"].id, [1.0, 0.0, 0.0])

        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-07",
            text="Today I baked sourdough bread.", user_id=env["user_id"],
        )
        classifier = _make_classifier(
            env, embedder=lambda _text: [0.0, 1.0, 0.0],  # orthogonal → cosine 0
        )
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert env["decider"].calls == []

    def test_embedding_stage_skipped_when_draft_has_none(
        self, classifier_env: dict[str, Any],
    ) -> None:
        """The auto-created draft chapter has no embedding yet (never
        narrated) → the fallback is skipped cleanly, no decider call,
        no AttributeError."""
        env = classifier_env  # draft_embedding is None by default

        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-08",
            text="Today I baked sourdough bread.", user_id=env["user_id"],
        )
        classifier = _make_classifier(
            env, embedder=lambda _text: [1.0, 0.0, 0.0],
        )
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert env["decider"].calls == []

    def test_embedding_stage_skipped_when_storyline_missing_draft_row(
        self, classifier_env: dict[str, Any],
    ) -> None:
        """Defensive: if a storyline somehow has no draft chapter at all
        (get_draft returns None), the fallback must not raise."""
        env = classifier_env
        env["conn"].execute(
            "DELETE FROM storyline_chapters WHERE storyline_id = ?",
            (env["storyline"].id,),
        )
        env["conn"].commit()

        entry_id = _insert_entry(
            env["conn"], entry_date="2026-03-09",
            text="Today I baked sourdough bread.", user_id=env["user_id"],
        )
        classifier = _make_classifier(
            env, embedder=lambda _text: [1.0, 0.0, 0.0],
        )
        results = classifier.classify_for_entry(
            entry_id=entry_id, user_id=env["user_id"],
        )
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert env["decider"].calls == []
