"""Chunking quality evaluation.

Computes intrinsic quality metrics over the stored chunks without
needing a ground-truth query set:

- **Cohesion**: for each chunk, the mean pairwise cosine similarity of
  its sentence embeddings. Averaged across all chunks that have at
  least 2 sentences. Higher = sentences in the same chunk are talking
  about the same thing.

- **Separation**: for each adjacent pair of chunks within the same
  entry, `1 - cosine(chunk_N_centroid, chunk_{N+1}_centroid)` where
  the centroids are the already-stored chunk embeddings. Higher =
  adjacent chunks are actually about different things.

- **Ratio**: `cohesion / max(1 - separation, epsilon)`. Higher = both
  coherent internally AND distinct externally. Dimensionless,
  comparable across rechunk runs.

These are "intrinsic" metrics — they reward chunkers that cluster
semantically related sentences together and separate unrelated ones.
They don't need labelled data.

Cost: one extra `embed_texts` call per chunk (to embed the chunk's
sentences individually). For a corpus of 50 entries × 5 chunks × 3
sentences per chunk, that's ~750 sentences in small batches. Still
cheap compared to a rechunk run.
"""

from dataclasses import dataclass

import numpy as np

from journal.db.repository import EntryRepository
from journal.providers.embeddings import EmbeddingsProvider
from journal.services.chunking import split_sentences
from journal.vectorstore.store import VectorStore


@dataclass
class ChunkingEvalResult:
    """Result of an eval-chunking run."""

    cohesion: float
    separation: float
    ratio: float
    n_chunks_evaluated: int
    n_entries_evaluated: int
    n_pairs_evaluated: int

    def as_dict(self) -> dict:
        return {
            "cohesion": self.cohesion,
            "separation": self.separation,
            "ratio": self.ratio,
            "n_chunks_evaluated": self.n_chunks_evaluated,
            "n_entries_evaluated": self.n_entries_evaluated,
            "n_pairs_evaluated": self.n_pairs_evaluated,
        }


def _mean_pairwise_cosine(vectors: list[list[float]]) -> float:
    """Mean of all pairwise cosine similarities in a list of vectors.

    Returns 1.0 for zero or one vector (vacuously coherent — nothing to
    compare). Uses numpy for the matrix math.
    """
    if len(vectors) < 2:
        return 1.0
    vecs = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    normed = vecs / np.maximum(norms, 1e-12)
    sim_matrix = normed @ normed.T
    # Take the upper triangle above the diagonal — those are the n*(n-1)/2
    # unique pairwise similarities.
    n = len(vectors)
    iu = np.triu_indices(n, k=1)
    return float(sim_matrix[iu].mean())


def _cosine(a: list[float], b: list[float]) -> float:
    """Single-pair cosine similarity (numpy). Returns 0.0 if either is zero."""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def evaluate_chunking(
    repository: EntryRepository,
    vector_store: VectorStore,
    embeddings: EmbeddingsProvider,
) -> ChunkingEvalResult:
    """Compute cohesion / separation / ratio over the whole stored corpus.

    Walks every entry, fetches its stored chunks + their embeddings,
    re-embeds the chunk's individual sentences to compute cohesion,
    and uses the already-stored chunk embeddings to compute separation
    between adjacent chunks within the entry.
    """
    entries = repository.list_entries(limit=1_000_000)

    total_cohesion = 0.0
    n_chunks_evaluated = 0
    n_entries_evaluated = 0

    total_separation = 0.0
    n_pairs_evaluated = 0

    for entry in entries:
        chunks = vector_store.get_chunks_for_entry(entry.id)
        if not chunks:
            continue
        n_entries_evaluated += 1

        # --- Cohesion: per-chunk mean pairwise sentence similarity ---
        # Batch all the sentences across all chunks in this entry to
        # reduce the number of embed_texts calls.
        sentence_lists: list[list[str]] = []
        flat_sentences: list[str] = []
        for chunk in chunks:
            sents = split_sentences(chunk.text)
            sentence_lists.append(sents)
            flat_sentences.extend(sents)

        if flat_sentences:
            flat_vectors = embeddings.embed_texts(flat_sentences)
            # Slice the flat list back into per-chunk sentence vectors.
            offset = 0
            for sents in sentence_lists:
                n = len(sents)
                if n >= 2:
                    chunk_vecs = flat_vectors[offset : offset + n]
                    total_cohesion += _mean_pairwise_cosine(chunk_vecs)
                    n_chunks_evaluated += 1
                elif n == 1:
                    # Single-sentence chunk is trivially cohesive. Count it
                    # with a perfect score so chunkers that produce lots of
                    # tiny chunks don't get unfairly penalised.
                    total_cohesion += 1.0
                    n_chunks_evaluated += 1
                offset += n

        # --- Separation: adjacent-chunk centroid similarity ---
        # Use the already-stored chunk embeddings; no re-embed needed.
        for prev, curr in zip(chunks[:-1], chunks[1:], strict=False):
            sim = _cosine(prev.embedding, curr.embedding)
            total_separation += 1.0 - sim
            n_pairs_evaluated += 1

    cohesion = total_cohesion / max(n_chunks_evaluated, 1)
    separation = total_separation / max(n_pairs_evaluated, 1)
    # The ratio rewards both (cohesion close to 1) and (separation
    # close to 1). Using (1 - separation) in the denominator inverts
    # the separation axis so larger separation → larger ratio.
    ratio = cohesion / max(1.0 - separation, 1e-6)

    return ChunkingEvalResult(
        cohesion=cohesion,
        separation=separation,
        ratio=ratio,
        n_chunks_evaluated=n_chunks_evaluated,
        n_entries_evaluated=n_entries_evaluated,
        n_pairs_evaluated=n_pairs_evaluated,
    )
