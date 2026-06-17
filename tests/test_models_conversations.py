"""Conversation domain dataclasses."""

from journal.models import Conversation, ConversationMessage


def test_message_defaults_to_empty_citations() -> None:
    m = ConversationMessage(
        id=1, role="user", content="hi", citations=[], created_at="2026-06-17T00:00:00Z"
    )
    assert m.role == "user"
    assert m.citations == []


def test_conversation_holds_messages_and_count() -> None:
    m = ConversationMessage(
        id=1, role="user", content="hi", citations=[], created_at="2026-06-17T00:00:00Z"
    )
    c = Conversation(
        id=5, user_id=1, title="hi", created_at="t", updated_at="t",
        messages=[m], message_count=1,
    )
    assert c.id == 5
    assert c.messages[0] is m
    assert c.message_count == 1
