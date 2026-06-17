"""Conversation service — start a thread from a Search answer, then chat.

A conversation is seeded from an answer the user already has (no second
synthesis call). Each ``reply`` classifies the message into an intent,
dispatches to the matching handler, and falls back to the ``lookup``
handler on any non-``AnswerUnavailable`` error so a routing bug never
breaks chat. Both user and assistant turns are persisted only after the
handler succeeds — a failed reply leaves the thread consistent.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from journal.db.conversation_repository import ConversationNotFound
from journal.providers.answerer import AnswerUnavailable, ConversationTurn

if TYPE_CHECKING:
    from journal.db.conversation_repository import SQLiteConversationRepository
    from journal.models import Conversation, ConversationMessage
    from journal.providers.answerer import Answerer
    from journal.providers.intent_classifier import IntentClassifier
    from journal.services.query import QueryService

log = logging.getLogger(__name__)

#: Conversation titles are the question text, trimmed.
_MAX_TITLE_CHARS = 120


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
        classifier: IntentClassifier,
        handlers: dict[str, object],
        model: str,
    ) -> None:
        self._repo = repository
        self._query = query_service
        self._answerer = answerer
        self._classifier = classifier
        self._handlers = handlers
        self._model = model

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

        history = [
            ConversationTurn(role=m.role, content=m.content) for m in conv.messages
        ]
        history.append(ConversationTurn(role="user", content=message))
        context = "\n".join(f"{m.role}: {m.content}" for m in conv.messages)

        intent = self._classifier.classify(message, context=context)
        outcome = self._dispatch(intent, history, user_id)

        try:
            added = self._repo.add_messages(
                conversation_id,
                user_id,
                [
                    {"role": "user", "content": message, "citations": []},
                    {
                        "role": "assistant",
                        "content": outcome.answer,
                        "citations": outcome.citations,
                    },
                ],
            )
        except ConversationNotFound as e:  # raced delete between get and add
            raise ConversationNotFoundError(str(e)) from e
        return added[-1]

    def _dispatch(self, intent, history, user_id):
        """Route to the intent's handler; fall back to lookup on any error.

        AnswerUnavailable propagates (the caller maps it to 502 and
        persists nothing); every other handler error degrades to the
        lookup path so a routing bug never breaks chat.
        """
        handler = self._handlers.get(intent.intent) or self._handlers["lookup"]
        try:
            return handler.handle(history, intent, user_id)
        except AnswerUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — deliberate degrade-to-lookup
            log.warning(
                "handler %r failed (%s); falling back to lookup",
                intent.intent, e,
            )
            lookup_intent = replace(intent, intent="lookup")
            return self._handlers["lookup"].handle(history, lookup_intent, user_id)

    def list(self, user_id: int) -> list[Conversation]:
        return self._repo.list(user_id)

    def get(self, user_id: int, conversation_id: int) -> Conversation | None:
        return self._repo.get(conversation_id, user_id)

    def delete(self, user_id: int, conversation_id: int) -> bool:
        return self._repo.delete(conversation_id, user_id)
