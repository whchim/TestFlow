"""`cases` 表的数据访问。

对应决策：`04-数据模型.md` §2.5、`06-Agent编排.md` §5.1（IP-1 决策落库）。

`review_status` 是 IP-1 的落点：批准 / 拒绝由决策接口写进这里，N3
只**读**"已批准"的集合（`approved_case_ids`）。本模块提供批量写
`set_review_status`，但**不提供"无条件全批"**——那是 M1 没有人工确认门
时的临时做法，已随 M2 移除（路线图 Q1：临时即永久）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import case as sa_case
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from testflow.storage.models import Case

__all__ = [
    "count_by_run",
    "count_dispatchable",
    "get",
    "insert_many",
    "list_by_run",
    "list_page",
    "set_review_status",
]

#: 置信度的展示排序：**`low` 在前，`NULL` 居中，`high` 在后**。
#:
#: 这不是"排序好看"，而是把人工确认的注意力引到最该看的地方：
#: 一条低置信度用例意味着它的期望值有一部分是**推断**出来的
#: （`TestCaseGen` §7 的六触发），而"推断"正是会出现"期望值本身写错了"
#: 的那个类别。高置信度的那批看着顺眼，不需要人逐条读。
#:
#: `NULL` 排在中间，是因为它的含义是"不确定"——M1 生成的用例没有这个字段
#: （那时还没有置信度标注），把历史数据当成"高置信"是错的，
#: 当成"低置信"又会把一整批老用例灌进待细看的那一堆。
_CONFIDENCE_RANK = sa_case(
    (Case.confidence == "low", 0),
    (Case.confidence.is_(None), 1),
    else_=2,
)


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


def list_page(session: Session, run_id: uuid.UUID, *, limit: int, offset: int) -> list[Case]:
    """按置信度排序分页取用例。**人工确认界面用这个。**

    排序与 `list_by_run` 不同，这是刻意的：那个服务于内部遍历
    （报告、投递），要的是"稳定顺序"，所以按 `created_at`；
    这个服务于**人看**，要的是"最该被检查的排在前面"。

    两个排序共用一个函数、用一个参数切换，会让两边的意图都变模糊
    ——所以它们是两个函数。
    """
    stmt = (
        select(Case)
        .where(Case.run_id == run_id)
        # 第二、三个排序键保证**分页稳定**：置信度相同的用例之间必须有确定
        # 顺序，否则翻页时同一条用例可能出现在两页里、也可能一条都不出现。
        # 这类 bug 只在数据量超过一页时出现，而那时已经很难联想到排序。
        .order_by(_CONFIDENCE_RANK, Case.created_at, Case.name)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def count_by_run(session: Session, run_id: uuid.UUID, *, review_status: str | None = None) -> int:
    """某 Run 的用例数。可按审核状态过滤（生成采纳率要 `approved`）。"""
    stmt = select(func.count()).select_from(Case).where(Case.run_id == run_id)
    if review_status is not None:
        stmt = stmt.where(Case.review_status == review_status)
    return session.scalar(stmt) or 0


def count_dispatchable(session: Session, run_id: uuid.UUID) -> int:
    """**应当有结果**的用例数——也就是 N4 真正会投递的那些。

    口径与 `dispatch_cases` **逐条对齐**，两边必须同时改：

        review_status == 'approved'   仅已批准的会进 approved_case_ids
        is_destructive is False       TE3 主动跳过，永不投递

    ## 为什么需要它，以及为什么不能用 `count_by_run`

    完成判定问的是"**该回来的结果都回来了吗**"（`gateway/internal.py`）。
    用全部用例数当分母的话，一个被拒绝的用例会让 `settled` 永远追不上
    `expected`——于是"所有结果到齐了"这个条件**永远不会成立**，
    回调唤醒全部变成空转，Run 只能等兜底轮询。

    而那个 bug 的表现是**慢**，不是错：Run 最终仍然会完成，只是每次都
    白等一个轮询周期。没有人会为一个"有点慢"的功能开一张工单。

    这个数与 `count_results` 配合，构成 DM5 要求的"以 `SELECT count(*)`
    为准"的完成判定——两个数都来自权威表，不读任何派生缓存。
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(Case)
            .where(
                Case.run_id == run_id,
                Case.review_status == "approved",
                Case.is_destructive.is_(False),
            )
        )
        or 0
    )


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
