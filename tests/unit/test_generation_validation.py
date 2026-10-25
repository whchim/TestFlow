"""L1 / L2 校验：每条规则一例，外加反馈文本。"""

from __future__ import annotations

from typing import Any

from testflow.agents.validation import (
    IssueLayer,
    feedback_text,
    validate_generation,
)
from testflow.domain.cases import DimensionSpec, GeneratedCase, GenerationResult


def _dim(name: str, *, status: int = 200) -> DimensionSpec:
    return DimensionSpec(dimension=name, category="normal", expected_status=[status], rationale="r")


def _case(
    dim: str,
    *,
    derived_from: str = "spec",
    assertions: list[dict[str, Any]] | None = None,
    path_params: dict[str, str] | None = None,
) -> GeneratedCase:
    return GeneratedCase(
        dimension=dim,
        name=dim,
        category="normal",
        path_params=path_params,
        assertions=assertions
        if assertions is not None
        else [{"type": "status_code", "expected": 200, "derived_from": "spec"}],
        derived_from=derived_from,
    )


def _validate(cases, *, requested, responses=None, operation_path="", request_body=None):
    return validate_generation(
        responses_schema=responses or {"200": {}},
        requested=requested,
        result=GenerationResult(cases=cases),
        operation_path=operation_path,
        request_body_schema=request_body,
    )


def _body(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": {"application/json": {"schema": {"type": "object", "properties": properties}}}
    }


def _codes(result) -> list[str]:
    return [issue.code for issue in result.issues]


# ─────────────────────────────────────────────── L1


def test_empty_cases_is_an_l1_issue() -> None:
    result = _validate([], requested=[_dim("value_valid")])
    assert "l1.cases_empty" in _codes(result)


def test_missing_dimension_is_reported() -> None:
    result = _validate([_case("value_valid")], requested=[_dim("value_valid"), _dim("b")])
    assert "l1.dimension_missing" in _codes(result)


def test_duplicate_dimension_is_reported() -> None:
    result = _validate(
        [_case("value_valid"), _case("value_valid")],
        requested=[_dim("value_valid")],
    )
    assert "l1.dimension_duplicate" in _codes(result)


def test_missing_path_parameter_is_reported() -> None:
    result = _validate(
        [_case("value_valid")],
        requested=[_dim("value_valid")],
        operation_path="/items/{item_id}",
    )

    assert "l1.path_param_missing" in _codes(result)


def test_provided_path_parameter_is_accepted() -> None:
    result = _validate(
        [_case("value_valid", path_params={"item_id": "item-42"})],
        requested=[_dim("value_valid")],
        operation_path="/items/{item_id}",
    )

    assert "l1.path_param_missing" not in _codes(result)


def test_schema_valid_without_a_response_schema_is_reported() -> None:
    result = _validate(
        [
            _case(
                "value_valid",
                assertions=[{"type": "schema_valid", "schema_ref": "201", "derived_from": "spec"}],
            )
        ],
        requested=[_dim("value_valid", status=201)],
        responses={"201": {"description": "创建成功"}},
    )

    assert "l1.schema_valid_without_schema" in _codes(result)


