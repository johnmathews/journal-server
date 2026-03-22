"""Embeddings Protocol and OpenAI adapter."""

import logging
from typing import Protocol, runtime_checkable

import openai

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingsProvider(Protocol):
    """Protocol for text embedding providers."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class OpenAIEmbeddingsProvider:
    """Embeddings provider using OpenAI's embeddings API."""

    def __init__(self, api_key: str, model: str, dimensions: int) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts and return their embedding vectors."""
        logger.info("Embedding %d texts via OpenAI (model=%s)", len(texts), self._model)

        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )

        embeddings = [item.embedding for item in response.data]
        logger.info("Embedding complete (%d vectors)", len(embeddings))
        return embeddings

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query string and return its embedding vector."""
        logger.info("Embedding query via OpenAI (model=%s)", self._model)
        return self.embed_texts([query])[0]
