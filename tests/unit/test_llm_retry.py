"""`QwenProvider.generate` 的重试语义。**不发真实请求。**

对应决策：`02-架构设计.md` §5、`10-部署运维.md`（锁 TTL）、`08` §2.3。

## 为什么这批测试值得单独写一个文件

"重试"是一个**策略**，而策略的正确性全在边界上：重试几次、等多久、
**哪一类失败不重试**。这些边界写错了不会报错，只会把代价悄悄放大——
而放大的那部分恰好是"最坏情况耗时"，也就是锁 TTL 要覆盖的那个数。

三类断言分别对应三类边界：

* **次数**：`llm_max_retries` 是"重试次数"而不是"总次数"，差一就多打一次
* **等待**：指数退避（0.5s → 1s），不是固定间隔
* **分类**：超时不重试、解析失败不重试、其他 SDK 异常不重试

`_sleep_recorder` 顶掉 `asyncio.sleep`：退避时长要能被断言，
而真的睡 1.5 秒会让这组测试变成"慢测试"——慢测试会被跳过，跳过和通过一样绿。
"""

from __future__ import annotations

import json
import uuid

import httpx2
import pytest
from pydantic import BaseModel

from testflow.guardrail.errors import GuardrailTimeout, GuardrailTransportError
from testflow.llm.client import LLMError, UsageRecord
from testflow.llm.qwen import _RETRY_BACKOFF_BASE_S, QwenProvider
from testflow.observability.tracing import (
    bind_llm_purpose,
    bind_run_id,
    reset_llm_purpose,
    reset_run_id,
)

__all__ = []


class _Greeting(BaseModel):
    message: str


_DEFAULT_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 8},
}


def _completion(content: str, *, usage: dict | None = _DEFAULT_USAGE) -> httpx2.Response:
    """一个最小可用的 chat completion 响应体。

    刻意**手写这个形状而不是用假 Provider**：`completions.parse` 的解析
    （JSON 文本 → Pydantic 对象）正是真实路径上的那一环，
    换成假 Provider 就把它绕过去了——而"离线测试通过、真实调用解析失败"
    正是那种绕法会造出来的问题。

    `usage` 默认为一个固定块（含缓存命中明细）；传 `None` 模拟"提供商不回用量"。
    """
    body: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx2.Response(200, json=body)


