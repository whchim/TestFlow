"""断言求值引擎的单元测试。

对应决策：`03/TestExecutor.md` §3 / TE2、`03/TestCaseGen.md` §3（算子表）、`04` DM6。

**这是 M1 里最该被充分覆盖的模块**（`09` §2.2 要求 ≥90%）。
理由是它的错法很隐蔽：把 `error` 合并进 `failed` 之后，**代码更短、
测试更容易过，而且不会有任何东西失败**——只是归因从此分不清
"服务返回了错的内容"与"响应根本不是 JSON"。

所以本文件里最重要的一组是 `TestThreeState`：它专门断言那两种结论**不同**。
"""

from __future__ import annotations

import pytest

from testflow.domain.assertions import (
    AssertionOutcome as Outcome,
)
from testflow.domain.assertions import (
    AssertionResult,
    BodyContainsAssertion,
    HeaderAssertion,
    JsonPathAssertion,
    ResponseTimeAssertion,
    SchemaValidAssertion,
    StatusCodeAssertion,
)
from testflow.execution.assertions import (
    RequestOutcome,
    ResponseContext,
    derive_case_status,
    evaluate,
)


def _ctx(
    *,
    status: int = 200,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
    elapsed_ms: int = 10,
    truncated: bool = False,
    responses_schema: dict | None = None,
) -> ResponseContext:
    return ResponseContext(
        status_code=status,
        headers=headers or {"content-type": "application/json"},
        body=body,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
        responses_schema=responses_schema,
    )


def _one(assertion, ctx: ResponseContext) -> AssertionResult:
    results = evaluate([assertion], ctx)
    assert len(results) == 1
    return results[0]


# ═══════════════════════════════════════════════════════════ 三态


class TestThreeState:
    """TE2：`error`（无法求值）与 `failed`（不成立）是两回事。"""

    def test_status_code_on_non_json_body_still_passes(self) -> None:
        """**这条是整个专项检查的核心。**

        `/bad-json` 返回 200 + 一段不是 JSON 的文本。状态码断言**照常求值**
        ——我们确实收到了一个状态码，这件事没有任何不确定性。
        """
        ctx = _ctx(status=200, body=b"this is not json at all")
        result = _one(
            StatusCodeAssertion(type="status_code", expected=200, derived_from="spec"), ctx
        )
        assert result.outcome is Outcome.PASSED

    def test_json_path_on_non_json_body_is_error_not_failed(self) -> None:
        """同一个响应、同一次求值，`json_path` 的结论必须是 **`error`**。

        对比上一条：状态码能判、路径判不了。**这就是三态存在的全部理由**——
        若这里判 `failed`，归因会指向"服务返回的结构不对"，
        而事实是我们连 JSON 都没拿到（响应可能来自网关、WAF、HTML 错误页）。
        """
        ctx = _ctx(status=200, body=b"this is not json at all")
        result = _one(
            JsonPathAssertion(
                type="json_path", path="$.status", operator="exists", derived_from="spec"
            ),
            ctx,
        )
        assert result.outcome is Outcome.ERROR
        assert result.outcome is not Outcome.FAILED, "两者绝不可互换"
        assert result.reason == "response_not_json"

    def test_wrong_value_is_failed_not_error(self) -> None:
        """反面对照：拿到了合法 JSON，只是值不对 → `failed`。"""
        ctx = _ctx(body=b'{"status": "down"}')
        result = _one(
            JsonPathAssertion(
                type="json_path", path="$.status", operator="eq", expected="ok", derived_from="spec"
            ),
            ctx,
        )
        assert result.outcome is Outcome.FAILED
        assert result.actual == "down"

    def test_missing_field_is_failed_not_error(self) -> None:
        """路径合法但字段不存在 → `failed`。

        我们**确实看完了整个响应体**，"这个字段没有"是一个确定的观测，
        不是"无法求值"。
        """
        ctx = _ctx(body=b'{"other": 1}')
        result = _one(
            JsonPathAssertion(
                type="json_path", path="$.status", operator="eq", expected="ok", derived_from="spec"
            ),
            ctx,
        )
        assert result.outcome is Outcome.FAILED

    def test_invalid_jsonpath_is_error(self) -> None:
        """JSONPath 语法非法 → `error`（**生成方的 bug**）。

        若静默判 `failed`，就等于把工具的错误算到被测系统头上——
        而用户会去查一个根本不存在的服务端问题。
        """
        ctx = _ctx(body=b'{"a": 1}')
        result = _one(
            JsonPathAssertion(
                type="json_path", path="$.[[", operator="exists", derived_from="spec"
            ),
            ctx,
        )
        assert result.outcome is Outcome.ERROR
        assert result.reason is not None and result.reason.startswith("invalid_jsonpath")

    def test_truncated_body_is_error_and_says_so(self) -> None:
        """截断后的响应体**必然**解析失败，但那个失败是我们造成的（DM6）。

        不区分的话，一个超过 256KB 的正常 JSON 响应会被判成"格式错误"。
        """
        ctx = _ctx(body=b'{"a": 1', truncated=True)
        result = _one(
            JsonPathAssertion(type="json_path", path="$.a", operator="exists", derived_from="spec"),
            ctx,
        )
        assert result.outcome is Outcome.ERROR
        assert result.reason == "response_truncated"

    def test_body_contains_still_works_on_a_truncated_body(self) -> None:
        """子串匹配对**结构**没有任何要求，所以截断不影响它。"""
        ctx = _ctx(body=b"<html>502 Bad Gateway</html>", truncated=True)
        result = _one(
            BodyContainsAssertion(type="body_contains", text="502", derived_from="inferred"), ctx
        )
        assert result.outcome is Outcome.PASSED


