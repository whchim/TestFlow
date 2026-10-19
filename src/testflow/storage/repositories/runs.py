"""`runs` 表的数据访问。

对应决策：`04-数据模型.md` §2.3、DM5（派生缓存）、`06-Agent编排.md` §6.4（兜底轮询）。

⚠️ **本模块存在的主要理由是 `updated_at`。**

`runs.updated_at` 只有 `server_default=func.now()`，**没有 `onupdate`**。
这是刻意的——`onupdate` 会在任何一次 `UPDATE` 上触发，包括那些与"Run 是否
还在推进"无关的写入。但代价是：**改 `phase` 的地方必须自己更新时间戳，
忘了不会有任何报错。**

而兜底轮询的判断依据正是 `updated_at` 是否过期。忘了更新 → 时间戳永远停在
创建时刻 → 轮询把它当成"卡住了" → 在一个正常运行的 Run 上强行推进。

所以 `set_phase` 把两件事绑在一起：**改 phase 的动作只有一个入口，
而那个入口一定会更新时间戳。** 不让调用方有机会只做一半。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from testflow.domain.graph import RunPhase
from testflow.storage.models import Run

__all__ = [
    "create",
    "delete_older_than",
    "get",
    "list_stalled",
    "set_case_counts",
    "set_phase",
    "touch",
]


def create(session: Session, *, spec_id: uuid.UUID, spec_fingerprint: str) -> Run:
    """新建一个 Run。

    `spec_fingerprint` 与 `spec_id` 并存不是冗余：前者是**发起时刻的事实快照**，
    用于 S2 的用例失效判定；后者是外键，用于引用完整性。Spec 表不可变，
    所以两者当前总是相等——但"当前总是相等"和"永远相等"是两回事。
    """
    run = Run(spec_id=spec_id, spec_fingerprint=spec_fingerprint, phase=RunPhase.INIT)
    session.add(run)
    session.flush()
    return run


def get(session: Session, run_id: uuid.UUID) -> Run | None:
    return session.get(Run, run_id)


def touch(session: Session, run_id: uuid.UUID) -> None:
    """只更新时间戳，不改 phase。

    用于那些"Run 在动，但阶段没变"的场合——例如 N4 投递了一批任务。
    若这里不更新，一个正在投递 2000 条用例的 Run 会被轮询误判为停滞。
    """
    session.execute(
        sa_update(Run).where(Run.id == run_id).values(updated_at=func.now()),
        execution_options={"synchronize_session": "fetch"},
    )


def set_phase(
    session: Session,
    run_id: uuid.UUID,
    phase: RunPhase,
    *,
    failure_reason: str | None = None,
) -> None:
    """推进 Run 的阶段。**这是改 `phase` 的唯一入口。**

    终态会顺带写 `finished_at`；非终态则清空它，以免"回到运行中"的 Run
    还带着一个结束时刻（那只会让报告里的耗时算错）。

    ⚠️ 与 `touch()` 同理：`updated_at` 在这里一并更新。兜底轮询依赖它。
    """
    values: dict[str, object] = {
        "phase": phase,
        "updated_at": func.now(),
        "failure_reason": failure_reason,
        "finished_at": func.now() if phase.is_terminal else None,
    }
    session.execute(
        sa_update(Run).where(Run.id == run_id).values(**values),
        # `synchronize_session="fetch"` 让已在身份映射里的 `Run` 对象**跟着失效**。
        # 少了它，`set_phase` 之后 `session.get(Run, id)` 会返回缓存里的**旧 phase**——
        # 而这类错误不会报错，只会让"改完立刻读"的代码读到幻觉。
        execution_options={"synchronize_session": "fetch"},
    )


def list_stalled(
    session: Session, *, phase: RunPhase, stale_after_s: float, limit: int = 100
) -> list[Run]:
    """找出停滞在某个阶段的 Run。兜底轮询的扫描入口（`06` §6.4）。

    走 `ix_runs_phase_updated_at`（`phase, updated_at` 联合索引）——
    那正是这个索引存在的理由，而不是先按 `updated_at` 全表扫再过滤。

    Args:
        stale_after_s: 距上次更新超过这么多秒才算停滞。调用方传
            `http_timeout_s × executor_stall_multiplier`。

    Note:
        阈值在 Python 侧算出具体时刻再进 SQL，而不是构造 `interval '30 seconds'`
        字符串——后者要拼 SQL 片段，是把用户配置直接送进 SQL 的入口。
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=stale_after_s)
    return list(
        session.scalars(
            select(Run)
            .where(Run.phase == phase, Run.updated_at < cutoff)
            .order_by(Run.updated_at)
            .limit(limit)
        )
    )


