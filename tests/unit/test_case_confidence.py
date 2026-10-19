"""§7 低置信度标注的单元测试。

对应决策：`03-模块详细设计/TestCaseGen.md` §7——低置信度由具体原因构成，
而非一个模糊的分数。验证 `_classify_confidence` 的触发条件与 `_resolve_confidence`
的保守聚合（任一来源判 `low` 即 `low`）。
"""

from __future__ import annotations

import pytest

from testflow.agents.test_case_gen import (
    _classify_confidence,
    _has_response_body_schema,
    _resolve_confidence,
)
from testflow.domain.assertions import JsonPathAssertion, StatusCodeAssertion
from testflow.domain.cases import GeneratedCase
from testflow.storage.models import Operation

_BODY_SCHEMA = {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}


def _operation(*, has_unresolved_ref: bool = False, responses_schema=None) -> Operation:
    return Operation(
        method="GET",
        path="/ok",
        has_unresolved_ref=has_unresolved_ref,
        responses_schema=responses_schema,
    )


def _status_assertion(derived_from: str = "spec") -> StatusCodeAssertion:
    return StatusCodeAssertion(type="status_code", expected=200, derived_from=derived_from)


def _case(*, derived_from: str = "spec", assertions=None) -> GeneratedCase:
    return GeneratedCase(
        dimension="value_valid",
        name="GET /ok 正常请求",
        category="normal",
        assertions=assertions or [_status_assertion()],
        derived_from=derived_from,
    )


# 触发 ①：断言 derived_from == "inferred"（期望状态码靠推断）
def test_inferred_assertion_is_low() -> None:
    op = _operation(responses_schema=None)
    case = _case(assertions=[_status_assertion(derived_from="inferred")])
    assert _classify_confidence(op, case) == "low"


# 触发 ⑥：用例本身标记 inferred
def test_inferred_case_is_low() -> None:
    op = _operation(responses_schema=_BODY_SCHEMA)
    case = _case(derived_from="inferred", assertions=[_status_assertion("spec")])
    assert _classify_confidence(op, case) == "low"


# 触发 ⑤：响应体无 schema + 内容断言缺失（只剩状态码）
def test_status_only_and_no_body_schema_is_low() -> None:
    op = _operation(responses_schema=None)
    case = _case(assertions=[_status_assertion("spec")])
    assert _classify_confidence(op, case) == "low"


# has_unresolved_ref 是更强的信号：即使断言全 spec、有 body schema，也 low
def test_unresolved_ref_is_low() -> None:
    op = _operation(has_unresolved_ref=True, responses_schema=_BODY_SCHEMA)
    case = _case(assertions=[_status_assertion("spec")])
    assert _classify_confidence(op, case) == "low"


# 高置信度：spec 断言 + 有内容断言 + 有 body schema
def test_spec_assertions_with_content_and_schema_is_high() -> None:
    op = _operation(responses_schema=_BODY_SCHEMA)
    case = _case(
        assertions=[
            _status_assertion("spec"),
            JsonPathAssertion(
                type="json_path", path="$.id", operator="exists", derived_from="spec"
            ),
        ]
    )
    assert _classify_confidence(op, case) == "high"


# 聚合：生成方自报 low → 落库 low（哪怕分类器判 high）
def test_resolve_generator_low_wins() -> None:
    op = _operation(responses_schema=_BODY_SCHEMA)
    case = _case(assertions=[_status_assertion("spec")])
    case = case.model_copy(update={"confidence": "low"})
    assert _resolve_confidence(op, case) == "low"


# 聚合：生成方自报 high 但分类器判 low → 仍 low（绝不因自报放行）
def test_resolve_classifier_low_beats_generator_high() -> None:
    op = _operation(responses_schema=None)
    case = _case(assertions=[_status_assertion("spec")])
    case = case.model_copy(update={"confidence": "high"})
    assert _resolve_confidence(op, case) == "low"


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (None, False),
        ({}, False),
        ({"200": {"description": "ok"}}, False),
        (_BODY_SCHEMA, True),
    ],
)
def test_has_response_body_schema(schema, expected: bool) -> None:
    assert _has_response_body_schema(schema) is expected