# ═══════════════════════════════════════════════════════ 各类型


class TestStatusCode:
    @pytest.mark.parametrize(
        ("actual", "expected", "want"),
        [
            (200, 200, Outcome.PASSED),
            (500, 200, Outcome.FAILED),
            (302, [301, 302, 307, 308], Outcome.PASSED),
            (200, [301, 302], Outcome.FAILED),
        ],
    )
    def test_evaluation(self, actual: int, expected, want: Outcome) -> None:
        ctx = _ctx(status=actual)
        result = _one(
            StatusCodeAssertion(type="status_code", expected=expected, derived_from="spec"), ctx
        )
        assert result.outcome is want
        assert result.actual == actual

    def test_redirect_is_a_normal_result(self) -> None:
        """GR5：3xx 作为**结果**返回，不是被跟随的中间态。

        所以"断言 302"是一条完全正常的用例——它甚至能发现被测 API
        配置了未经声明的重定向。
        """
        ctx = _ctx(status=302, headers={"location": "http://elsewhere/"})
        result = _one(
            StatusCodeAssertion(
                type="status_code", expected=[301, 302, 307, 308], derived_from="spec"
            ),
            ctx,
        )
        assert result.outcome is Outcome.PASSED


class TestHeader:
    def test_exists(self) -> None:
        ctx = _ctx(headers={"content-type": "application/json"})
        r = _one(
            HeaderAssertion(
                type="header", name="content-type", operator="exists", derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.PASSED

    def test_exists_is_case_insensitive(self) -> None:
        """HTTP 头名大小写不敏感。依赖"上游记得转小写"会在某次重构后突然失败。"""
        ctx = _ctx(headers={"Content-Type": "application/json"})
        r = _one(
            HeaderAssertion(
                type="header", name="CONTENT-TYPE", operator="exists", derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.PASSED

    def test_missing_header_fails_rather_than_errors(self) -> None:
        """头没出现是**确定的观测**，不是"无法求值"。"""
        ctx = _ctx(headers={})
        r = _one(
            HeaderAssertion(
                type="header",
                name="x-request-id",
                operator="eq",
                expected="abc",
                derived_from="spec",
            ),
            ctx,
        )
        assert r.outcome is Outcome.FAILED

    @pytest.mark.parametrize(
        ("operator", "expected", "value", "want"),
        [
            ("eq", "application/json", "application/json", Outcome.PASSED),
            ("eq", "application/json", "text/html", Outcome.FAILED),
            ("contains", "json", "application/json; charset=utf-8", Outcome.PASSED),
            ("matches", r"application/.*", "application/json", Outcome.PASSED),
            ("matches", r"text/.*", "application/json", Outcome.FAILED),
        ],
    )
    def test_operators(self, operator: str, expected: str, value: str, want: Outcome) -> None:
        ctx = _ctx(headers={"x": value})
        r = _one(
            HeaderAssertion(
                type="header", name="x", operator=operator, expected=expected, derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is want


class TestJsonPathOperators:
    BODY = b'{"data": {"id": 7, "tags": ["a", "b"], "score": 88}, "name": "x"}'

    @pytest.mark.parametrize(
        ("path", "operator", "expected", "want"),
        [
            ("$.data.id", "exists", None, Outcome.PASSED),
            ("$.data.missing", "exists", None, Outcome.FAILED),
            ("$.data.missing", "not_exists", None, Outcome.PASSED),
            ("$.data.id", "eq", 7, Outcome.PASSED),
            ("$.data.id", "ne", 8, Outcome.PASSED),
            ("$.data.tags", "contains", "a", Outcome.PASSED),
            ("$.data.tags", "contains", "z", Outcome.FAILED),
            ("$.name", "matches", "x", Outcome.PASSED),
            ("$.data.score", "gt", 80, Outcome.PASSED),
            ("$.data.score", "lt", 80, Outcome.FAILED),
            ("$.data.score", "in_range", [80, 90], Outcome.PASSED),
            ("$.data.score", "in_range", [90, 100], Outcome.FAILED),
        ],
    )
    def test_operators(self, path: str, operator: str, expected, want: Outcome) -> None:
        ctx = _ctx(body=self.BODY)
        r = _one(
            JsonPathAssertion(
                type="json_path",
                path=path,
                operator=operator,
                expected=expected,
                derived_from="spec",
            ),
            ctx,
        )
        assert r.outcome is want

    def test_filter_expression(self) -> None:
        """`ext` 解析器支持过滤表达式——Spec 里的数组结构常需要它才能定位元素。"""
        ctx = _ctx(body=b'{"items": [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}]}')
        r = _one(
            JsonPathAssertion(
                type="json_path",
                path="$.items[?(@.id==2)].n",
                operator="eq",
                expected="b",
                derived_from="spec",
            ),
            ctx,
        )
        assert r.outcome is Outcome.PASSED

    def test_root_path(self) -> None:
        ctx = _ctx(body=b'{"a": 1}')
        r = _one(
            JsonPathAssertion(type="json_path", path="$", operator="exists", derived_from="spec"),
            ctx,
        )
        assert r.outcome is Outcome.PASSED

    def test_comparing_a_string_to_a_number_is_error(self) -> None:
        """类型不符 → `error`（无法求值）。

        一个字符串和数字比大小没有意义。把这判成 `failed` 会让归因指向
        "服务返回值不对"，而真实原因是"响应结构与 Spec 声明的类型不符"。
        """
        ctx = _ctx(body=b'{"score": "high"}')
        r = _one(
            JsonPathAssertion(
                type="json_path", path="$.score", operator="gt", expected=80, derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.ERROR

    def test_bool_is_not_treated_as_a_number(self) -> None:
        """Python 里 `isinstance(True, int)` 是 `True`。

        不特判的话，`{"flag": true}` 断言 `gt 0` 会"通过"——因为 True == 1。
        """
        ctx = _ctx(body=b'{"flag": true}')
        r = _one(
            JsonPathAssertion(
                type="json_path", path="$.flag", operator="gt", expected=0, derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.ERROR

    def test_invalid_regex_pattern_fails_rather_than_crashing(self) -> None:
        """一条断言的正则写错，不该让整条用例崩掉——其他断言的结果仍然有价值。"""
        ctx = _ctx(body=b'{"a": "x"}')
        r = _one(
            JsonPathAssertion(
                type="json_path", path="$.a", operator="matches", expected="[", derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.FAILED


class TestSchemaValid:
    SCHEMA = {
        "200": {
            "description": "ok",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    }
                }
            },
        }
    }

    def test_valid_body_passes(self) -> None:
        ctx = _ctx(body=b'{"id": 1}', responses_schema=self.SCHEMA)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.PASSED

    def test_violating_body_fails(self) -> None:
        ctx = _ctx(body=b'{"id": "not-an-int"}', responses_schema=self.SCHEMA)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.FAILED

    def test_undeclared_status_is_error_not_failed(self) -> None:
        """**这条容易搞错。** 服务返回了一个 Spec 里没声明过的状态码（例如 502）。

        我们**不能**判它"schema 不符"——Spec 里根本没有关于 502 的约定，
        说它不符合约定是无意义的。正确结论是"没有可校验的 schema"。
        """
        ctx = _ctx(status=502, body=b"{}", responses_schema=self.SCHEMA)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="502", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.ERROR
        assert r.reason == "no_schema_for_status"

    def test_no_responses_schema_at_all_is_error(self) -> None:
        ctx = _ctx(body=b"{}", responses_schema=None)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.ERROR

    def test_truncated_body_is_error(self) -> None:
        ctx = _ctx(body=b'{"id": 1', truncated=True, responses_schema=self.SCHEMA)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.ERROR
        assert r.reason == "response_truncated"

    def test_nullable_is_honoured(self) -> None:
        """**OpenAPI 的 `nullable` 不是 JSON Schema 的关键字。**

        `{type: "string", nullable: true}` 对值 `null` 做 JSON Schema 校验会**失败**
        ——因为 `jsonschema` 直接忽略 `nullable`。

        那会把一个合法的 `null` 响应判成"schema 不符"，而归因会指向
        被测系统。**这恰好是 TE2 要防的那类误判**，只不过来源是我们自己
        的方言转换。
        """
        schema = {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"note": {"type": "string", "nullable": True}},
                        }
                    }
                },
            }
        }
        ctx = _ctx(body=b'{"note": null}', responses_schema=schema)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.PASSED, "nullable 字段的 null 值应当合法"

    def test_non_nullable_still_rejects_null(self) -> None:
        """反面：不给 `nullable` 时，`null` 仍然不合法。

        少了这条，"把所有字段都当成可空"也能让上一条通过——
        而那会让 schema 校验失去意义。
        """
        schema = {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"note": {"type": "string"}},
                        }
                    }
                },
            }
        }
        ctx = _ctx(body=b'{"note": null}', responses_schema=schema)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.FAILED


