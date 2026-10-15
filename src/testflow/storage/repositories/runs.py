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

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from testflow.domain.graph import RunPhase
from testflow.storage.models import Run

__all__ = ["create", "get", "list_stalled", "set_phase", "touch"]


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
