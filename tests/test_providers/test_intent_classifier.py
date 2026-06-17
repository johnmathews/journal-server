"""Intent classifier — heuristic fallback + Anthropic JSON parsing."""

from __future__ import annotations

from journal.providers.intent_classifier import (
    HeuristicIntentClassifier,
    IntentResult,
    build_intent_classifier,
)


def test_heuristic_aggregate() -> None:
    r = HeuristicIntentClassifier().classify("how many times did I mention my back?")
    assert r.intent == "aggregate"


def test_heuristic_temporal() -> None:
    r = HeuristicIntentClassifier().classify("when did the back pain start?")
    assert r.intent == "temporal"


def test_heuristic_trend() -> None:
    r = HeuristicIntentClassifier().classify("have I gotten happier this year?")
    assert r.intent == "trend"


def test_heuristic_defaults_to_lookup() -> None:
    r = HeuristicIntentClassifier().classify("what did I say about Vienna?")
    assert r.intent == "lookup"
    # search_query defaults to the question itself
    assert r.search_query == "what did I say about Vienna?"


def test_anthropic_parse_via_builder_falls_back_to_heuristic_on_blank() -> None:
    clf = build_intent_classifier("none")
    assert isinstance(clf.classify("anything"), IntentResult)


def test_anthropic_parses_structured_json(monkeypatch) -> None:
    from journal.providers import intent_classifier as mod

    clf = mod.AnthropicIntentClassifier(api_key="x")

    class _Block:
        text = (
            '{"intent": "aggregate", "topic": "back", '
            '"start_date": null, "end_date": null, "dimension": null, '
            '"search_query": "back pain mentions"}'
        )

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(clf._client.messages, "create", lambda **_: _Resp())
    r = clf.classify("how many times did I mention my back?")
    assert r.intent == "aggregate"
    assert r.topic == "back"
    assert r.search_query == "back pain mentions"


def test_anthropic_malformed_falls_back_to_heuristic(monkeypatch) -> None:
    from journal.providers import intent_classifier as mod

    clf = mod.AnthropicIntentClassifier(api_key="x")

    class _Block:
        text = "not json"

    class _Resp:
        content = [_Block()]

    monkeypatch.setattr(clf._client.messages, "create", lambda **_: _Resp())
    r = clf.classify("when did my back start hurting?")
    assert r.intent == "temporal"  # heuristic rescued it
