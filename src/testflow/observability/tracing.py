"""追踪标识的传递。

对应决策：`08-工程规范.md` NF7 / EN5、`04-数据模型.md` §2.9 `audit_logs.trace_id`。

**一个 Run 的日志要能串起来**。多进程 + 异步 + Celery 重投的环境里，
"日志按时间排"是不成立的——多个 Run 的日志会交错，同一个 Run 的日志会
分散在不同进程。`trace_id` 是唯一能把它们重新归拢的东西。

用 `contextvar` 而不是全局变量：asyncio 的并发单元是任务，
contextvar 天然随任务复制，不会出现"两个协程互相改对方的 trace_id"。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

__all__ = ["bind_trace_id", "current_trace_id", "new_trace_id"]

_trace_id: ContextVar[str | None] = ContextVar("testflow_trace_id", default=None)


def new_trace_id() -> str:
    """生成一个新的 trace id。

    用 UUID4 而非 UUID7：这里要的是**低碰撞**而非有序——它不落库做索引，
    只出现在日志与 `audit_logs.trace_id` 里，可读性不重要。
    """
    return uuid.uuid4().hex


def bind_trace_id(value: str | None = None) -> Token[str | None]:
    """绑定 trace id 到当前上下文，返回可用于还原的 token。

    返回 token 是刻意的：Celery 任务在进程内复用，若不还原，前一个任务的
    trace_id 会粘到后一个任务的日志上——比没有 trace_id 更误导。
    """
    return _trace_id.set(value or new_trace_id())


def current_trace_id() -> str | None:
    return _trace_id.get()
