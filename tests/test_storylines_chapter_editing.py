"""Tests for the _enqueue_chapter_regens module-level helper (Task 7).

This helper queues one storyline generation job per affected chapter and
returns the list of job ids.  Closed chapters regenerate with
``mode="replace"``; the open chapter omits ``mode`` so the worker's
default applies.  Per-chapter ``ValueError``/``RuntimeError`` from the
runner are swallowed (logged + continue).
"""

from __future__ import annotations

from journal.api.storylines_write import _enqueue_chapter_regens


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict]] = []

    def submit_storyline_generation(self, sid: int, **kw: object) -> object:
        self.calls.append((sid, kw))
        return type("Job", (), {"id": f"job-{len(self.calls)}", "status": "queued"})()


class _Ch:
    def __init__(self, cid: int, state: str) -> None:
        self.id, self.state = cid, state


def test_enqueue_chapter_regens_one_job_per_chapter() -> None:
    runner = _FakeRunner()
    jobs = _enqueue_chapter_regens(
        runner,
        storyline_id=7,
        user_id=1,
        chapters=[_Ch(10, "closed"), _Ch(11, "open")],
    )
    assert len(jobs) == 2
    assert runner.calls[0] == (7, {"user_id": 1, "chapter_id": 10, "mode": "replace"})
    assert runner.calls[1] == (7, {"user_id": 1, "chapter_id": 11})


def test_enqueue_chapter_regens_swallows_runner_errors() -> None:
    class _BadRunner:
        def submit_storyline_generation(self, sid: int, **kw: object) -> object:
            raise RuntimeError("queue full")

    jobs = _enqueue_chapter_regens(_BadRunner(), 7, 1, [_Ch(10, "closed")])
    assert jobs == []
