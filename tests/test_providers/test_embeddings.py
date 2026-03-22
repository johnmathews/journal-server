"""Tests for the OpenAI embeddings provider."""

from unittest.mock import MagicMock, patch

from journal.providers.embeddings import EmbeddingsProvider, OpenAIEmbeddingsProvider


class TestOpenAIEmbeddingsProvider:
    """Tests for OpenAIEmbeddingsProvider."""

    def _make_provider(self) -> OpenAIEmbeddingsProvider:
        with patch("journal.providers.embeddings.openai.OpenAI"):
            provider = OpenAIEmbeddingsProvider(
                api_key="test-key",
                model="text-embedding-3-large",
                dimensions=1024,
            )
        return provider

    def test_implements_protocol(self) -> None:
        with patch("journal.providers.embeddings.openai.OpenAI"):
            provider = OpenAIEmbeddingsProvider(
                api_key="test-key", model="text-embedding-3-large", dimensions=1024
            )
        assert isinstance(provider, EmbeddingsProvider)

    def test_embed_texts_returns_correct_embeddings(self) -> None:
        provider = self._make_provider()
        mock_item_1 = MagicMock()
        mock_item_1.embedding = [0.1, 0.2, 0.3]
        mock_item_2 = MagicMock()
        mock_item_2.embedding = [0.4, 0.5, 0.6]
        mock_response = MagicMock()
        mock_response.data = [mock_item_1, mock_item_2]
        provider._client.embeddings.create.return_value = mock_response

        result = provider.embed_texts(["hello", "world"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        provider._client.embeddings.create.assert_called_once()

    def test_embed_query_returns_single_vector(self) -> None:
        provider = self._make_provider()
        mock_item = MagicMock()
        mock_item.embedding = [0.7, 0.8, 0.9]
        mock_response = MagicMock()
        mock_response.data = [mock_item]
        provider._client.embeddings.create.return_value = mock_response

        result = provider.embed_query("test query")

        assert result == [0.7, 0.8, 0.9]

    def test_dimensions_parameter_passed_to_api(self) -> None:
        provider = self._make_provider()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1])]
        provider._client.embeddings.create.return_value = mock_response

        provider.embed_texts(["test"])

        call_kwargs = provider._client.embeddings.create.call_args.kwargs
        assert call_kwargs["dimensions"] == 1024
        assert call_kwargs["model"] == "text-embedding-3-large"
