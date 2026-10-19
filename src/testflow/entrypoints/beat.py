"""Celery Beat 入口。**只投递，不执行**（OP9）。

对应决策：`10-部署运维.md` §7、`06-Agent编排.md` §6.4。

## 为什么需要这个入口点

`08` §1 的脚注说"beat 无入口点，由 `celery -A testflow beat` 启动"。
但 `-A` 需要一个**模块级的 app 实例**，而在导入时构造 app 意味着
在导入时读配置——违反 EN3（配置只由 `load_settings()` 显式构造）。

所以加了这个入口点。代价是一个文件；收益是配置注入的规则没有例外。
（入口点本身被排除在覆盖率要求之外，见 `08` §2.3。）

## 定时任务

| 任务 | 间隔 | 作用 |
| :--- | :--- | :--- |
| `testflow.fallback_poll` | 1 分钟 | 找回因回调丢失而悬空的 Run |
| `testflow.abandon_stale_reviews` | 每小时 | 了结审阅超时的 Run（IP-1） |
| `testflow.cleanup_expired_data` | 每日 | 删除超期 Run 与孤儿 Spec（`04` §5） |

三个任务都必须**幂等且可重入**（`10` §7）：它们可能因进程重启而被重复
触发，重复执行不应产生副作用。备份不走 beat——`pg_dump` 需要 PostgreSQL
客户端工具，而 Worker 镜像里没有；它由 `scripts/pg_backup.sh` 在宿主机上
按 cron 执行（`10` OP3）。
"""

from __future__ import annotations

import sys

from celery.schedules import crontab

from testflow.celery_app import ORCHESTRATE_QUEUE, build_celery_app
from testflow.config import load_settings
from testflow.observability.logging import configure_logging, get_logger

__all__ = ["build_beat_app", "main"]

logger = get_logger(__name__)


def build_beat_app(settings=None):
    settings = settings or load_settings()
    app = build_celery_app(settings, include=["testflow.graph.tasks_beat"])

    app.conf.beat_schedule = {
        "fallback-poll": {
            "task": "testflow.fallback_poll",
            "schedule": crontab(minute="*"),  # 每分钟
            "options": {"queue": ORCHESTRATE_QUEUE},
        },
        "abandon-stale-reviews": {
            "task": "testflow.abandon_stale_reviews",
            # 审阅超时以天计（默认 7 天），每小时查一次足够，且不撞整点。
            "schedule": crontab(minute=17),
            "options": {"queue": ORCHESTRATE_QUEUE},
        },
        "cleanup-expired-data": {
            "task": "testflow.cleanup_expired_data",
            # 每天一次，凌晨低峰。用 37 分而不是整点：整点是所有"每天跑一次"
            # 的任务的默认选择，而它们会在同一分钟互相争抢数据库连接。
            "schedule": crontab(hour=3, minute=37),
            "options": {"queue": ORCHESTRATE_QUEUE},
        },
    }
    return app


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    app = build_beat_app(settings)

    app.start(
        argv=[
            "beat",
            # 持久化调度状态：不持久化的话，beat 重启会让"下一次执行时间"
            # 从头算，于是一次重启可能推迟一整轮扫描——而那正好发生在
            # "有 Run 悬空、正等兜底"的时候。
            "--schedule",
            "/tmp/testflow-celerybeat-schedule",
            "--loglevel",
            settings.log_level,
        ]
    )
    sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
