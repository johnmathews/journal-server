"""Conversation service — start a thread from a Search answer, then chat.

A conversation is seeded from an answer the user already has (no second
synthesis call). Each ``reply`` re-grounds: it re-retrieves passages with
the combined ``original_question + "\\n" + follow-up`` query so new
specifics pull the right entries, hands the full thread to the multi-turn
answerer, and persists the user + assistant turns only after the LLM
succeeds — a failed reply leaves the thread consistent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.conversation_repository import ConversationNotFound
from journal.providers.answerer import AnswerPassage, ConversationTurn

if TYPE_CHECKING:
    from journal.db.conversation_repository import SQLiteConversationRepository
    from journal.models import Conversation, ConversationMessage
    from journal.providers.answerer import Answerer
    from journal.services.query import QueryService

#: Conversation titles are the question text, trimmed.
_MAX_TITLE_CHARS = 120

#: Length of the citation preview snippet (chars of the entry text).
#: Mirrors ``services.answer._SNIPPET_CHARS`` — keep the two in sync.
_SNIPPET_CHARS = 160


class ConversationNotFoundError(Exception):  # noqa: N818
    """Raised when a conversation isn't found / not owned by the user.

    The route maps this to a 404.
    """


class ConversationService:
    def __init__(
        self,
        *,
        repository: SQLiteConversationRepository,
        query_service: QueryService,
        answerer: Answerer,
        model: str,
        context_entries: int = 8,
        passage_chars: int = 800,
    ) -> None:
        self._repo = repository
        self._query = query_service
        self._answerer = answerer
        self._model = model
        self._context_entries = context_entries
        self._passage_chars = passage_chars

    def start(
        self,
        user_id: int,
        question: str,
        answer: str,
        citations: list[dict],
    ) -> Conversation:
        title = question.strip()[:_MAX_TITLE_CHARS]
        seed = [
            {"role": "user", "content": question, "citations": []},
            {"role": "assistant", "content": answer, "citations": citations or []},
        ]
        return self._repo.create(user_id, title, seed)

    def reply(
        self,
        user_id: int,
        conversation_id: int,
        message: str,
    ) -> ConversationMessage:
        conv = self._repo.get(conversation_id, user_id)
        if conv is None:
            raise ConversationNotFoundError(
                f"conversation {conversation_id} not found"
            )

        original_question = conv.messages[0].content if conv.messages else message
        results = self._query.search_entries(
            query=f"{original_question}\n{message}",
            limit=self._context_entries,
            offset=0,
            user_id=user_id,
        )

        history = [
            ConversationTurn(role=m.role, content=m.content) for m in conv.messages
        ]
        history.append(ConversationTurn(role="user", content=message))

        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=r.text[: self._passage_chars],
            )
            for r in results
        ]
        # Propagates AnswerUnavailable; nothing is persisted on failure.
        result = self._answerer.continue_conversation(history, passages)

        by_id = {r.entry_id: r for r in results}
        citations = [
            {
                "entry_id": eid,
                "entry_date": by_id[eid].entry_date,
                "snippet": by_id[eid].text[:_SNIPPET_CHARS].strip(),
            }
            for eid in result.cited_entry_ids
            if eid in by_id
        ]

        try:
            added = self._repo.add_messages(
                conversation_id,
                user_id,
                [
                    {"role": "user", "content": message, "citations": []},
                    {
                        "role": "assistant",
                        "content": result.answer,
                        "citations": citations,
                    },
                ],
            )
        except ConversationNotFound as e:  # raced delete between get and add
            raise ConversationNotFoundError(str(e)) from e

        return added[-1]  # the assistant turn

    def list(self, user_id: int) -> list[Conversation]:
        return self._repo.list(user_id)

    def get(self, user_id: int, conversation_id: int) -> Conversation | None:
        return self._repo.get(conversation_id, user_id)

    def delete(self, user_id: int, conversation_id: int) -> bool:
        return self._repo.delete(conversation_id, user_id)
