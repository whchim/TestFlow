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

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "ExecutionSummary",
    "FailureItem",
    "LLMUsageSummary",
    "ReportContent",
    "SkippedItem",
    "pass_rate",
]


class ExecutionSummary(BaseModel):
    """执行概览。`04` DM5 提醒：这些数字是**统计结果**，权威判定一律查 `case_results`。"""

    model_config = {"extra": "forbid"}

    total_cases: int
    executed: int
    passed: int
    failed: int
    timeout: int
    error: int
    #: 生成但未执行（破坏性用例、被拒绝、尚未了结）。
    skipped: int = 0
    #: **平台补记**的超时（`error_kind = executor_stall`）。
    #:
    #: 与 `timeout` 分开：`timeout` 是"发了请求但目标没按时回应"，
    #: 是**测试结论**；`unfinished` 是"结果根本没回来"，不是测试结论。
    #: 它不进通过率分母——否则"我们没跑成"会稀释真实的失败率（RA3 细化）。
    unfinished: int = 0


class FailureItem(BaseModel):
    """失败清单的一条。"""

    model_config = {"extra": "forbid"}

    case_id: str
    case_name: str
    dimension: str
    status: str
    http_status: int | None = None
    #: 期望与实际，用于让人一眼看出"哪里不一样"。
    expected: str | None = None
    actual: str | None = None
    #: M3 归因。无归因时为空（例如分析器没跑到这一步）。
    category: str | None = None
    method: str | None = None
    rule_ref: str | None = None
    llm_model: str | None = None
    confidence: str | None = None
    evidence: dict[str, Any] | None = None
    human_verdict: str | None = None
    human_category: str | None = None
    #: 给人看的补充提示。目前只有低置信度 `assertion_mismatch` 会填。
    hint: str | None = None


class SkippedItem(BaseModel):
    """未执行的用例。**RA2 要求报告必须显式列出"没测什么"。**

    不列出来的后果是报告看起来"全绿"，而实际上有一类用例从未运行——
    这种报告比没有报告更危险。
    """

    model_config = {"extra": "forbid"}

    case_id: str
    case_name: str
    reason: str
    """如 `destructive_not_authorized` / `rejected` / `not_settled`。"""


class LLMUsageSummary(BaseModel):
    """本次 Run 的 LLM 用量与成本（RA6）。

    ## 为什么它不放进 `ExecutionSummary`

    那里是**测试结果**的语义（通过/失败/超时）。成本是**平台开销**，
    两者混在一起会让"这个数字算不算在通过率里"变成一个要猜的问题。
    """

    model_config = {"extra": "forbid"}

    calls: int = 0
    generation_calls: int = 0
    attribution_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 命中前缀缓存的输入 token（`prompt_tokens` 的子集）。
    cached_tokens: int = 0
    #: 缓存命中率 = cached / prompt。长期为 0 是缺陷，不是波动（`07` §5.3）。
    cache_hit_rate: float | None = None
    #: 元。**未知模型时为 `None`**——宁可说"不知道"，也不给一个错的金额。
    cost_cny: float | None = None
    #: 没有单价、因而未计入 `cost_cny` 的模型名。
    unpriced_models: list[str] = Field(default_factory=list)
    #: 单次调用耗时的 P95（毫秒）。包含生成与归因两条路。
    p95_latency_ms: int | None = None
    #: 生成采纳率 = 被批准的用例 / 生成的用例。
    generation_acceptance_rate: float | None = None
    #: 提供商没有返回 usage 的调用数。>0 说明成本被低估了。
    usage_missing_calls: int = 0


class ReportContent(BaseModel):
    """报告的完整结构化内容。"""

    model_config = {"extra": "forbid"}

    run_id: str
    spec_fingerprint: str
    summary: ExecutionSummary
    failures: list[FailureItem] = Field(default_factory=list)
    skipped: list[SkippedItem] = Field(default_factory=list)
    #: **平台补记的超时**（`executor_stall`）。不是失败、也不是"没跑"，
    #: 而是"结果没回来"。单独一节列出——否则用户会把它们当成目标服务的
    #: 性能问题去排查。
    unfinished_cases: list[SkippedItem] = Field(default_factory=list)

    #: 按**责任归属**分组的失败数。键是组名，顺序即展示顺序。
    #:
    #: 零值的组也会出现（RA1）："环境侧 0 条"本身就是一条信息——
    #: 它告诉用户"不用去查网络"。
    attribution_groups: dict[str, int] = Field(default_factory=dict)

    #: 规则覆盖率 = 被规则确定性归因的失败数 / 可归因的失败数。
    #:
    #: 这是 LogAnalyzer 的第一指标（LA §2.2）：它直接决定成本，
    #: 也是"要不要补规则"的唯一依据。无失败时为 `None`（不是 0——"没失败"
    #: 和"规则一条都没覆盖"是两件事）。
    rule_coverage: float | None = None

    #: 走 LLM 兜底的**归因条数**。⚠️ 它不是真实调用次数（不含生成、重试、
    #: 失败调用）——真实的数量与成本看 `llm_usage`。保留它是为了兼容
    #: M3 的既有契约与测试。
    llm_calls: int = 0

    #: 真实的调用次数、token 与成本（M4）。
    llm_usage: LLMUsageSummary = Field(default_factory=LLMUsageSummary)


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
