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

from typing import Any

from celery import shared_task

from testflow.celery_app import ORCHESTRATE_QUEUE
from testflow.domain.graph import RunPhase
from testflow.graph.tasks import _get_context, orchestrate
from testflow.observability.logging import get_logger
from testflow.storage.repositories import runs as runs_repo

__all__ = ["fallback_poll"]

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
