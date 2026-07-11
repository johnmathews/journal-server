"""Tests for the OpenAI embeddings provider."""

from unittest.mock import MagicMock, patch

from journal.providers.embeddings import EmbeddingsProvider, OpenAIEmbeddingsProvider


class TestOpenAIEmbeddingsProvider:
    """Tests for OpenAIEmbeddingsProvider."""

    def _make_provider(self) -> tuple[OpenAIEmbeddingsProvider, MagicMock]:
        """Build the provider with the SDK class patched and return the
        fake client alongside it so tests configure ``embeddings.create``
        without reaching into ``provider._client``.
        """
        fake_client = MagicMock(name="openai.OpenAI")
        with patch(
            "journal.providers.embeddings.openai.OpenAI",
            return_value=fake_client,
        ):
            provider = OpenAIEmbeddingsProvider(
                api_key="test-key",
                model="text-embedding-3-large",
                dimensions=1024,
            )
        return provider, fake_client

    def test_implements_protocol(self) -> None:
        provider, _client = self._make_provider()
        assert isinstance(provider, EmbeddingsProvider)

    def test_embed_texts_returns_correct_embeddings(self) -> None:
        provider, client = self._make_provider()
        mock_item_1 = MagicMock()
        mock_item_1.embedding = [0.1, 0.2, 0.3]
        mock_item_2 = MagicMock()
        mock_item_2.embedding = [0.4, 0.5, 0.6]
        mock_response = MagicMock()
        mock_response.data = [mock_item_1, mock_item_2]
        client.embeddings.create.return_value = mock_response

        result = provider.embed_texts(["hello", "world"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        client.embeddings.create.assert_called_once()

    def test_embed_query_returns_single_vector(self) -> None:
        provider, client = self._make_provider()
        mock_item = MagicMock()
        mock_item.embedding = [0.7, 0.8, 0.9]
        mock_response = MagicMock()
        mock_response.data = [mock_item]
        client.embeddings.create.return_value = mock_response

        result = provider.embed_query("test query")

        assert result == [0.7, 0.8, 0.9]

    def test_embed_texts_records_openai_usage_in_scope(self) -> None:
        from types import SimpleNamespace

        from journal.services import usage

        provider, client = self._make_provider()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1])]
        # Embeddings responses carry prompt_tokens but no completion.
        mock_response.usage = SimpleNamespace(prompt_tokens=64)
        client.embeddings.create.return_value = mock_response

        with usage.usage_scope() as collector:
            provider.embed_texts(["hello", "world"])

        assert collector.totals == (64, 0)

    def test_embed_texts_off_job_does_not_crash(self) -> None:
        # Default MagicMock response (.usage is a MagicMock) + no scope:
        # record_openai must no-op without raising.
        provider, client = self._make_provider()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1])]
        client.embeddings.create.return_value = mock_response
        assert provider.embed_texts(["hi"]) == [[0.1]]

    def test_dimensions_parameter_passed_to_api(self) -> None:
        provider, client = self._make_provider()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1])]
        client.embeddings.create.return_value = mock_response

        provider.embed_texts(["test"])

        call_kwargs = client.embeddings.create.call_args.kwargs
        assert call_kwargs["dimensions"] == 1024
        assert call_kwargs["model"] == "text-embedding-3-large"
