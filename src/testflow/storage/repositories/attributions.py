"""`attributions` 表的数据访问。

对应决策：`04-数据模型.md` §2.7、`03-模块详细设计/LogAnalyzer.md` §7 / §8。

## 为什么 upsert 只更新"机器字段"

归因可能被重复计算（Run 恢复、节点重投）。重算应当覆盖上一次的**机器结论**，
但**绝不能抹掉人的判断**（`human_verdict` / `human_category`）。

这两类字段的生命周期不同：

- 机器字段：每次分析都重新产生，最后一次获胜
- 人工字段：由人给出，只有人能改

把它们混在一个 `DO UPDATE` 里，表现是"重跑一次，人工纠错全没了"——
而它不会报错，只会在某天有人发现纠错数据对不上时才知道。

## 唯一约束就是幂等性

`UNIQUE (run_id, case_id)` 让"同一条失败的用例只可能有一条归因"由数据库
保证，而不是靠调用方记得先查。这与 `case_results` 是同一个原则
（DM1：能靠数据结构保证的，不要靠代码逻辑保证）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Literal, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from testflow.storage.models import Attribution

__all__ = [
    "count_by_run",
    "delete_for_cases",
    "get_by_id",
    "get_by_run_case",
    "list_by_run",
    "list_page",
    "set_human_verdict",
    "upsert_many",
]

#: upsert 冲突时**允许**更新的列。
#:
#: `human_verdict` / `human_category` **不在这里**——见模块文档。
_MACHINE_COLUMNS: tuple[str, ...] = (
    "category",
    "method",
    "rule_ref",
    "llm_model",
    "confidence",
    "evidence",
)


def upsert_many(session: Session, rows: Iterable[dict[str, Any]]) -> int:
    """按 `(run_id, case_id)` upsert。返回写入行数。

    Args:
        rows: 每个元素是 `attributions` 的列字典，**必须含 `run_id` 与 `case_id`**。
    """
    rows = list(rows)
    if not rows:
        return 0

    stmt = pg_insert(Attribution).values(rows)
    updatable = {col: stmt.excluded[col] for col in _MACHINE_COLUMNS if col in rows[0]}
    session.execute(
        stmt.on_conflict_do_update(index_elements=["run_id", "case_id"], set_=updatable)
    )
    session.flush()
    return len(rows)


def get_by_id(session: Session, attribution_id: uuid.UUID) -> Attribution | None:
    return session.get(Attribution, attribution_id)


def get_by_run_case(session: Session, run_id: uuid.UUID, case_id: uuid.UUID) -> Attribution | None:
    return session.scalars(
        select(Attribution).where(Attribution.run_id == run_id, Attribution.case_id == case_id)
    ).one_or_none()


def list_by_run(session: Session, run_id: uuid.UUID) -> list[Attribution]:
    return list(
        session.scalars(
            select(Attribution)
            .where(Attribution.run_id == run_id)
            # `id` 是 uuidv7（时间有序）。同一事务里插入的行共享同一个
            # `now()`，只按 `created_at` 排会得到不稳定顺序。
            .order_by(Attribution.created_at, Attribution.id)
        )
    )


def list_page(session: Session, run_id: uuid.UUID, *, limit: int, offset: int) -> list[Attribution]:
    return list(
        session.scalars(
            select(Attribution)
            .where(Attribution.run_id == run_id)
            .order_by(Attribution.created_at, Attribution.id)
            .limit(limit)
            .offset(offset)
        )
    )


def count_by_run(session: Session, run_id: uuid.UUID) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(Attribution).where(Attribution.run_id == run_id)
        )
        or 0
    )


def set_human_verdict(
    session: Session,
    attribution_id: uuid.UUID,
    *,
    verdict: Literal["agree", "disagree"],
    human_category: str | None,
) -> Attribution | None:
    """记录人工表态。返回更新后的行；找不到返回 `None`。

    **幂等**：重复提交覆盖上一次的表态（人可以改主意）。
    """
    row = session.get(Attribution, attribution_id)
    if row is None:
        return None
    row.human_verdict = verdict
    row.human_category = human_category
    session.flush()
    return row


def delete_for_cases(session: Session, run_id: uuid.UUID, case_ids: Sequence[uuid.UUID]) -> int:
    """删除指定用例的归因。用于"重算后这些用例已经不失败了"的清理。

    不清理的后果：一条用例后来通过了，但归因表里还留着它上次失败的原因，
    报告会显示"失败 0 条，归因 3 条"——两个数字永远对不上。
    """
    if not case_ids:
        return 0
    # `Session.execute` 的静态返回类型是 `Result`，而 DML 实际返回 `CursorResult`
    # （它才有 `rowcount`）。显式转换而不是 `# type: ignore`——前者写明了
    # "我们知道的比类型标注更多"（同 `cases.set_review_status`）。
    result = cast(
        "CursorResult[Any]",
        session.execute(
            delete(Attribution).where(
                Attribution.run_id == run_id, Attribution.case_id.in_(list(case_ids))
            )
        ),
    )
    session.flush()
    return int(result.rowcount or 0)