class TestBodyContains:
    @pytest.mark.parametrize(("text", "want"), [("502", Outcome.PASSED), ("404", Outcome.FAILED)])
    def test_evaluation(self, text: str, want: Outcome) -> None:
        ctx = _ctx(body=b"<html>502 Bad Gateway</html>")
        r = _one(
            BodyContainsAssertion(type="body_contains", text=text, derived_from="inferred"), ctx
        )
        assert r.outcome is want

    def test_works_on_binaryish_bodies(self) -> None:
        """非 UTF-8 字节不该让求值崩掉——用 `errors="replace"` 解码。"""
        ctx = _ctx(body=b"\xff\xfe binary \x00 stuff")
        r = _one(
            BodyContainsAssertion(type="body_contains", text="binary", derived_from="inferred"), ctx
        )
        assert r.outcome is Outcome.PASSED


class TestResponseTime:
    @pytest.mark.parametrize(
        ("operator", "expected_ms", "elapsed", "want"),
        [
            ("lt", 100, 50, Outcome.PASSED),
            ("lt", 100, 150, Outcome.FAILED),
            ("gt", 100, 150, Outcome.PASSED),
            ("gt", 100, 50, Outcome.FAILED),
        ],
    )
    def test_evaluation(self, operator: str, expected_ms: int, elapsed: int, want: Outcome) -> None:
        ctx = _ctx(elapsed_ms=elapsed)
        r = _one(
            ResponseTimeAssertion(
                type="response_time",
                operator=operator,
                expected_ms=expected_ms,
                derived_from="inferred",
            ),
            ctx,
        )
        assert r.outcome is want


