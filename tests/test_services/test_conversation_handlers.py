"""Conversation intent handlers."""

from __future__ import annotations

from journal.models import Entry, MoodTrend, SearchResult, TopicFrequency
from journal.providers.answerer import AnswerResult, ConversationTurn
from journal.providers.intent_classifier import IntentResult
from journal.services.conversations.handlers import (
    AggregateHandler,
    LookupHandler,
    ReplyOutcome,
    TemporalHandler,
    TrendHandler,
)


def _sr(entry_id: int, text: str, score: float = 1.0) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date="2026-03-01", text=text, score=score,
        matching_chunks=[], snippet=None,
    )


class _Query:
    def __init__(self, **returns):
        self.returns = returns
        self.calls: list[tuple[str, dict]] = []

    def search_entries(self, **kw):
        self.calls.append(("search_entries", kw))
        return self.returns.get("search_entries", [])

    def get_topic_frequency(self, topic, start_date=None, end_date=None, user_id=None):
        self.calls.append(("get_topic_frequency", {"topic": topic}))
        return self.returns["get_topic_frequency"]

    def get_mood_trends(self, start_date=None, end_date=None, granularity="week", user_id=None):
        self.calls.append(("get_mood_trends", {}))
        return self.returns.get("get_mood_trends", [])


class _Answerer:
    def __init__(self, result: AnswerResult):
        self._result = result
        self.last_kwargs = None

    def continue_conversation(self, history, passages, *, context_note=None, retrieve=None):
        self.last_kwargs = {
            "passages": passages,
            "context_note": context_note,
            "retrieve": retrieve,
        }
        return self._result


def _history() -> list[ConversationTurn]:
    return [ConversationTurn(role="user", content="follow up")]


def test_lookup_handler_passes_retrieve_and_resolves_citations() -> None:
    query = _Query(search_entries=[_sr(7, "Back better now.")])
    answerer = _Answerer(AnswerResult("Around March.", True, [7]))
    handler = LookupHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="lookup", search_query="back pain better")

    out = handler.handle(_history(), intent, user_id=1)

    assert isinstance(out, ReplyOutcome)
    assert out.answer == "Around March."
    assert out.citations[0]["entry_id"] == 7
    # lookup gives the answerer a one-hop retrieve callback
    assert answerer.last_kwargs["retrieve"] is not None
    # retrieval used the condensed search_query, larger candidate pool
    assert query.calls[0][1]["query"] == "back pain better"
    assert query.calls[0][1]["limit"] >= 15


def test_aggregate_handler_injects_count_note_and_cites_entries() -> None:
    entries = [Entry(id=7, entry_date="2026-02-14", source_type="text",
                     raw_text="my back hurt", final_text="my back hurt")]
    tf = TopicFrequency(topic="back", count=40, entries=entries)
    query = _Query(get_topic_frequency=tf)
    answerer = _Answerer(AnswerResult("You mentioned it 40 times.", True, [7]))
    handler = AggregateHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="aggregate", search_query="back",
                          topic="back")

    out = handler.handle(_history(), intent, user_id=1)

    assert query.calls[0][0] == "get_topic_frequency"
    assert "40" in answerer.last_kwargs["context_note"]
    assert out.citations[0]["entry_id"] == 7
    assert out.answer == "You mentioned it 40 times."


def test_temporal_handler_sorts_ascending_and_cites() -> None:
    query = _Query(search_entries=[_sr(7, "First back pain.")])
    answerer = _Answerer(AnswerResult("It started 2026-03-01.", True, [7]))
    handler = TemporalHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="temporal", search_query="back pain start")

    out = handler.handle(_history(), intent, user_id=1)

    # date_asc guarantees the earliest evidencing entry is present
    assert query.calls[0][1]["sort"] == "date_asc"
    assert out.citations[0]["entry_id"] == 7
    assert out.answer == "It started 2026-03-01."


def test_trend_handler_summarizes_series_into_note() -> None:
    trends = [
        MoodTrend(period="2026-W01", dimension="happiness", avg_score=0.3,
                  entry_count=4),
        MoodTrend(period="2026-W20", dimension="happiness", avg_score=0.7,
                  entry_count=5),
    ]
    query = _Query(get_mood_trends=trends,
                   search_entries=[_sr(7, "Felt great.")])
    answerer = _Answerer(AnswerResult("You've trended happier.", True, [7]))
    handler = TrendHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="trend", search_query="happiness over time",
                          dimension="happiness")

    out = handler.handle(_history(), intent, user_id=1)

    assert query.calls[0][0] == "get_mood_trends"
    assert "happiness" in answerer.last_kwargs["context_note"]
    assert "0.3" in answerer.last_kwargs["context_note"]
    assert out.answer == "You've trended happier."


