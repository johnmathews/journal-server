"""Text chunking with tiktoken for embedding preparation."""

import logging

import tiktoken

log = logging.getLogger(__name__)

_encoder = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoder.encode(text))


def chunk_text(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 100,
) -> list[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries.

    Returns at least one chunk even if the text is shorter than max_tokens.
    """
    if not text.strip():
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # If the whole text fits in one chunk, return it directly
    if count_tokens(text) <= max_tokens:
        return [text.strip()]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_tokens = 0

    for paragraph in paragraphs:
        para_tokens = count_tokens(paragraph)

        # If a single paragraph exceeds max_tokens, split it by sentences
        if para_tokens > max_tokens:
            # Flush current chunk first
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_tokens = 0

            sentence_chunks = _split_long_paragraph(paragraph, max_tokens, overlap_tokens)
            chunks.extend(sentence_chunks)
            continue

        # Check if adding this paragraph would exceed the limit
        if current_tokens + para_tokens > max_tokens and current_chunk:
            chunks.append("\n\n".join(current_chunk))

            # Keep overlap: walk backwards through paragraphs until we hit overlap_tokens
            overlap_parts: list[str] = []
            overlap_count = 0
            for prev in reversed(current_chunk):
                prev_tokens = count_tokens(prev)
                if overlap_count + prev_tokens > overlap_tokens:
                    break
                overlap_parts.insert(0, prev)
                overlap_count += prev_tokens

            current_chunk = overlap_parts
            current_tokens = overlap_count

        current_chunk.append(paragraph)
        current_tokens += para_tokens

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    log.debug("Chunked text into %d chunks", len(chunks))
    return chunks


def _split_long_paragraph(
    paragraph: str, max_tokens: int, overlap_tokens: int
) -> list[str]:
    """Split a long paragraph by sentences with overlap."""
    # Simple sentence splitting on '. ', '! ', '? '
    sentences: list[str] = []
    current = ""
    for char in paragraph:
        current += char
        if char in ".!?" and len(current) > 1:
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())

    if not sentences:
        return [paragraph]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        sent_tokens = count_tokens(sentence)

        if current_tokens + sent_tokens > max_tokens and current_chunk:
            chunks.append(" ".join(current_chunk))

            # Overlap
            overlap_parts: list[str] = []
            overlap_count = 0
            for prev in reversed(current_chunk):
                prev_tokens = count_tokens(prev)
                if overlap_count + prev_tokens > overlap_tokens:
                    break
                overlap_parts.insert(0, prev)
                overlap_count += prev_tokens

            current_chunk = overlap_parts
            current_tokens = overlap_count

        current_chunk.append(sentence)
        current_tokens += sent_tokens

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return chunks
