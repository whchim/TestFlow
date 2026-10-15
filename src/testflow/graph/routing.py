"""条件边：节点之后该往哪走。

对应决策：`06-Agent编排.md` §4、G4。

## G4：**一律依据业务字段，绝不依据 `phase`**

`phase` 是派生值——它由节点写、为展示和轮询扫描服务。用它做路由等于
用缓存做判断，而缓存会落后：崩溃、重投、并发都可能让它和真实状态不一致。

所以这里读的全是**业务字段**：`target_operation_ids` 空不空、
`case_ids` 空不空、`approved_case_ids` 空不空。这些字段的语义是唯一的，
不存在"落后"这个状态。

## 三个条件边的共同形状

每个都回答同一个问题：**"上一步有没有产出可继续的东西？"**

没有 → 终止（`FAILED`），因为继续走下去只会产出一份空报告。
**空报告比失败更糟**：它看起来像"测试通过了"。
"""

from __future__ import annotations

from typing import Literal

from testflow.graph.state import GraphState, failures_of

__all__ = [
    "after_collect",
    "after_generate",
    "after_parse",
    "after_decisions",
]

#: 终止原因。写进 `runs.failure_reason`，也是日志里能搜到的关键词。
_NO_OPERATIONS = "spec 里没有可用接口"
_NO_CASES = "没有生成出任何用例"
_PARSE_FAILED = "Spec 解析失败"


def after_parse(state: GraphState) -> Literal["generate_cases", "__end__"]:
    """N1 之后。

    两种终止情形：

    * **解析失败** —— 非法 Spec 不该进入后续流程
    * **没有可用接口** —— 继续走下去会产出一份空报告

    注意"解析失败"的判据是 `node_failures` 里**含解析错误**，
    而不是"列表非空"：N1 也可能报告一些不致命的警告，
    而那些不该中断流程。
    """
    fatal = {
        "spec_missing",
        "no_operations",
        "syntax_error",
        "invalid_openapi",
        "dangling_ref",
        "external_ref_unsupported",
        "not_an_object",
        "missing_openapi_version",
    }
    if any(f.node == "parse_spec" and f.kind in fatal for f in failures_of(state)):
        return "__end__"
    if not state.get("target_operation_ids"):
        return "__end__"
    return "generate_cases"


def after_generate(state: GraphState) -> Literal["apply_decisions", "__end__"]:
    """N2 之后。

    **部分成功不阻塞全局**——只要产出了至少一条用例就继续。
    一个接口的 schema 特别难处理，不该让整份 Spec 的生成全部作废。

    一条都没产出时才终止：那意味着后续每一步都会在空集合上运转。
    """
    if not state.get("case_ids"):
        return "__end__"
    return "apply_decisions"


def after_decisions(state: GraphState) -> Literal["dispatch_cases", "__end__"]:
    """N3 之后。

    用户把全部用例都丢弃时走终止——但那是**正常结束**，不是失败
    （`06` §4 明确：`END(DONE)`）。这一点很重要：一份"用户看过之后
    决定不测"的报告，和一份"平台没跑成"的报告，含义完全不同。

    M1 没有 IP-1，所以这条分支实际上走不到（N3 无条件批准全部）。
    保留它是因为 M2 插入确认门之后，它是**唯一**能表达"用户全否"的路径；
    到那时再补，图的形状就要变了。
    """
    if not state.get("approved_case_ids"):
        return "__end__"
    return "dispatch_cases"


def after_collect(state: GraphState) -> Literal["analyze_failures", "build_report"]:
    """N5 之后。

    没有失败用例就直接出报告，省掉一趟归因。

    ⚠️ **M1 的判据是"结果里有非 passed 的"**，而 N6 本身是空操作。
    这个短路在 M1 看着没什么用（省一个空函数调用），但它是 M3 的
    前提——归因要花 LLM 调用，而"零失败"的 Run 不该为此付钱。
    """
    _ = state  # M1 无法从 state 判断是否有失败（结果在 PG 里），一律走归因
    return "analyze_failures"
