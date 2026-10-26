"""提示词构造——三层结构。**稳定前缀逐字节不变**（TG6）。

对应决策：`03-模块详细设计/TestCaseGen.md` §6.1（三层结构）、TG4（逐 Operation）、
TG6（稳定前缀）、`02-架构设计.md` §5.4（上下文缓存）。

## 三层结构

```
┌─ 稳定前缀 ───────────────────────────┐  ← 缓存断点
│  系统提示：角色 · 生成规则 · 维度分类法  │     整个 Run 内逐字节不变
│            断言类型表 · 输出 Schema    │
├─ 半稳定 ─────────────────────────────┤
│  该 Spec 的全局信息                    │     同一 Spec 内不变
├─ 易变 ───────────────────────────────┤
│  当前 Operation 的裁剪后 schema        │     每个 Operation 变
│  + 阶段 A 派生的维度清单               │
└──────────────────────────────────────┘
```

## TG6：稳定前缀必须逐字节相同，而**它坏了不会有任何报错**

上下文缓存命中时输入只收 10% 的价格（`02` §5.4）。缓存是**按前缀匹配**的——
前缀里出现任何变化（时间戳、Run ID、字典顺序、字段枚举顺序）都会让缓存全部失效。

而失效的表现是**账单变高**，不是报错。所以 `02` §5.4 要求把"缓存命中 token 数"
作为观测指标——长期为 0 说明前缀被破坏了。那条观测属于 M4。

**本模块的职责是把"能变的东西"全放到后半段。** 一个具体的例子：
Run ID 放在半稳定层，而不是系统提示里——它每个 Run 都变，
放进前缀会让**每一次 Run 的缓存全部失效**。

## 给假 Provider 的标记行

`OPERATION:` 与 `DIMENSIONS:` 两行是给 `testing/fake_llm.py` 解析用的。
它们同时是**人可读的**——真实模型也会看到它们，而多看一遍结构化的输入
只会让它更清楚要做什么。这不是"为测试牺牲生产"，是两边都受益。
"""

from __future__ import annotations

import json
from typing import Any

from testflow.agents.spec_context import SpecContext
from testflow.domain.attribution import FailureContext
from testflow.domain.cases import DimensionSpec, GenerationResult

__all__ = [
    "ATTRIBUTION_SYSTEM_PREFIX",
    "SYSTEM_PREFIX",
    "build_attribution_prompt",
    "build_generation_prompt",
]