class _ScriptedTransport(httpx2.AsyncBaseTransport):
    """按脚本逐个应答：元素是异常就抛，是响应就返回。

    用脚本而不是"第 n 次抛"的回调：脚本把"这次调用序列长什么样"**写在了测试里**，
    读测试的人一眼能看到"第一次失败、第二次成功"，不必去反推一个计数器。
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls = 0
        self.last_request: httpx2.Request | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        self.last_request = request
        if not self._script:
            raise AssertionError(f"调用次数超出脚本预期（第 {self.calls} 次）")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, httpx2.Response)
        return item


@pytest.fixture
def sleep_recorder(monkeypatch) -> list[float]:
    """记录退避时长并**真的不睡**。

    ## ⚠️ 只换掉 `qwen.py` 看到的那个 `asyncio`，不动全局的

    第一版写的是 `monkeypatch.setattr("testflow.llm.qwen.asyncio.sleep", ...)`
    ——看着很"局部"，其实 `testflow.llm.qwen.asyncio` **就是那个共享的
    `asyncio` 模块对象**，于是它是全局替换。两个后果，一个比一个难发现：

    1. **断言被污染**：uvicorn、anyio 也在调 `asyncio.sleep`，
       整套测试跑下来 `sleep_recorder` 里攒了 8 万多个别人的 0.1 秒，
       而"没有重试就不该等待"这条断言因此随机变红——**单跑本文件时是绿的**。
    2. **整套测试从 53 秒变成 303 秒**：把 `sleep` 换成"立即返回"会毁掉
       `anyio.fail_after` 那类定时器的语义（它靠 sleep 推进事件循环），
       超时等待退化成忙等。

    所以这里造一个**只给 `qwen.py` 看的替身**：它转发 `asyncio` 的一切，
    只把 `sleep` 换掉。被测代码的视角改变了，进程里其他东西的视角没有。
    """
    import asyncio
    import types

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    shim = types.SimpleNamespace(**{**vars(asyncio), "sleep": _fake_sleep})
    monkeypatch.setattr("testflow.llm.qwen.asyncio", shim)
    return slept


def _provider(
    settings, script: list[object], *, on_usage=None
) -> tuple[QwenProvider, _ScriptedTransport]:
    transport = _ScriptedTransport(script)
    client = httpx2.AsyncClient(transport=transport, follow_redirects=False)
    provider = QwenProvider(settings, http_client=client, on_usage=on_usage)
    return provider, transport


class TestRetryCount:
    async def test_transient_transport_error_is_retried(
        self, make_settings, sleep_recorder
    ) -> None:
        provider, transport = _provider(
            make_settings(llm_max_retries=2),
            [GuardrailTransportError("连接被拒"), _completion('{"message": "好"}')],
        )

        result = await provider.generate(prompt="hi", schema=_Greeting)

        assert result.message == "好"
        assert transport.calls == 2, "第一次失败后应当再试一次"

    async def test_retries_are_bounded_by_the_setting(self, make_settings, sleep_recorder) -> None:
        """`llm_max_retries=2` 意味着**总共 3 次**（首次 + 2 次重试）。

        差一就多打一次模型调用，而那既花钱又拉长最坏耗时。
        """
        provider, transport = _provider(
            make_settings(llm_max_retries=2),
            [GuardrailTransportError("网络抖动")] * 3,
        )

        with pytest.raises(LLMError, match="连续 3 次传输失败"):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert transport.calls == 3
        assert len(sleep_recorder) == 2, "3 次尝试之间只有 2 次等待"

    async def test_zero_retries_means_exactly_one_attempt(
        self, make_settings, sleep_recorder
    ) -> None:
        provider, transport = _provider(
            make_settings(llm_max_retries=0),
            [GuardrailTransportError("连接被拒")],
        )

        with pytest.raises(LLMError):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert transport.calls == 1
        assert sleep_recorder == [], "没有重试就不该等待"


class TestBackoff:
    async def test_waits_grow_exponentially(self, make_settings, sleep_recorder) -> None:
        """0.5s → 1s，而不是固定 0.5s。

        固定间隔在"对端正在重启"这种场景下等于把重试全部打在同一个瞬间，
        三次机会退化成一个。
        """
        provider, _ = _provider(
            make_settings(llm_max_retries=2),
            [
                GuardrailTransportError("a"),
                GuardrailTransportError("b"),
                _completion('{"message": "好"}'),
            ],
        )

        await provider.generate(prompt="hi", schema=_Greeting)

        assert sleep_recorder == [_RETRY_BACKOFF_BASE_S, _RETRY_BACKOFF_BASE_S * 2]


class TestWhatIsNotRetried:
    """**"不重试什么"比"重试什么"更重要。** 三类失败各有各的理由。"""

    async def test_timeout_is_not_retried(self, make_settings, sleep_recorder) -> None:
        """超时是唯一"重试会让代价变乘积"的失败。

        三次 120 秒超时 = 360 秒，超过编排层的锁 TTL——锁在推进中途过期，
        另一个推进者就会同时跑同一个节点。所以 SDK 内建重试被设成 0，
        而这里也不能把它加回来。
        """
        provider, transport = _provider(
            make_settings(llm_max_retries=2),
            [GuardrailTimeout("超时了"), _completion('{"message": "不该到这里"}')],
        )

        with pytest.raises(LLMError, match="超时"):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert transport.calls == 1, "超时不该重试——脚本里的第二次应答不该被用到"

    async def test_unparseable_output_is_not_retried(self, make_settings, sleep_recorder) -> None:
        """模型给出的内容不合 Schema 是**内容**问题，重试只是再赌一次。"""
        provider, transport = _provider(
            make_settings(llm_max_retries=2),
            [_completion("这不是 JSON"), _completion('{"message": "也不该被用到"}')],
        )

        with pytest.raises(LLMError):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert transport.calls == 1

    async def test_other_sdk_errors_are_not_retried(self, make_settings, sleep_recorder) -> None:
        """服务端 4xx（鉴权、参数错）重试没用：同一个请求打三次得到同一个拒绝。

        ⚠️ 限流（429）也归在这类，是**有意**的简化：该等多久由服务端的
        `Retry-After` 说了算，而我们这里猜一个退避时长只会把配额耗得更快。
        真要处理 429 应当读那个响应头——那是一件独立的事，不靠这个循环兜。
        """
        provider, transport = _provider(
            make_settings(llm_max_retries=2),
            [httpx2.Response(401, json={"error": {"message": "invalid api key"}})],
        )

        with pytest.raises(LLMError):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert transport.calls == 1


class TestSdkLevelRetryIsOff:
    def test_sdk_max_retries_is_zero(self, make_settings) -> None:
        """★ 两层重试叠加会让最坏耗时变成**乘积**。

        我们这一层重试 3 次 × SDK 那一层重试 3 次 = 9 次调用，
        而那个数会超过编排层的锁 TTL。这个断言把"只留一层"钉住——
        它是个容易在某次"顺手加个重试"里被破坏的设置。
        """
        provider, _ = _provider(make_settings(), [])
        assert provider._client.max_retries == 0


class TestUsageCapture:
    """用量采集：在解析校验**之前**记录，连计费但不可用的调用也不漏。"""

    async def test_usage_is_emitted_with_tokens_and_context(self, make_settings) -> None:
        records: list[UsageRecord] = []
        provider, _ = _provider(
            make_settings(), [_completion('{"message": "好"}')], on_usage=records.append
        )
        run_id = uuid.uuid4()
        token = bind_run_id(run_id)
        purpose_token = bind_llm_purpose("attribution")
        try:
            await provider.generate(prompt="hi", schema=_Greeting)
        finally:
            reset_llm_purpose(purpose_token)
            reset_run_id(token)

        assert len(records) == 1
        record = records[0]
        assert record.run_id == run_id
        assert record.purpose == "attribution"
        assert record.prompt_tokens == 100
        assert record.completion_tokens == 20
        assert record.cached_tokens == 40
        assert record.cache_write_tokens == 8
        assert record.total_tokens == 120
        assert record.attempt == 1
        assert record.elapsed_ms >= 0

    async def test_usage_is_emitted_even_when_the_model_refuses(self, make_settings) -> None:
        """模型返回 200 就已经计费——内容不可用不代表这次调用免费。

        用"拒答"（`refusal` 非空、`parsed=None`）而不是非法 JSON：
        非法 JSON 会在 SDK 内部抛 `ValidationError`，那个异常**不带
        completion**，用量在客户端已经拿不到了。拒答则能走到我们自己的
        `parsed is None` 分支——这里要证明的是那条分支**之后**才记用量。
        """
        records: list[UsageRecord] = []
        provider, _ = _provider(
            make_settings(),
            [
                httpx2.Response(
                    200,
                    json={
                        "id": "x",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "refusal": "不能回答",
                                },
                            }
                        ],
                        "usage": _DEFAULT_USAGE,
                    },
                )
            ],
            on_usage=records.append,
        )

        with pytest.raises(LLMError):
            await provider.generate(prompt="hi", schema=_Greeting)

        assert len(records) == 1
        assert records[0].total_tokens == 120

    async def test_missing_usage_does_not_crash(self, make_settings) -> None:
        records: list[UsageRecord] = []
        provider, _ = _provider(
            make_settings(),
            [_completion('{"message": "好"}', usage=None)],
            on_usage=records.append,
        )

        await provider.generate(prompt="hi", schema=_Greeting)

        assert len(records) == 1
        assert records[0].total_tokens == 0

    async def test_sink_failure_is_swallowed(self, make_settings) -> None:
        def boom(record: UsageRecord) -> None:
            raise RuntimeError("指标系统挂了")

        provider, _ = _provider(make_settings(), [_completion('{"message": "好"}')], on_usage=boom)

        result = await provider.generate(prompt="hi", schema=_Greeting)
        assert result.message == "好"

    async def test_retry_records_the_successful_attempt_number(
        self, make_settings, sleep_recorder
    ) -> None:
        records: list[UsageRecord] = []
        provider, _ = _provider(
            make_settings(llm_max_retries=2),
            [GuardrailTransportError("连接被拒"), _completion('{"message": "好"}')],
            on_usage=records.append,
        )

        await provider.generate(prompt="hi", schema=_Greeting)

        assert len(records) == 1
        assert records[0].attempt == 2


class TestPurposeAwareParameters:
    async def test_generation_thinking_follows_the_configured_default(self, make_settings) -> None:
        """默认值是 M4 实测选的 **off**（见 `config.py` 的注释）。

        这里守的是"配置真的被读到了"；默认值本身由那一行注释与
        `benchmark_llm.py` 的实测表负责。
        """
        provider, transport = _provider(make_settings(), [_completion('{"message": "好"}')])

        await provider.generate(prompt="hi", schema=_Greeting)

        body = json.loads(transport.last_request.content)
        assert body["enable_thinking"] is False

    async def test_generation_can_enable_thinking_explicitly(self, make_settings) -> None:
        provider, transport = _provider(
            make_settings(llm_enable_thinking_generation=True),
            [_completion('{"message": "好"}')],
        )

        await provider.generate(prompt="hi", schema=_Greeting)

        body = json.loads(transport.last_request.content)
        assert body["enable_thinking"] is True

    async def test_attribution_disables_thinking(self, make_settings) -> None:
        provider, transport = _provider(make_settings(), [_completion('{"message": "好"}')])
        token = bind_llm_purpose("attribution")
        try:
            await provider.generate(prompt="hi", schema=_Greeting)
        finally:
            reset_llm_purpose(token)

        body = json.loads(transport.last_request.content)
        assert body["enable_thinking"] is False

    async def test_sampling_params_are_only_sent_when_configured(self, make_settings) -> None:
        provider, transport = _provider(make_settings(), [_completion('{"message": "好"}')])
        await provider.generate(prompt="hi", schema=_Greeting)
        body = json.loads(transport.last_request.content)
        assert "temperature" not in body

        provider2, transport2 = _provider(
            make_settings(llm_temperature=0.2, llm_top_p=0.9, llm_seed=42),
            [_completion('{"message": "好"}')],
        )
        await provider2.generate(prompt="hi", schema=_Greeting)
        body2 = json.loads(transport2.last_request.content)
        assert body2["temperature"] == 0.2
        assert body2["top_p"] == 0.9
        assert body2["seed"] == 42
