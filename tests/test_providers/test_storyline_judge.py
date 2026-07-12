"""Tests for the Anthropic storyline judge provider.

Follows the canned dict-response pattern from ``test_mood_scorer.py``: a
fake client whose ``messages.create`` returns a dict-shaped response with
a ``tool_use`` content block, so tests never touch the real Anthropic SDK.
"""

from __future__ import annotations

from typing import Any

from journal.providers.storyline_judge import (
    AnthropicStorylineJudge,
    EntryForJudge,
)


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    @property
    def messages(self) -> _FakeClient:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


def _tool_response(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "tool_use", "name": name, "input": tool_input}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_judge_extension_parses_assignments() -> None:
    resp = _tool_response(
        "record_judgment",
        {
            "assignments": [
                {"entry_id": 7, "target": "draft"},
                {"entry_id": 8, "target": "new_chapter"},
                {"entry_id": 9, "target": "published_chapter", "chapter_id": 3},
            ],
            "draft_arc_complete": True,
            "reasoning": "The race happened; a new training block starts.",
        },
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="Running",
        storyline_description="",
        draft_narrative="He trained.",
        draft_entries=[],
        new_entries=[
            EntryForJudge(7, "2026-07-01", "ran"),
            EntryForJudge(8, "2026-07-02", "signed up"),
            EntryForJudge(9, "2026-03-01", "old page"),
        ],
        published_chapters=[(3, "Spring", "2026-03-01", "2026-03-31")],
    )
    assert not result.failed and result.draft_arc_complete
    assert [(a.entry_id, a.target, a.chapter_id) for a in result.assignments] == [
        (7, "draft", None), (8, "new_chapter", None), (9, "published_chapter", 3)]
    assert result.model_used == "claude-haiku-4-5"
    assert result.reasoning == "The race happened; a new training block starts."


def test_judge_extension_unknown_entry_ids_are_dropped() -> None:
    # response mentions entry_id 999 not in new_entries → dropped, not failed
    resp = _tool_response(
        "record_judgment",
        {
            "assignments": [
                {"entry_id": 7, "target": "draft"},
                {"entry_id": 999, "target": "new_chapter"},
            ],
            "draft_arc_complete": False,
            "reasoning": "Still mid-arc.",
        },
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="Running", storyline_description="",
        draft_narrative="He trained.", draft_entries=[],
        new_entries=[EntryForJudge(7, "2026-07-01", "ran")],
        published_chapters=[],
    )
    assert not result.failed
    assert [(a.entry_id, a.target, a.chapter_id) for a in result.assignments] == [
        (7, "draft", None)]


def test_judge_extension_missing_entries_default_to_draft() -> None:
    # entry 8 is absent from the model's response → parser appends it as draft
    # (nothing is ever silently lost)
    resp = _tool_response(
        "record_judgment",
        {
            "assignments": [{"entry_id": 7, "target": "new_chapter"}],
            "draft_arc_complete": False,
            "reasoning": "Only one entry classified.",
        },
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="Running", storyline_description="",
        draft_narrative="", draft_entries=[],
        new_entries=[
            EntryForJudge(7, "2026-07-01", "ran"),
            EntryForJudge(8, "2026-07-02", "signed up"),
        ],
        published_chapters=[],
    )
    assert not result.failed
    assert [(a.entry_id, a.target, a.chapter_id) for a in result.assignments] == [
        (7, "new_chapter", None), (8, "draft", None)]


def test_judge_extension_published_chapter_without_valid_id_demoted_to_draft() -> None:
    resp = _tool_response(
        "record_judgment",
        {
            "assignments": [
                {"entry_id": 7, "target": "published_chapter", "chapter_id": 999},
            ],
            "draft_arc_complete": False,
            "reasoning": "Hallucinated chapter id.",
        },
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="Running", storyline_description="",
        draft_narrative="", draft_entries=[],
        new_entries=[EntryForJudge(7, "2026-07-01", "ran")],
        published_chapters=[(3, "Spring", "2026-03-01", "2026-03-31")],
    )
    assert not result.failed
    assert [(a.entry_id, a.target, a.chapter_id) for a in result.assignments] == [
        (7, "draft", None)]


def test_judge_extension_no_tool_block_returns_failed() -> None:
    resp = {"content": [{"type": "text", "text": "sorry, I refuse"}]}
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="R", storyline_description="",
        draft_narrative="", draft_entries=[],
        new_entries=[], published_chapters=[],
    )
    assert result.failed
    assert result.assignments == []


