"""`audit_logs` 表的数据访问。

对应决策：`04-数据模型.md` §2.9、`03/Guardrail.md` §10 / GR6。

**`allow` 与 `deny` 都要记。** 只记拒绝，就无法回答"这个 Run 到底访问了哪些
host"——而那正是安全复盘时最有价值的问题。一个只有拒绝记录的审计表，
在出事之后帮不上忙：你能看到是谁被拦了，看不到谁进来了。

⚠️ **调用方负责脱敏。** 本模块不做清洗——它只写它拿到的东西。脱敏在
`guardrail/audit.py` 完成，且发生在**构造记录的时候**，而不是"写日志之前
记得擦一下"。理由同 EN5：调用方会忘记，而审计一旦落库就难以彻底清除。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import AuditLog

__all__ = ["list_by_run", "record"]


def record(
    session: Session,
    *,
    direction: str,
    target_host: str,
    decision: str,
    target_url: str | None = None,
    deny_reason: str | None = None,
    run_id: uuid.UUID | None = None,
    request_digest: str | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """写一条出站审计。

    Args:
        direction: `llm` 或 `target`——两条出站路径都要记。
        decision: `allow` 或 `deny`。**没有第三种取值**：请求要么发出，要么不发出。
        target_url: **已剥离 query 中敏感值**的 URL。由调用方负责——见模块 docstring。

    Note:
        `run_id` 可为空：非 Run 关联的出站（如启动期的连通性检查）也要记。
        这正是 `audit_logs.run_id` 用 `ON DELETE SET NULL` 而非 CASCADE 的原因：
        Run 被删除后，"曾经发生过这次访问"仍应保留。
    """
    log = AuditLog(
        direction=direction,
        target_host=target_host,
        target_url=target_url,
        decision=decision,
        deny_reason=deny_reason,
        run_id=run_id,
        request_digest=request_digest,
        trace_id=trace_id,
    )
    session.add(log)
    session.flush()
    return log


def list_by_run(session: Session, run_id: uuid.UUID) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.run_id == run_id)))
