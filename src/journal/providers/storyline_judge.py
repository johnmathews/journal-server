"""LLM judge for storylines-redesign chapter boundaries (Haiku).

Two forced tool calls handle the two decisions the redesign needs:

* ``judge_extension`` — given a draft chapter's narrative-so-far and a
  batch of new journal entries, assign each entry to the draft
  (continues the current arc), a new chapter (a fresh arc begins), or
  an already-published chapter (a late-arriving addendum to a finished
  arc) — and say whether the draft's own arc has concluded.
* ``partition`` — given a storyline's full entry history (used once, at
  bootstrap, when a storyline has no chapters yet), split it into an
  ordered list of chapters.

Both methods follow the house provider pattern (see
``storyline_extension_decider.py`` for the forced tool-use idiom and
``storyline_narrator.py`` for the ``_attr_or_key`` dict/attr bridging
this module also uses, since tests inject dict-shaped canned responses
while the real Anthropic SDK returns Pydantic objects).

Parsing is defensive by construction: unknown entry ids are dropped
(logged), a ``published_chapter`` assignment without a valid
``chapter_id`` is demoted to ``draft``, and any new/candidate entry the
model's response doesn't mention is appended as ``draft`` (extension) or
folded into the last chapter (partition) — nothing is ever silently
lost. Any exception, missing tool block, wrong tool name, or malformed
tool input yields a ``failed=True`` result; callers decide how to
retry or surface that to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from journal.services import usage

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the chapter editor for a personal journal's "storylines" — evolving
narrative threads about subjects in the author's life. Chapters are arcs with
a beginning and an end, like chapters of a memoir: a phase, a project, a
build-up to an event, an aftermath. You will be asked either to judge whether
new journal entries continue the current draft chapter or begin a new arc, or
to partition a full history of entries into chapters.

Judgment principles — all load-bearing:

* Boundaries are SEMANTIC: a new arc starts when the situation, goal, or
  emotional register genuinely shifts (an event happens, a decision lands, a
  phase ends). Never split on word count, entry count, or elapsed time alone.
* Prefer continuing the draft when in doubt. Chapters should feel complete;
  a premature break produces fragments.
* An entry dated long before the draft's period that clearly belongs to an
  earlier, already-published arc should be assigned to that published
  chapter (it becomes an addendum there).
* Base every decision only on the provided material. Keep reasoning to one
  or two sentences; it is shown to the user.
"""


_JUDGMENT_TOOL: dict[str, Any] = {
    "name": "record_judgment",
    "description": "Record the chapter assignment for each new entry.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "target": {"type": "string",
                                   "enum": ["draft", "new_chapter", "published_chapter"]},
                        "chapter_id": {"type": "integer",
                                       "description": "Required when target is published_chapter."},
                    },
                    "required": ["entry_id", "target"],
                },
            },
            "draft_arc_complete": {
                "type": "boolean",
                "description": (
                    "True if the draft chapter's arc has concluded and it "
                    "should be published."
                ),
            },
            "reasoning": {"type": "string"},
        },
        "required": ["assignments", "draft_arc_complete", "reasoning"],
    },
}


_PARTITION_TOOL: dict[str, Any] = {
    "name": "record_partition",
    "description": "Record the partition of a full entry history into chapters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "chapters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entry_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "working_title": {"type": "string"},
                    },
                    "required": ["entry_ids", "working_title"],
                },
            },
        },
        "required": ["chapters"],
    },
}


@dataclass
class EntryAssignment:
    entry_id: int
    target: str  # 'draft' | 'new_chapter' | 'published_chapter'
    chapter_id: int | None = None  # set iff target == 'published_chapter'


@dataclass
class ExtensionJudgment:
    assignments: list[EntryAssignment]
    draft_arc_complete: bool
    reasoning: str
    model_used: str = ""
    failed: bool = False  # True on API failure / malformed response


@dataclass
class PartitionChapter:
    entry_ids: list[int]
    working_title: str


@dataclass
class PartitionResult:
    chapters: list[PartitionChapter]
    model_used: str = ""
    failed: bool = False


@dataclass
class EntryForJudge:
    entry_id: int
    entry_date: str
    text: str


@runtime_checkable
class StorylineJudgeProtocol(Protocol):
    def judge_extension(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        draft_narrative: str,
        draft_entries: list[EntryForJudge],
        new_entries: list[EntryForJudge],
        published_chapters: list[tuple[int, str, str, str]],
    ) -> ExtensionJudgment: ...
    # published_chapters: (chapter_id, title, first_entry_date, last_entry_date)

    def partition(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        entries: list[EntryForJudge],
    ) -> PartitionResult: ...