def delete_older_than(
    session: Session,
    *,
    cutoff: datetime.datetime,
    batch_size: int = 500,
) -> int:
    """删除 `created_at < cutoff` 的 Run，返回删除行数（`04` §5 / `10` §7）。

    ## 为什么按批删而不是一条 DELETE

    一条 `DELETE FROM runs WHERE created_at < ...` 会级联删除 cases /
    results / attributions / reports / run_operations / llm_usage，
    在大积压时生成一条极大的语句（WAL、内存、锁表）。按主键分批
    （默认 500）把单条语句的规模限住。

    ⚠️ **分批不等于分事务。** 本函数从头到尾在一笔事务里；锁要到调用方
    commit 时才释放。清理任务在结束时 commit 一次。若将来积压大到影响
    在线请求，正确做法是把"取一批 → commit"这一层上移到任务里循环调用，
    而不是在这一层偷偷 commit（仓库层不该决定事务边界）。
    **中断后重跑天然安全**：下一批还是从"最早且超期"的那条开始。

    ## 级联由数据库负责

    子表外键都是 `ON DELETE CASCADE`（`04` §2），应用侧不需要逐表删。
    唯一例外是 `audit_logs.run_id`：它是 `ON DELETE SET NULL`——**审计记录
    不该因为业务数据过期而消失**，只是失去 Run 关联（§5）。

    ## 为什么返回行数

    清理任务必须"记录每次删除的数量以便审计"（§5）。返回 0 也是信息：
    它证明任务跑过、且没有可删的东西，而不是"任务没跑"。
    """
    total = 0
    while True:
        ids = list(
            session.scalars(
                select(Run.id)
                .where(Run.created_at < cutoff)
                .order_by(Run.created_at)
                .limit(batch_size)
            )
        )
        if not ids:
            break

        result = session.execute(
            sa_delete(Run).where(Run.id.in_(ids)),
            # ORM 不需要同步身份映射：这些行在清理任务里不会被再次读取。
            execution_options={"synchronize_session": False},
        )
        session.flush()
        # `Result.rowcount` 在 SQLAlchemy 的 CursorResult 上存在，但类型标注
        # 挂在 `CursorResult` 上而不是泛化的 `Result`。getattr 比 cast 更直接：
        # 这里要的就是"驱动报了几行"，拿不到时按 0 处理不会掩盖任何正常路径。
        deleted = int(getattr(result, "rowcount", 0) or 0)
        total += deleted

        # 极端情况下（行刚被并发删掉）rowcount 可能为 0，但下一轮仍会
        # 查到同一批 id。显式退出，避免无限循环。
        if deleted == 0:
            break
    return total


def set_case_counts(
    session: Session,
    run_id: uuid.UUID,
    *,
    expected: int | None = None,
    settled: int | None = None,
) -> None:
    """更新两个**派生缓存**（DM5）。**仅供展示。**

    `expected_case_count` 的口径是"**应当有结果**的用例数"，与
    `cases_repo.count_dispatchable` 一致——不是"生成了多少条"。
    用生成数当分母，一个被拒绝或破坏性的用例就会让 `settled` 永远追不上它。

    ## ⚠️ 这不是任何正确性判断的依据

    DM5 明确要求"完成判定一律 `SELECT count(*)` 查 `case_results`"，
    这两个字段只服务 `GET /api/runs/{id}` 与 CLI 的展示。

    第一版**没有任何地方写它们**，于是接口永远返回 `0/0`——
    而网关里有一处完成判定**恰好读了它**（见 `gateway/internal.py`），
    于是"缓存没人维护"和"有人拿缓存做判断"这两件事叠在一起，
    把回调唤醒整条路径变成了空转。**两个各自看起来无害的决定，
    合起来是一个功能性的失效**——这也是 DM5 为什么把话说到那个份上。

    传 `None` 表示"这次不动这个字段"，所以调用方可以只更新其中一个。
    """
    values: dict[str, object] = {"updated_at": func.now()}
    if expected is not None:
        values["expected_case_count"] = expected
    if settled is not None:
        values["settled_case_count"] = settled
    session.execute(
        sa_update(Run).where(Run.id == run_id).values(**values),
        execution_options={"synchronize_session": "fetch"},
    )
