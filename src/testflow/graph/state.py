"""图的共享状态。

对应决策：`06-Agent编排.md` §2.2、G3（只存引用与聚合值）、G4（路由依据业务字段）。

## 为什么是 `TypedDict` 而不是文档里写的 `BaseModel`

`06` §2.2 画的是 Pydantic 模型。实现时改成了 `TypedDict`（LangGraph 的一等状态形式），
理由有两条：

1. **Pydantic 状态每一步都要重新校验**——而这个状态的字段全是 ID 与计数
   （G3 明确要求"实体只在 PG 里"）。校验它们得不到任何保护，
   却会在每次状态合并时付一遍代价。
2. **中断载荷的序列化会更复杂**。`Command(resume=...)` 的载荷要经过
   checkpoint 的序列化器，混入 Pydantic 模型会让那条路径多一层需要
   单独验证的转换。

字段与 `06` §2.2 逐一对应，**一个都没少**——改的只是表示形式。

## 所有字段都必须是可序列化的

状态会进 checkpoint（Redis）。放一个数据库连接或一个 HTTP 客户端进去，
第一次保存 checkpoint 时才会报错，而那时图已经跑到一半了。
"""

from __future__ import annotations

from typing import Any, TypedDict

from testflow.domain.graph import NodeFailure, RunPhase

__all__ = ["GraphState", "initial_state"]


class GraphState(TypedDict, total=False):
    """图在节点之间传递的状态。

    `total=False`：节点只返回自己改动的键，LangGraph 会把它们合并进状态。
    要求"每个节点返回完整状态"会让每个节点都要读一遍自己不该关心的字段。
    """

    # —— 标识 ——
    run_id: str
    spec_id: str
    spec_fingerprint: str
    phase: str

    # —— 引用（实体在 PG 里，这里只有 ID）——
    target_operation_ids: list[str]
    case_ids: list[str]
    approved_case_ids: list[str]
    attribution_ids: list[str]
    report_id: str | None

    # —— 聚合值 ——
    #
    # ⚠️ **仅用于展示与快速判断。** 任何正确性判定——尤其是"是否全部完成"
    # （`06` §6.2）——一律 `SELECT count(*)` 查 `case_results`。
    # 这些值会因为崩溃、重投、并发而落后，而"缓存落后"导致的最坏情况是
    # "该推进时不推进"——那正是兜底轮询要解决的问题，它自己不能再依赖
    # 一个可能不准的值。
    expected_case_count: int
    settled_case_count: int

    # —— 错误累积 ——
    node_failures: list[NodeFailure]
    retry_counts: dict[str, int]


def initial_state(*, run_id: str, spec_id: str, spec_fingerprint: str) -> GraphState:
    """一个新 Run 的初始状态。

    用函数而不是字典字面量：初始值写错（漏一个键、类型不对）只会表现成
    "某个节点读到 `None`"，而那时已经在图的中段了。
    """
    return GraphState(
        run_id=run_id,
        spec_id=spec_id,
        spec_fingerprint=spec_fingerprint,
        phase=RunPhase.INIT.value,
        target_operation_ids=[],
        case_ids=[],
        approved_case_ids=[],
        attribution_ids=[],
        report_id=None,
        expected_case_count=0,
        settled_case_count=0,
        node_failures=[],
        retry_counts={},
    )


def failures_of(state: GraphState) -> list[NodeFailure]:
    """取节点错误列表，**总是返回一个列表**。

    `state.get("node_failures")` 在还没写入时是 `None`，而 `for f in None`
    会抛 `TypeError`——在一个"处理错误"的代码路径上抛异常，是最讽刺也最常见的
    一类崩溃。
    """
    return state.get("node_failures") or []


def as_dict(state: GraphState) -> dict[str, Any]:
    return dict(state)
