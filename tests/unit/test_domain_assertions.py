"""断言判别联合的单元测试。

对应决策：`03-模块详细设计/TestCaseGen.md` §3（A1~A5）。

**这组测试挡的是什么**

A2~A4 这三条约束的共同点是：**违反它们的输出在结构上完全合法**。
`{"type": "json_path", "operator": "eq"}` 是一个合法 JSON 对象；
LLM 写出 `status_code: 999` 也通得过任何"是不是整数"的检查。
只有把约束写成模型校验，它们才会在**生成时**失败，而不是在**执行时**。

第 ③ 段（`error` 与 `failed` 的区分）是 TE2 的落点——它现在只是类型，
真正的求值逻辑在 `execution/assertions.py`。
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from testflow.domain.assertions import (
    Assertion,
    AssertionOutcome,
    AssertionResult,
    JsonPathAssertion,
    StatusCodeAssertion,
)

_adapter = TypeAdapter(list[Assertion])


class TestDiscriminatedUnion:
    def test_each_type_is_selected_by_its_discriminator(self) -> None:
        parsed = _adapter.validate_python(
            [
                {"type": "status_code", "expected": 200, "derived_from": "spec"},
                {
                    "type": "json_path",
                    "path": "$.data.id",
                    "operator": "exists",
                    "derived_from": "spec",
                },
                {"type": "body_contains", "text": "502", "derived_from": "inferred"},
                {
                    "type": "header",
                    "name": "content-type",
                    "operator": "contains",
                    "expected": "json",
                    "derived_from": "spec",
                },
                {"type": "schema_valid", "schema_ref": "200", "derived_from": "spec"},
                {
                    "type": "response_time",
                    "operator": "lt",
                    "expected_ms": 2000,
                    "derived_from": "inferred",
                },
            ]
        )
        # 顺序必须与上面的输入逐一对应——这正是"按判别字段选分支"的证据。
        assert [type(x).__name__ for x in parsed] == [
            "StatusCodeAssertion",
            "JsonPathAssertion",
            "BodyContainsAssertion",
            "HeaderAssertion",
            "SchemaValidAssertion",
            "ResponseTimeAssertion",
        ]

    def test_unknown_type_names_the_type(self) -> None:
        """未知的 `type` 应当报"不认识它"，而不是"每个分支都不匹配"。

        这是用 `Field(discriminator=...)` 而非裸 `Union` 的直接收益：
        裸 Union 会依次尝试所有分支，错误信息变成一长串无关的类型错误。
        """
        with pytest.raises(ValidationError, match="llm_judge"):
            _adapter.validate_python([{"type": "llm_judge", "prompt": "看着像吗"}])

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                [
                    {
                        "type": "status_code",
                        "expected": 200,
                        "derived_from": "spec",
                        "expectedt": 200,  # 拼错，必须报错而非静默忽略
                    }
                ]
            )


class TestA2ExpectedRequired:
    """需要期望值的算子必须带 `expected`。"""

    @pytest.mark.parametrize(
        "operator", ["eq", "ne", "contains", "matches", "gt", "lt", "in_range"]
    )
    def test_operators_requiring_expected_reject_its_absence(self, operator: str) -> None:
        with pytest.raises(ValidationError, match="需要 expected"):
            JsonPathAssertion(type="json_path", path="$.x", operator=operator, derived_from="spec")

    @pytest.mark.parametrize("operator", ["exists", "not_exists"])
    def test_existence_operators_need_no_expected(self, operator: str) -> None:
        """`exists` / `not_exists` 问的是"在不在"，没有可比的对象。"""
        a = JsonPathAssertion(type="json_path", path="$.x", operator=operator, derived_from="spec")
        assert a.expected is None


class TestA4StatusCodeRange:
    """状态码必须在 100–599。"""

    @pytest.mark.parametrize("code", [-1, 0, 99, 600, 999])
    def test_out_of_range_is_rejected(self, code: int) -> None:
        with pytest.raises(ValidationError, match="100–599"):
            StatusCodeAssertion(type="status_code", expected=code, derived_from="spec")

    @pytest.mark.parametrize("code", [100, 200, 302, 404, 500, 599])
    def test_boundaries_are_accepted(self, code: int) -> None:
        assert (
            StatusCodeAssertion(type="status_code", expected=code, derived_from="spec").expected
            == code
        )

    def test_list_form_is_checked_elementwise(self) -> None:
        """`expected` 可以是列表（如重定向的多种 3xx）。逐个都要合法。"""
        a = StatusCodeAssertion(
            type="status_code", expected=[301, 302, 307, 308], derived_from="spec"
        )
        assert a.expected == [301, 302, 307, 308]

        with pytest.raises(ValidationError, match="100–599"):
            StatusCodeAssertion(type="status_code", expected=[200, 999], derived_from="spec")

    def test_empty_list_is_rejected(self) -> None:
        """空列表意味着"什么都不接受"——用例必然失败，是生成方的 bug。"""
        with pytest.raises(ValidationError, match="不能是空列表"):
            StatusCodeAssertion(type="status_code", expected=[], derived_from="spec")


class TestSchemaValidIsAlwaysSpecDerived:
    def test_inferred_is_rejected(self) -> None:
        """`schema_valid` 校验的是 Spec 里写的 schema，不可能是推断的。"""
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                [{"type": "schema_valid", "schema_ref": "200", "derived_from": "inferred"}]
            )


class TestAssertionOutcomeThreeState:
    """TE2：断言级结果**三态**，`error` 不等于 `failed`。"""

    def test_has_exactly_three_values(self) -> None:
        assert {o.value for o in AssertionOutcome} == {"passed", "failed", "error"}

    def test_error_is_not_failed(self) -> None:
        """两者绝不相等——这是归因（M3）能分辨"服务缺陷"与"响应非 JSON"的前提。"""
        assert AssertionOutcome.ERROR != AssertionOutcome.FAILED

    def test_result_carries_reason_for_error(self) -> None:
        r = AssertionResult(
            index=2,
            type="schema_valid",
            outcome=AssertionOutcome.ERROR,
            reason="response_truncated",
        )
        assert r.outcome is AssertionOutcome.ERROR
        assert r.reason == "response_truncated"