def test_schema_valid_with_a_declared_schema_is_accepted() -> None:
    result = _validate(
        [
            _case(
                "value_valid",
                assertions=[{"type": "schema_valid", "schema_ref": "201", "derived_from": "spec"}],
            )
        ],
        requested=[_dim("value_valid", status=201)],
        responses={
            "201": {
                "description": "创建成功",
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        },
    )

    assert "l1.schema_valid_without_schema" not in _codes(result)


def _boundary_case(dimension: str, field: str, value: Any) -> GeneratedCase:
    return GeneratedCase(
        dimension=dimension,
        name=dimension,
        category="boundary",
        request_body={field: value},
        assertions=[{"type": "status_code", "expected": 201, "derived_from": "spec"}],
        derived_from="spec",
    )


def test_len_max_must_match_max_length_exactly() -> None:
    schema = _body({"bio": {"type": "string", "minLength": 1, "maxLength": 280}})
    dim = _dim("boundary:bio:len_max", status=201)

    bad = _validate(
        [_boundary_case("boundary:bio:len_max", "bio", "a" * 254)],
        requested=[dim],
        request_body=schema,
    )
    good = _validate(
        [_boundary_case("boundary:bio:len_max", "bio", "a" * 280)],
        requested=[dim],
        request_body=schema,
    )

    assert "l1.boundary_value_mismatch" in _codes(bad)
    assert "l1.boundary_value_mismatch" not in _codes(good)


def test_max_over_must_respect_multiple_of() -> None:
    schema = _body({"quantity": {"type": "integer", "maximum": 100, "multipleOf": 5}})
    dim = _dim("boundary:quantity:max_over", status=400)

    bad = _validate(
        [_boundary_case("boundary:quantity:max_over", "quantity", 101)],
        requested=[dim],
        request_body=schema,
    )
    good = _validate(
        [_boundary_case("boundary:quantity:max_over", "quantity", 105)],
        requested=[dim],
        request_body=schema,
    )

    assert "l1.boundary_value_mismatch" in _codes(bad)
    assert "l1.boundary_value_mismatch" not in _codes(good)


def test_non_boundary_dimensions_are_ignored_by_the_boundary_check() -> None:
    schema = _body({"bio": {"type": "string", "maxLength": 280}})
    result = _validate(
        [_case("value_valid")],
        requested=[_dim("value_valid")],
        request_body=schema,
    )

    assert "l1.boundary_value_mismatch" not in _codes(result)


def test_extra_inferred_dimension_within_limit_is_allowed() -> None:
    result = _validate(
        [_case("a"), _case("b"), _case("extra", derived_from="inferred")],
        requested=[_dim("a"), _dim("b")],
    )
    assert result.ok


def test_extra_inferred_dimension_over_limit_is_rejected() -> None:
    """1 个请求维度最多带 0 个额外维度（严格不超过一半）。"""
    result = _validate(
        [_case("a"), _case("extra", derived_from="inferred")],
        requested=[_dim("a")],
    )
    assert "l1.too_many_extra_dimensions" in _codes(result)


def test_extra_dimension_must_be_inferred() -> None:
    result = _validate(
        [_case("a"), _case("b")],
        requested=[_dim("a")],
    )
    assert "l1.dimension_not_requested" in _codes(result)


def test_invalid_jsonpath_is_reported() -> None:
    result = _validate(
        [
            _case(
                "a",
                assertions=[
                    {
                        "type": "json_path",
                        "path": "$.[",
                        "operator": "exists",
                        "derived_from": "spec",
                    }
                ],
            )
        ],
        requested=[_dim("a")],
    )
    assert "l1.invalid_jsonpath" in _codes(result)


def test_valid_jsonpath_is_accepted() -> None:
    result = _validate(
        [
            _case(
                "a",
                assertions=[
                    {
                        "type": "json_path",
                        "path": "$.data.items[0].id",
                        "operator": "exists",
                        "derived_from": "spec",
                    }
                ],
            )
        ],
        requested=[_dim("a")],
    )
    assert result.ok


# ─────────────────────────────────────────────── L2


def test_uncovered_dimension_is_reported_at_l2() -> None:
    result = _validate([_case("a")], requested=[_dim("a"), _dim("b")])
    assert "l2.dimension_uncovered" in _codes(result)
    assert any(issue.layer is IssueLayer.L2 for issue in result.issues)


def test_declared_4xx_must_have_a_case_expecting_it() -> None:
    result = _validate(
        [_case("a")],
        requested=[_dim("a")],
        responses={"200": {}, "409": {}},
    )
    assert "l2.declared_4xx_uncovered" in _codes(result)


def test_status_code_list_satisfies_coverage() -> None:
    result = _validate(
        [
            _case(
                "a",
                assertions=[
                    {"type": "status_code", "expected": [200, 409], "derived_from": "spec"}
                ],
            )
        ],
        requested=[_dim("a")],
        responses={"200": {}, "409": {}},
    )
    assert result.ok


def test_4xx_placeholder_keys_are_not_treated_as_specific_codes() -> None:
    result = _validate(
        [_case("a")],
        requested=[_dim("a")],
        responses={"200": {}, "4XX": {}, "default": {}},
    )
    assert result.ok


# ─────────────────────────────────────────────── 反馈


def test_feedback_names_the_missing_dimension() -> None:
    result = _validate([_case("a")], requested=[_dim("a"), _dim("boundary:age")])
    text = feedback_text(result)
    assert "boundary:age" in text
    assert "[L1]" in text or "[L2]" in text


def test_feedback_is_bounded() -> None:
    requested = [_dim(f"d{i}") for i in range(20)]
    result = _validate([], requested=requested)
    text = feedback_text(result)
    assert text.count("- [") <= 8
    assert "未列出" in text


def test_ok_result_has_empty_feedback() -> None:
    result = _validate([_case("a")], requested=[_dim("a")])
    assert feedback_text(result) == ""
