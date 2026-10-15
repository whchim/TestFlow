"""断言求值引擎。**三态结果，不是两态。**

对应决策：`03-模块详细设计/TestExecutor.md` §3、TE2、`03/TestCaseGen.md` §3（断言结构）、
`04-数据模型.md` DM6（响应体截断）。

## 为什么必须区分 `failed` 与 `error`

两者都是"这条断言没通过"，但**指向完全不同的原因**：

| 结果 | 含义 | 归因指向 |
| :--- | :--- | :--- |
| `failed` | 断言求值成功，但不成立 | 被测系统返回了不符合预期的内容 |
| `error` | **断言根本无法求值** | 响应不是 JSON / 被截断 / JSONPath 写错了 |

一个具体的例子：`$.status` 断言 `eq "ok"`。

    {"status": "down"}     → failed  →  服务确实出了问题
    <html>502 Bad Gateway</html>  → error  →  我们连 JSON 都没拿到，
                                             这句话可能来自网关、WAF、或错误页

**合并成一种的代价**：M3 的归因再也分不清"服务缺陷"与"响应根本不是 JSON"，
而这两者的处理方式完全不同——前者要报 bug，后者要检查网络路径。

`TestExecutor.md` 把这一点标为"最容易被偷懒的地方"：合并之后代码更短、
测试更容易过，而且**不会有任何东西失败**。所以这里刻意拆开，并且有专门的
测试（`/bad-json` 那条专项检查）守着。

## 这个模块是纯函数

输入是一个已经拿到的响应 + 断言列表，输出是每一条的结果。不碰网络、
不碰数据库、不看时间。所以它可以用毫秒级跑完所有边界用例，
而"平台跑测试时用的判据"与"单元测试里验的判据"是**同一份代码**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import jsonpath_ng
import jsonpath_ng.ext
import jsonschema

from testflow.domain.assertions import (
    Assertion,
    AssertionResult,
    BodyContainsAssertion,
    HeaderAssertion,
    JsonPathAssertion,
    ResponseTimeAssertion,
    SchemaValidAssertion,
    StatusCodeAssertion,
)
from testflow.domain.assertions import (
    AssertionOutcome as Outcome,
)

__all__ = [
    "CaseStatus",
    "RequestOutcome",
    "ResponseContext",
    "derive_case_status",
    "evaluate",
]

CaseStatus = Literal["passed", "failed", "timeout", "error"]

#: `error` 的细分原因。写进 `AssertionResult.reason`，用于排障与归因。
_NOT_JSON: Final = "response_not_json"
_TRUNCATED: Final = "response_truncated"
_BAD_PATH: Final = "invalid_jsonpath"
_NO_SCHEMA: Final = "no_schema_for_status"


@dataclass(frozen=True, slots=True)
class ResponseContext:
    """求值所需的全部响应信息。**刻意不包含"是否成功"这样的结论。**

    断言引擎自己从这些原始事实里得出结论——把 `httpx.Response` 直接传进来
    会让这个纯函数依赖网络库，从而没法在单元测试里构造边界情形。
    """

    status_code: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int
    #: 响应体是否被截断（DM6）。**必须传进来**——截断后的 JSON 必然解析失败，
    #: 若不告知，引擎会把它归因成"响应格式错误"，而那是被测系统的问题。
    truncated: bool = False
    #: 该 Operation 的 `responses_schema`，供 `schema_valid` 查表。
    responses_schema: Mapping[str, Any] | None = None

    def json(self) -> tuple[Any | None, str | None]:
        """尝试解析响应体。

        Returns:
            `(解析结果, 失败原因)`。成功时原因为 `None`。
        """
        if self.truncated:
            # 截断的响应体不是一个"完整的 JSON 文档"，解析它必然失败——
            # 但这个失败是**我们造成的**，不是被测系统的。所以要单独区分，
            # 否则一个超过 256KB 的正常 JSON 响应会被判成"格式错误"。
            return None, _TRUNCATED
        try:
            return json.loads(self.body), None
        except (ValueError, UnicodeDecodeError):
            return None, _NOT_JSON

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """请求层面的结果。**用例级状态的前两级由它决定。**

    `kind` 的三个取值对应三种完全不同的情况：

    * `completed` —— 拿到了 HTTP 响应（哪怕状态码是 500）。接下来交给断言。
    * `timeout`   —— 请求超时。**这是测试结果，不是系统故障**（TE7），
      所以它不会被重试。
    * `failed`    —— 请求层失败：连接被拒、DNS 失败、TLS 失败、被 Guardrail 拦下。
    """

    kind: Literal["completed", "timeout", "failed"] = "completed"
    #: 仅在 `kind == "failed"` 时有意义。取自 `03/TestExecutor.md`：
    #: `guardrail_denied` / `dns_failure` / 连接被拒 / TLS 失败 …
    error_kind: str | None = None


# ═══════════════════════════════════════════════════════════ 主入口


def evaluate(
    assertions: Sequence[Assertion],
    response: ResponseContext,
) -> list[AssertionResult]:
    """逐条求值，返回与输入**等长且顺序一致**的结果列表。

    顺序一致是刻意的：`case_results.assertion_outcomes` 里的 `index` 要能
    对回 `cases.assertions` 的定义，否则排查时无法知道"哪一条"失败了。
    """
    return [
        AssertionResult(
            index=index,
            type=assertion.type,
            outcome=outcome,
            expected=_expected_of(assertion),
            actual=actual,
            reason=reason,
        )
        for index, assertion in enumerate(assertions)
        for outcome, actual, reason in [_evaluate_one(assertion, response)]
    ]


def _evaluate_one(
    assertion: Assertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    if isinstance(assertion, StatusCodeAssertion):
        return _check_status_code(assertion, response)
    if isinstance(assertion, HeaderAssertion):
        return _check_header(assertion, response)
    if isinstance(assertion, BodyContainsAssertion):
        return _check_body_contains(assertion, response)
    if isinstance(assertion, ResponseTimeAssertion):
        return _check_response_time(assertion, response)
    if isinstance(assertion, JsonPathAssertion):
        return _check_json_path(assertion, response)
    if isinstance(assertion, SchemaValidAssertion):
        return _check_schema(assertion, response)
    raise AssertionError(f"未处理的断言类型：{type(assertion).__name__}")


def _expected_of(assertion: Assertion) -> Any | None:
    return getattr(assertion, "expected", None)


# ═══════════════════════════════════════════════════════ 各类型的求值


def _check_status_code(
    assertion: StatusCodeAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """状态码断言。**永远可求值**——我们一定拿到了一个状态码。

    所以它不产生 `error`。这也是为什么每条用例都必须有恰好一条状态码断言：
    它在最坏情况下仍能给出一个二值结论。
    """
    actual = response.status_code
    return (_verdict(actual in _as_list(assertion.expected)), actual, None)


def _check_header(
    assertion: HeaderAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """响应头断言。**头名大小写不敏感**（HTTP 规范如此）。

    调用方通常已经把名字小写化并塞进了 dict，但这里仍归一化一次——
    依赖"上游记得转小写"会让一个显式写 `Content-Type` 的测试数据
    在某次重构后突然失败。
    """
    wanted = assertion.name.lower()
    actual = next((v for k, v in response.headers.items() if k.lower() == wanted), None)

    if assertion.operator == "exists":
        return (_verdict(actual is not None), actual, None)
    if actual is None:
        # `eq "application/json"` 对一个**不存在**的头，结论是"不成立"
        # 而不是"无法求值"——头没出现是确定的观测，不是我们看不懂响应。
        return (Outcome.FAILED, None, None)

    expected = assertion.expected or ""
    if assertion.operator == "eq":
        return (_verdict(actual == expected), actual, None)
    if assertion.operator == "contains":
        return (_verdict(expected in actual), actual, None)
    if assertion.operator == "matches":
        return (_regex_verdict(expected, actual), actual, None)
    raise AssertionError(f"未处理的 header 算子：{assertion.operator}")


def _check_body_contains(
    assertion: BodyContainsAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """原文子串匹配。

    **不使用 `json_path` 的场合靠它。** 一个非 JSON 的响应体没有任何路径可走，
    但"里面有没有这句话"仍是可以确定的观测——例如断言错误页里含不含
    `502` 或某个 trace id。

    被截断时**照常求值**：子串可能就在被保留的前 256KB 里。
    找不到时结论是 `failed`（确实没找到），而不是 `error`——
    这一点与 `json_path` 不同：那里"无法求值"是因为响应**结构**不可解析，
    而这里子串匹配对结构没有任何要求。
    """
    found = assertion.text in response.text
    return (_verdict(found), None, None)


def _check_response_time(
    assertion: ResponseTimeAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """耗时断言。永远可求值。"""
    actual = response.elapsed_ms
    if assertion.operator == "lt":
        return (_verdict(actual < assertion.expected_ms), actual, None)
    return (_verdict(actual > assertion.expected_ms), actual, None)


def _check_json_path(
    assertion: JsonPathAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """JSONPath 断言。**这里是 `error` 最主要的来源。**

    `error` 的两种成因，各自的含义完全不同：

    * 响应体不是 JSON（或被截断）→ `error` + `response_not_json` / `response_truncated`。
      取不到值，也就无从比较。**这不是服务端的问题**——响应可能来自
      网关、WAF、或一个 HTML 错误页。
    * JSONPath 语法非法 → `error` + `invalid_jsonpath`。**这是生成方的 bug**，
      但如果在这里静默判 `failed`，就等于把工具的错误算到被测系统头上。
    """
    data, parse_error = response.json()
    if parse_error is not None:
        return (Outcome.ERROR, None, parse_error)

    try:
        expression = _compile(assertion.path)
    except Exception as exc:  # noqa: BLE001 —— jsonpath_ng 的异常类型不稳定
        return (Outcome.ERROR, None, f"{_BAD_PATH}:{exc}")

    matches = expression.find(data)
    values = [m.value for m in matches]

    if assertion.operator == "exists":
        return (_verdict(bool(values)), None, None)
    if assertion.operator == "not_exists":
        return (_verdict(not values), None, None)

    if not values:
        # 路径合法但取不到值。对一个"应当存在"的字段来说，这就是不成立——
        # 不是"无法求值"：我们确实看完了整个响应体。
        return (Outcome.FAILED, None, None)

    actual = values[0]
    expected = assertion.expected
    operator = assertion.operator

    if operator == "eq":
        return (_verdict(actual == expected), actual, None)
    if operator == "ne":
        return (_verdict(actual != expected), actual, None)
    if operator == "contains":
        return (_verdict(_contains(actual, expected)), actual, None)
    if operator == "matches":
        return (_regex_verdict(str(expected), str(actual)), actual, None)
    if operator in ("gt", "lt"):
        return (_numeric_verdict(operator, actual, expected), actual, None)
    if operator == "in_range":
        return (_in_range_verdict(actual, expected), actual, None)
    raise AssertionError(f"未处理的 json_path 算子：{operator}")


def _check_schema(
    assertion: SchemaValidAssertion, response: ResponseContext
) -> tuple[Outcome, Any | None, str | None]:
    """整体 schema 校验。

    两种"无法求值"：
    * 响应被截断 → 一个不完整的 JSON 必然通不过校验，而这个失败是我们造成的
    * 状态码在 `responses_schema` 里没有声明 → **没有可校验的 schema**，
      断言本身就无从执行

    第二种值得展开：`schema_valid` 的 `schema_ref` 指向某个状态码。
    当被测服务返回了一个 Spec 里没声明过的状态码时（例如 502），
    我们**不能**把它判成"schema 不符"——Spec 里根本没有关于 502 的约定，
    说它不符合约定是无意义的。
    """
    if response.truncated:
        return (Outcome.ERROR, None, _TRUNCATED)

    schema = _schema_for(assertion.schema_ref, response.responses_schema)
    if schema is None:
        return (Outcome.ERROR, None, _NO_SCHEMA)

    data, parse_error = response.json()
    if parse_error is not None:
        return (Outcome.ERROR, None, parse_error)

    validator = jsonschema.Draft202012Validator(_openapi_to_jsonschema(schema))
    errors = list(validator.iter_errors(data))
    return (_verdict(not errors), None, errors[0].message if errors else None)


# ═══════════════════════════════════════════════════════════ 用例级状态


def derive_case_status(request: RequestOutcome, results: Sequence[AssertionResult]) -> CaseStatus:
    """把"请求结果 + 逐条断言结果"归成一个用例状态。

    **顺序不可调换**（`03/TestExecutor.md` §3.3）：

    | 顺序 | 条件 | 结果 |
    | :--- | :--- | :--- |
    | 1 | 请求超时 | `timeout` |
    | 2 | 请求层失败 | `error` |
    | 3 | 全部断言通过 | `passed` |
    | 4 | 其余一切 | `failed` |

    第 1、2 条与断言无关——**没拿到响应就没有可断言的东西**。
    先看请求是否完成，再看断言，是这个顺序的全部含义。

    第 4 条**刻意不区分** `failed` 与 `error`：用例级状态只回答
    "这条用例过没过"，"为什么没过"由 `assertion_outcomes` 承担。
    把细分塞进 `status` 会让那个列的取值变成组合爆炸，而它的
    CHECK 约束只有四个值（`04` §2.6）。
    """
    if request.kind == "timeout":
        return "timeout"
    if request.kind == "failed":
        return "error"
    if results and all(r.outcome is Outcome.PASSED for r in results):
        return "passed"
    return "failed"


# ═══════════════════════════════════════════════════════════ 小工具


def _as_list(expected: int | list[int]) -> list[int]:
    return expected if isinstance(expected, list) else [expected]


def _verdict(ok: bool) -> Outcome:
    return Outcome.PASSED if ok else Outcome.FAILED


def _contains(actual: Any, expected: Any) -> bool:
    """`contains` 对字符串和数组都成立——两者的"包含"都是自然的语义。"""
    if isinstance(actual, (str, list, tuple)):
        return expected in actual
    return False


def _regex_verdict(pattern: str, value: str) -> Outcome:
    """正则匹配。**模式非法时判 `failed` 而非 `error`。**

    模式来自生成阶段，理论上已由 A3 类约束校验过。真出现了非法模式，
    它对任何输入都不可能匹配，所以 `failed` 是正确的语义。
    （此处不抛异常：一条断言的正则写错不该让整条用例崩掉，
    其他断言的结果仍然是有价值的信息。）
    """
    try:
        return _verdict(re.fullmatch(pattern, value) is not None)
    except re.error:
        return Outcome.FAILED


def _numeric_verdict(operator: str, actual: Any, expected: Any) -> Outcome:
    """数值比较。**类型不符判 `error`**，因为那是"无法求值"。

    一个字符串和数字比大小没有意义，而我们确实无法给出结论——
    这与"1 < 2 不成立"是两回事。归因会因此指向"响应结构与 Spec 不符"，
    而那正是真实原因。
    """
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return Outcome.ERROR
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        return Outcome.ERROR
    return _verdict(actual < expected if operator == "lt" else actual > expected)


def _in_range_verdict(actual: Any, expected: Any) -> Outcome:
    if (
        not isinstance(expected, (list, tuple))
        or len(expected) != 2
        or not isinstance(actual, (int, float))
        or isinstance(actual, bool)
    ):
        return Outcome.ERROR
    low, high = expected
    if not all(isinstance(bound, (int, float)) for bound in (low, high)):
        return Outcome.ERROR
    return _verdict(low <= actual <= high)


@dataclass
class _CompiledPaths:
    """JSONPath 编译结果的缓存。

    编译一个 JSONPath 是解析工作，而同一批用例会反复用同一个路径
    （例如 50 条用例都断言 `$.data.id`）。没有缓存时每条都要重新解析一次。

    用 `field(default_factory=dict)` 而不是模块级全局：全局缓存会在
    多线程（uvicorn 的线程池）下需要加锁，而这里没有理由引入那把锁。
    """

    cache: dict[str, Any] = field(default_factory=dict)


_compiled = _CompiledPaths()


def _compile(path: str) -> Any:
    expression = _compiled.cache.get(path)
    if expression is None:
        # 用 `ext` 而非基础解析器：它支持过滤表达式（`$.items[?(@.id==1)]`），
        # 而 Spec 里的数组结构经常需要这种写法才能定位到具体元素。
        expression = jsonpath_ng.ext.parse(path)
        _compiled.cache[path] = expression
    return expression


def _schema_for(
    schema_ref: str, responses_schema: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """从 `responses_schema` 里取出某个状态码的 JSON schema。

    只认 JSON 媒体类型——TestFlow 目前处理的是 JSON API。
    其他类型（`text/plain`、二进制）没有 schema 可校验，
    调用方会得到 `None` 进而判成 `error`，而不是一个假的通过。
    """
    if not responses_schema:
        return None
    entry = responses_schema.get(schema_ref)
    if not isinstance(entry, Mapping):
        return None
    content = entry.get("content")
    if not isinstance(content, Mapping):
        return None
    for media_type, media in content.items():
        if "json" in str(media_type).lower() and isinstance(media, Mapping):
            schema = media.get("schema")
            if isinstance(schema, Mapping):
                return schema
    return None


def _openapi_to_jsonschema(node: Any) -> Any:
    """把 OpenAPI 的 schema 方言转成 JSON Schema。

    **主要目的是 `nullable`。** OpenAPI 3.0 用 `nullable: true` 表示可空，
    而 JSON Schema 没有这个关键字——`jsonschema` 会**直接忽略它**。
    后果很具体：

        {type: "string", nullable: true}   对值 null 校验 → 失败

    于是一个合法的 `null` 响应会被判成"schema 不符"。而这是**我们的**
    转换缺陷，不是被测系统的——恰好是 TE2 要防的那类误判。

    只做这一项转换，其余关键字原样保留：`jsonschema` 会忽略不认识的键，
    而多做转换（比如处理 `example`、`xml`）只会引入新的出错机会。
    """
    if isinstance(node, dict):
        converted = {
            key: _openapi_to_jsonschema(value) for key, value in node.items() if key != "nullable"
        }
        if node.get("nullable") is True:
            existing = converted.get("type")
            if isinstance(existing, str):
                converted["type"] = [existing, "null"]
            elif isinstance(existing, list) and "null" not in existing:
                converted["type"] = [*existing, "null"]
            else:
                # 没写 `type` 时，`nullable` 只能靠 `anyOf` 表达。
                # 这种情况少见（OpenAPI 规范建议 `nullable` 与 `type` 同用），
                # 保守处理成"任意类型"，而不是猜一个。
                converted.pop("$ref", None)
        return converted
    if isinstance(node, list):
        return [_openapi_to_jsonschema(item) for item in node]
    return node
