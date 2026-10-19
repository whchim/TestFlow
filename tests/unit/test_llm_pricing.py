"""成本估算：分档、缓存折扣、未知模型。"""

from __future__ import annotations

from decimal import Decimal

from testflow.llm.pricing import (
    DEFAULT_PRICE_TABLE,
    PriceTier,
    estimate_cost,
    price_table_from_overrides,
)


def test_lowest_tier_uses_first_price() -> None:
    cost = estimate_cost("qwen3.7-flash", prompt_tokens=10_000, completion_tokens=1_000)
    # (10_000 × 0.2 + 1_000 × 0.8) / 1e6
    assert cost == Decimal("0.002800")


def test_cached_tokens_are_discounted_and_not_double_counted() -> None:
    cost = estimate_cost(
        "qwen3.7-flash",
        prompt_tokens=10_000,
        completion_tokens=1_000,
        cached_tokens=4_000,
    )
    # 未命中 6_000 × 0.2 + 命中 4_000 × 0.04 + 输出 1_000 × 0.8
    assert cost == Decimal("0.002160")


def test_cache_write_is_charged_at_125_percent_of_input() -> None:
    cost = estimate_cost(
        "qwen3.7-flash",
        prompt_tokens=10_000,
        completion_tokens=0,
        cache_write_tokens=1_000,
    )
    # 10_000 × 0.2 + 1_000 × 0.2 × 1.25
    assert cost == Decimal("0.002250")


def test_mid_tier_switches_the_whole_request() -> None:
    """跨档时**整单**按新价算，不是分段累加。"""
    cost = estimate_cost("qwen3.7-flash", prompt_tokens=100_000, completion_tokens=10_000)
    assert cost == Decimal("0.084000")


def test_high_tier() -> None:
    cost = estimate_cost("qwen3.7-flash", prompt_tokens=300_000, completion_tokens=1_000)
    assert cost == Decimal("0.364800")


def test_plus_model_has_its_own_tiers() -> None:
    low = estimate_cost("qwen3.5-plus", prompt_tokens=10_000, completion_tokens=1_000)
    high = estimate_cost("qwen3.5-plus", prompt_tokens=200_000, completion_tokens=1_000)

    assert low == Decimal("0.012800")
    assert high == Decimal("0.824000")


def test_unknown_model_returns_none_instead_of_guessing() -> None:
    assert estimate_cost("some-other-model", prompt_tokens=100, completion_tokens=10) is None


def test_overrides_replace_a_model_entirely() -> None:
    table = price_table_from_overrides(
        {
            "qwen3.7-flash": [
                {
                    "max_input_tokens": 1000,
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 2.0,
                    "cached_input_per_mtok": 0.5,
                }
            ]
        }
    )

    cost = estimate_cost(
        "qwen3.7-flash",
        prompt_tokens=1_000,
        completion_tokens=1_000,
        price_table=table,
    )
    assert cost == Decimal("0.003000")


def test_overrides_leave_other_models_untouched() -> None:
    table = price_table_from_overrides(
        {
            "new-model": [
                {
                    "max_input_tokens": 1000,
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 2.0,
                    "cached_input_per_mtok": 0.5,
                }
            ]
        }
    )
    assert "qwen3.7-flash" in table
    assert DEFAULT_PRICE_TABLE["qwen3.7-flash"][0] == PriceTier(
        max_input_tokens=32_000,
        input_per_mtok=Decimal("0.2"),
        output_per_mtok=Decimal("0.8"),
        cached_input_per_mtok=Decimal("0.04"),
    )


def test_cost_is_quantized_to_six_decimals() -> None:
    cost = estimate_cost("qwen3.7-flash", prompt_tokens=1, completion_tokens=1)
    assert cost is not None
    assert cost.as_tuple().exponent == -6
