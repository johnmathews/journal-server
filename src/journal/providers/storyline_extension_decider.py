"""Haiku-based decider for the storyline extension classifier.

Given (storyline_name, storyline_description, entry_text), returns
a structured ``ExtensionDecision`` with a yes/no/maybe verdict and a
short LLM-supplied reasoning string. The decider is stage 3 of the
hybrid classifier (after deterministic entity-overlap and
case-insensitive surface-form prefilters); the spike's expected
call volume is 1-2 Haiku invocations per ingestion, so cost is in
the fractions-of-a-cent range.

The reasoning string is captured and surfaced by future UI ("why
is this entry attached?"). Even if the LLM occasionally
misclassifies, the rationale is debuggable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a triage helper for a personal journal. The user has a "storyline" —
an evolving narrative thread about a specific subject in their life (e.g.
"running", "Atlas-my-son", "the house renovation"). You are given the
storyline's name and description, and a single new journal entry. Decide
whether this new entry meaningfully extends the storyline.

Answer with one of:

* "yes" — the entry clearly continues, advances, or relates to the storyline.
  Picking yes means the storyline's narrative will be regenerated to
  incorporate this entry.
* "no" — the entry does not relate to the storyline, or only mentions the
  subject in passing without substantive new material.
* "maybe" — the entry is on the borderline; surface this for the user to
  decide manually. Use sparingly.

Also produce a one-sentence "reasoning" explaining your call. Reasoning is
shown to the user, so keep it readable. Do not invent details.

You must call the `record_decision` tool with your verdict.
"""


_RECORD_DECISION_TOOL: dict[str, Any] = {
    "name": "record_decision",
    "description": (
        "Record the extension classification verdict for this entry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["yes", "no", "maybe"],
                "description": "Verdict: does this entry extend the storyline?",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One short sentence explaining the verdict. Shown to user."
                ),
            },
        },
        "required": ["decision", "reasoning"],
    },
}


@dataclass
class ExtensionDecision:
    decision: Literal["yes", "no", "maybe"]
    reasoning: str
    model_used: str = ""


@runtime_checkable
class StorylineExtensionDeciderProtocol(Protocol):
    def decide(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        entry_date: str,
        entry_text: str,
    ) -> ExtensionDecision: ...


class AnthropicStorylineExtensionDecider:
    """Haiku decider for whether an entry extends a storyline."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 256,
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

    def decide(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        entry_date: str,
        entry_text: str,
    ) -> ExtensionDecision:
        user_text = (
            f"Storyline: {storyline_name}\n"
            f"Description: {storyline_description.strip() or '(none provided)'}\n\n"
            f"New entry — date: {entry_date}\n"
            f"{entry_text}\n\n"
            "Call the record_decision tool with your verdict."
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
                tools=[_RECORD_DECISION_TOOL],
                tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception:  # noqa: BLE001 — provider failures surface as maybe
            log.exception("Extension decider API call failed")
            return ExtensionDecision(
                decision="maybe",
                reasoning="LLM decider unavailable; surfacing for manual review.",
                model_used=self._model,
            )

        return _parse_decision(response, model=self._model)


def _parse_decision(response: Any, *, model: str) -> ExtensionDecision:  # noqa: ANN401
    content = getattr(response, "content", None) or []
    if not content and isinstance(response, dict):
        content = response.get("content", [])
    for block in content:
        block_type = (
            block.get("type") if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type != "tool_use":
            continue
        tool_input = (
            block.get("input") if isinstance(block, dict)
            else getattr(block, "input", None)
        )
        if not isinstance(tool_input, dict):
            continue
        decision_raw = tool_input.get("decision")
        reasoning = tool_input.get("reasoning", "")
        if decision_raw not in {"yes", "no", "maybe"}:
            continue
        return ExtensionDecision(
            decision=decision_raw,  # type: ignore[arg-type]
            reasoning=str(reasoning),
            model_used=model,
        )
    log.warning("Extension decider response had no usable tool call")
    return ExtensionDecision(
        decision="maybe",
        reasoning="Decider response was malformed; surfacing for manual review.",
        model_used=model,
    )
