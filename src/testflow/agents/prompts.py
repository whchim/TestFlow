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

from testflow.domain.cases import DimensionSpec, GenerationResult

__all__ = ["SYSTEM_PREFIX", "build_generation_prompt"]

#: **稳定前缀**——整个 Run 内逐字节不变。任何会变的东西都不能放进来。
#:
#: 内容包含：角色、生成规则、断言类型表、输出约束。
#: **不包含**：Run ID、时间戳、当前 Operation 的任何信息、Spec 的任何信息。
SYSTEM_PREFIX = """你是 API 测试用例生成助手。你的任务：给定一个接口的结构描述与一份测试维度清单，\
为**每一个**维度产出一条具体的测试用例。

## 硬性规则

1. **每个维度恰好产出一条用例**，`dimension` 字段必须与清单里的标识逐字一致。
2. **断言的期望值必须来自给定的响应声明**。若接口没有声明某个错误状态码，\
仍然要断言，但把该断言的 `derived_from` 标为 `inferred`。
3. **禁止生成 LLM 判定型断言**——断言必须可确定性求值。可用的类型只有下面这些。
4. **不要把任何凭证放进请求头或请求体**。凭证由平台在执行时按主机注入。
5. 取值必须**合法且有区分度**：`value_valid` 维度给一组能通过校验的输入；\
边界维度给刚好在边界上的值。

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
) -> str:
    """组装一次生成调用的提示词。

    Args:
        operation: 当前接口的裁剪后结构。**只含这一个接口**——
            实测的成本约束：Qwen3.5-Plus 按单次输入长度阶梯计费，
            超过 128K 单价跳 5 倍（`02` §5.1）。逐 Operation 天然把
            单次输入限制在几 KB。
        dimensions: 阶段 A 派生的维度清单。

    Returns:
        完整的提示词。**顺序是固定的**：稳定前缀 → 半稳定 → 易变。
    """
    # 半稳定层：同一 Spec 内的所有 Operation 共享这一段的内容。
    # 目前为空——M1 还没有"Spec 全局信息"（比如认证方案、通用错误约定）。
    # 留着一个明确的位置，是为了让 M4 加缓存观测时知道该往哪儿放。
    semi_stable = ""

    # 易变层。
    volatile = "\n".join(
        (
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
        )
    )

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


_ = GenerationResult  # 保留导入：Schema 由调用方传给 Provider
