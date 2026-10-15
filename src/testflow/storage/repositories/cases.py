"""`cases` 表的数据访问。

对应决策：`04-数据模型.md` §2.5、`06-Agent编排.md` §5.1（IP-1 决策落库）。

**M1 没有 IP-1 人工确认门**，但 `review_status` 仍要被写成 `approved`——
因为 N3 的路由依据是 `approved_case_ids` 是否为空（G4），而不是"有没有人审过"。
M2 把门插进图之后，这里的 `approve_all_pending` 会换成"读人工决策"，
而下游的路由逻辑一行都不用改。**这正是让状态字段先于功能存在的价值。**
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from testflow.storage.models import Case

__all__ = [
    "approve_all_pending",
    "count_by_run",
    "get",
    "insert_many",
    "list_by_run",
    "set_review_status",
]


def insert_many(session: Session, cases: Sequence[Case]) -> int:
    session.add_all(cases)
    session.flush()
    return len(cases)


def get(session: Session, case_id: uuid.UUID) -> Case | None:
    return session.get(Case, case_id)


def list_by_run(
    session: Session, run_id: uuid.UUID, *, review_status: str | None = None
) -> list[Case]:
    """列出某 Run 的用例，可按审核状态过滤。

    带 `review_status` 的查询走 `ix_cases_run_review`——N4 投递前取
    "已批准"的集合是热路径。
    """
    stmt = select(Case).where(Case.run_id == run_id)
    if review_status is not None:
        stmt = stmt.where(Case.review_status == review_status)
    # 稳定排序：用例的展示顺序不该随机变化，否则两次运行的报告难以对比。
    return list(session.scalars(stmt.order_by(Case.created_at, Case.name)))


def count_by_run(session: Session, run_id: uuid.UUID) -> int:
    return session.scalar(select(func.count()).select_from(Case).where(Case.run_id == run_id)) or 0


def set_review_status(session: Session, case_ids: Sequence[uuid.UUID], status: str) -> int:
    """批量设置审核状态。返回受影响行数。

    用一条 `UPDATE ... WHERE id IN (...)` 而不是逐个 `session.get` 再赋值：
    逐个读改写会（a）产生 N 次往返，（b）在并发下丢失更新——两个请求
    分别改同一条用例时，后写的会静默覆盖先写的。
    """
    if not case_ids:
        return 0
    # `Session.execute` 的静态返回类型是 `Result`，但 DML 语句实际返回的是
    # `CursorResult`（它才有 `rowcount`）。这里显式转换而不是 `# type: ignore`：
    # 转换写明了"我们知道的比类型标注更多"，而 ignore 只是让检查闭嘴。
    result = cast(
        "CursorResult[Any]",
        session.execute(
            sa_update(Case)
            .where(Case.id.in_(case_ids))
            .values(review_status=status, reviewed_at=func.now()),
            # 同 `runs.set_phase`：不 `synchronize_session` 的话，改完再读会拿到旧值。
            execution_options={"synchronize_session": "fetch"},
        ),
    )
    return result.rowcount or 0


def approve_all_pending(session: Session, run_id: uuid.UUID) -> int:
    """把某 Run 下全部待审用例标为已批准。返回条数。

    **这是 M1 绕过 IP-1 的方式**——不是"加了一个可以打开的自动批准开关"，
    而是"这个门此刻还不存在"。路线图 Q1 警告过前者的风险：
    可关闭的安全开关会被误设、被复制到生产、被"临时"关掉后遗忘。
    一个根本不存在的东西，没有会被忘记关掉的问题。

    M2 把 IP-1 插入图之后，本函数被 `apply_decisions` 的"读人工决策"取代。
    """
    return set_review_status(
        session,
        list(
            session.scalars(
                select(Case.id).where(Case.run_id == run_id, Case.review_status == "pending")
            )
        ),
        "approved",
    )
