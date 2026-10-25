"""边界取值的确定性修正。

M5 人工标注发现：模型会把 `maxLength=280` 写成 252/254 个字符，
把 `max=100` + `multipleOf=5` 的越界值写成 101。边界值是数学上唯一的，
所以在生成侧直接改对，而不是让整条用例失败或带着错误取值进入执行。
"""

from __future__ import annotations

from testflow.agents.llm_case_gen import repair_boundary_values
from testflow.domain.assertions import StatusCodeAssertion
from testflow.domain.cases import GeneratedCase, GenerationResult


def _body(properties: dict) -> dict:
    return {
        "content": {"application/json": {"schema": {"type": "object", "properties": properties}}}
    }


def _case(dimension: str, body: dict | None) -> GeneratedCase:
    return GeneratedCase(
        dimension=dimension,
        name=dimension,
        category="boundary",
        request_body=body,
        assertions=[StatusCodeAssertion(type="status_code", expected=201, derived_from="spec")],
        derived_from="spec",
    )


def _repair(case: GeneratedCase, schema: dict) -> GeneratedCase:
    return repair_boundary_values(GenerationResult(cases=[case]), schema).cases[0]


_PROFILE = _body({"bio": {"type": "string", "minLength": 1, "maxLength": 280}})
_PRODUCT = _body({"quantity": {"type": "integer", "minimum": 1, "maximum": 100, "multipleOf": 5}})


def test_len_max_is_padded_or_truncated_to_max_length() -> None:
    fixed = _repair(_case("boundary:bio:len_max", {"bio": "a" * 254}), _PROFILE)

    assert len(fixed.request_body["bio"]) == 280


def test_len_max_over_is_exactly_one_over() -> None:
    fixed = _repair(_case("boundary:bio:len_max_over", {"bio": "a" * 255}), _PROFILE)

    assert len(fixed.request_body["bio"]) == 281


def test_len_min_over_can_be_empty() -> None:
    fixed = _repair(_case("boundary:bio:len_min_over", {"bio": "abc"}), _PROFILE)

    assert fixed.request_body["bio"] == ""


def test_max_over_respects_multiple_of() -> None:
    fixed = _repair(
        _case("boundary:quantity:max_over", {"quantity": 101}),
        _PRODUCT,
    )

    assert fixed.request_body["quantity"] == 105


def test_correct_values_are_left_alone() -> None:
    fixed = _repair(_case("boundary:quantity:max", {"quantity": 100}), _PRODUCT)

    assert fixed.request_body["quantity"] == 100


def test_missing_field_is_added() -> None:
    fixed = _repair(_case("boundary:quantity:max", {"price": 10}), _PRODUCT)

    assert fixed.request_body == {"price": 10, "quantity": 100}


def test_non_boundary_dimensions_are_not_touched() -> None:
    fixed = _repair(_case("value_valid", {"quantity": 3}), _PRODUCT)

    assert fixed.request_body == {"quantity": 3}