#: **稳定前缀**——整个 Run 内逐字节不变。任何会变的东西都不能放进来。
#:
#: 内容包含：角色、生成规则、断言类型表、输出约束。
#: **不包含**：Run ID、时间戳、当前 Operation 的任何信息、Spec 的任何信息。
SYSTEM_PREFIX = """你是 API 测试用例生成助手。你的任务：给定一个接口的结构描述与一份测试维度清单，\
为**每一个**维度产出一条具体的测试用例。

## 硬性规则

1. **每个维度恰好产出一条用例**，`dimension` 字段必须与清单里的标识逐字一致。
2. **断言的期望值必须来自给定的响应声明**。`derived_from` 的判定：
   期望的状态码/结构出现在 `responses` 里 → `spec`；没有出现 → `inferred`。\
**声明过的状态码不要标成 `inferred`。**
3. **禁止生成 LLM 判定型断言**——断言必须可确定性求值。可用的类型只有下面这些。
4. **不要把任何凭证放进请求头或请求体**。凭证由平台在执行时按主机注入。
5. 取值必须**合法且有区分度**：`value_valid` 维度给一组能通过校验的输入；\
边界维度给**恰好**在边界上的值：`len_max` 的长度等于 `maxLength`、\
`len_max_over` 等于 `maxLength + 1`、`max` 等于 `maximum`、\
`max_over` 等于 `maximum + step`（`step` 取 `multipleOf`，没有则为 1）。\
字段有 `multipleOf` 时，越界值也必须同时满足它（例如 `maximum=100`、\
`multipleOf=5` → `max_over` 用 105，不是 101）。\
`format` 无法判断时，给一个符合常见语义的合法值（例如 IMEI 是 15 位数字）。
6. 维度名形如 `status:409` 表示"期望返回该状态码"——对应用例必须断言它。
7. 接口 `responses` 里声明的**每个 4xx 状态码**至少要有 1 条用例断言它。\
若清单里没有对应维度，可以补充维度，但必须把该用例标为 `inferred`，\
且额外维度**不得超过给定维度的一半**。
8. **只有 `responses` 中对应状态码下声明了 `content.schema` 时**，才能使用\
`schema_valid` 断言。如果整个接口没有声明任何响应体结构，不要生成\
`json_path` / `body_contains` 断言，只用 `status_code`（和已声明的 header）断言。\
所有断言都必须有实际内容：`body_contains.text`、`json_path.path`、`header.name` 不能为空。
9. **请求体写在 `request_body_json` 字段里**，值为一个 **JSON 字符串**：\
键和字符串值都用双引号，例如 `"{\"name\": \"Alice\", \"age\": 30}"`。\
不要使用 `request_body` 字段；不要输出 YAML；\
不要包成 `{"content": ...}`、`{"type": ..., "value": ...}` 或 `{"body": ...}`。\
没有请求体时填 `null`。
10. **查询参数写在 `request_query_json`**，**请求头写在 `request_headers_json`**，\
值都是 JSON 字符串；不要使用 `request_query` / `request_headers` 字段。\
查询参数是扁平对象（如 `{"page": 1}`），不是 OpenAPI 的 `parameters` 数组；\
请求头只放必要的非凭证头（如 `Content-Type`）。
11. **路径参数写在 `path_params_json`**：路径模板里每个 `{变量}` 都必须有值，\
例如 `/invoices/{invoice_id}` → `"{\"invoice_id\": \"inv-123\"}"`。\
没有路径参数时填 `null`。**不要把 `{invoice_id}` 这样的模板原样留在请求里。**
12. 请求体**只能包含 schema 里声明过的字段**，不要添加 `id`、`created_at` \
之类的服务端字段。

## 断言类型

| type | 用途 | 字段 |
| :--- | :--- | :--- |
| `status_code` | 状态码 | `expected`（整数或整数列表） |
| `json_path` | 响应体中的值 | `path`（JSONPath）、`operator`、`expected` |
| `header` | 响应头 | `name`、`operator`、`expected` |
| `schema_valid` | 整体结构 | `schema_ref`（状态码） |
| `body_contains` | 原文子串（用于非 JSON 响应） | `text` |
| `response_time` | 耗时 | `operator`（lt/gt）、`expected_ms` |

`json_path` 的可用算子：`exists` / `not_exists` / `eq` / `ne` / `contains` / \
`matches` / `gt` / `lt` / `in_range`。

## 输出

严格按给定的 JSON Schema 输出。**不要输出任何解释性文字**。
"""


