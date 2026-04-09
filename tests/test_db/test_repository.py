"""Tests for SQLite repository."""

import pytest

from journal.db.repository import SQLiteEntryRepository


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
        sf_id = repo._conn.execute("SELECT id FROM source_files WHERE file_hash = 'abc123'").fetchone()["id"]

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

        with pytest.raises(Exception):
            repo.add_entry_page(entry.id, 1, "Duplicate page one")
