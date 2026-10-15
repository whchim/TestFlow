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

__all__ = [
    "CreateRunRequest",
    "HealthResponse",
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
