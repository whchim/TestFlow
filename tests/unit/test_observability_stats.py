"""最近秩分位数。"""

from __future__ import annotations

import pytest

from testflow.observability.stats import percentile


def test_empty_returns_none() -> None:
    assert percentile([], 0.95) is None


def test_single_value() -> None:
    assert percentile([7], 0.95) == 7


def test_p95_of_one_to_one_hundred() -> None:
    assert percentile(list(range(1, 101)), 0.95) == 95


def test_p50_uses_nearest_rank_not_interpolation() -> None:
    # ceil(0.5 × 4) = 2 → 第 2 个（1 起），不是 2.5。
    assert percentile([1, 2, 3, 4], 0.5) == 2


def test_boundaries_are_clamped() -> None:
    values = [10, 20, 30]
    assert percentile(values, 0.0) == 10
    assert percentile(values, 1.0) == 30


def test_out_of_range_p_is_rejected() -> None:
    with pytest.raises(ValueError):
        percentile([1, 2], 1.5)
