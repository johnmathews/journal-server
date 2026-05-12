"""Storylines feature service modules.

Top-level orchestrator lives in `service.py`; provider-side
narrative + glue calls live alongside the other providers
(``providers/storyline_narrator.py``, ``providers/storyline_glue.py``).
Segment helpers live in `segments.py`; extension classifier in
`extension.py`.

Design notes: ``docs/storylines-plan.md``.
"""
