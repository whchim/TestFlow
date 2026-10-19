"""兜底轮询。**回调全部丢失时的唯一保障。**

对应决策：`06-Agent编排.md` §6.4、`10-部署运维.md` §7（beat 只投递不执行）。

## 它防的是什么

整条唤醒链路依赖 Worker 主动回调 Gateway。而回调**可能全部丢失**：
Gateway 重启、网络抖动、`internal_token` 配错、Worker 被强杀在回调之前。

没有兜底的话，那些 Run 会永远停在 `awaiting_results`——不是失败，
是**悬空**，而悬空是最糟的一种状态：用户看不出它坏了，只是"一直没结果"。

## 扫描依据是 `updated_at`，所以它必须被维护

扫描条件是"阶段是 `awaiting_results` 且 `updated_at` 早于阈值"。
而 `runs.updated_at` **没有 `onupdate`**——它靠每个改 `phase` 的地方
显式写（`storage/repositories/runs.py` 的 `set_phase` 与 `touch` 把这件事
绑在了一起，就是为了让"忘记更新"不可能发生）。

走的是 `ix_runs_phase_updated_at` 联合索引——那正是这个索引存在的理由。

## beat **只投递，不执行**（OP9）

调度与执行耦合会让 beat 重启时中断正在跑的任务。所以这里只负责
"发现该处理的 Run 并把推进指令丢进队列"，真正的推进由编排器做——
而编排器有 per-Run 锁，所以重复投递是安全的。
"""

from __future__ import annotations

import datetime
from typing import Any

from celery import shared_task

from testflow.celery_app import ORCHESTRATE_QUEUE
from testflow.domain.graph import RunPhase
from testflow.graph.tasks import _get_context, orchestrate
from testflow.observability.logging import get_logger
from testflow.storage.repositories import runs as runs_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = ["abandon_stale_reviews", "cleanup_expired_data", "fallback_poll"]

logger = get_logger(__name__)


@shared_task(name="testflow.fallback_poll", queue=ORCHESTRATE_QUEUE, max_retries=0)
def fallback_poll() -> dict[str, Any]:
    """扫描停滞的 Run 并重新投递推进指令。

    Returns:
        `{"scanned": n, "enqueued": m}`。这两个数值得看：
        **`enqueued` 长期大于 0 说明回调链路有病**——兜底在正常工作，
        但那意味着每一次 Run 都在依赖它，而不是依赖正常的回调。
        那是一个应当被处理的问题，而它不是以"错误"的形式出现的。
    """
    context = _get_context()
    settings = context.settings

    # 阈值的依据是"最坏情况下一次执行该花多久"。取 `http_timeout_s` 的
    # 倍数（`executor_stall_multiplier`，默认 2）——太小会在正常的慢用例上
    # 误触发，太大则让悬空持续更久。
    stale_after = settings.http_timeout_s * settings.executor_stall_multiplier

    session = context.sessions()
    try:
        stalled = runs_repo.list_stalled(
            session,
            phase=RunPhase.AWAITING_RESULTS,
            stale_after_s=stale_after,
        )
    finally:
        session.close()

    for run in stalled:
        # 只投递，不执行（OP9）。真正的推进由编排器做，而它有 per-Run 锁——
        # 所以即便这次投递和一次刚好到达的正常回调撞上，也不会重复推进。
        orchestrate.apply_async(args=[str(run.id), "fallback_poll"], queue=ORCHESTRATE_QUEUE)

    if stalled:
        logger.warning("兜底轮询发现 %d 个停滞的 Run（回调链路可能有问题）", len(stalled))

    return {"scanned": len(stalled), "enqueued": len(stalled)}


@shared_task(name="testflow.abandon_stale_reviews", queue=ORCHESTRATE_QUEUE, max_retries=0)
def abandon_stale_reviews() -> dict[str, Any]:
    """把挂起超过 `review_timeout_days` 的 Run 标为 `abandoned`。

    对应 `06` §5.1：IP-1 不能永远等人。一个 Run 停在 `awaiting_review` 太久
    （默认 7 天），说明用户已经放弃它了——留着会让"审阅超时扫描"以外的
    任何东西（前端列表、报告汇总）都看到一个永不结束的 Run。

    与 `fallback_poll` 的区别：那个是**推进**（把卡住的 Run 救回来），
    这个是**放弃**（把没人管的 Run 了结）。放弃是终态，所以这里**直接写**
    `set_phase(ABANDONED)`，不需要投递到编排器——没有下一步可走。
    """
    context = _get_context()
    settings = context.settings

    session = context.sessions()
    try:
        stalled = runs_repo.list_stalled(
            session,
            phase=RunPhase.AWAITING_REVIEW,
            stale_after_s=settings.review_timeout_days * 86400,
        )
        for run in stalled:
            runs_repo.set_phase(
                session, run.id, RunPhase.ABANDONED, failure_reason="review_timeout"
            )
        session.commit()
    finally:
        session.close()

    if stalled:
        logger.warning("审阅超时，abandon %d 个 Run", len(stalled))

    return {"abandoned": len(stalled)}


@shared_task(name="testflow.cleanup_expired_data", queue=ORCHESTRATE_QUEUE, max_retries=0)
def cleanup_expired_data() -> dict[str, Any]:
    """删除超过 `run_retention_days` 的 Run，以及因此变成孤儿的 Spec。

    对应 `04-数据模型.md` §5、`10-部署运维.md` §7。

    ## 顺序：先 Run，后 Spec

    一个 Spec 只有在**引用它的最后一个 Run 也被删掉之后**才是孤儿。
    反过来做的话，一个刚过期 Run 的 Spec 会多活一整个周期。

    ## 幂等与可重入

    两个操作都是"按时间条件删除"，重复执行第二次只会删到 0 条。
    中断后重跑也一样：条件没有状态，不依赖"上次删到哪"。

    ## 刻意不删 `audit_logs`

    `04` §5 说审计日志同样保留 90 天。这里**没有实现**那条删除，是 M5 的
    一个显式决定：审计记录是"发生过什么"的唯一凭证，批量删除它的能力
    应当有一个独立的人工授权入口，而不是藏在一个每日定时任务里。
    Run 删除本身会把 `audit_logs.run_id` 置 NULL（外键 `SET NULL`），
    记录仍然保留。这个偏差已回写到 `04` §5。

    ## 返回删除数量

    "记录每次删除的数量以便审计"（§5）。零也是信息：它证明任务跑过，
    而不是调度停了。
    """
    context = _get_context()
    settings = context.settings
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=settings.run_retention_days
    )

    session = context.sessions()
    try:
        runs_deleted = runs_repo.delete_older_than(session, cutoff=cutoff)
        # 同一个事务里判断孤儿：此时刚删掉的 Run 已经对 `NOT EXISTS` 不可见，
        # 于是它们的 Spec 立即有资格被清理，不必等到明天。
        specs_deleted = specs_repo.delete_orphans_older_than(session, cutoff=cutoff)
        session.commit()
    finally:
        session.close()

    logger.info(
        "数据清理完成：删除 Run %d 个、孤儿 Spec %d 个（保留期 %d 天）",
        runs_deleted,
        specs_deleted,
        settings.run_retention_days,
    )
    return {"runs_deleted": runs_deleted, "specs_deleted": specs_deleted}
