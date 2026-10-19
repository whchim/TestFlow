"""报告里的 LLM 用量聚合：计数、token、缓存、成本、P95、采纳率。"""

from __future__ import annotations

import uuid

import pytest

from testflow.agents.report_agent import summarize_usage
from testflow.storage.models import LLMUsage


def _row(**overrides) -> LLMUsage:
    base = {
        "run_id": uuid.uuid4(),
        "purpose": "case_generation",
        "model": "qwen3.7-flash",
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 1_100,
        "attempt": 1,
        "elapsed_ms": 100,
        "trace_id": None,
    }
    base.update(overrides)
    return LLMUsage(**base)


def test_counts_are_split_by_purpose() -> None:
    summary = summarize_usage(
        [_row(), _row(), _row(purpose="attribution")],
        generated_cases=10,
        approved_cases=8,
    )

    assert summary.calls == 3
    assert summary.generation_calls == 2
    assert summary.attribution_calls == 1


def test_tokens_and_cache_hit_rate() -> None:
    summary = summarize_usage(
        [
            _row(prompt_tokens=1_000, completion_tokens=100, cached_tokens=200),
            _row(prompt_tokens=2_000, completion_tokens=200, cached_tokens=0),
        ],
        generated_cases=2,
        approved_cases=2,
    )

    assert summary.prompt_tokens == 3_000
    assert summary.completion_tokens == 300
    assert summary.cached_tokens == 200
    assert summary.cache_hit_rate == pytest.approx(200 / 3_000)


def test_cost_is_the_sum_of_per_call_costs() -> None:
    summary = summarize_usage(
        [
            _row(prompt_tokens=1_000, completion_tokens=100, cached_tokens=200),
            _row(prompt_tokens=2_000, completion_tokens=200, cached_tokens=0),
        ],
        generated_cases=2,
        approved_cases=2,
    )

    # 第一条：(800×0.2 + 200×0.04 + 100×0.8)/1e6 = 0.000248
    # 第二条：(2000×0.2 + 200×0.8)/1e6      = 0.000560
    assert summary.cost_cny == pytest.approx(0.000808)


def test_unpriced_model_makes_the_total_unknown_not_partial() -> None:
    """只算了一部分模型的金额看起来最权威，也最容易误导。"""
    summary = summarize_usage(
        [_row(), _row(model="mystery-model")],
        generated_cases=2,
        approved_cases=2,
    )

    assert summary.cost_cny is None
    assert summary.unpriced_models == ["mystery-model"]


def test_no_calls_means_zero_cost_not_unknown() -> None:
    summary = summarize_usage([], generated_cases=5, approved_cases=0)

    assert summary.calls == 0
    assert summary.cost_cny == 0.0
    assert summary.cache_hit_rate is None


def test_p95_and_missing_usage_are_visible() -> None:
    summary = summarize_usage(
        [
            _row(elapsed_ms=100),
            _row(elapsed_ms=200),
            _row(elapsed_ms=300),
            _row(elapsed_ms=400, total_tokens=0),
        ],
        generated_cases=4,
        approved_cases=4,
    )

    assert summary.p95_latency_ms == 400
    assert summary.usage_missing_calls == 1


def test_generation_acceptance_rate() -> None:
    summary = summarize_usage([_row()], generated_cases=4, approved_cases=3)
    assert summary.generation_acceptance_rate == pytest.approx(0.75)


def test_acceptance_is_none_when_nothing_was_generated() -> None:
    summary = summarize_usage([], generated_cases=0, approved_cases=0)
    assert summary.generation_acceptance_rate is None