class AnthropicStorylineJudge:
    """Haiku judge for chapter extension decisions and history partitioning."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 8192,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def judge_extension(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        draft_narrative: str,
        draft_entries: list[EntryForJudge],
        new_entries: list[EntryForJudge],
        published_chapters: list[tuple[int, str, str, str]],
    ) -> ExtensionJudgment:
        user_text = _build_judge_user_text(
            storyline_name=storyline_name,
            storyline_description=storyline_description,
            draft_narrative=draft_narrative,
            draft_entries=draft_entries,
            new_entries=new_entries,
            published_chapters=published_chapters,
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_JUDGMENT_TOOL],
                tool_choice={"type": "tool", "name": "record_judgment"},
                messages=[{"role": "user", "content": user_text}],
            )
            usage.record_anthropic(self._model, response)
        except Exception:  # noqa: BLE001 — provider failures surface as failed
            log.exception("Storyline judge extension API call failed")
            return _failed_judgment(self._model)

        try:
            return _parse_judgment(
                response,
                new_entries=new_entries,
                published_chapters=published_chapters,
                model=self._model,
            )
        except Exception:  # noqa: BLE001 — malformed response, never crash
            log.exception("Storyline judge extension response was unparsable")
            return _failed_judgment(self._model)

    def partition(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        entries: list[EntryForJudge],
    ) -> PartitionResult:
        user_text = _build_partition_user_text(
            storyline_name=storyline_name,
            storyline_description=storyline_description,
            entries=entries,
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_PARTITION_TOOL],
                tool_choice={"type": "tool", "name": "record_partition"},
                messages=[{"role": "user", "content": user_text}],
            )
            usage.record_anthropic(self._model, response)
        except Exception:  # noqa: BLE001 — provider failures surface as failed
            log.exception("Storyline judge partition API call failed")
            return PartitionResult(chapters=[], model_used=self._model, failed=True)

        try:
            return _parse_partition(response, entries=entries, model=self._model)
        except Exception:  # noqa: BLE001 — malformed response, never crash
            log.exception("Storyline judge partition response was unparsable")
            return PartitionResult(chapters=[], model_used=self._model, failed=True)


def _failed_judgment(model: str) -> ExtensionJudgment:
    return ExtensionJudgment(
        assignments=[],
        draft_arc_complete=False,
        reasoning="judge unavailable",
        model_used=model,
        failed=True,
    )


def _build_judge_user_text(
    *,
    storyline_name: str,
    storyline_description: str,
    draft_narrative: str,
    draft_entries: list[EntryForJudge],
    new_entries: list[EntryForJudge],
    published_chapters: list[tuple[int, str, str, str]],
) -> str:
    lines: list[str] = [
        f"Storyline: {storyline_name}",
        f"Description: {storyline_description.strip() or '(none provided)'}",
        "",
        "Draft chapter narrative so far:",
        draft_narrative.strip() or "(none yet)",
        "",
    ]
    if draft_entries:
        lines.append("Draft chapter entries:")
        for entry in draft_entries:
            excerpt = entry.text[:300]
            lines.append(f"- [id {entry.entry_id}] {entry.entry_date}: {excerpt}")
        lines.append("")
    if published_chapters:
        lines.append("Published chapters (already-finished arcs):")
        for chapter_id, title, first_date, last_date in published_chapters:
            lines.append(
                f'- chapter {chapter_id} "{title}" ({first_date} → {last_date})'
            )
        lines.append("")
    lines.append("New entries to classify:")
    for entry in new_entries:
        lines.append(f"- [id {entry.entry_id}] {entry.entry_date}:")
        lines.append(entry.text)
        lines.append("")
    lines.append("Call the record_judgment tool with your assignment for every new entry.")
    return "\n".join(lines)


def _build_partition_user_text(
    *,
    storyline_name: str,
    storyline_description: str,
    entries: list[EntryForJudge],
) -> str:
    lines: list[str] = [
        f"Storyline: {storyline_name}",
        f"Description: {storyline_description.strip() or '(none provided)'}",
        "",
        "Entries to partition into chapters, in chronological order:",
    ]
    for entry in entries:
        lines.append(f"- [id {entry.entry_id}] {entry.entry_date}:")
        lines.append(entry.text)
        lines.append("")
    lines.append(
        "Call the record_partition tool with an ordered list of chapters; "
        "every entry id must appear in exactly one chapter."
    )
    return "\n".join(lines)


def _attr_or_key(obj: Any, key: str) -> Any:  # noqa: ANN401
    """Read ``key`` from either an attribute (SDK objects) or a dict
    (tests' canned responses)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _find_tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    """Find the first ``tool_use`` block named ``tool_name`` and return its
    ``input`` dict, or ``None`` if no such block exists (missing tool block,
    wrong tool name, or a non-dict ``input``)."""
    content = _attr_or_key(response, "content")
    if not content:
        return None
    for block in content:
        if _attr_or_key(block, "type") != "tool_use":
            continue
        if _attr_or_key(block, "name") != tool_name:
            continue
        tool_input = _attr_or_key(block, "input")
        if isinstance(tool_input, dict):
            return tool_input
    return None


def _parse_judgment(
    response: Any,  # noqa: ANN401
    *,
    new_entries: list[EntryForJudge],
    published_chapters: list[tuple[int, str, str, str]],
    model: str,
) -> ExtensionJudgment:
    tool_input = _find_tool_input(response, "record_judgment")
    if tool_input is None:
        log.warning("Storyline judge: no usable record_judgment tool call in response")
        return _failed_judgment(model)

    raw_assignments = tool_input.get("assignments")
    draft_arc_complete = tool_input.get("draft_arc_complete")
    reasoning = tool_input.get("reasoning", "")
    if not isinstance(raw_assignments, list) or not isinstance(draft_arc_complete, bool):
        log.warning("Storyline judge: malformed record_judgment input %r", tool_input)
        return _failed_judgment(model)

    known_entries = {entry.entry_id: entry for entry in new_entries}
    known_chapter_ids = {chapter_id for chapter_id, *_ in published_chapters}

    assignments: list[EntryAssignment] = []
    seen_ids: set[int] = set()
    for raw in raw_assignments:
        if not isinstance(raw, dict):
            log.warning("Storyline judge: dropping non-dict assignment %r", raw)
            continue
        entry_id = raw.get("entry_id")
        target = raw.get("target")
        if not isinstance(entry_id, int) or entry_id not in known_entries:
            log.warning("Storyline judge: dropping unknown entry_id %r", entry_id)
            continue
        if entry_id in seen_ids:
            log.warning(
                "Storyline judge: entry_id %d assigned more than once — "
                "keeping first assignment", entry_id,
            )
            continue
        if target not in {"draft", "new_chapter", "published_chapter"}:
            log.warning(
                "Storyline judge: dropping entry %d with unknown target %r",
                entry_id, target,
            )
            continue
        chapter_id = raw.get("chapter_id")
        if target == "published_chapter":
            if not isinstance(chapter_id, int) or chapter_id not in known_chapter_ids:
                log.warning(
                    "Storyline judge: entry %d assigned to published_chapter "
                    "with invalid chapter_id %r — demoting to draft",
                    entry_id, chapter_id,
                )
                target = "draft"
                chapter_id = None
        else:
            chapter_id = None
        assignments.append(
            EntryAssignment(entry_id=entry_id, target=target, chapter_id=chapter_id)
        )
        seen_ids.add(entry_id)

    # Every new entry absent from the response is appended as draft — nothing
    # is ever silently lost.
    for entry in new_entries:
        if entry.entry_id not in seen_ids:
            log.warning(
                "Storyline judge: entry %d missing from response — "
                "defaulting to draft", entry.entry_id,
            )
            assignments.append(
                EntryAssignment(entry_id=entry.entry_id, target="draft", chapter_id=None)
            )
            seen_ids.add(entry.entry_id)

    return ExtensionJudgment(
        assignments=assignments,
        draft_arc_complete=draft_arc_complete,
        reasoning=str(reasoning),
        model_used=model,
        failed=False,
    )


def _parse_partition(
    response: Any,  # noqa: ANN401
    *,
    entries: list[EntryForJudge],
    model: str,
) -> PartitionResult:
    tool_input = _find_tool_input(response, "record_partition")
    if tool_input is None:
        log.warning("Storyline judge: no usable record_partition tool call in response")
        return PartitionResult(chapters=[], model_used=model, failed=True)

    raw_chapters = tool_input.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        log.warning("Storyline judge: malformed record_partition input %r", tool_input)
        return PartitionResult(chapters=[], model_used=model, failed=True)

    known_ids = [entry.entry_id for entry in entries]
    known_id_set = set(known_ids)

    chapters: list[PartitionChapter] = []
    seen_ids: set[int] = set()
    for raw in raw_chapters:
        if not isinstance(raw, dict):
            log.warning("Storyline judge: dropping non-dict chapter %r", raw)
            continue
        raw_entry_ids = raw.get("entry_ids")
        working_title = raw.get("working_title")
        if not isinstance(raw_entry_ids, list) or not isinstance(working_title, str):
            log.warning("Storyline judge: dropping malformed chapter %r", raw)
            continue
        chapter_entry_ids: list[int] = []
        for raw_id in raw_entry_ids:
            if not isinstance(raw_id, int) or raw_id not in known_id_set:
                log.warning(
                    "Storyline judge: dropping unknown partition entry_id %r", raw_id,
                )
                continue
            if raw_id in seen_ids:
                log.warning(
                    "Storyline judge: entry_id %d appears in multiple chapters "
                    "— keeping first chapter's claim", raw_id,
                )
                continue
            chapter_entry_ids.append(raw_id)
            seen_ids.add(raw_id)
        chapters.append(
            PartitionChapter(entry_ids=chapter_entry_ids, working_title=working_title)
        )

    if not chapters:
        log.warning("Storyline judge: record_partition produced no usable chapters")
        return PartitionResult(chapters=[], model_used=model, failed=True)

    # Every candidate entry absent from the response is folded into the
    # final chapter — nothing is ever silently lost.
    missing = [entry_id for entry_id in known_ids if entry_id not in seen_ids]
    if missing:
        log.warning(
            "Storyline judge: entries %s missing from partition response — "
            "appending to final chapter", missing,
        )
        chapters[-1].entry_ids.extend(missing)

    return PartitionResult(chapters=chapters, model_used=model, failed=False)
