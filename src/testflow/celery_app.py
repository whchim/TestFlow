"""Celery 应用构造。

对应决策：`02-架构设计.md` §1.3（队列拓扑）、`03/TestExecutor.md` §6/§8（超时与重试）、
`10-部署运维.md` §2.1（服务清单）、`08-工程规范.md` EN1。

**为什么是 `build_celery_app()` 而不是模块级的 `celery_app = Celery(...)`**

Celery 的惯例是模块级单例（`celery -A proj` 就靠它）。但那样必须在**导入时**
读配置，而 EN3 明确要求配置只由 `load_settings()` 显式构造——导入时实例化会让
测试无法注入配置，且 `import testflow.celery_app` 这个动作本身产生副作用。

所以这里是工厂函数，由入口点（`entrypoints/worker.py` 等）调用一次。

## 三条超时（TE7）

    10s   单请求 HTTP 超时（`http_timeout_s`）
    30s   Celery soft limit —— 抛可捕获的软超时，让任务清理并落一条 timeout 结果
    45s   Celery hard limit —— 强制杀掉 Worker 进程

**必须严格递增。** 若 soft ≥ hard，任务在被中断前来不及写"超时"结果，
那条用例就永久悬空——而悬空的表现是 Run 永远停在 `awaiting_results`，
只能靠兜底轮询收尾。那是对的兜底，但不该是常态。

## 两条队列（D6）

    orchestrate   编排推进（短跳，秒级）
    exec          用例执行（IO 密集）

M1 就是这两个。**不做长短任务分流**（D6）——那是为了应对"某些任务特别慢"
的优化，而 M1 还没有任何数据说明需要它。
"""

from __future__ import annotations

from celery import Celery
from kombu import Queue

from testflow.config import Settings

__all__ = ["ORCHESTRATE_QUEUE", "EXEC_QUEUE", "build_celery_app"]

ORCHESTRATE_QUEUE = "orchestrate"
EXEC_QUEUE = "exec"

#: 与 `http_timeout_s`（10s，可配）组成 TE7 的三层。
#:
#: **写死在代码里而非配置项**：它们是"任务被中断前必须写完结果"这个约束的
#: 体现，与 `http_timeout_s` 之间存在必须保持的不等式。做成三个独立配置项
#: 就等于允许配出一个无效组合（soft < http），而那不会报错，只会让结果丢失。
_HARD_TIME_LIMIT_S = 45
_SOFT_TIME_LIMIT_S = 30


def build_celery_app(settings: Settings, *, include: list[str] | None = None) -> Celery:
    """构造 Celery 应用。

    Args:
        settings: 配置。broker/backend 都指向 Redis 的 **broker 库**——
            与 Checkpoint 的库分开（`02` §6 / ADR-0009）：混用同一个 DB 会让
            `FLUSHDB` 类的运维操作误删对方的数据，且两者的淘汰策略需求不同。
        include: 要注册的任务模块。

    Returns:
        未启动的 `Celery` 实例。启动方式由入口点决定（`worker` / `beat`）。
    """
    app = Celery("testflow", include=include or [])
    app.conf.update(
        broker_url=f"{settings.redis_url.rstrip('/')}/{settings.redis_broker_db}",
        # 结果不存 Redis：结果的权威位置是 PG 的 `case_results`（D4）。
        # 让 Celery 再存一份会立刻产生"两份结果不一致"的问题，
        # 而图推进的依据必须是 PG（`06` §6.2）。
        task_ignore_result=True,
        # —— 投递语义 ——
        # acks_late：任务**执行完**才 ack。Worker 被杀时任务会重投。
        # 这与 TE5 的 ON CONFLICT 幂等是配套的：重投必然导致重复执行，
        # 而重复执行必须无害。
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # prefetch=1 是 acks_late 的必要配套。默认的 prefetch（4×并发）会让
        # 一个 Worker 预取一堆任务攥在手里，其它空闲 Worker 却拿不到——
        # 在"任务长度不均"时（LLM 调用 vs 纯规则路径）尤其明显。
        worker_prefetch_multiplier=1,
        # —— 超时（TE7）——
        task_time_limit=_HARD_TIME_LIMIT_S,
        task_soft_time_limit=_SOFT_TIME_LIMIT_S,
        # —— 队列 ——
        task_queues=(Queue(ORCHESTRATE_QUEUE), Queue(EXEC_QUEUE)),
        task_default_queue=ORCHESTRATE_QUEUE,
        timezone="UTC",
        enable_utc=True,
    )
    return app
