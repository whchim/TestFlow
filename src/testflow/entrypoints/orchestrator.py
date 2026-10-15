"""Orchestrator 入口（消费 `orchestrate` 队列）。

对应决策：`06-Agent编排.md` G1（编排层独立进程）、`10-部署运维.md` §2.2、`02` D1。

职责：推进 LangGraph 至下一个中断点，然后释放进程。
图推进是**短跳操作**，不是长驻负载，因此单副本足够。

## 为什么 `--pool=solo --concurrency=1`

编排层不是"处理负载"，而是"推动状态机往前走一步"。

* **solo 池**：避免在 prefork 子进程里 fork 一个活的事件循环——
  那是已知会出问题的地方（`asyncio` 的循环不可跨进程继承）。
* **concurrency=1**：per-Run 的串行由 Redis 锁保证（`06` §6.3），
  **不靠池并发**。多开几个进程不会让任何事变快，只会增加"同时拿到锁"
  的窗口——而那正是锁要避免的。
"""

from __future__ import annotations

import sys

from testflow.celery_app import EXEC_QUEUE, ORCHESTRATE_QUEUE, build_celery_app
from testflow.config import load_settings
from testflow.graph.tasks import set_dispatch
from testflow.observability.logging import configure_logging, get_logger

__all__ = ["build_orchestrator_app", "main"]

logger = get_logger(__name__)


def build_orchestrator_app(settings=None):
    """构造编排器用的 Celery 应用，并接好投递回调。"""
    settings = settings or load_settings()
    app = build_celery_app(settings, include=["testflow.graph.tasks", "testflow.graph.tasks_beat"])

    def _dispatch(run_id, case_id) -> None:
        """把**一条用例**投到 `exec` 队列。

        **`send_task` 而不是 `exec_case.delay`**：拿任务函数会让这个入口点
        依赖 `execution/tasks.py` 的具体符号，而"队列名 + 任务名"才是我
        真正要约定的东西。前者在重构时会断（改名、挪模块），后者不会。

        ⚠️ **必须是一个函数，不是"带方法对象"。**
        `GraphDeps.dispatch` 的契约是 `Callable[[UUID, UUID], None]`——
        第一版这里传的是一个 `_Producer` 实例（它有 `orchestrate` 方法），
        N4 调用时直接 `TypeError: '_Producer' object is not callable`。

        而这个错误**集成测试发现不了**：那边注入的是一个 lambda，
        恰好是可调用的。**测试替换掉的正是错的那一环。**
        只有真的把服务起起来跑一遍才会撞上。
        """
        app.send_task(
            "testflow.exec_case",
            args=[str(run_id), str(case_id)],
            queue=EXEC_QUEUE,
        )

    set_dispatch(_dispatch)
    return app


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    app = build_orchestrator_app(settings)

    app.worker_main(
        argv=[
            "worker",
            "--pool=solo",
            "--concurrency=1",
            f"--queues={ORCHESTRATE_QUEUE}",
            "--loglevel",
            settings.log_level,
        ]
    )
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
