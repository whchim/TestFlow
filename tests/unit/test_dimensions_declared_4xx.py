"""阶段 A：为 Spec 声明的 4xx 派生 `status:<code>` 维度。"""

from __future__ import annotations

from testflow.agents.dimensions import (
    declared_client_error_codes,
    derive_dimensions,
)


def _dims(responses: dict | None, *, path: str = "/plain") -> list[str]:
    return [
        spec.dimension
        for spec in derive_dimensions(
            operation_path=path,
            responses_schema=responses,
        )
    ]


def test_each_uncovered_declared_4xx_gets_a_status_dimension() -> None:
    dims = _dims({"200": {}, "409": {}, "429": {}})

    assert "status:409" in dims
    assert "status:429" in dims


def test_declared_404_without_path_params_gets_a_dimension() -> None:
    assert "status:404" in _dims({"200": {}, "404": {}})


def test_declared_404_with_path_params_is_not_duplicated() -> None:
    """参数化路径上已经有 `not_found`，再加 `status:404` 就是两条一样的用例。"""
    dims = _dims({"200": {}, "404": {}}, path="/users/{id}")

    assert "not_found" in dims
    assert "status:404" not in dims


def test_status_dimensions_are_numeric_and_ordered() -> None:
    dims = _dims({"200": {}, "429": {}, "400": {}, "409": {}})
    statuses = [name for name in dims if name.startswith("status:")]

    assert statuses == ["status:400", "status:409", "status:429"]


def test_only_exact_three_digit_4xx_keys_count() -> None:
    assert declared_client_error_codes(
        {"200": {}, "4XX": {}, "default": {}, "409": {}, "499": {}, "abc": {}}
    ) == [409, 499]


def test_no_response_schema_means_no_status_dimensions() -> None:
    assert _dims(None) == ["value_valid"]


def test_undeclared_codes_are_not_derived() -> None:
    dims = _dims({"200": {}})
    assert not any(name.startswith("status:") for name in dims)


def test_declared_400_from_body_constraints_is_not_duplicated() -> None:
    """请求体约束维度已经用 400 作为期望状态码时，不再补一条重复的。"""
    dims = [
        spec.dimension
        for spec in derive_dimensions(
            operation_path="/users",
            request_body_schema={
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {"name": {"type": "string"}},
                        }
                    }
                }
            },
            responses_schema={"200": {}, "400": {}},
        )
    ]

    assert any(name.startswith("required_missing:") for name in dims)
    assert "status:400" not in dims