def test_judge_extension_wrong_tool_name_returns_failed() -> None:
    resp = _tool_response(
        "some_other_tool",
        {"assignments": [], "draft_arc_complete": True, "reasoning": "x"},
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="R", storyline_description="",
        draft_narrative="", draft_entries=[],
        new_entries=[], published_chapters=[],
    )
    assert result.failed


def test_judge_extension_malformed_input_dict_returns_failed() -> None:
    # "assignments" is a string, not a list — malformed but must not crash
    resp = _tool_response(
        "record_judgment",
        {"assignments": "oops", "draft_arc_complete": "not-a-bool", "reasoning": "x"},
    )
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="R", storyline_description="",
        draft_narrative="", draft_entries=[],
        new_entries=[EntryForJudge(1, "2026-01-01", "t")], published_chapters=[],
    )
    assert result.failed


def test_judge_extension_api_failure_returns_failed() -> None:
    class _Boom:
        @property
        def messages(self) -> _Boom:
            return self

        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("api down")

    judge = AnthropicStorylineJudge(api_key="k", client=_Boom())
    result = judge.judge_extension(storyline_name="R", storyline_description="",
                                   draft_narrative="", draft_entries=[],
                                   new_entries=[], published_chapters=[])
    assert result.failed


def test_partition_parses_chapters_and_validates_coverage() -> None:
    resp = _tool_response("record_partition", {
        "chapters": [
            {"entry_ids": [1, 2], "working_title": "The Move"},
            {"entry_ids": [3], "working_title": "Settling In"},
        ]})
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(i, f"2026-0{i}-01", "t")
                                      for i in (1, 2, 3)])
    assert not result.failed
    assert [c.entry_ids for c in result.chapters] == [[1, 2], [3]]
    assert [c.working_title for c in result.chapters] == ["The Move", "Settling In"]


def test_partition_missing_entries_folded_into_last_chapter() -> None:
    # model omits entry 3 → parser appends it to the final chapter
    # (every candidate entry must land somewhere; losing entries silently
    #  is the old system's bug class)
    resp = _tool_response("record_partition", {
        "chapters": [
            {"entry_ids": [1], "working_title": "The Move"},
            {"entry_ids": [2], "working_title": "Settling In"},
        ]})
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(i, f"2026-0{i}-01", "t")
                                      for i in (1, 2, 3)])
    assert not result.failed
    assert [c.entry_ids for c in result.chapters] == [[1], [2, 3]]


def test_partition_unknown_entry_ids_are_dropped() -> None:
    resp = _tool_response("record_partition", {
        "chapters": [{"entry_ids": [1, 999], "working_title": "The Move"}]})
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(1, "2026-01-01", "t")])
    assert not result.failed
    assert [c.entry_ids for c in result.chapters] == [[1]]


def test_partition_duplicate_entry_ids_first_chapter_wins() -> None:
    resp = _tool_response("record_partition", {
        "chapters": [
            {"entry_ids": [1, 2], "working_title": "First"},
            {"entry_ids": [2, 3], "working_title": "Second"},
        ]})
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(i, f"2026-0{i}-01", "t")
                                      for i in (1, 2, 3)])
    assert not result.failed
    assert [c.entry_ids for c in result.chapters] == [[1, 2], [3]]


def test_partition_no_tool_block_returns_failed() -> None:
    resp = {"content": [{"type": "text", "text": "nope"}]}
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(1, "2026-01-01", "t")])
    assert result.failed
    assert result.chapters == []


def test_partition_api_failure_returns_failed() -> None:
    class _Boom:
        @property
        def messages(self) -> _Boom:
            return self

        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("api down")

    judge = AnthropicStorylineJudge(api_key="k", client=_Boom())
    result = judge.partition(storyline_name="House", storyline_description="", entries=[])
    assert result.failed


def test_model_property_and_tool_choice_forced() -> None:
    resp = _tool_response("record_judgment", {
        "assignments": [], "draft_arc_complete": False, "reasoning": "x"})
    client = _FakeClient(resp)
    judge = AnthropicStorylineJudge(api_key="k", model="claude-haiku-4-5", client=client)
    assert judge.model == "claude-haiku-4-5"
    judge.judge_extension(storyline_name="R", storyline_description="",
                           draft_narrative="", draft_entries=[],
                           new_entries=[], published_chapters=[])
    assert client.calls[0]["tool_choice"] == {"type": "tool", "name": "record_judgment"}
    assert client.calls[0]["tools"][0]["name"] == "record_judgment"
