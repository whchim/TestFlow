"""编排图的领域类型。

对应决策：`06-Agent编排.md` §2.2（GraphState 与 RunPhase）、G3、G4。

**`RunPhase` 的取值必须与 `runs.phase` 的 CHECK 约束逐字一致**——那是数据库层
的同一份枚举。两边不一致的后果不是报错，而是"某条路径上的 phase 写不进去"，
且只在跑到那一步时才暴露。

`phase` 只用于展示与轮询扫描；**图的路由一律依据业务字段**（`approved_case_ids`
等），不得依据 `phase`（G4）——因为 `phase` 是派生值，用它做路由等于用缓存做判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["NodeFailure", "RunPhase"]


class RunPhase(StrEnum):
    """Run 的生命周期阶段。取值对齐 `04-数据模型.md` §2.3 的 CHECK 约束。

    用 `StrEnum` 而非 `(str, Enum)`：与 `guardrail.policy.DenyReason` 同理——
    后者忘记写 `.value` 时，落库的是 `'RunPhase.INIT'` 而不是 `'init'`，
    而那个字符串**看起来像是对的**，CHECK 约束才会拦住它。
    """

    INIT = "init"
    PARSING = "parsing"
    GENERATING = "generating"
    AWAITING_REVIEW = "awaiting_review"  # IP-1 挂起态
    DISPATCHING = "dispatching"
    AWAITING_RESULTS = "awaiting_results"  # IP-2 挂起态
    ANALYZING = "analyzing"
    REPORTING = "reporting"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        """终态：到达后不会再推进。

        兜底轮询与完成判定都要问"这个 Run 还活着吗"，把这个判断收在这里，
        避免各处各写一遍 `in ("done", "failed", "abandoned")`。
        """
        return self in (RunPhase.DONE, RunPhase.FAILED, RunPhase.ABANDONED)


@dataclass(frozen=True, slots=True)
class NodeFailure:
    """一个节点自己报告的错误。

    **与"用例执行失败"是两回事**：这里记的是**平台**没做成某事（解析失败、
    生成失败、LLM 超时），而 `case_results.status` 记的是**被测系统**的表现。
    两者混在一起，报告就会把"我们没跑成"说成"你服务坏了"。
    """

    node: str
    kind: str
    """错误类别，如 `spec_parse_error` / `generation_failed`."""
    detail: str
    retryable: bool = False
