"""三层 Prompt：稳定前缀不变、半稳定层随 Spec、反馈只在易变层。"""

from __future__ import annotations

import json

from testflow.agents.prompts import (
    ATTRIBUTION_SYSTEM_PREFIX,
    SYSTEM_PREFIX,
    build_attribution_prompt,
    build_generation_prompt,
)
from testflow.agents.spec_context import build_spec_context
from testflow.domain.attribution import FailureContext
from testflow.domain.cases import DimensionSpec


def _dimensions() -> list[DimensionSpec]:
    return [
        DimensionSpec(
            dimension="value_valid",
            category="normal",
            expected_status=[200],
            rationale="正常路径",
        )
    ]


def _operation(path: str) -> dict:
    return {
        "method": "GET",
        "path": path,
        "responses_schema": {"200": {"description": "ok"}},
    }


def _context(scheme: str) -> object:
    return build_spec_context(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "t", "version": "1"},
                "paths": {},
                "components": {"securitySchemes": {scheme: {"type": "http", "scheme": "bearer"}}},
            }
        )
    )


def test_stable_prefix_is_byte_identical_across_operations() -> None:
    first = build_generation_prompt(
        operation=_operation("/a"), dimensions=_dimensions(), spec_context=_context("a")
    )
    second = build_generation_prompt(
        operation=_operation("/b"), dimensions=_dimensions(), spec_context=_context("a")
    )

    assert first.startswith(SYSTEM_PREFIX)
    assert second.startswith(SYSTEM_PREFIX)
    assert first[: len(SYSTEM_PREFIX)] == second[: len(SYSTEM_PREFIX)]


def test_semi_stable_layer_differs_between_specs() -> None:
    first = build_generation_prompt(
        operation=_operation("/a"), dimensions=_dimensions(), spec_context=_context("alpha")
    )
    second = build_generation_prompt(
        operation=_operation("/a"), dimensions=_dimensions(), spec_context=_context("beta")
    )

    assert first != second
    assert "alpha" in first
    assert "beta" in second


def test_feedback_is_appended_to_the_volatile_layer_only() -> None:
    """反馈进稳定前缀的话，**每一次重试都会让缓存失效**。"""
    plain = build_generation_prompt(
        operation=_operation("/a"), dimensions=_dimensions(), spec_context=_context("a")
    )
    with_feedback = build_generation_prompt(
        operation=_operation("/a"),
        dimensions=_dimensions(),
        spec_context=_context("a"),
        feedback="缺少维度 boundary:age",
    )

    assert with_feedback.startswith(plain[: len(plain)])
    stable_len = len(SYSTEM_PREFIX)
    assert "boundary:age" not in with_feedback[:stable_len]
    assert with_feedback.endswith("缺少维度 boundary:age")


def test_operation_payload_is_still_deterministic() -> None:
    """易变层的 dict 排序不能丢——否则同一接口两次调用提示词不同。"""
    a = build_generation_prompt(operation=_operation("/a"), dimensions=_dimensions())
    b = build_generation_prompt(operation=_operation("/a"), dimensions=_dimensions())
    assert a == b


def test_attribution_prompt_has_a_stable_prefix() -> None:
    ctx = FailureContext(
        status="failed",
        http_status=500,
        error_kind=None,
        response_body="{}",
        truncated=False,
        assertion_outcomes=(),
        case_confidence="high",
        responses_schema=None,
    )
    prompt = build_attribution_prompt(ctx)
    assert prompt.startswith(ATTRIBUTION_SYSTEM_PREFIX)
    assert "HTTP_STATUS: 500" in prompt
    assert "TRUNCATED: false" in prompt
