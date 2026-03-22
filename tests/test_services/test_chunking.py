"""Tests for text chunking."""

from journal.services.chunking import chunk_text, count_tokens


def test_count_tokens():
    count = count_tokens("Hello world")
    assert count > 0


def test_short_text_single_chunk():
    chunks = chunk_text("This is a short journal entry.")
    assert len(chunks) == 1
    assert chunks[0] == "This is a short journal entry."


def test_empty_text():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_splits():
    # Create text longer than default 500 tokens
    paragraphs = [f"This is paragraph number {i}. " * 20 for i in range(10)]
    text = "\n\n".join(paragraphs)

    chunks = chunk_text(text, max_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1

    # Each chunk should be within the token limit (with some tolerance for boundaries)
    for chunk in chunks:
        tokens = count_tokens(chunk)
        assert tokens <= 150  # Allow some tolerance for paragraph boundaries


def test_overlap_between_chunks():
    # Create text that will be split into multiple chunks
    paragraphs = [f"Unique paragraph {i} with some content." for i in range(20)]
    text = "\n\n".join(paragraphs)

    chunks = chunk_text(text, max_tokens=50, overlap_tokens=20)
    assert len(chunks) > 1

    # Check that consecutive chunks share some content (overlap)
    for i in range(len(chunks) - 1):
        # At least some words from the end of chunk i should appear in chunk i+1
        words_end = set(chunks[i].split()[-5:])
        words_start = set(chunks[i + 1].split()[:10])
        assert words_end & words_start, f"No overlap between chunk {i} and {i + 1}"


def test_preserves_paragraph_structure():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = chunk_text(text, max_tokens=1000)
    assert len(chunks) == 1
    assert "First paragraph." in chunks[0]
    assert "Second paragraph." in chunks[0]
    assert "Third paragraph." in chunks[0]
