"""ContextVar 的 run_id / purpose，以及它如何修好 LLM 审计归属。"""

from __future__ import annotations

import asyncio
import uuid

import httpx2

from testflow.guardrail.async_client import _AllowlistAsyncTransport
from testflow.observability.tracing import (
    bind_llm_purpose,
    bind_run_id,
    current_llm_purpose,
    current_run_id,
    reset_llm_purpose,
    reset_run_id,
)


def test_run_id_binds_and_resets() -> None:
    run_id = uuid.uuid4()
    token = bind_run_id(run_id)

    assert current_run_id() == run_id

    reset_run_id(token)
    assert current_run_id() is None


def test_purpose_defaults_to_case_generation_and_resets() -> None:
    assert current_llm_purpose() == "case_generation"

    token = bind_llm_purpose("attribution")
    assert current_llm_purpose() == "attribution"

    reset_llm_purpose(token)
    assert current_llm_purpose() == "case_generation"


async def test_run_id_is_isolated_between_async_tasks() -> None:
    """两个并发任务互不串号——这是 N6 并发归因能正确归集的前提。"""
    first, second = uuid.uuid4(), uuid.uuid4()

    async def child(value: uuid.UUID) -> uuid.UUID | None:
        token = bind_run_id(value)
        await asyncio.sleep(0)
        observed = current_run_id()
        reset_run_id(token)
        return observed

    assert await asyncio.gather(child(first), child(second)) == [first, second]


class _OkDelegate(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"ok": True})


async def test_llm_audit_uses_the_current_run_id_over_the_constructor_value() -> None:
    """进程级客户端构造时没有 Run；审计必须在**发送那一刻**取当前上下文。

    M3 结束时的现象是：16 条 LLM 审计记录，`run_id` 全是 NULL。
    这条测试守住的正是那个缺口。
    """
    records = []
    constructor_run = uuid.uuid4()
    current_run = uuid.uuid4()
    transport = _AllowlistAsyncTransport(
        allowed_host_port="example.invalid:443",
        audit_sink=records.append,
        run_id=constructor_run,
        delegate=_OkDelegate(),
    )
    request = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")

    token = bind_run_id(current_run)
    try:
        await transport.handle_async_request(request)
    finally:
        reset_run_id(token)

    assert len(records) == 1
    assert records[0].run_id == current_run

    # 上下文还原之后，退回构造时给的值（显式构造的单 Run 客户端仍然可用）。
    await transport.handle_async_request(request)
    assert records[1].run_id == constructor_run
