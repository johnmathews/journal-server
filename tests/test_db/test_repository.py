"""Tests for SQLite repository."""

import sqlite3

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.models import ChunkSpan


@pytest.fixture
def repo(db_conn):
    return SQLiteEntryRepository(db_conn)


class TestCreateAndGetEntry:
    def test_create_entry(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Today was a good day.", 5)
        assert entry.id == 1
        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "ocr"
        assert entry.raw_text == "Today was a good day."
        assert entry.word_count == 5

    def test_get_entry(self, repo):
        created = repo.create_entry("2026-03-22", "ocr", "Hello world", 2)
        fetched = repo.get_entry(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.raw_text == "Hello world"

    def test_get_entry_not_found(self, repo):
        assert repo.get_entry(999) is None

    def test_get_entries_by_date(self, repo):
        repo.create_entry("2026-03-22", "ocr", "Entry one", 2)
        repo.create_entry("2026-03-22", "voice", "Entry two", 2)
        repo.create_entry("2026-03-23", "ocr", "Entry three", 2)

        entries = repo.get_entries_by_date("2026-03-22")
        assert len(entries) == 2


class TestListEntries:
    def test_list_entries_all(self, repo):
        for i in range(5):
            repo.create_entry(f"2026-03-{20 + i:02d}", "ocr", f"Entry {i}", 2)
        entries = repo.list_entries()
        assert len(entries) == 5

    def test_list_entries_with_date_filter(self, repo):
        repo.create_entry("2026-03-01", "ocr", "March start", 2)
        repo.create_entry("2026-03-15", "ocr", "March mid", 2)
        repo.create_entry("2026-03-31", "ocr", "March end", 2)

        entries = repo.list_entries(start_date="2026-03-10", end_date="2026-03-20")
        assert len(entries) == 1
        assert entries[0].raw_text == "March mid"

    def test_list_entries_pagination(self, repo):
        for i in range(10):
            repo.create_entry(f"2026-03-{i + 1:02d}", "ocr", f"Entry {i}", 2)
        page1 = repo.list_entries(limit=3, offset=0)
        page2 = repo.list_entries(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0].id != page2[0].id


class TestFTS:
    def test_search_text(self, repo):
        repo.create_entry("2026-03-22", "ocr", "Walked through Vienna today", 4)
        repo.create_entry("2026-03-23", "ocr", "Stayed home and read a book", 6)

        results = repo.search_text("Vienna")
        assert len(results) == 1
        assert "Vienna" in results[0].raw_text

    def test_search_text_with_date_filter(self, repo):
        repo.create_entry("2026-01-15", "ocr", "Vienna in January", 3)
        repo.create_entry("2026-03-15", "ocr", "Vienna in March", 3)

        results = repo.search_text("Vienna", start_date="2026-03-01")
        assert len(results) == 1
        assert results[0].entry_date == "2026-03-15"

    def test_search_text_no_results(self, repo):
        repo.create_entry("2026-03-22", "ocr", "Nothing relevant here", 3)
        results = repo.search_text("Vienna")
        assert len(results) == 0


class TestFTSSnippets:
    def test_search_text_with_snippets_wraps_matches(self, repo):
        repo.create_entry(
            "2026-03-22",
            "ocr",
            "Walked through Vienna with Atlas and later met Robyn.",
            10,
        )
        results = repo.search_text_with_snippets("Vienna")
        assert len(results) == 1
        entry, snippet = results[0]
        assert entry.entry_date == "2026-03-22"
        # STX/ETX should wrap the matched token (case-insensitive FTS5).
        assert "\x02" in snippet
        assert "\x03" in snippet
        # Extracting the wrapped segment should equal the matched term.
        start = snippet.index("\x02")
        end = snippet.index("\x03")
        assert snippet[start + 1 : end].lower() == "vienna"

    def test_search_text_with_snippets_date_filter(self, repo):
        repo.create_entry("2026-01-15", "ocr", "Vienna in January", 3)
        repo.create_entry("2026-03-15", "ocr", "Vienna in March", 3)

        results = repo.search_text_with_snippets(
            "Vienna", start_date="2026-03-01"
        )
        assert len(results) == 1
        entry, _ = results[0]
        assert entry.entry_date == "2026-03-15"

    def test_search_text_with_snippets_pagination(self, repo):
        for i in range(5):
            repo.create_entry(
                f"2026-03-{10 + i:02d}",
                "ocr",
                f"Entry number {i} mentions Atlas in some form.",
                8,
            )
        page_one = repo.search_text_with_snippets("Atlas", limit=2, offset=0)
        page_two = repo.search_text_with_snippets("Atlas", limit=2, offset=2)
        assert len(page_one) == 2
        assert len(page_two) == 2
        ids_one = {e.id for e, _ in page_one}
        ids_two = {e.id for e, _ in page_two}
        assert ids_one.isdisjoint(ids_two)

    def test_search_text_with_snippets_no_match(self, repo):
        repo.create_entry("2026-03-22", "ocr", "Nothing relevant here", 3)
        results = repo.search_text_with_snippets("Vienna")
        assert results == []

    def test_count_text_matches(self, repo):
        repo.create_entry("2026-03-01", "ocr", "Atlas the dog", 3)
        repo.create_entry("2026-03-02", "ocr", "Atlas again", 2)
        repo.create_entry("2026-03-03", "ocr", "No match here", 3)
        assert repo.count_text_matches("Atlas") == 2

    def test_count_text_matches_with_date_filter(self, repo):
        repo.create_entry("2026-01-15", "ocr", "Vienna visit", 2)
        repo.create_entry("2026-03-15", "ocr", "Vienna trip", 2)
        assert (
            repo.count_text_matches("Vienna", start_date="2026-03-01") == 1
        )


class TestWritingFrequency:
    """T1.3a.i — get_writing_frequency across the four granularities."""

    def test_invalid_granularity_raises(self, repo):
        import pytest

        with pytest.raises(ValueError, match="Unsupported granularity"):
            repo.get_writing_frequency(None, None, "fortnight")

    def test_empty_returns_empty_list(self, repo):
        assert repo.get_writing_frequency(None, None, "week") == []

    def test_week_bins_start_on_monday(self, repo):
        # 2026-03-22 is a Sunday, 2026-03-23 is a Monday,
        # 2026-03-24 is a Tuesday. All three should fall into the
        # week starting Monday 2026-03-23 — except the Sunday
        # entry, which belongs to the PREVIOUS week starting
        # 2026-03-16. Verify both.
        repo.create_entry("2026-03-22", "ocr", "Sunday entry", 2)
        repo.create_entry("2026-03-23", "ocr", "Monday entry", 2)
        repo.create_entry("2026-03-24", "ocr", "Tuesday entry", 2)
        repo.create_entry("2026-03-30", "ocr", "Next Monday", 2)

        bins = repo.get_writing_frequency(None, None, "week")
        by_start = {b.bin_start: b for b in bins}

        assert "2026-03-16" in by_start  # Sunday rolled into prior Monday
        assert by_start["2026-03-16"].entry_count == 1
        assert "2026-03-23" in by_start
        assert by_start["2026-03-23"].entry_count == 2
        assert "2026-03-30" in by_start
        assert by_start["2026-03-30"].entry_count == 1

    def test_month_bins_start_on_first_of_month(self, repo):
        repo.create_entry("2026-03-01", "ocr", "march start", 2)
        repo.create_entry("2026-03-15", "ocr", "march mid", 2)
        repo.create_entry("2026-03-31", "ocr", "march end", 2)
        repo.create_entry("2026-04-01", "ocr", "april start", 2)

        bins = repo.get_writing_frequency(None, None, "month")
        by_start = {b.bin_start: b for b in bins}

        assert by_start["2026-03-01"].entry_count == 3
        assert by_start["2026-04-01"].entry_count == 1

    def test_quarter_bins_start_on_jan_apr_jul_oct(self, repo):
        repo.create_entry("2026-01-15", "ocr", "q1 mid", 2)
        repo.create_entry("2026-02-28", "ocr", "q1 end", 2)
        repo.create_entry("2026-04-01", "ocr", "q2 start", 2)
        repo.create_entry("2026-07-15", "ocr", "q3 mid", 2)
        repo.create_entry("2026-12-31", "ocr", "q4 end", 2)

        bins = repo.get_writing_frequency(None, None, "quarter")
        by_start = {b.bin_start: b for b in bins}

        assert by_start["2026-01-01"].entry_count == 2
        assert by_start["2026-04-01"].entry_count == 1
        assert by_start["2026-07-01"].entry_count == 1
        assert by_start["2026-10-01"].entry_count == 1

    def test_year_bins_start_on_jan_first(self, repo):
        repo.create_entry("2025-06-15", "ocr", "2025 entry", 2)
        repo.create_entry("2026-01-01", "ocr", "2026 start", 2)
        repo.create_entry("2026-12-31", "ocr", "2026 end", 2)

        bins = repo.get_writing_frequency(None, None, "year")
        by_start = {b.bin_start: b for b in bins}

        assert by_start["2025-01-01"].entry_count == 1
        assert by_start["2026-01-01"].entry_count == 2

    def test_total_words_sums_per_bin(self, repo):
        # 2026-03-02 is a Monday, 2026-03-03 is Tuesday. Both fall
        # in the week starting 2026-03-02.
        repo.create_entry("2026-03-02", "ocr", "a b c", 3)
        repo.create_entry("2026-03-03", "ocr", "d e f g", 4)
        bins = repo.get_writing_frequency(None, None, "week")
        assert len(bins) == 1
        assert bins[0].bin_start == "2026-03-02"
        assert bins[0].entry_count == 2
        assert bins[0].total_words == 7

    def test_date_filter_clamps_bins(self, repo):
        repo.create_entry("2026-01-15", "ocr", "january", 2)
        repo.create_entry("2026-03-15", "ocr", "march", 2)
        repo.create_entry("2026-06-15", "ocr", "june", 2)

        bins = repo.get_writing_frequency(
            start_date="2026-02-01",
            end_date="2026-04-30",
            granularity="month",
        )
        assert len(bins) == 1
        assert bins[0].bin_start == "2026-03-01"

    def test_empty_buckets_are_omitted(self, repo):
        """March and May have entries; April has none. April must
        NOT appear as a zero-count bin — callers that need dense
        series fill gaps client-side."""
        repo.create_entry("2026-03-01", "ocr", "march", 2)
        repo.create_entry("2026-05-01", "ocr", "may", 2)
        bins = repo.get_writing_frequency(None, None, "month")
        starts = {b.bin_start for b in bins}
        assert starts == {"2026-03-01", "2026-05-01"}

    def test_results_sorted_ascending(self, repo):
        repo.create_entry("2026-05-01", "ocr", "may", 2)
        repo.create_entry("2026-01-01", "ocr", "jan", 2)
        repo.create_entry("2026-03-01", "ocr", "march", 2)
        bins = repo.get_writing_frequency(None, None, "month")
        starts = [b.bin_start for b in bins]
        assert starts == sorted(starts)


class TestIngestionStats:
    """T1.2.b — get_ingestion_stats aggregates everything /health shows."""

    def test_empty_corpus(self, repo):
        from datetime import UTC, datetime

        stats = repo.get_ingestion_stats(now=datetime(2026, 4, 11, tzinfo=UTC))
        assert stats.total_entries == 0
        assert stats.entries_last_7d == 0
        assert stats.entries_last_30d == 0
        assert stats.by_source_type == {}
        assert stats.avg_words_per_entry == 0.0
        assert stats.avg_chunks_per_entry == 0.0
        assert stats.last_ingestion_at is None
        assert stats.total_chunks == 0
        # row_counts surfaces every whitelisted table even when empty.
        for table in (
            "entries",
            "entry_pages",
            "entry_chunks",
            "mood_scores",
            "source_files",
            "entities",
            "entity_aliases",
            "entity_mentions",
            "entity_relationships",
        ):
            assert stats.row_counts[table] == 0

    def test_counts_by_source_and_date_windows(self, repo):
        from datetime import UTC, datetime

        now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
        # Three entries within the last 7 days.
        repo.create_entry("2026-04-05", "ocr", "recent one two three", 4)
        repo.create_entry("2026-04-08", "voice", "recent voice note here", 4)
        repo.create_entry("2026-04-10", "ocr", "also recent entry body", 4)
        # One entry in last 30 days but NOT last 7.
        repo.create_entry("2026-03-20", "ocr", "medium age entry text", 4)
        # One entry beyond 30 days.
        repo.create_entry("2026-02-01", "voice", "old voice entry longer text", 5)

        stats = repo.get_ingestion_stats(now=now)

        assert stats.total_entries == 5
        assert stats.entries_last_7d == 3
        assert stats.entries_last_30d == 4  # 3 recent + 1 March 20
        assert stats.by_source_type == {"ocr": 3, "voice": 2}
        assert stats.avg_words_per_entry == 4.2
        assert stats.row_counts["entries"] == 5
        assert stats.last_ingestion_at is not None

    def test_avg_chunks_reflects_update_chunk_count(self, repo):
        from datetime import UTC, datetime

        e = repo.create_entry("2026-04-01", "ocr", "body body body", 3)
        repo.update_chunk_count(e.id, 4)
        stats = repo.get_ingestion_stats(
            now=datetime(2026, 4, 11, tzinfo=UTC)
        )
        assert stats.total_chunks == 4
        assert stats.avg_chunks_per_entry == 4.0

    def test_row_counts_include_entity_tables(self, repo):
        from datetime import UTC, datetime

        # Row counts are computed directly from COUNT(*) on the table
        # names in `_HEALTH_ROW_COUNT_TABLES`, so the entity tables
        # should show zero on a fresh schema without any entity
        # extraction having been run.
        stats = repo.get_ingestion_stats(now=datetime(2026, 4, 11, tzinfo=UTC))
        assert "entity_mentions" in stats.row_counts
        assert "entity_relationships" in stats.row_counts


class TestStatistics:
    def test_get_statistics(self, repo):
        repo.create_entry("2026-01-15", "ocr", "January entry", 2)
        repo.create_entry("2026-02-15", "ocr", "February entry with more words", 5)
        repo.create_entry("2026-03-15", "voice", "March entry", 2)

        stats = repo.get_statistics()
        assert stats.total_entries == 3
        assert stats.total_words == 9
        assert stats.avg_words_per_entry == 3.0
        assert stats.date_range_start == "2026-01-15"
        assert stats.date_range_end == "2026-03-15"
        assert stats.entries_per_month == 1.0

    def test_get_statistics_empty(self, repo):
        stats = repo.get_statistics()
        assert stats.total_entries == 0
        assert stats.total_words == 0

    def test_get_statistics_date_filtered(self, repo):
        repo.create_entry("2026-01-15", "ocr", "Old entry", 2)
        repo.create_entry("2026-03-15", "ocr", "New entry", 2)

        stats = repo.get_statistics(start_date="2026-03-01")
        assert stats.total_entries == 1


class TestPeopleAndPlaces:
    def test_add_people(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Met Atlas and Luna today", 5)
        repo.add_people(entry.id, ["Atlas", "Luna"])

        sql = (
            "SELECT p.name FROM entry_people ep"
            " JOIN people p ON p.id = ep.person_id WHERE ep.entry_id = ?"
        )
        rows = repo._conn.execute(sql, (entry.id,)).fetchall()
        names = {r["name"] for r in rows}
        assert names == {"Atlas", "Luna"}

    def test_add_places(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Visited Vienna and Graz", 4)
        repo.add_places(entry.id, ["Vienna", "Graz"])

        sql = (
            "SELECT p.name FROM entry_places ep"
            " JOIN places p ON p.id = ep.place_id WHERE ep.entry_id = ?"
        )
        rows = repo._conn.execute(sql, (entry.id,)).fetchall()
        names = {r["name"] for r in rows}
        assert names == {"Vienna", "Graz"}

    def test_add_tags(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Reflection on life", 3)
        repo.add_tags(entry.id, ["reflection", "philosophy"])

        sql = (
            "SELECT t.name FROM entry_tags et"
            " JOIN tags t ON t.id = et.tag_id WHERE et.entry_id = ?"
        )
        rows = repo._conn.execute(sql, (entry.id,)).fetchall()
        names = {r["name"] for r in rows}
        assert names == {"reflection", "philosophy"}


class TestMoodScores:
    def test_add_mood_score(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Feeling great", 2)
        repo.add_mood_score(entry.id, "overall", 0.8, confidence=0.9)

        row = repo._conn.execute(
            "SELECT * FROM mood_scores WHERE entry_id = ?", (entry.id,)
        ).fetchone()
        assert row["dimension"] == "overall"
        assert row["score"] == 0.8
        assert row["confidence"] == 0.9

    def test_get_mood_trends(self, repo):
        e1 = repo.create_entry("2026-03-01", "ocr", "Good day", 2)
        e2 = repo.create_entry("2026-03-08", "ocr", "Bad day", 2)
        repo.add_mood_score(e1.id, "overall", 0.8)
        repo.add_mood_score(e2.id, "overall", -0.3)

        trends = repo.get_mood_trends(granularity="month")
        assert len(trends) == 1  # Same month
        assert trends[0].dimension == "overall"

    def test_get_mood_trends_by_week(self, repo):
        e1 = repo.create_entry("2026-03-01", "ocr", "Good day", 2)
        e2 = repo.create_entry("2026-03-15", "ocr", "Bad day", 2)
        repo.add_mood_score(e1.id, "overall", 0.8)
        repo.add_mood_score(e2.id, "overall", -0.3)

        trends = repo.get_mood_trends(granularity="week")
        assert len(trends) == 2  # Different weeks


class TestMoodScoresCRUD:
    """T1.3b.iii — replace / get / missing / prune operations on
    mood_scores."""

    def test_replace_mood_scores_inserts_fresh(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "hello", 1)
        repo.replace_mood_scores(
            e.id,
            [
                ("joy_sadness", 0.5, 0.9),
                ("agency", 0.7, None),
            ],
        )
        scores = repo.get_mood_scores(e.id)
        assert len(scores) == 2
        by_dim = {s.dimension: s for s in scores}
        assert by_dim["joy_sadness"].score == 0.5
        assert by_dim["joy_sadness"].confidence == 0.9
        assert by_dim["agency"].score == 0.7
        assert by_dim["agency"].confidence is None

    def test_replace_mood_scores_is_idempotent(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "hello", 1)
        repo.replace_mood_scores(e.id, [("joy_sadness", 0.5, None)])
        repo.replace_mood_scores(e.id, [("joy_sadness", 0.8, None)])
        scores = repo.get_mood_scores(e.id)
        # Second call REPLACED the first rather than appending.
        assert len(scores) == 1
        assert scores[0].score == 0.8

    def test_replace_mood_scores_preserves_untouched_dims(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "hello", 1)
        repo.replace_mood_scores(
            e.id,
            [
                ("joy_sadness", 0.5, None),
                ("agency", 0.7, None),
            ],
        )
        # Rewrite only joy_sadness — agency row should survive.
        repo.replace_mood_scores(e.id, [("joy_sadness", 0.2, None)])
        scores = repo.get_mood_scores(e.id)
        assert len(scores) == 2
        by_dim = {s.dimension: s.score for s in scores}
        assert by_dim["joy_sadness"] == 0.2
        assert by_dim["agency"] == 0.7

    def test_replace_mood_scores_empty_list_is_noop(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "hello", 1)
        repo.replace_mood_scores(e.id, [("joy_sadness", 0.5, None)])
        repo.replace_mood_scores(e.id, [])
        scores = repo.get_mood_scores(e.id)
        assert len(scores) == 1  # unchanged

    def test_get_entries_missing_mood_scores_empty_dims(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "hello", 1)
        assert repo.get_entries_missing_mood_scores([]) == []
        # Also doesn't break when entries exist.
        assert e.id

    def test_get_entries_missing_mood_scores_all_missing(self, repo):
        e1 = repo.create_entry("2026-04-01", "ocr", "one", 1)
        e2 = repo.create_entry("2026-04-02", "ocr", "two", 1)
        missing = repo.get_entries_missing_mood_scores(
            ["joy_sadness", "agency"]
        )
        assert sorted(missing) == sorted([e1.id, e2.id])

    def test_get_entries_missing_mood_scores_partial(self, repo):
        e1 = repo.create_entry("2026-04-01", "ocr", "one", 1)
        e2 = repo.create_entry("2026-04-02", "ocr", "two", 1)
        # e1 has joy_sadness only; e2 has both.
        repo.replace_mood_scores(e1.id, [("joy_sadness", 0.5, None)])
        repo.replace_mood_scores(
            e2.id,
            [("joy_sadness", 0.5, None), ("agency", 0.3, None)],
        )
        missing = repo.get_entries_missing_mood_scores(
            ["joy_sadness", "agency"]
        )
        assert missing == [e1.id]

    def test_get_entries_missing_mood_scores_ignores_retired_dims(
        self, repo
    ):
        """An entry that has a score for a RETIRED dimension (not in
        `dimension_names`) should still count as missing if it's
        missing a current one. `dimension_names` is the current set,
        not the union of all scored dims."""
        e = repo.create_entry("2026-04-01", "ocr", "x", 1)
        repo.replace_mood_scores(e.id, [("old_dim", 0.5, None)])
        missing = repo.get_entries_missing_mood_scores(["joy_sadness"])
        assert missing == [e.id]

    def test_prune_retired_mood_scores(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "x", 1)
        repo.replace_mood_scores(
            e.id,
            [
                ("joy_sadness", 0.5, None),
                ("agency", 0.3, None),
                ("retired_one", 0.1, None),
            ],
        )
        pruned = repo.prune_retired_mood_scores(["joy_sadness", "agency"])
        assert pruned == 1
        scores = repo.get_mood_scores(e.id)
        assert {s.dimension for s in scores} == {"joy_sadness", "agency"}

    def test_prune_retired_mood_scores_empty_current_wipes_all(
        self, repo
    ):
        e = repo.create_entry("2026-04-01", "ocr", "x", 1)
        repo.replace_mood_scores(
            e.id, [("joy_sadness", 0.5, None), ("agency", 0.3, None)]
        )
        pruned = repo.prune_retired_mood_scores([])
        assert pruned == 2
        assert repo.get_mood_scores(e.id) == []

    def test_prune_retired_mood_scores_noop_when_all_current(self, repo):
        e = repo.create_entry("2026-04-01", "ocr", "x", 1)
        repo.replace_mood_scores(
            e.id, [("joy_sadness", 0.5, None)]
        )
        assert repo.prune_retired_mood_scores(["joy_sadness"]) == 0


class TestMoodTrendsCanonicalDates:
    """The refactor moves `get_mood_trends` onto the shared
    `_bin_start_sql` helper so `period` is now a canonical ISO date
    (not a %Y-%W string) and the supported granularities match
    `get_writing_frequency` plus `day`."""

    def test_week_period_is_monday_iso_date(self, repo):
        # 2026-03-02 is a Monday, 2026-03-04 is Wednesday — same
        # week, bin_start is Monday 2026-03-02.
        e1 = repo.create_entry("2026-03-02", "ocr", "a", 1)
        e2 = repo.create_entry("2026-03-04", "ocr", "b", 1)
        repo.add_mood_score(e1.id, "joy_sadness", 0.5)
        repo.add_mood_score(e2.id, "joy_sadness", 0.7)
        trends = repo.get_mood_trends(granularity="week")
        assert len(trends) == 1
        assert trends[0].period == "2026-03-02"

    def test_month_period_is_first_of_month(self, repo):
        e = repo.create_entry("2026-03-15", "ocr", "a", 1)
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        trends = repo.get_mood_trends(granularity="month")
        assert trends[0].period == "2026-03-01"

    def test_quarter_period_is_jan_apr_jul_oct(self, repo):
        e = repo.create_entry("2026-08-15", "ocr", "a", 1)  # Q3
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        trends = repo.get_mood_trends(granularity="quarter")
        assert trends[0].period == "2026-07-01"

    def test_year_period_is_jan_first(self, repo):
        e = repo.create_entry("2026-06-15", "ocr", "a", 1)
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        trends = repo.get_mood_trends(granularity="year")
        assert trends[0].period == "2026-01-01"

    def test_day_granularity_still_works(self, repo):
        """Backward compat for the LLM-facing MCP tool."""
        e = repo.create_entry("2026-03-15", "ocr", "a", 1)
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        trends = repo.get_mood_trends(granularity="day")
        assert trends[0].period == "2026-03-15"

    def test_invalid_granularity_raises(self, repo):
        import pytest

        with pytest.raises(ValueError, match="Unsupported granularity"):
            repo.get_mood_trends(granularity="fortnight")


class TestTopicFrequency:
    def test_get_topic_frequency(self, repo):
        repo.create_entry("2026-03-01", "ocr", "Walked through Vienna", 3)
        repo.create_entry("2026-03-02", "ocr", "More time in Vienna", 4)
        repo.create_entry("2026-03-03", "ocr", "Stayed home", 2)

        freq = repo.get_topic_frequency("Vienna")
        assert freq.topic == "Vienna"
        assert freq.count == 2
        assert len(freq.entries) == 2


class TestFinalText:
    def test_create_entry_defaults_final_text_to_raw_text(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Hello world", 2)
        assert entry.final_text == "Hello world"
        assert entry.raw_text == "Hello world"

    def test_create_entry_with_explicit_final_text(self, repo):
        entry = repo.create_entry(
            "2026-03-22", "ocr", "raw OCR output", 3, final_text="corrected text"
        )
        assert entry.raw_text == "raw OCR output"
        assert entry.final_text == "corrected text"

    def test_update_final_text(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "raw text", 2)
        assert entry.final_text == "raw text"

        updated = repo.update_final_text(entry.id, "corrected text", 2, 3)
        assert updated is not None
        assert updated.final_text == "corrected text"
        assert updated.word_count == 2
        assert updated.chunk_count == 3
        # raw_text unchanged
        assert updated.raw_text == "raw text"

    def test_update_final_text_not_found(self, repo):
        result = repo.update_final_text(999, "text", 1, 1)
        assert result is None

    def test_chunk_count_default(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Hello", 1)
        assert entry.chunk_count == 0

    def test_update_chunk_count(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Hello world", 2)
        repo.update_chunk_count(entry.id, 5)
        updated = repo.get_entry(entry.id)
        assert updated is not None
        assert updated.chunk_count == 5

    def test_fts_indexes_final_text(self, repo):
        """FTS should index final_text, not raw_text."""
        repo.create_entry(
            "2026-03-22", "ocr", "raw OCR garbled",
            3, final_text="corrected Vienna text"
        )
        # Should find via final_text
        results = repo.search_text("Vienna")
        assert len(results) == 1
        # Should NOT find via raw_text content that's not in final_text
        results = repo.search_text("garbled")
        assert len(results) == 0


class TestEntryPages:
    def test_add_and_get_entry_pages(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Combined text", 2)
        repo.add_entry_page(entry.id, 1, "Page one text")
        repo.add_entry_page(entry.id, 2, "Page two text")

        pages = repo.get_entry_pages(entry.id)
        assert len(pages) == 2
        assert pages[0].page_number == 1
        assert pages[0].raw_text == "Page one text"
        assert pages[1].page_number == 2
        assert pages[1].raw_text == "Page two text"

    def test_get_entry_pages_empty(self, repo):
        entry = repo.create_entry("2026-03-22", "voice", "Voice note", 2)
        pages = repo.get_entry_pages(entry.id)
        assert pages == []

    def test_add_entry_page_with_source_file(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        # Create a source file first
        repo._conn.execute(
            "INSERT INTO source_files (entry_id, file_path, file_type, file_hash)"
            " VALUES (?, ?, ?, ?)",
            (entry.id, "image.jpg", "image/jpeg", "abc123"),
        )
        repo._conn.commit()
        row = repo._conn.execute(
            "SELECT id FROM source_files WHERE file_hash = 'abc123'"
        ).fetchone()
        sf_id = row["id"]

        repo.add_entry_page(entry.id, 1, "Page text", source_file_id=sf_id)
        pages = repo.get_entry_pages(entry.id)
        assert len(pages) == 1
        assert pages[0].source_file_id == sf_id

    def test_pages_ordered_by_page_number(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Combined", 1)
        # Insert in reverse order
        repo.add_entry_page(entry.id, 3, "Third")
        repo.add_entry_page(entry.id, 1, "First")
        repo.add_entry_page(entry.id, 2, "Second")

        pages = repo.get_entry_pages(entry.id)
        assert [p.page_number for p in pages] == [1, 2, 3]
        assert [p.raw_text for p in pages] == ["First", "Second", "Third"]

    def test_unique_page_number_per_entry(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        repo.add_entry_page(entry.id, 1, "Page one")

        with pytest.raises(sqlite3.IntegrityError):
            repo.add_entry_page(entry.id, 1, "Duplicate page one")


class TestEntryChunks:
    def _span(self, text: str, start: int, end: int, tokens: int = 3) -> ChunkSpan:
        return ChunkSpan(text=text, char_start=start, char_end=end, token_count=tokens)

    def test_replace_chunks_inserts_rows(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Chunk me.", 2)
        spans = [
            self._span("First chunk.", 0, 12),
            self._span("Second chunk.", 14, 27),
        ]
        repo.replace_chunks(entry.id, spans)

        result = repo.get_chunks(entry.id)
        assert len(result) == 2
        assert result[0].text == "First chunk."
        assert result[0].char_start == 0
        assert result[0].char_end == 12
        assert result[0].token_count == 3
        assert result[1].text == "Second chunk."
        assert result[1].char_start == 14

    def test_replace_chunks_clears_previous_rows(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Some text", 2)
        repo.replace_chunks(entry.id, [self._span("old one", 0, 7)])
        repo.replace_chunks(entry.id, [self._span("new one", 0, 7), self._span("new two", 8, 15)])

        result = repo.get_chunks(entry.id)
        assert [c.text for c in result] == ["new one", "new two"]

    def test_replace_chunks_with_empty_list_clears_table(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        repo.replace_chunks(entry.id, [self._span("only chunk", 0, 10)])
        repo.replace_chunks(entry.id, [])
        assert repo.get_chunks(entry.id) == []

    def test_get_chunks_empty_for_entry_without_chunks(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        assert repo.get_chunks(entry.id) == []

    def test_get_chunks_returns_insertion_order(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        spans = [self._span(f"chunk {i}", i * 10, i * 10 + 7) for i in range(5)]
        repo.replace_chunks(entry.id, spans)

        result = repo.get_chunks(entry.id)
        assert [c.text for c in result] == [f"chunk {i}" for i in range(5)]

    def test_delete_entry_cascades_to_chunks(self, repo):
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        repo.replace_chunks(entry.id, [self._span("to be deleted", 0, 13)])
        repo.delete_entry(entry.id)
        assert repo.get_chunks(entry.id) == []
