"""Celery Worker 入口（消费 `exec` 队列）。

对应决策：`08-工程规范.md` EN1、`03/TestExecutor.md` §8、`02` D1、`06` §6.1。

职责（M1）：消费测试任务、经 Guardrail 打目标 API、求值断言、回写结果、唤醒编排层。
**执行层为同步代码**（ADR-0001），因此使用 prefork 池。

## 唤醒回调在这里注入

`execution/tasks.py` 里没有唤醒的 HTTP 客户端——它只有一个可注册的回调位。
**入口点是唯一知道"我跑在哪个 Gateway 后面"的地方**，所以由它来接线。

这样 `execution/` 不需要 import 任何 HTTP 库（架构规则禁止），
而"怎么打 Gateway"这件事集中在一个可替换的位置。
"""

from __future__ import annotations

import sys

from testflow.celery_app import EXEC_QUEUE, build_celery_app
from testflow.client.control_plane import ControlPlaneClient
from testflow.config import load_settings
from testflow.execution.tasks import set_wake_callback
from testflow.observability.logging import configure_logging, get_logger

__all__ = ["build_worker_app", "main"]

logger = get_logger(__name__)


def build_worker_app(settings=None, *, gateway_url: str | None = None):
    """构造 Worker 用的 Celery 应用，并接好唤醒回调。"""
    settings = settings or load_settings()
    # 配置里的 `gateway_url` 优先——容器部署时它是一个服务名，
    # 而 `127.0.0.1` 在容器里指向 Worker 自己。
    control_plane = ControlPlaneClient(settings, gateway_url=gateway_url or settings.gateway_url)

    if not control_plane.health():
        # **只警告，不退出。** Worker 的职责是执行用例，而唤醒只是"通知"——
        # 回调打不通时兜底轮询会收尾。为一个"通知通道"不可用而拒绝启动，
        # 等于让一个能干活的过程因为一个可选的依赖而停工。
        #
        # 但必须**大声警告**：这条路径的生产者不会看到任何错误，
        # 只会看到"Run 完成得比较慢"。
        logger.warning(
            "Gateway 不可达（%s）——唤醒回调将失败，Run 只能靠兜底轮询收尾。",
            gateway_url or settings.gateway_url or f"http://127.0.0.1:{settings.bind_port}",
        )

    # 用 lambda 而不是 `control_plane.wake.__call__`：后者取的是绑定方法的
    # dunder，类型信息在那一层就丢了（mypy 报 "Callable[[object], ...] not callable"）。
    # 这里显式包一层，签名是清楚的：run_id 进去，什么都不返回。
    def _wake(run_id) -> None:
        control_plane.wake(run_id)

    set_wake_callback(_wake)
    return build_celery_app(settings, include=["testflow.execution.tasks"])


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    app = build_worker_app(settings)

    app.worker_main(
        argv=[
            "worker",
            # **prefork**：执行层是同步代码（ADR-0001），一个进程跑一个任务，
            # 互不干扰、崩溃隔离干净。也正因为如此，限流器是**每进程**的
            # （见 `guardrail/ratelimit.py` 的说明）。
            "--pool=prefork",
            "--concurrency",
            str(settings.worker_concurrency),
            f"--queues={EXEC_QUEUE}",
            "--loglevel",
            settings.log_level,
        ]
    )
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