# ═══════════════════════════════════════════════════════ 用例级状态


class TestDeriveCaseStatus:
    """ "先看请求是否完成，再看断言"——顺序不可调换。"""

    def _results(self, *outcomes: Outcome) -> list[AssertionResult]:
        return [
            AssertionResult(index=i, type="status_code", outcome=o) for i, o in enumerate(outcomes)
        ]

    def test_timeout_wins_over_everything(self) -> None:
        """没拿到响应就没有可断言的东西——请求层面的结论优先。"""
        status = derive_case_status(RequestOutcome(kind="timeout"), self._results(Outcome.PASSED))
        assert status == "timeout"

    def test_request_failure_is_error(self) -> None:
        """连接被拒、DNS 失败、被 Guardrail 拦下——都记 `error`。"""
        status = derive_case_status(
            RequestOutcome(kind="failed", error_kind="guardrail_denied"), self._results()
        )
        assert status == "error"

    def test_all_passed_is_passed(self) -> None:
        status = derive_case_status(RequestOutcome(), self._results(Outcome.PASSED, Outcome.PASSED))
        assert status == "passed"

    def test_any_failed_is_failed(self) -> None:
        status = derive_case_status(RequestOutcome(), self._results(Outcome.PASSED, Outcome.FAILED))
        assert status == "failed"

    def test_error_outcome_also_makes_the_case_failed(self) -> None:
        """**用例级不区分 `failed` 与 `error`**——两者都让用例"没过"。

        细分由 `assertion_outcomes` 承担。把细分塞进 `status` 会让那个列
        取值组合爆炸，而它的 CHECK 约束只有四个值（`04` §2.6）。
        """
        status = derive_case_status(RequestOutcome(), self._results(Outcome.PASSED, Outcome.ERROR))
        assert status == "failed"

    def test_no_assertions_is_not_a_pass(self) -> None:
        """一条断言都没有时**不能判通过**。

        `all([])` 在 Python 里是 `True`——不特判的话，一条没有任何断言的用例
        （生成方的 bug、或被截断的 JSON）会永远显示为绿色。
        """
        status = derive_case_status(RequestOutcome(), [])
        assert status == "failed"


