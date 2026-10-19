"""接口契约——**以这里为准，`openapi.json` 是它的产物**（API1 / ADR-0023）。

对应决策：`05-API契约.md` §4。

## 为什么放在一个文件里

这些模型是**给别人看的**：前端的类型定义、CLI 的解析、将来的外部集成，
都从这里派生。分散在各自的 `routes/*.py` 里，看"这个服务对外提供什么"
就得翻三个文件——而这件事是外部消费者最常问的。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from testflow.domain.attribution import AttributionCategory

__all__ = [
    "AttributionItem",
    "AttributionListResponse",
    "CaseListItem",
    "CaseListResponse",
    "CreateRunRequest",
    "DecisionApplied",
    "DecisionItem",
    "DecisionRequest",
    "DecisionResponse",
    "HealthResponse",
    "HumanVerdictRequest",
    "ReportResponse",
    "RunCreatedResponse",
    "RunStatusResponse",
    "SpecCreatedResponse",
    "WarningItem",
    "WakeResponse",
]


class WarningItem(BaseModel):
    """解析期间的降级提示（`05` §4.1）。

    **不可省略**：`SpecParser` 的降级情形（相对 URL、无 `servers`、`$ref` 超深）
    必须在这里告知用户——否则用户后续看到 `blocked_by_policy` 归因时，
    会以为是平台故障，而真正的原因在解析阶段就被掩盖了。
    """

    code: str = Field(description="机器可读的提示码，如 `relative_server_url`")
    detail: str = Field(description="给人读的说明")


class SpecCreatedResponse(BaseModel):
    spec_id: str
    fingerprint: str
    openapi_version: str
    operation_count: int
    declared_targets: list[str] = Field(
        description="从 servers 提取的出站目标，值为 `scheme://host:port`。为空意味着"
        "所有请求都会被 Guardrail 拒绝。"
    )
    reused: bool = Field(description="指纹命中已有 Spec 时为 true——**没有重新解析、没有重复落库**")
    warnings: list[WarningItem] = Field(default_factory=list)


class CreateRunRequest(BaseModel):
    spec_id: str


class RunCreatedResponse(BaseModel):
    run_id: str
    phase: str


class RunStatusResponse(BaseModel):
    run_id: str
    phase: str
    spec_fingerprint: str
    expected_case_count: int
    settled_case_count: int
    failure_reason: str | None = None
    #: 仅在中断状态下出现（`05` §4.3）。**前端据此决定"要不要显示确认界面"**，
    #: 而不是去猜 `phase` 字符串的语义。
    interrupt: dict[str, Any] | None = None


class CaseListItem(BaseModel):
    """确认界面里的一条用例。

    ## 为什么把 `assertions` 也带出来

    确认界面的**全部意义**就是让人看"这条用例到底要断言什么"。只给名字与
    维度的话，用户只能凭标题猜——而标题是自然语言，断言是结构化数据，
    两者可以不一致（模型写了个像样的标题配一条错的断言）。

    代价是响应体更大，而一页只有 50 条、每条几条断言——可以接受。

    ⚠️ `request_headers` / `request_body` **刻意不带**：前者可能含凭证
    （SR-6），后者的信息量对"要不要批准"这个判断几乎没有帮助。
    需要看请求体的场景应当通过 `GET /api/runs/{id}/report` 的失败清单
    去定位具体问题，而不是把 2000 条用例的请求体塞进列表接口。
    """

    case_id: str
    name: str
    category: Literal["normal", "abnormal", "boundary"]
    dimension: str
    #: `low` 表示期望值有一部分是**推断**的（`TestCaseGen` §7）。
    #: `None` 是 M1 生成的历史数据，语义是"未标注"——**不等于高质量**。
    confidence: Literal["high", "low"] | None = None
    review_status: Literal["pending", "approved", "rejected"]
    #: 破坏性方法（DELETE/PUT/PATCH）。它们**永远不会被执行**（TE3）：
    #: 界面上要让用户看得见这件事，否则"批准了却没跑"看着像 bug。
    is_destructive: bool
    request_method: str
    request_path: str
    assertions: list[dict[str, Any]] = Field(default_factory=list)


class CaseListResponse(BaseModel):
    """用例列表（分页）。

    `total` 与 `items` 的长度**不是一回事**：前者是全部用例数，
    后者是这一页的数量。少了 `total`，前端就没法显示"共 N 条"
    或做"最后一页"的判断。
    """

    run_id: str
    total: int
    limit: int
    offset: int
    items: list[CaseListItem] = Field(default_factory=list)


class DecisionItem(BaseModel):
    """一条人工决策。

    `decision` 用 `Literal` 而不是 `str`：拼错的值（`"approved"`、
    `"Approve"`）必须在**边界上**被拒绝，而不是落库之后变成一个
    `CHECK` 约束报错——那时用户看到的是 500，而真正的原因是他拼错了。
    """

    model_config = {"extra": "forbid"}

    case_id: str
    decision: Literal["approve", "reject"]


class DecisionRequest(BaseModel):
    """IP-1 的决策提交（`05` §4.5）。

    ## 为什么**必须完整**（所有 pending 用例都要出现）

    部分提交会让图带着"还有未决用例"的状态恢复，而 IP-1 的语义是
    "人已经看过了"。允许部分提交，就等于允许"没被提到的用例保持 pending"
    ——而它们**既不会被执行，也不会出现在报告里**，用户以为默认是放行。
    """

    #: ★ **`extra="forbid"`，与其他请求模型不同。**
    #:
    #: 默认行为是**静默忽略**未知字段。对普通接口那只是宽容；
    #: 对这里它是一个**误导性的成功**：有人写
    #: `{"case_id": ..., "decision": "approve", "destructive_authorized": true}`
    #: 会拿到 200，并合理地以为自己授权了破坏性请求——而那个字段根本不存在
    #: （ADR-0021：MVP 不提供授权入口），于是那几条用例仍然不会被执行。
    #:
    #: 报错比沉默好：它把"你想要的这个功能不存在"变成一句能读懂的话。
    model_config = {"extra": "forbid"}

    decisions: list[DecisionItem]


class DecisionApplied(BaseModel):
    """本次实际生效的条数。**回显计数而不是只说"成功"。**

    用户提交了 118 条的决策，界面上回一个 `{"ok": true}` 是没用的——
    他要确认的是"我批的 40 条真的批了"。数字对不上时立刻能发现。
    """

    approved: int
    rejected: int


class DecisionResponse(BaseModel):
    """决策已生效。

    **200 而不是 202**（M2 的决定）：决策落库是同步且权威的——返回之前
    `review_status` 已经提交，用户的决定**已经生效**。202 的"已受理、
    尚未处理"语义反而不对：resume 的投递是内部细节，且失败可恢复
    （投递幂等 + 兜底轮询），不该让它把"已生效"降格成"已受理"。
    """

    run_id: str
    applied: DecisionApplied


class ReportResponse(BaseModel):
    """报告的 JSON 形式。

    `Accept: text/markdown` 时返回 `rendered` 那一段 Markdown 原文
    （快照，非重渲染——RA4）。
    """

    run_id: str
    content: dict[str, Any]


class WakeResponse(BaseModel):
    """内部唤醒端点的响应（`05` §4.7）。

    两个取值，**刻意用响应体而不是状态码区分**：

    * `pending` —— 还有用例没跑完，Gateway 什么都没做
    * `resumed` —— 全部到齐，已经投递了推进指令

    用 202/200 区分的话，Worker 侧看不出"到底有没有生效"——
    而那是排查唤醒链路时第一个要问的问题。
    """

    status: Literal["pending", "resumed"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessCheck(BaseModel):
    """一项依赖的连通性。`detail` 只放异常类型，不放异常消息。"""

    name: str
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """`GET /readyz` 的响应。

    与 `/healthz` 的分工：
    `/healthz` 只回答"进程活着"（不碰任何依赖），
    `/readyz` 回答"现在能不能干活"（会碰 PG 与 Redis）。
    """

    status: Literal["ok", "degraded"]
    checks: list[ReadinessCheck]


# ---------------------------------------------------------------- 归因（M3）


class AttributionItem(BaseModel):
    """一条失败归因。**机器结论与人工表态在同一条里**。

    两者并存而不是分成两个接口：用户看归因时关心的就是"机器判了什么、
    我同意不同意"——拆开会让他先拉一次列表、再拉一次表态。
    """

    attribution_id: str
    case_id: str
    case_name: str
    dimension: str
    status: str
    http_status: int | None = None
    category: str
    method: Literal["rule", "llm"]
    rule_ref: str | None = None
    llm_model: str | None = None
    confidence: str | None = None
    evidence: dict[str, Any] | None = None
    human_verdict: Literal["agree", "disagree"] | None = None
    human_category: str | None = None


class AttributionListResponse(BaseModel):
    """归因列表（分页）。分页约定与 `CaseListResponse` 一致。"""

    run_id: str
    total: int
    limit: int
    offset: int
    items: list[AttributionItem] = Field(default_factory=list)


class HumanVerdictRequest(BaseModel):
    """人工对一条归因表态（LA §8）。

    `human_category` 的**必填性取决于 verdict**，所以不在这里用 Pydantic
    校验——那样返回的是 FastAPI 默认错误体，而契约要求 `problem+json`
    与可区分的 `code`（LA6）。校验放在路由里。
    """

    model_config = {"extra": "forbid"}

    verdict: Literal["agree", "disagree"]
    human_category: AttributionCategory | None = None
