"""失败归因的领域类型。

对应决策：`03-模块详细设计/LogAnalyzer.md` §3（11 类）、§6（LA3 置信度耦合）、
`04-数据模型.md` §2.7。

## 为什么类别是 `StrEnum` 而不是一堆字符串常量

`attributions.category` 有数据库 `CHECK` 约束，11 个取值是**封闭枚举**（LA2）。
用 `StrEnum` 让"拼错类别名"在 Python 侧就暴露，而不是落库时变成一条
`CHECK` 违约——那时用户看到的是 500，真正的原因是他写错了一个词。

## 责任归属为什么单独成一个映射

报告的目标不是"列出 11 个类别"，而是让人一眼看出**该去找谁**（RA1）。
11 个类别对使用者太细；责任归属（请求侧 / 服务侧 / 环境侧…）才是他真正
要做的第一个决定。映射放在领域层而不是报告模板里，是因为 API 与 CLI
也会用到同一份口径——两处各写一份，迟早对不上。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from testflow.domain.graph import NodeFailure

__all__ = [
    "RESPONSIBILITY_GROUPS",
    "RESPONSIBILITY_UNCLASSIFIED",
    "AnalysisOutcome",
    "AttributionCategory",
    "AttributionDecision",
    "Confidence",
    "FailedCase",
    "FailureContext",
    "FallbackAttribution",
    "cap_confidence",
    "is_synthesized_stall",
    "responsibility_of",
]


class AttributionCategory(StrEnum):
    """归因类别。**取值必须与 `attributions.category` 的 CHECK 逐字一致。**

    两边不一致的后果不是报错，而是"某条归因写不进去"——且只在跑到那一步时
    才暴露（与 `RunPhase` 同理）。
    """

    REQUEST_INVALID = "request_invalid"
    AUTH_ERROR = "auth_error"
    RESOURCE_NOT_FOUND = "resource_not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    RESPONSE_SCHEMA_MISMATCH = "response_schema_mismatch"
    ASSERTION_MISMATCH = "assertion_mismatch"
    NETWORK_ERROR = "network_error"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    UNKNOWN = "unknown"


Confidence = Literal["high", "medium", "low"]

#: 置信度的弱→强顺序。封顶就是在这个序列上取较小值。
_CONFIDENCE_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}
_RANK_TO_CONFIDENCE: Final[dict[int, Confidence]] = {0: "low", 1: "medium", 2: "high"}

#: 归因可靠性的**结构上限**：`unknown` 永远不可信（LA §3.4）。
_CONFIDENCE_CEILING: Final[dict[AttributionCategory, Confidence]] = {
    AttributionCategory.UNKNOWN: "low",
}

#: "用例置信度 → 归因置信度上限"（LA3）。
#:
#: `assertion_mismatch` 是唯一需要看用例置信度的类别：同一类失败，
#: 断言来自 spec 时更可能是服务的问题，断言来自推断时更可能是用例写错了。
#: 不给这个上限，低置信度用例的失败会与服务缺陷混在一起，
#: 报告里"服务缺陷 N 条"就不可信了（LA §6）。
#:
#: ⚠️ **没有"除非有额外证据"的口子。** 那句话在设计文档里没有可执行定义，
#: 留着等于没有上限。
_CASE_CONFIDENCE_CEILING: Final[dict[tuple[AttributionCategory, str], Confidence]] = {
    (AttributionCategory.ASSERTION_MISMATCH, "high"): "medium",
    (AttributionCategory.ASSERTION_MISMATCH, "low"): "low",
    (AttributionCategory.ASSERTION_MISMATCH, "unknown"): "low",
}


def is_synthesized_stall(status: str, error_kind: str | None) -> bool:
    """这条结果是不是"平台补记的超时"（`executor_stall`），而非真正的测试结论。

    补记发生在兜底轮询里（`results.mark_unfinished_as_timeout`）：结果没回来，
    系统把它记成 `timeout` 好让流程能往下走。它**不是**"我们发了请求但没等到
    回应"，而是"我们根本没拿到结果"。两者的区别决定了：

    - 不参与归因（归因是给测试结论解释原因的）
    - 不进通过率分母（否则真实的失败率会被稀释）
    - 在报告里单列成"未完成"
    """
    return status == "timeout" and error_kind == "executor_stall"


def cap_confidence(
    base: Confidence,
    *,
    category: AttributionCategory,
    case_confidence: str | None,
    truncated: bool = False,
) -> Confidence:
    """按 LA3 与"截断即降级"封顶归因置信度。

    `case_confidence is None` 按 `low` 处理，而不是"不设限"：
    缺值意味着 `TestCaseGen` 没给出可靠判断，不能当成"高置信度"用。

    `truncated=True` 时最高 `medium`（DM6）：模型看不到被截掉的部分，
    它的"字段缺失"判断可能只是"没看到"。
    """
    ceiling = _CONFIDENCE_RANK[base]
    if truncated:
        ceiling = min(ceiling, _CONFIDENCE_RANK["medium"])

    structural = _CONFIDENCE_CEILING.get(category)
    if structural is not None:
        ceiling = min(ceiling, _CONFIDENCE_RANK[structural])

    case_level = case_confidence if case_confidence in _CONFIDENCE_RANK else "unknown"
    by_case = _CASE_CONFIDENCE_CEILING.get((category, case_level))
    if by_case is not None:
        ceiling = min(ceiling, _CONFIDENCE_RANK[by_case])

    return _RANK_TO_CONFIDENCE[ceiling]


#: 报告里"该去找谁"的映射。**顺序即报告中的展示顺序**（从最可能自己改的
#: 往最需要报告的开始排）。
#:
#: 比 RA1 原文多出三组：
#:
#: - **凭证侧**：`auth_error` 的排查方向是"令牌过期/权限不足"，与
#:   "请求参数写错了"（请求侧）完全是两件事，合并会让人朝错误方向查
#: - **需人工判断**：`assertion_mismatch` 天然二义（服务缺陷 or 用例缺陷），
#:   假装它能自动归到某一侧，等于把不确定性藏起来（LA §3.3）
#: - **未归类**：`unknown` 是缺陷信号，必须可见（LA §3.4）
RESPONSIBILITY_GROUPS: Final[dict[str, tuple[AttributionCategory, ...]]] = {
    "请求侧": (AttributionCategory.REQUEST_INVALID,),
    "凭证侧": (AttributionCategory.AUTH_ERROR,),
    "测试资产侧": (
        AttributionCategory.RESOURCE_NOT_FOUND,
        AttributionCategory.CONFLICT,
    ),
    "环境侧": (
        AttributionCategory.RATE_LIMITED,
        AttributionCategory.NETWORK_ERROR,
    ),
    "服务侧": (
        AttributionCategory.SERVER_ERROR,
        AttributionCategory.RESPONSE_SCHEMA_MISMATCH,
    ),
    "需人工判断": (AttributionCategory.ASSERTION_MISMATCH,),
    "平台策略侧": (AttributionCategory.BLOCKED_BY_POLICY,),
    "未归类": (AttributionCategory.UNKNOWN,),
}

#: 兜底组名。出现不认识的值时落到这里，而不是抛异常——
#: 一条归因展示错组远好过整份报告生成失败。
RESPONSIBILITY_UNCLASSIFIED: Final = "未归类"


def responsibility_of(category: AttributionCategory | str) -> str:
    """类别 → 责任归属组名。

    未知类别返回 `"未归类"`。**不抛异常**：报告是给人看的产物，
    不该因为一条历史脏数据就整份失败。
    """
    for group, members in RESPONSIBILITY_GROUPS.items():
        if category in members:
            return group
    return RESPONSIBILITY_UNCLASSIFIED


@dataclass(frozen=True, slots=True)
class AttributionDecision:
    """一条失败用例的归因结论。规则路径与 LLM 路径产出同一个形状。"""

    category: AttributionCategory
    method: Literal["rule", "llm"]
    confidence: Confidence
    rule_ref: str | None = None
    llm_model: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class FallbackAttribution(BaseModel):
    """LLM 兜底的输出契约（LA §5.3）。

    `category` 用封闭枚举：模型只能从 11 个类别里选，**不允许自由生成类别名**。
    自由生成的类别会迅速漂移成同义词（`server_error` / `backend_error` /
    `internal_error`），使按类别聚合的统计彻底失效，而且这些脏数据会沉淀进
    `attributions` 表无法回收。
    """

    model_config = {"extra": "forbid"}

    category: AttributionCategory
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    """关键信号，逐条列出。用于人工复核——归因结论会被用来排查生产问题，
    不能是黑盒。"""

    reasoning: str
    """判定理由。"""


@dataclass(frozen=True, slots=True)
class FailedCase:
    """一条待归因的失败，连同它的用例标识。

    `case_id` 刻意**不放进** `FailureContext`：规则引擎不该知道"这是哪条用例"，
    否则同一类失败在不同用例上可能得到不同结论，规则就失去可回归性。
    身份只在这一层出现，用于把结论映射回数据库行。
    """

    case_id: uuid.UUID
    context: FailureContext


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """一次批量归因的结果。

    约定：**每条输入都必须在 `decisions` 里有一条结论**。缺了就是 bug——
    失败用例在报告里没有归因，等于它的失败原因被悄悄吞掉。
    """

    decisions: dict[uuid.UUID, AttributionDecision]
    failures: tuple[NodeFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class FailureContext:
    """规则引擎与 LLM 兜底的**唯一输入**。

    只放判定需要的字段，不放 `case_id` / `run_id` / 时间戳——归因的结论
    不该依赖"哪条用例"，否则同一类失败在不同用例上会得到不同结论，
    规则就失去可回归性（LA §2.1）。

    `assertion_outcomes` 的三态是必须保留的：`failed` 表示"求值成功但不成立"
    （服务端问题），`error` 表示"无法求值"（响应不是 JSON 等）。合并两者，
    归因就再也分不清这两种（TE2）。
    """

    status: str
    """`failed` / `timeout` / `error`。`passed` 不会构造本对象。"""

    http_status: int | None
    error_kind: str | None
    response_body: str | None
    truncated: bool
    assertion_outcomes: Sequence[dict[str, Any]]
    case_confidence: str | None
    responses_schema: dict[str, Any] | None
    elapsed_ms: int | None = None

    @property
    def is_2xx(self) -> bool:
        return self.http_status is not None and 200 <= self.http_status < 300

    @property
    def is_4xx(self) -> bool:
        return self.http_status is not None and 400 <= self.http_status < 500

    @property
    def is_5xx(self) -> bool:
        return self.http_status is not None and 500 <= self.http_status < 600