def test_trend_handler_resolves_near_miss_dimension_to_real_facet() -> None:
    # LLM emits "energy"; the real facet is "energy_vigor". Exact-equality
    # matching yielded an empty series + "no data". Now it must resolve.
    trends = [
        MoodTrend(period="2026-W01", dimension="energy_vigor", avg_score=-0.2,
                  entry_count=4),
        MoodTrend(period="2026-W20", dimension="energy_vigor", avg_score=0.4,
                  entry_count=5),
        MoodTrend(period="2026-W01", dimension="joy_sadness", avg_score=0.1,
                  entry_count=4),
    ]
    query = _Query(get_mood_trends=trends,
                   search_entries=[_sr(7, "Low energy.")])
    answerer = _Answerer(AnswerResult("Your energy dipped then recovered.",
                                      True, [7]))
    handler = TrendHandler(
        query, answerer, passage_chars=800,
        dimension_names=["joy_sadness", "energy_vigor", "physical_fatigue"],
    )
    intent = IntentResult(intent="trend", search_query="energy over time",
                          dimension="energy")

    out = handler.handle(_history(), intent, user_id=1)

    note = answerer.last_kwargs["context_note"]
    assert "energy_vigor" in note
    assert "No mood-trend data" not in note
    # resolved to a single facet — the unrelated facet must not leak in
    assert "joy_sadness" not in note
    assert out.answer == "Your energy dipped then recovered."


def test_trend_handler_ambiguous_near_miss_falls_back_to_all_dimensions() -> None:
    # "tiredness" is ambiguous between physical_ and mental_fatigue, so it
    # must degrade to all dimensions rather than returning no data.
    trends = [
        MoodTrend(period="2026-W01", dimension="physical_fatigue",
                  avg_score=0.6, entry_count=4),
        MoodTrend(period="2026-W01", dimension="mental_fatigue",
                  avg_score=0.3, entry_count=4),
    ]
    query = _Query(get_mood_trends=trends,
                   search_entries=[_sr(7, "Wiped out.")])
    answerer = _Answerer(AnswerResult("You've been tired.", True, [7]))
    handler = TrendHandler(
        query, answerer, passage_chars=800,
        dimension_names=["physical_fatigue", "mental_fatigue"],
    )
    intent = IntentResult(intent="trend", search_query="why so tired",
                          dimension="tiredness")

    out = handler.handle(_history(), intent, user_id=1)

    note = answerer.last_kwargs["context_note"]
    assert "No mood-trend data" not in note
    assert "physical_fatigue" in note
    assert "mental_fatigue" in note
    assert out.answer == "You've been tired."


def test_trend_handler_null_dimension_labels_each_series() -> None:
    # With no dimension, every facet's series must be labeled so the LLM
    # can tell them apart (previously all interleaved unlabeled).
    trends = [
        MoodTrend(period="2026-W01", dimension="energy_vigor", avg_score=0.2,
                  entry_count=4),
        MoodTrend(period="2026-W20", dimension="energy_vigor", avg_score=0.5,
                  entry_count=5),
        MoodTrend(period="2026-W01", dimension="agency", avg_score=-0.1,
                  entry_count=4),
    ]
    query = _Query(get_mood_trends=trends,
                   search_entries=[_sr(7, "Mixed week.")])
    answerer = _Answerer(AnswerResult("Here's the breakdown.", True, [7]))
    handler = TrendHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="trend", search_query="how have I been",
                          dimension=None)

    out = handler.handle(_history(), intent, user_id=1)

    note = answerer.last_kwargs["context_note"]
    # each dimension is identifiable in the serialized note
    assert "energy_vigor" in note
    assert "agency" in note
    assert out.answer == "Here's the breakdown."


def test_trend_handler_no_trends_reports_no_data() -> None:
    query = _Query(get_mood_trends=[], search_entries=[_sr(7, "Nothing.")])
    answerer = _Answerer(AnswerResult("No data.", True, [7]))
    handler = TrendHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="trend", search_query="mood", dimension=None)

    handler.handle(_history(), intent, user_id=1)

    assert "No mood-trend data" in answerer.last_kwargs["context_note"]