def build_generation_prompt(
    *,
    operation: dict[str, Any],
    dimensions: list[DimensionSpec],
    spec_context: SpecContext | None = None,
    feedback: str | None = None,
) -> str:
    """组装一次生成调用的提示词。

    Args:
        operation: 当前接口的裁剪后结构。**只含这一个接口**——
            实测的成本约束：Qwen3.5-Plus 按单次输入长度阶梯计费，
            超过 128K 单价跳 5 倍（`02` §5.1）。逐 Operation 天然把
            单次输入限制在几 KB。
        dimensions: 阶段 A 派生的维度清单。
        spec_context: 同一 Spec 内不变的全局信息。为空时半稳定层为空串。
        feedback: 上一次输出没通过 L1/L2 时的修正提示。**只追加在易变层末尾**——
            放进稳定前缀会让缓存**每一次重试都失效**。

    Returns:
        完整的提示词。**顺序是固定的**：稳定前缀 → 半稳定 → 易变。
    """
    # 半稳定层：同一 Spec 内的所有 Operation 共享这一段的内容。
    # 它必须是逐字节确定的，否则前缀缓存静默失效（`07` §5.3）。
    semi_stable = spec_context.prompt_block() if spec_context is not None else ""

    # 易变层。
    volatile_parts = [
        f"OPERATION: {operation.get('method', 'GET')} {operation.get('path', '/')}",
        # 维度清单用**一行 JSON**：假 Provider 要能按固定规则取出来，
        # 而真实模型读它也不困难。缩进对它没有帮助，反而让"一行一个标记"
        # 这个约定变得模糊。
        "DIMENSIONS: "
        + json.dumps([d.model_dump(mode="json") for d in dimensions], ensure_ascii=False),
        "",
        "## 接口结构",
        json.dumps(_trim(operation), ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "请为上面 `DIMENSIONS` 里的每一个维度产出一条用例。",
    ]
    if feedback:
        volatile_parts.extend(
            (
                "",
                "## 上一次输出没有通过校验，请针对性修正",
                feedback,
            )
        )
    volatile = "\n".join(volatile_parts)

    return f"{SYSTEM_PREFIX}\n{semi_stable}\n{volatile}"


def _trim(operation: dict[str, Any]) -> dict[str, Any]:
    """裁剪出提示词需要的部分。

    **丢掉与生成无关的大块内容**：`summary`、`description`、`tags`、
    各种 `x-` 扩展——它们对"构造一个合法请求"没有帮助，却会显著增加
    token 数，而 token 数直接决定落在哪个计费档。

    `sort_keys=True` 在 `json.dumps` 里是**必需的**：字典顺序会随
    Python 版本、随上游构造方式变化，而它是易变层的内容——虽然不影响缓存前缀，
    但会让"同一个 Operation 两次调用得到不同的提示词"，
    从而让"生成结果为什么不一样"这个问题多出一个无关变量。
    """
    keep = ("method", "path", "parameters_schema", "request_body_schema", "responses_schema")
    return {key: operation[key] for key in keep if operation.get(key) is not None}


# ══════════════════════════════════════════════ 归因（M3）

#: 归因兜底的稳定前缀。
#:
#: 与生成提示词同理：整段逐字节不变，才能吃到上下文缓存（`02` §5.4）。
#: **不包含**任何失败现场的信息——那些全在易变段。
ATTRIBUTION_SYSTEM_PREFIX = """你是 API 测试失败归因助手。你的任务：给定一次失败的全部现场\
（状态码、响应、断言结果、期望结构），判断这次失败的责任归属。

## 只能从下面的类别中选择

| 类别 | 含义 |
| :--- | :--- |
| `request_invalid` | 请求参数不符合接口契约（400/422） |
| `auth_error` | 凭证无效、过期或权限不足（401/403） |
| `resource_not_found` | 依赖的资源不存在（404）——通常是测试数据没准备好 |
| `conflict` | 状态冲突（409）——常见于重复执行或数据未清理 |
| `rate_limited` | 被限流（429） |
| `server_error` | 被测服务异常（5xx） |
| `response_schema_mismatch` | 响应结构与 Spec 声明不符 |
| `assertion_mismatch` | HTTP 正常但内容不符——**可能是服务错了，也可能是用例的断言写错了** |
| `network_error` | 连接失败、DNS、TLS、超时——请求未到达或未返回 |
| `blocked_by_policy` | 被平台安全策略拦截 |
| `unknown` | 无法归类。**这是缺陷信号，不是正常输出** |

## 硬性规则

1. **不要因为响应体里找不到某个字段就判定结构不符。** 若 `TRUNCATED: true`，\
说明响应体被截断——字段可能在被截掉的部分里。
2. **不要臆测没有给出的信息。** 证据只能来自输入里明确出现的内容。
3. `assertion_mismatch` 天然二义。若用例置信度是 `low`，失败更可能是用例本身的问题。
4. 若确实无法判断，选 `unknown` 并说明理由。**不要为了给出一个答案而硬选。**

## 输出

严格按给定的 JSON Schema 输出。`evidence` 逐条列出关键信号；`reasoning` 写明判定理由。
"""


def build_attribution_prompt(ctx: FailureContext) -> str:
    """组装一次归因调用的提示词（LA §5.2）。

    刻意**不包含** `case_id` / `run_id`：归因结论不该依赖"哪条用例"。
    也不包含请求头与请求体——前者可能含凭证（SR-6），后者对判断
    "为什么失败"几乎没有帮助。
    """
    failures = [
        item for item in ctx.assertion_outcomes if item.get("outcome") in ("failed", "error")
    ]
    volatile = "\n".join(
        (
            f"FAILURE_STATUS: {ctx.status}",
            f"HTTP_STATUS: {ctx.http_status if ctx.http_status is not None else 'null'}",
            f"ERROR_KIND: {ctx.error_kind or 'null'}",
            # ★ 必须显式告知截断。模型不知道时，会把"字段在被截掉的部分里"
            #   误判成"字段缺失"，得出 `response_schema_mismatch` 的错误结论（DM6）。
            f"TRUNCATED: {'true' if ctx.truncated else 'false'}",
            f"CASE_CONFIDENCE: {ctx.case_confidence or 'unknown'}",
            f"ELAPSED_MS: {ctx.elapsed_ms if ctx.elapsed_ms is not None else 'null'}",
            "ASSERTION_OUTCOMES: " + json.dumps(failures, ensure_ascii=False, sort_keys=True),
            "EXPECTED_RESPONSE_SCHEMA: "
            + (
                json.dumps(ctx.responses_schema, ensure_ascii=False, sort_keys=True)
                if ctx.responses_schema is not None
                else "null"
            ),
            "",
            "RESPONSE_BODY:",
            ctx.response_body or "(空)",
            "",
            "请判断这次失败的责任归属。",
        )
    )
    return f"{ATTRIBUTION_SYSTEM_PREFIX}\n{volatile}"


_ = GenerationResult  # 保留导入：Schema 由调用方传给 Provider