class TestEvaluateBatch:
    def test_results_align_with_input_order(self) -> None:
        """`index` 要能对回 `cases.assertions` 的定义，否则排查时不知道是哪条。"""
        ctx = _ctx(status=500, body=b"nope")
        assertions = [
            StatusCodeAssertion(type="status_code", expected=200, derived_from="spec"),
            JsonPathAssertion(type="json_path", path="$.a", operator="exists", derived_from="spec"),
            BodyContainsAssertion(type="body_contains", text="nope", derived_from="inferred"),
        ]
        results = evaluate(assertions, ctx)

        assert [r.index for r in results] == [0, 1, 2]
        assert [r.outcome for r in results] == [Outcome.FAILED, Outcome.ERROR, Outcome.PASSED]

    def test_empty_assertions_yields_empty_results(self) -> None:
        assert evaluate([], _ctx()) == []


class TestMalformedAssertionsDegradeToError:
    """生成方写错的结构必须判 `error`，不能崩、也不能误判成 `failed`。

    这几条覆盖的是"断言本身格式就不对"的分支。它们在实际运行里应当很罕见
    （A2~A4 在生成期就拦了大部分），但**罕见不等于不会发生**——
    而任何一种"罕见输入让整条用例崩掉"的情况，都会让那批用例的结果
    永久缺失。
    """

    @pytest.mark.parametrize("expected", ["not-a-range", [1], [1, 2, 3], ["a", "b"]])
    def test_in_range_with_a_malformed_bound_is_error(self, expected) -> None:
        ctx = _ctx(body=b'{"score": 50}')
        r = _one(
            JsonPathAssertion(
                type="json_path",
                path="$.score",
                operator="in_range",
                expected=expected,
                derived_from="spec",
            ),
            ctx,
        )
        assert r.outcome is Outcome.ERROR

    def test_numeric_operator_with_a_non_numeric_expectation_is_error(self) -> None:
        ctx = _ctx(body=b'{"score": 50}')
        r = _one(
            JsonPathAssertion(
                type="json_path",
                path="$.score",
                operator="gt",
                expected="fifty",
                derived_from="spec",
            ),
            ctx,
        )
        assert r.outcome is Outcome.ERROR

    def test_contains_on_a_scalar_fails_rather_than_crashing(self) -> None:
        """`contains` 对数字没有意义——判 `failed`（这个值不包含那个东西），
        而不是让 `in` 抛 `TypeError` 把整条用例带崩。"""
        ctx = _ctx(body=b'{"n": 42}')
        r = _one(
            JsonPathAssertion(
                type="json_path", path="$.n", operator="contains", expected=4, derived_from="spec"
            ),
            ctx,
        )
        assert r.outcome is Outcome.FAILED


