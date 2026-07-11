"""Tests for the contextvar-scoped LLM usage collector (W2).

Covers the collector's arithmetic, ``usage_scope`` isolation, the three
SDK normalizers on real-shaped and degenerate usage objects, and — the
crux — that a child thread spawned via ``copy_context().run`` records
into the parent scope's collector.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from journal.services import usage


class TestUsageCollector:
    def test_add_sums_within_a_model(self) -> None:
        collector = usage.UsageCollector()
        collector.add("m1", 100, 40)
        collector.add("m1", 5, 3)
        assert collector.per_model["m1"] == {
            "input_tokens": 105,
            "output_tokens": 43,
        }
        assert collector.totals == (105, 43)

    def test_totals_sum_across_models(self) -> None:
        collector = usage.UsageCollector()
        collector.add("m1", 100, 40)
        collector.add("m2", 10, 2)
        assert collector.totals == (110, 42)

    def test_empty_collector_totals_zero(self) -> None:
        assert usage.UsageCollector().totals == (0, 0)


class TestUsageScope:
    def test_record_no_op_without_scope(self) -> None:
        # Must not raise and must not accumulate anywhere observable.
        usage.record("m1", 100, 40)  # no active scope — silently ignored

    def test_record_lands_in_active_scope(self) -> None:
        with usage.usage_scope() as collector:
            usage.record("m1", 100, 40)
        assert collector.totals == (100, 40)

    def test_sequential_scopes_do_not_leak(self) -> None:
        with usage.usage_scope() as first:
            usage.record("m1", 100, 40)
        with usage.usage_scope() as second:
            usage.record("m1", 1, 2)
        assert first.totals == (100, 40)
        assert second.totals == (1, 2)

    def test_nested_scopes_isolate(self) -> None:
        with usage.usage_scope() as outer:
            usage.record("m1", 10, 5)
            with usage.usage_scope() as inner:
                usage.record("m1", 1, 1)
            usage.record("m1", 20, 5)
        assert inner.totals == (1, 1)
        assert outer.totals == (30, 10)

    def test_scope_resets_to_no_op_on_exit(self) -> None:
        with usage.usage_scope() as collector:
            usage.record("m1", 3, 3)
        # After exit, record is a no-op again — does not touch the collector.
        usage.record("m1", 99, 99)
        assert collector.totals == (3, 3)


class TestNormalizers:
    def test_record_anthropic_real_shape(self) -> None:
        message = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1200, output_tokens=340),
        )
        with usage.usage_scope() as collector:
            usage.record_anthropic("claude", message)
        assert collector.totals == (1200, 340)

    def test_record_anthropic_missing_usage(self) -> None:
        message = SimpleNamespace()  # no .usage
        with usage.usage_scope() as collector:
            usage.record_anthropic("claude", message)
        assert collector.totals == (0, 0)

    def test_record_anthropic_none_fields(self) -> None:
        message = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=None, output_tokens=None),
        )
        with usage.usage_scope() as collector:
            usage.record_anthropic("claude", message)
        assert collector.totals == (0, 0)

    def test_record_gemini_real_shape(self) -> None:
        response = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=900, candidates_token_count=120,
            ),
        )
        with usage.usage_scope() as collector:
            usage.record_gemini("gemini", response)
        assert collector.totals == (900, 120)

    def test_record_gemini_missing_metadata(self) -> None:
        response = SimpleNamespace()
        with usage.usage_scope() as collector:
            usage.record_gemini("gemini", response)
        assert collector.totals == (0, 0)

    def test_record_gemini_none_candidates(self) -> None:
        # candidates_token_count is None on some empty completions.
        response = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=50, candidates_token_count=None,
            ),
        )
        with usage.usage_scope() as collector:
            usage.record_gemini("gemini", response)
        assert collector.totals == (50, 0)

    def test_record_openai_real_shape(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=700, completion_tokens=90),
        )
        with usage.usage_scope() as collector:
            usage.record_openai("gpt", response)
        assert collector.totals == (700, 90)

    def test_record_openai_embeddings_no_completion(self) -> None:
        # Embeddings responses carry prompt_tokens but no completion_tokens.
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=64))
        with usage.usage_scope() as collector:
            usage.record_openai("embed", response)
        assert collector.totals == (64, 0)

    def test_record_openai_missing_usage(self) -> None:
        response = SimpleNamespace()
        with usage.usage_scope() as collector:
            usage.record_openai("gpt", response)
        assert collector.totals == (0, 0)


class TestCopyContextPropagation:
    def test_child_thread_records_into_parent_collector(self) -> None:
        """copy_context().run in a worker thread lands in the parent scope."""
        with (
            usage.usage_scope() as collector,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [
                pool.submit(
                    contextvars.copy_context().run,
                    usage.record,
                    "m1",
                    100,
                    40,
                ),
                pool.submit(
                    contextvars.copy_context().run,
                    usage.record,
                    "m2",
                    10,
                    2,
                ),
            ]
            for future in futures:
                future.result()
        assert collector.totals == (110, 42)

    def test_child_thread_without_copy_context_does_not_record(self) -> None:
        """A bare pool.submit does NOT see the scope — proves why the two
        fan-out sites must wrap with copy_context()."""
        with (
            usage.usage_scope() as collector,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pool.submit(usage.record, "m1", 100, 40).result()
        assert collector.totals == (0, 0)
