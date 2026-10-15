"""断言的权威定义——判别联合 + 求值结果。

对应决策：`03-模块详细设计/TestCaseGen.md` §3（A1~A5）与 `03/TestExecutor.md` §3（TE2）。

**为什么用"判别联合"**：一条断言是"用某个算子把响应里的某个东西和一个期望值比"。
不同类型的断言，比的**东西**（状态码 / JSONPath / 响应头 / 整体 schema / 原文 / 耗时）
和**算子**都不同。若用一个宽结构 + 一堆可选字段表示，"哪些字段在哪种类型下必填"
就只存在于文档里——而这里它由类型系统强制（A2）。

**TG2：断言禁止由 LLM 判定。** 这里没有、也不会有 `llm_judge` 类型。
语义判断属于归因（`LogAnalyzer`），不属于断言。理由是断言必须**可确定性求值**——
否则同一个响应跑两次可能得到不同结论，测试本身就失去了意义。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "Assertion",
    "AssertionOutcome",
    "AssertionResult",
    "BodyContainsAssertion",
    "DerivedFrom",
    "HeaderAssertion",
    "JsonPathAssertion",
    "ResponseTimeAssertion",
    "SchemaValidAssertion",
    "StatusCodeAssertion",
]

#: 该断言是照 Spec 写的，还是推断出来的。A5。
#:
#: 这个字段决定了用例的置信度：推断出来的期望值（如 Spec 没声明 404 却断言 404）
#: 是**猜**的，出错的可能来自生成方而非被测系统。
DerivedFrom = Literal["spec", "inferred"]


class _Base(BaseModel):
    """共同配置。`extra="forbid"`：多出的字段应当报错而非静默忽略。

    静默忽略的后果是"生成方以为写了个有效约束，实际从未生效"——
    而那正是 A1~A4 这些约束要防的同一类问题。
    """

    model_config = {"extra": "forbid"}


class StatusCodeAssertion(_Base):
    """状态码断言。每条用例**恰好一条**。"""

    type: Literal["status_code"]
    expected: int | list[int]
    derived_from: DerivedFrom

    @model_validator(mode="after")
    def _codes_are_plausible(self) -> StatusCodeAssertion:
        """A4：状态码必须在 100–599。

        拦的是"结构合法但语义荒谬"的输出——LLM 写出 `999` 或 `0` 时，
        Schema 校验（只保证结构）是拦不住的，而它会让断言**永远失败**，
        表现为"被测系统有问题"。
        """
        codes = self.expected if isinstance(self.expected, list) else [self.expected]
        if not codes:
            raise ValueError("status_code 断言的 expected 不能是空列表")
        bad = [c for c in codes if not 100 <= c <= 599]
        if bad:
            raise ValueError(f"状态码超出 100–599：{bad}")
        return self


class JsonPathAssertion(_Base):
    """JSONPath 断言。`path` 从响应体中取值，再按 `operator` 比较。"""

    type: Literal["json_path"]
    path: str
    operator: Literal[
        "exists", "not_exists", "eq", "ne", "contains", "matches", "gt", "lt", "in_range"
    ]
    expected: Any | None = None
    derived_from: DerivedFrom

    @model_validator(mode="after")
    def _expected_present_when_needed(self) -> JsonPathAssertion:
        """A2：算子需要期望值时，`expected` 必填。

        `exists` / `not_exists` 是"取值存不存在"，没有可比的对象；
        其余算子都在和一个期望值比——缺了它，断言在**执行期**才会崩，
        而且是在一条已经跑完请求的用例上崩，代价更高。
        """
        if self.operator not in ("exists", "not_exists") and self.expected is None:
            raise ValueError(f"算子 {self.operator!r} 需要 expected，但未提供")
        return self


class HeaderAssertion(_Base):
    """响应头断言。头名大小写不敏感（HTTP 规范如此）。"""

    type: Literal["header"]
    name: str
    operator: Literal["exists", "eq", "contains", "matches"]
    expected: str | None = None
    derived_from: DerivedFrom

    @model_validator(mode="after")
    def _expected_present_when_needed(self) -> HeaderAssertion:
        """A2，同 `JsonPathAssertion`。"""
        if self.operator != "exists" and self.expected is None:
            raise ValueError(f"算子 {self.operator!r} 需要 expected，但未提供")
        return self


class SchemaValidAssertion(_Base):
    """整体 schema 校验。指向 `operations.responses_schema` 的某个状态码。"""

    type: Literal["schema_valid"]
    schema_ref: str
    #: 固定为 `spec`——schema 来自 Spec，不可能是推断的。写成字面量而非
    #: `DerivedFrom`，让"它只能是 spec"成为类型事实而非约定。
    derived_from: Literal["spec"]


class BodyContainsAssertion(_Base):
    """原文子串匹配。用于**非 JSON 响应**——这正是 `json_path` 无法求值的场合。"""

    type: Literal["body_contains"]
    text: str
    derived_from: Literal["inferred"]


class ResponseTimeAssertion(_Base):
    """响应耗时断言。"""

    type: Literal["response_time"]
    operator: Literal["lt", "gt"]
    expected_ms: int
    derived_from: Literal["inferred"]


#: 判别联合。`Field(discriminator="type")` 让 Pydantic 直接按 `type` 选分支——
#: 少了它，Pydantic 会依次尝试每个分支，错误信息会变成一串"都不匹配"，
#: 而不是"你写的 type 不认识"。
Assertion = Annotated[
    StatusCodeAssertion
    | JsonPathAssertion
    | HeaderAssertion
    | SchemaValidAssertion
    | BodyContainsAssertion
    | ResponseTimeAssertion,
    Field(discriminator="type"),
]


class AssertionOutcome(StrEnum):
    """断言级结果。**三态**，这是 TE2 的核心。

    | 取值 | 含义 |
    | :--- | :--- |
    | `passed` | 断言成立 |
    | `failed` | 求值成功，但不成立 |
    | `error` | **无法求值**（响应不是 JSON、响应被截断、JSONPath 非法） |

    **为什么必须区分 `failed` 与 `error`**：`json_path` 判 `failed` 指向
    "服务返回的结构和它声明的不一样"（服务端问题）；判 `error` 指向
    "响应根本不是 JSON"（可能是错误页、可能是被 WAF 拦了）。
    合并成一种，归因（M3）就再也分不清这两种，而它们的处理方式完全不同。
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class AssertionResult(_Base):
    """一条断言的求值结果。`case_results.assertion_outcomes` 的元素。"""

    index: int
    type: str
    outcome: AssertionOutcome
    #: 仅在需要解释时出现，不参与控制流。
    expected: Any | None = None
    actual: Any | None = None
    #: `outcome == error` 时说明为什么无法求值，如 `response_not_json` /
    #: `response_truncated` / `invalid_jsonpath`。
    reason: str | None = None
