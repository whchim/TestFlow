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
from typing import Literal

__all__ = [
    "LlmPurpose",
    "bind_llm_purpose",
    "bind_run_id",
    "bind_trace_id",
    "current_llm_purpose",
    "current_run_id",
    "current_trace_id",
    "new_trace_id",
    "reset_llm_purpose",
    "reset_run_id",
    "reset_trace_id",
]

LlmPurpose = Literal["case_generation", "attribution"]

_trace_id: ContextVar[str | None] = ContextVar("testflow_trace_id", default=None)

#: 当前正在推进的 Run。**为什么需要它**：LLM 客户端是进程级的
#: （`graph/tasks.py` 的取舍），构造时还不知道任何 `run_id`。
#: 而审计与用量都必须按 Run 归集——没有它，`audit_logs.run_id`
#: 全是 NULL，成本也只能看总数。
#:
#: 用 ContextVar 而不是给客户端加参数：asyncio 的任务会**复制**当前上下文，
#: 所以 N6 里并发发起的归因调用天然各自带着正确的 Run。
_run_id: ContextVar[uuid.UUID | None] = ContextVar("testflow_run_id", default=None)

#: 这次 LLM 调用是干什么用的。Provider 据此选 `enable_thinking`
#: （`02` §5.3：生成开、归因关）与采样参数。
#:
#: 默认 `case_generation`：绝大多数调用是生成，且"开思考"是更保守的默认
#: （质量优先；归因那条路显式绑定为 `attribution` 才会关掉）。
_llm_purpose: ContextVar[LlmPurpose] = ContextVar("testflow_llm_purpose", default="case_generation")


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


def bind_run_id(value: uuid.UUID | None) -> Token[uuid.UUID | None]:
    """绑定当前 Run。返回 token，调用方必须在 `finally` 里 `reset`。

    为什么必须还原：编排层进程在任务之间复用（`--pool=solo`），
    不还原的话上一个 Run 的 id 会粘到下一个 Run 的调用上——
    那比 NULL 更糟：**数据看起来是对的，只是属于另一个 Run**。
    """
    return _run_id.set(value)


def current_run_id() -> uuid.UUID | None:
    return _run_id.get()


def reset_run_id(token: Token[uuid.UUID | None]) -> None:
    _run_id.reset(token)


def bind_llm_purpose(value: LlmPurpose) -> Token[LlmPurpose]:
    return _llm_purpose.set(value)


def current_llm_purpose() -> LlmPurpose:
    return _llm_purpose.get()


def reset_llm_purpose(token: Token[LlmPurpose]) -> None:
    _llm_purpose.reset(token)


def reset_trace_id(token: Token[str | None]) -> None:
    """还原 trace id。

    和 `bind_trace_id` 成对使用：Celery 任务在进程内复用，不还原的话
    前一个任务的 trace 会粘到后一个任务上——比没有更误导。
    """
    _trace_id.reset(token)
