"""`case_results` 表的数据访问——**幂等写入**。

对应决策：`04-数据模型.md` §2.6、DM1（全系统最重要的约束）、`TestExecutor.md` TE5。

**⚠️ 这个文件里最重要的一行是 `on_conflict_do_update`。**

消息队列会重发、Worker 会重启、兜底轮询会补写。同一个 `(run_id, case_id)`
可能被写入多次。天真做法是"先查有没有，没有就插"：

    Worker A: 查 → 没有 → 插入 ✅
    Worker B: 查 → 没有 → 插入 ✅    ← 慢了一步，插重了

**两个都查了，都查到"没有"，然后都插了。** 中间那零点几毫秒就是竞态，
而它只在并发时出现——本地单线程测试**永远测不出来**。

正确做法是把判断交给数据库：`case_results` 上有 `UNIQUE (run_id, case_id)`，
用 `ON CONFLICT DO UPDATE` 写入。竞态窗口在数据库的原子操作里闭合。

**通用原则：能靠数据结构保证的，不要靠代码逻辑保证。**
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from testflow.storage.models import Case, CaseResult

__all__ = [
    "TERMINAL_STATUSES",
    "count_results",
    "get",
    "list_by_run",
    "mark_unfinished_as_timeout",
    "upsert_many",
]

#: `case_results.status` 的全部合法取值（`models.py` 的 CHECK 约束）。
#:
#: **它们全都是终态。** 没有"进行中"这个状态——结果行是执行**完成后**
#: 才写入的（先落库再回调，D4）。所以"已完成的用例数"就等于
#: "case_results 的行数"，不需要额外的条件过滤。
#:
#: 把这个认识写出来是因为它反直觉：一般会以为要 `status NOT IN (...)` 过滤掉
#: 中间态，于是写出一堆永远不会为真的条件。
TERMINAL_STATUSES: tuple[str, ...] = ("passed", "failed", "timeout", "error")


def get(session: Session, run_id: uuid.UUID, case_id: uuid.UUID) -> CaseResult | None:
    return session.scalars(
        select(CaseResult).where(CaseResult.run_id == run_id, CaseResult.case_id == case_id)
    ).one_or_none()


def upsert_many(session: Session, rows: Iterable[dict[str, Any]]) -> int:
    """按 `(run_id, case_id)` upsert。返回写入行数。

    用 `ON CONFLICT DO UPDATE` 而非 `DO NOTHING`：重投的任务可能带着**更新**的
    结果（例如第一次写入被中断在半路、或超时后重试成功）。`DO NOTHING` 会让
    第一次的残缺结果永久留下，而 `DO UPDATE` 让最后一次写入获胜。

    不用 `on_conflict_do_nothing` 的另一个理由：它不返回 `RETURNING` 行，
    于是调用方无法区分"写入了"和"跳过了"。

    Args:
        rows: 每个元素是 `case_results` 的列字典，**必须含 `run_id` 与 `case_id`**。
    """
    rows = list(rows)
    if not rows:
        return 0

    stmt = pg_insert(CaseResult).values(rows)
    # 冲突时更新除主键与唯一键之外的全部列——它们都来自"最新的这次执行结果"。
    updatable = {
        col: stmt.excluded[col] for col in rows[0] if col not in ("id", "run_id", "case_id")
    }
    session.execute(
        stmt.on_conflict_do_update(index_elements=["run_id", "case_id"], set_=updatable)
    )
    return len(rows)


def count_results(session: Session, run_id: uuid.UUID) -> int:
    """某 Run 已完成的用例数。**完成判定的权威依据**（`06` §6.2）。

    查这里而不是读 `runs.settled_case_count`（DM5）——后者是派生缓存，
    会因为崩溃、重投、并发而落后。缓存落后导致的最坏情况是"该推进时不推进"，
    而兜底轮询要解决的正是这类问题，它自己不能再依赖一个可能不准的值。
    """
    return (
        session.scalar(
            select(func.count()).select_from(CaseResult).where(CaseResult.run_id == run_id)
        )
        or 0
    )


def list_by_run(session: Session, run_id: uuid.UUID) -> list[CaseResult]:
    return list(session.scalars(select(CaseResult).where(CaseResult.run_id == run_id)))


def mark_unfinished_as_timeout(session: Session, run_id: uuid.UUID) -> int:
    """把"该执行却没有结果"的用例补记为 `timeout`。返回补记条数。

    兜底轮询的最后一步（`06` §6.4）。**目的是让系统永远不无限等下去**——
    回调可能全部丢失，Worker 可能被强杀，超时链路的某一层可能失效。
    到点时把缺失的结果记为 `timeout` 并当作**部分失败**推进，
    这比"永远挂在那里"有用得多：用户拿到一份标着"部分未完成"的报告，
    远比拿到一个永远转圈的任务好。

    Returns:
        补记的条数。0 表示所有用例都已有结果——此时不应调用本函数。
    """
    missing = session.scalars(
        select(Case.id)
        .outerjoin(
            CaseResult,
            (CaseResult.case_id == Case.id) & (CaseResult.run_id == Case.run_id),
        )
        .where(
            Case.run_id == run_id,
            CaseResult.id.is_(None),
            # ⚠️ **排除破坏性用例。**
            #
            # N4 会主动跳过它们（TE3：MVP 不执行、也没有授权入口）。
            # 不排除的话，兜底轮询会把它们标成 `timeout`——而那是在说
            # "我们发了请求但没等到回应"，与事实（**我们故意没发**）相反。
            #
            # 后果不只是报告失真：一份"12 条超时"的报告会让用户去查
            # 目标服务为什么慢，而真相是那些用例按设计就不该被执行。
            Case.is_destructive.is_(False),
        )
    ).all()

    if not missing:
        return 0

    session.add_all(
        CaseResult(
            run_id=run_id,
            case_id=case_id,
            status="timeout",
            error_kind="executor_stall",
            assertion_outcomes=[],
        )
        for case_id in missing
    )
    session.flush()
    return len(missing)


def upsert_all(session: Session, results: Sequence[CaseResult]) -> int:
    """便捷包装：把 ORM 对象转成 upsert 的行字典。"""
    return upsert_many(
        session,
        (
            {
                column.name: getattr(r, column.name)
                for column in CaseResult.__table__.columns
                if column.name != "id"  # 主键交给数据库的 uuidv7()
            }
            for r in results
        ),
    )
