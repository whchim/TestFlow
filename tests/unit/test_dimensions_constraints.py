"""`constraint_violation` 的四种约束来源。

对应决策：`03-模块详细设计/TestCaseGen.md` §2.3 的维度表——
`enum` / `pattern` / `format` / `multipleOf` 都归在同一个维度名下。

## 为什么这组测试在 M5 才有

第一版只认 `enum`。带 `pattern` 的字段一个维度都派生不出来，而生产覆盖率
仍然显示 100%——因为分母里根本没有它（`09` §5.2 的自我参照陷阱）。
Golden Set 的第 3 号接口把这件事暴露出来之后，这里补上回归测试。

`format` 还带一层降级：标准格式（`uuid`）语义确定，自定义格式（`imei`）
的"什么值算违法"只能由模型猜，因此该用例**标低置信度**（§7 触发 ④）。
"""

from __future__ import annotations

from testflow.agents.dimensions import derive_dimensions
from testflow.agents.test_case_gen import _classify_confidence
from testflow.domain.assertions import StatusCodeAssertion
from testflow.domain.cases import GeneratedCase
from testflow.storage.models import Operation

_RESPONSES = {"201": {"description": "ok"}}


def _dims(properties: dict) -> list[str]:
    return [
        spec.dimension
        for spec in derive_dimensions(
            operation_path="/things",
            request_body_schema={
                "content": {
                    "application/json": {"schema": {"type": "object", "properties": properties}}
                }
            },
            responses_schema=_RESPONSES,
        )
    ]


def test_pattern_derives_a_constraint_dimension() -> None:
    assert "constraint_violation:code" in _dims(
        {"code": {"type": "string", "pattern": "^[A-Z]{3}$"}}
    )


def test_multiple_of_derives_a_constraint_dimension() -> None:
    assert "constraint_violation:quantity" in _dims(
        {"quantity": {"type": "integer", "multipleOf": 5}}
    )


def test_standard_format_derives_a_constraint_dimension() -> None:
    assert "constraint_violation:user_id" in _dims(
        {"user_id": {"type": "string", "format": "uuid"}}
    )


def test_custom_format_still_derives_a_constraint_dimension() -> None:
    """语义未知不等于"不测"：应当拒绝不符合格式的值，这件事是确定的。"""
    assert "constraint_violation:serial" in _dims({"serial": {"type": "string", "format": "imei"}})


def test_enum_and_pattern_on_one_field_are_one_dimension() -> None:
    """两条约束指向同一个维度——派生两次会让 L1 报"重复维度"。"""
    dims = _dims({"role": {"type": "string", "enum": ["admin"], "pattern": "^a"}})

    assert dims.count("constraint_violation:role") == 1


def test_unknown_format_does_not_invent_extra_dimensions() -> None:
    assert _dims({"serial": {"type": "string", "format": "imei"}}) == [
        "value_valid",
        "constraint_violation:serial",
    ]


# ------------------------------------------------------- 低置信度（§7 触发 ④）


def _operation(format_name: str) -> Operation:
    return Operation(
        method="POST",
        path="/devices",
        request_body_schema={
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"serial": {"type": "string", "format": format_name}},
                    }
                }
            }
        },
        responses_schema={
            "201": {
                "description": "ok",
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        },
        has_unresolved_ref=False,
    )


def _case(dimension: str) -> GeneratedCase:
    return GeneratedCase(
        dimension=dimension,
        name="POST /devices",
        category="abnormal",
        assertions=[
            StatusCodeAssertion(type="status_code", expected=400, derived_from="spec"),
        ],
        derived_from="spec",
    )


def test_custom_format_constraint_case_is_low_confidence() -> None:
    case = _case("constraint_violation:serial")

    assert _classify_confidence(_operation("imei"), case) == "low"


def test_standard_format_constraint_case_is_high_confidence() -> None:
    case = _case("constraint_violation:serial")

    assert _classify_confidence(_operation("uuid"), case) == "high"
