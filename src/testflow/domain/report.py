"""报告的结构化内容——`reports.content` 的形状。

对应决策：`03-模块详细设计/ReportAgent.md` RA1~RA5。

**RA5：模板只负责呈现，不负责计算。** 所有聚合都在构造 `ReportContent` 时完成，
模板（Jinja2）拿到的是一个已经算好的对象。理由很实际：模板里的算术不可测试——
它只在渲染成字符串之后才存在，而那个字符串又长又难断言。
算好了再给模板，每一步聚合都能被单测覆盖。

**RA3：通过率的分子分母都要小心。** 分母只含**已执行**的用例
（`passed + failed + timeout + error`），不含未执行、未确认、被拒绝的。
否则"我们没跑的用例"会稀释通过率，把一个真实的失败率说小。

M1 只填最小集（通过率 + 失败清单），其余字段位留着——它们由 M3 的归因填。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ExecutionSummary", "FailureItem", "ReportContent", "SkippedItem", "pass_rate"]


class ExecutionSummary(BaseModel):
    """执行概览。`04` DM5 提醒：这些数字是**统计结果**，权威判定一律查 `case_results`。"""

    model_config = {"extra": "forbid"}

    total_cases: int
    executed: int
    passed: int
    failed: int
    timeout: int
    error: int
    #: 生成但未执行（M1 里只有破坏性用例会被跳过，TE3）。
    skipped: int = 0


class FailureItem(BaseModel):
    """失败清单的一条。M1 不含归因，`category` 由 M3 填。"""

    model_config = {"extra": "forbid"}

    case_id: str
    case_name: str
    dimension: str
    status: str
    http_status: int | None = None
    #: 期望与实际，用于让人一眼看出"哪里不一样"。
    expected: str | None = None
    actual: str | None = None


class SkippedItem(BaseModel):
    """未执行的用例。**RA2 要求报告必须显式列出"没测什么"。**

    不列出来的后果是报告看起来"全绿"，而实际上有一类用例从未运行——
    这种报告比没有报告更危险。
    """

    model_config = {"extra": "forbid"}

    case_id: str
    case_name: str
    reason: str
    """如 `destructive_not_authorized` / `not_settled`。"""


class ReportContent(BaseModel):
    """报告的完整结构化内容。"""

    model_config = {"extra": "forbid"}

    run_id: str
    spec_fingerprint: str
    summary: ExecutionSummary
    failures: list[FailureItem] = Field(default_factory=list)
    skipped: list[SkippedItem] = Field(default_factory=list)


def pass_rate(summary: ExecutionSummary) -> float | None:
    """通过率。**分母只含已执行用例**（RA3）。

    Returns:
        0.0–1.0；**一条都没执行时返回 `None` 而不是 0.0**——"没跑"和"全挂了"
        是两件事，用 0.0 表示前者会让人以为后者发生了。
    """
    denominator = summary.passed + summary.failed + summary.timeout + summary.error
    if denominator == 0:
        return None
    return summary.passed / denominator