class TestSchemaLookupEdgeCases:
    def test_non_json_media_type_yields_no_schema(self) -> None:
        """只认 JSON 媒体类型。其他类型没有 schema 可校验——

        判成 `error` 而不是一个假的"通过"。
        """
        schema = {
            "200": {
                "description": "ok",
                "content": {"text/plain": {"schema": {"type": "string"}}},
            }
        }
        ctx = _ctx(body=b"plain text", responses_schema=schema)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.ERROR
        assert r.reason == "no_schema_for_status"

    def test_response_without_content_section(self) -> None:
        """`204` 这类响应在 Spec 里常常只有 `description`、没有 `content`。"""
        schema = {"204": {"description": "no content"}}
        ctx = _ctx(status=204, body=b"", responses_schema=schema)
        r = _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="204", derived_from="spec"), ctx
        )
        assert r.outcome is Outcome.ERROR


class TestNullableConversion:
    """OpenAPI 的 `nullable` 需要转成 JSON Schema 的 `type: [x, "null"]`。"""

    @staticmethod
    def _run(schema: dict, body: bytes) -> Outcome:
        wrapped = {
            "200": {"description": "ok", "content": {"application/json": {"schema": schema}}}
        }
        ctx = _ctx(body=body, responses_schema=wrapped)
        return _one(
            SchemaValidAssertion(type="schema_valid", schema_ref="200", derived_from="spec"), ctx
        ).outcome

    def test_nullable_with_a_list_type(self) -> None:
        schema = {"type": ["string", "integer"], "nullable": True}
        assert self._run(schema, b"null") is Outcome.PASSED
        assert self._run(schema, b'"x"') is Outcome.PASSED

    def test_nullable_without_a_type_is_lenient(self) -> None:
        """规范建议 `nullable` 与 `type` 同用，但实际文档常常不合规。

        没写 `type` 时保守地放开——**猜一个类型比不校验更糟**：
        猜错会把合法响应判成"schema 不符"。
        """
        schema = {"nullable": True, "description": "任意"}
        assert self._run(schema, b"null") is Outcome.PASSED
        assert self._run(schema, b'{"any": "thing"}') is Outcome.PASSED

    def test_nested_nullable_property(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {"inner": {"type": "string", "nullable": True}},
                }
            },
        }
        assert self._run(schema, b'{"outer": {"inner": null}}') is Outcome.PASSED
