"""`run_operations` 表的数据访问——**N2 幂等语义的唯一载体**。

对应决策：`04-数据模型.md` §2.4 / DM4、ADR-0011。

**这张表存在的唯一理由**：LLM 输出是非确定性的。同一个 Operation 重跑会得到
不同的用例，所以 N2 的幂等不能是"重放"（那会静默替换掉用户已经确认过的用例），
只能是"**已完成则跳过**"。而"是否已完成"必须持久化——它就存在这里。

对比 N1（SpecParser）：那是真幂等，同输入必得同输出，重跑无害。
**两个节点的幂等语义不同，这是本项目里最容易搞混的一点。**
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import RunOperation

__all__ = ["list_by_run", "mark_failed", "mark_succeeded", "pending_operation_ids", "status_of"]

#: 需要（重新）生成的两种状态。
#:
#: `failed` 也在此列：失败通常是瞬时的（限流、网络），重跑一次是合理的。
#: 而 `succeeded` 一旦到达就不再重试——这正是 DM4 的语义。
REGENERABLE: tuple[str, ...] = ("pending", "failed")


def status_of(session: Session, run_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """一次查出该 Run 下全部 operation 的生成状态。

    返回 dict 而非逐条查询：N2 要遍历所有 Operation 做跳过判断，
    逐条查会变成 N+1。
    """
    return {
        row.operation_id: row.gen_status
        for row in session.scalars(select(RunOperation).where(RunOperation.run_id == run_id))
    }


def pending_operation_ids(
    session: Session, run_id: uuid.UUID, candidates: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """从候选里筛出**需要生成**的（`pending` / `failed` / 尚无记录）。

    尚无记录视为待生成是对的：行是在第一次尝试时才创建的，
    而"没有记录"与"记录了 pending"在这里的语义相同。
    """
    if not candidates:
        return []
    existing = status_of(session, run_id)
    return [op_id for op_id in candidates if existing.get(op_id, "pending") in REGENERABLE]


def _ensure(session: Session, run_id: uuid.UUID, operation_id: uuid.UUID) -> RunOperation:
    row = session.get(RunOperation, (run_id, operation_id))
    if row is None:
        row = RunOperation(run_id=run_id, operation_id=operation_id, gen_status="pending")
        session.add(row)
        session.flush()
    return row


def mark_succeeded(
    session: Session, run_id: uuid.UUID, operation_id: uuid.UUID, *, case_count: int
) -> None:
    """标记为生成成功。**此后重跑该 Run 不会再生成这个 Operation 的用例。**"""
    row = _ensure(session, run_id, operation_id)
    row.gen_status = "succeeded"
    row.case_count = case_count
    row.error = None
    session.flush()


def mark_failed(
    session: Session, run_id: uuid.UUID, operation_id: uuid.UUID, *, error: str
) -> None:
    """标记为生成失败。

    **失败不阻塞其他 Operation**（`TestCaseGen.md` §10）：一个接口的 schema 特别
    难处理，不该让整份 Spec 的生成全部作废。失败会在报告里作为"未覆盖的接口"呈现。
    """
    row = _ensure(session, run_id, operation_id)
    row.gen_status = "failed"
    row.error = error
    session.flush()


def list_by_run(session: Session, run_id: uuid.UUID) -> list[RunOperation]:
    return list(session.scalars(select(RunOperation).where(RunOperation.run_id == run_id)))
