"""`beat` 的调度表：三个任务一个都不能少。

对应决策：`10-部署运维.md` §7。

**为什么这条也要测**：任务函数存在但没被调度，是"运维就绪"里最安静的一种
失效——代码、测试、日志都正常，只是那个任务永远不会被触发。而它的表现
（备份没做、旧数据越积越多）要过很久才会有人注意。

`build_beat_app()` 只构造 Celery 实例，不连接 broker，所以这是一条纯单测。
"""

from __future__ import annotations


def test_three_periodic_tasks_are_scheduled(make_settings) -> None:
    from testflow.entrypoints.beat import build_beat_app

    app = build_beat_app(make_settings())
    tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}

    assert tasks == {
        "testflow.fallback_poll",
        "testflow.abandon_stale_reviews",
        "testflow.cleanup_expired_data",
    }
