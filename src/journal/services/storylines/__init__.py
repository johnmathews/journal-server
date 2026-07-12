"""Storylines feature service modules.

Top-level orchestrator is :class:`~journal.services.storylines.engine.
StorylineEngine` (``engine.py``) — the judge-driven continue-or-break
update/bootstrap/refresh flows. Provider-side narrative + judge calls
live alongside the other providers (``providers/storyline_narrator.py``,
``providers/storyline_judge.py``). Segment helpers live in
`segments.py`; extension classifier in `extension.py` (still on the
round-1 repository surface — Task 7 updates it).

Design notes: ``docs/superpowers/specs/2026-07-12-storylines-redesign-
design.md``.
"""
