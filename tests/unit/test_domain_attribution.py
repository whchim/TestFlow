"""归因领域类型：封闭枚举、置信度封顶、补记超时的识别。"""

from __future__ import annotations

import pytest

from testflow.domain.attribution import (
    AttributionCategory,
    cap_confidence,
    is_synthesized_stall,
)


def test_category_values_match_the_database_check() -> None:
    """Python 侧的枚举必须与 `attributions.category` 的 CHECK 逐字一致。

    不一致不会报错，只会让"某条归因写不进去"——且只在跑到那一步时才暴露。
    """
    assert {category.value for category in AttributionCategory} == {
        "request_invalid",
        "auth_error",
        "resource_not_found",
        "conflict",
        "rate_limited",
        "server_error",
        "response_schema_mismatch",
        "assertion_mismatch",
        "network_error",
        "blocked_by_policy",
        "unknown",
    }


# ─────────────────────────────────────────────── 补记超时


@pytest.mark.parametrize(
    ("status", "error_kind", "expected"),
    [
        ("timeout", "executor_stall", True),
        ("timeout", "timeout", False),
        ("timeout", None, False),
        ("failed", "executor_stall", False),
        ("passed", None, False),
    ],
)
def test_is_synthesized_stall(status: str, error_kind: str | None, expected: bool) -> None:
    assert is_synthesized_stall(status, error_kind) is expected


# ─────────────────────────────────────────────── 置信度封顶（LA3）


def test_assertion_mismatch_is_capped_at_medium_for_high_confidence_case() -> None:
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.ASSERTION_MISMATCH,
            case_confidence="high",
        )
        == "medium"
    )


def test_assertion_mismatch_is_capped_at_low_for_low_confidence_case() -> None:
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.ASSERTION_MISMATCH,
            case_confidence="low",
        )
        == "low"
    )


def test_missing_case_confidence_is_treated_as_low_not_unlimited() -> None:
    """缺值不能当成"不设限"——那恰好会放过最不可靠的用例。

    这条曾经是 confidence 功能整体失效的同一个问题：`None` 被当成
    "没有证据表明不可靠"，于是上限从未生效。
    """
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.ASSERTION_MISMATCH,
            case_confidence=None,
        )
        == "low"
    )


def test_unrecognized_case_confidence_is_also_treated_as_low() -> None:
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.ASSERTION_MISMATCH,
            case_confidence="certain",
        )
        == "low"
    )


def test_other_categories_are_not_capped_by_case_confidence() -> None:
    """服务端 5xx 与用例写得准不准无关，不该被用例置信度拖累。"""
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.SERVER_ERROR,
            case_confidence="low",
        )
        == "high"
    )


def test_unknown_is_always_low() -> None:
    """`unknown` 是缺陷信号，不是可用的归因结论（LA §3.4）。"""
    assert (
        cap_confidence(
            "high",
            category=AttributionCategory.UNKNOWN,
            case_confidence="high",
        )
        == "low"
    )


def test_capping_never_raises_a_confidence() -> None:
    """封顶只能是"往下压"，不能把 medium 抬成 high。"""
    assert (
        cap_confidence(
            "medium",
            category=AttributionCategory.ASSERTION_MISMATCH,
            case_confidence="high",
        )
        == "medium"
    )
