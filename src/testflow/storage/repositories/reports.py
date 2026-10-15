"""`reports` 表的数据访问。

对应决策：`04-数据模型.md` §2.8、`03/ReportAgent.md` RA4。

**`rendered` 是快照，不是视图。** 写入之后不再重渲染，模板改了也不回填。
理由：报告会被引用（评审材料、问题工单、聊天记录里的截图）。三个月前那份
报告应当保持原样，而不是因为模板升级被追溯性地改写——那会让"当时看到的"
和"现在查到的"对不上，而这正是引用报告的人最不能接受的事。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import Report

__all__ = ["get_by_run", "upsert"]


def get_by_run(session: Session, run_id: uuid.UUID) -> Report | None:
    return session.scalars(select(Report).where(Report.run_id == run_id)).one_or_none()


def upsert(
    session: Session, *, run_id: uuid.UUID, content: dict[str, Any], rendered: str
) -> Report:
    """写入或覆盖某 Run 的报告。

    `reports.run_id` 上有 UNIQUE 约束（一个 Run 一份报告），因此这里是
    "先查再改"——与 `case_results` 的并发 upsert 不同，N7 是图中的单点，
    一个 Run 在同一时刻只可能有一个 N7 在跑（由图 + per-Run 锁保证）。
    真要在这里防并发，会引入一个本不存在的复杂度。
    """
    existing = get_by_run(session, run_id)
    if existing is not None:
        existing.content = content
        existing.rendered = rendered
        session.flush()
        return existing

    report = Report(run_id=run_id, content=content, rendered=rendered)
    session.add(report)
    session.flush()
    return report
