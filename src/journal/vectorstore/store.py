"""Vector store interface and ChromaDB implementation."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import chromadb

log = logging.getLogger(__name__)


@dataclass
class VectorSearchResult:
    entry_id: int
    chunk_text: str
    distance: float
    metadata: dict[str, Any]


@runtime_checkable
class VectorStore(Protocol):
    def add_entry(
        self,
        entry_id: int,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict[str, Any],
    ) -> None: ...

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]: ...

    def delete_entry(self, entry_id: int) -> None: ...

    def count(self) -> int: ...


class ChromaVectorStore:
    def __init__(self, host: str, port: int, collection_name: str) -> None:
        self._client = chromadb.HttpClient(host=host, port=port)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "Connected to ChromaDB at %s:%d, collection '%s'", host, port, collection_name
        )

    def add_entry(
        self,
        entry_id: int,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict[str, Any],
    ) -> None:
        ids = [f"{entry_id}-{i}" for i in range(len(chunks))]
        metadatas = [
            {**metadata, "entry_id": entry_id, "chunk_index": i} for i in range(len(chunks))
        ]

        self._collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        log.info("Added %d chunks for entry %d", len(chunks), entry_id)

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = self._collection.query(**kwargs)

        search_results = []
        if results["ids"] and results["ids"][0]:
            for i, _doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                search_results.append(
                    VectorSearchResult(
                        entry_id=meta.get("entry_id", 0),
                        chunk_text=results["documents"][0][i] if results["documents"] else "",
                        distance=results["distances"][0][i] if results["distances"] else 0.0,
                        metadata=meta,
                    )
                )
        return search_results

    def delete_entry(self, entry_id: int) -> None:
        self._collection.delete(where={"entry_id": entry_id})
        log.info("Deleted chunks for entry %d", entry_id)

    def count(self) -> int:
        return self._collection.count()


class InMemoryVectorStore:
    """Simple in-memory vector store for testing."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def add_entry(
        self,
        entry_id: int,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict[str, Any],
    ) -> None:
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
            doc_id = f"{entry_id}-{i}"
            self._entries[doc_id] = {
                "entry_id": entry_id,
                "chunk_text": chunk,
                "embedding": embedding,
                "metadata": {**metadata, "entry_id": entry_id, "chunk_index": i},
            }

    def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[VectorSearchResult]:
        results = []
        for doc in self._entries.values():
            if where:
                skip = False
                for key, value in where.items():
                    if doc["metadata"].get(key) != value:
                        skip = True
                        break
                if skip:
                    continue

            distance = self._cosine_distance(query_embedding, doc["embedding"])
            results.append(
                VectorSearchResult(
                    entry_id=doc["entry_id"],
                    chunk_text=doc["chunk_text"],
                    distance=distance,
                    metadata=doc["metadata"],
                )
            )
        results.sort(key=lambda r: r.distance)
        return results[:limit]

    def delete_entry(self, entry_id: int) -> None:
        to_delete = [k for k, v in self._entries.items() if v["entry_id"] == entry_id]
        for k in to_delete:
            del self._entries[k]

    def count(self) -> int:
        return len(self._entries)

    @staticmethod
    def _cosine_distance(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 1.0
        return 1.0 - dot / (norm_a * norm_b)
