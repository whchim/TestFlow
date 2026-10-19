"""LLM 出站的行为测试：**真的发一次请求，看它有没有经过 Guardrail。**

对应决策：ADR-0002（Guardrail 是唯一出站通道）、`02` D2、`08-工程规范.md` §2.3。

## 这是 M2 的专项检查 b，也是唯一能抓到这个洞的东西

`llm/qwen.py` 的 `http_client` 类型是 `Any`，而 `llm/` 本来就允许 import LLM SDK
——所以"忘了注入 `http_client`，SDK 自建一个"这件事：

* **静态检查不到**：`import openai` 和 `AsyncOpenAI(http_client=...)`
  在 AST 层面都是合法语句，`tests/architecture/` 一个字都说不出来
* **单测也抓不到**：单测自己拼客户端，正好把"装配"那一环替换掉了

只有这里能抓到——**发一次真实调用，然后去问审计表**。做法与
`test_guardrail_client.py` 里那些"请求确实没到达"的断言同源：
安全功能大部分时间在做"让某事发生"或"不让某事发生"，
而这两件事都只能向**第三方**求证，从调用方的返回值里看不出来。

## 为什么这一条必须是集成测试

它需要一个真的监听着的 HTTP 服务，才能区分"请求发出去了"和"请求被拦下来了"。
用 mock 顶替传输层，就等于把被测的那一环换掉了——而那正是本测试的全部内容。

桩服务在测试进程内起（照抄 `conftest.py::fixture_server` 的做法）：
要求"先手工跑一个假 LLM"会让这条测试在 CI 里永远处于"被跳过"状态，
而跳过和通过看起来一样绿。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

from testflow.guardrail.async_client import build_llm_http_client
from testflow.guardrail.pipeline import RequestContext, evaluate
from testflow.guardrail.policy import Decision, Whitelist
from testflow.llm.client import LLMError
from testflow.llm.qwen import QwenProvider

pytestmark = pytest.mark.integration

#: 桩服务的端口。与 `conftest.FIXTURE_PORT`（9000）错开——那个是"被测 API"的替身，
#: 这个是"模型提供商"的替身。两者混在一个端口上会让本文件的断言失去意义。
STUB_HOST = "127.0.0.1"
STUB_PORT = 9101
STUB_BASE = f"http://{STUB_HOST}:{STUB_PORT}"

#: 桩服务返回的内容。**刻意包含中文**：`completions.parse` 走的是
#: "JSON 文本 → Pydantic 对象"，编码问题会在这里暴露，而不是在某个更远的地方。
STUB_REPLY = '{"message": "来自桩服务"}'

#: 桩服务计数器，跨测试共享（由 `stub_llm` fixture 初始化）。
_state: dict[str, object] = {"hits": 0, "requests": []}


class _Greeting(BaseModel):
    message: str


def _stub_app():
    """一个**只实现 OpenAI 兼容形状**的最小服务。

    不引真模型：这条测试要验的是"请求从哪条路出去"，而不是"模型答得对不对"。
    桩服务越笨，测试失败时能被误解的方向就越少。

    ⚠️ `fastapi` 的导入**必须在模块顶层**，不能挪进这个函数。
    本模块有 `from __future__ import annotations`，于是 `request: Request`
    是一个**字符串**注解，而 FastAPI 会拿它去函数的 `__globals__`
    （也就是本模块）里求值。导入在函数作用域里的话求不到 `Request`，
    FastAPI 就把它当成一个**查询参数**——失败形式是 422
    `{"loc": ["query", "request"], "msg": "Field required"}`，
    而信息里看不出跟注解有任何关系。
    """

    app = FastAPI()

    @app.get("/__ready")
    async def ready() -> dict:
        # 就绪探测走的路径**不计数**——否则它自己会把 hits 加一，
        # 而 hits 正是"这次 generate 发了几次请求"的证据。
        return {"ok": True}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> dict:
        payload = await request.json()
        _state["hits"] = int(_state["hits"]) + 1  # type: ignore[arg-type]
        requests = _state["requests"]
        assert isinstance(requests, list)
        requests.append(payload)
        return {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": payload.get("model", "stub"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": STUB_REPLY},
                }
            ],
        }

    return app


def _ready() -> bool:
    import httpx

    try:
        return httpx.get(f"{STUB_BASE}/__ready", timeout=0.5).status_code == 200
    except Exception:  # noqa: BLE001 —— 连不上就是没就绪
        return False


@pytest.fixture(scope="module")
def stub_llm() -> Iterator[str]:
    import uvicorn

    if not _ready():
        server = uvicorn.Server(
            uvicorn.Config(_stub_app(), host=STUB_HOST, port=STUB_PORT, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _ready():
                break
            time.sleep(0.05)
        else:  # pragma: no cover
            pytest.skip("LLM 桩服务未能在 10 秒内就绪")
    else:  # pragma: no cover
        server = None
        thread = None

    _state["hits"] = 0
    _state["requests"] = []
    yield STUB_BASE

    if server is not None:
        server.should_exit = True
        assert thread is not None
        thread.join(timeout=5)


class TestALlmCallGoesThroughTheGuardrail:
    async def test_generate_succeeds_and_is_audited(self, stub_llm: str, make_settings) -> None:
        """★ 本文件的核心：**一次 `generate` 走完全程，并在审计里留下痕迹。**

        审计记录是"请求确实从 Guardrail 出去了"的唯一证据——
        一个 SDK 自建的客户端也会成功返回，但审计表里什么都不会有。
        """
        settings = make_settings(llm_base_url=f"{stub_llm}/v1")
        # 桩服务在环回地址上，而配置里**没有任何内网例外**——这正说明
        # LLM 那条路不做 SR-2。对比见 `TestTheTwoEgressPathsDifferOnPurpose`。
        assert settings.guardrail_allow_private_targets == []

        records: list = []
        client = build_llm_http_client(settings, audit_sink=records.append)
        provider = QwenProvider(settings, http_client=client)

        try:
            result = await provider.generate(prompt="打个招呼", schema=_Greeting)
        finally:
            await client.aclose()

        assert result.message == "来自桩服务", "解析链路（JSON → Pydantic）应当走通"
        assert [r.direction for r in records] == ["llm"], (
            "没有 direction='llm' 的审计，说明这次调用绕开了 Guardrail"
        )
        assert records[0].decision == "allow"
        assert records[0].deny_reason is None
        assert records[0].target_host == STUB_HOST
        assert records[0].target_url == f"{stub_llm}/v1/chat/completions"
        assert _state["hits"] == 1, "重试策略不该在这里补发请求"

    async def test_provider_specific_params_reach_the_wire(
        self, stub_llm: str, make_settings
    ) -> None:
        """`enable_thinking` 这类**厂商特有参数**必须真的发出去。

        它们藏在 `extra_body` 里（`qwen.py` 里唯一出现它们名字的一行）。
        发不出去的表现不是报错，而是"模型没开思考模式"——答案质量下降，
        而没有任何东西会告诉你为什么。

        顺带钉住的是 `response_format`：结构化输出的 Schema 由此传过去，
        少了它模型会返回自由文本，`parsed` 就永远是 None。
        """
        # 显式打开思考模式：这条测的是"厂商参数能不能到达线上"，
        # 不是"M4 的默认值是多少"（默认关闭是基准实测选的）。
        settings = make_settings(llm_base_url=f"{stub_llm}/v1", llm_enable_thinking_generation=True)
        client = build_llm_http_client(settings)
        provider = QwenProvider(settings, http_client=client)

        try:
            await provider.generate(prompt="打个招呼", schema=_Greeting)
        finally:
            await client.aclose()

        requests = _state["requests"]
        assert isinstance(requests, list)
        sent = requests[-1]
        assert sent["enable_thinking"] is True
        assert sent["result_format"] == "message"
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["messages"][0]["content"] == "打个招呼"

    async def test_a_request_to_a_foreign_host_is_blocked_and_audited(
        self, stub_llm: str, make_settings
    ) -> None:
        """同一条路径上的反向断言：**换个宿主就发不出去，而且要留痕。**

        与目标路径同理，"我们决定不发"和"它确实没发出去"是两件事。
        这里两边都验：异常 + 审计里的 `deny`。

        ⚠️ 桩服务的计数器**不该**变——它只监听 `/v1/chat/completions`，
        而这条请求连传输层都没走到。
        """
        from testflow.guardrail.errors import GuardrailError

        settings = make_settings(llm_base_url=f"{stub_llm}/v1")
        records: list = []
        client = build_llm_http_client(settings, audit_sink=records.append)
        before = int(_state["hits"])  # type: ignore[arg-type]

        try:
            with pytest.raises(GuardrailError, match="不在允许范围内"):
                await client.get("https://evil.example.com/v1/chat")
        finally:
            await client.aclose()

        assert [r.decision for r in records] == ["deny"]
        assert records[0].deny_reason == "whitelist"
        assert _state["hits"] == before, "被拒绝的请求不该到达桩服务"


class TestFailuresSurfaceAsLlmErrors:
    async def test_a_404_from_the_provider_becomes_an_llm_error(
        self, stub_llm: str, make_settings
    ) -> None:
        """SDK 与 httpx2 的异常**不得漏到业务层**。

        业务层（`agents/`）依赖的是 `llm/client.py` 那个窄接口，
        它不该认识 `openai.NotFoundError` 或 `httpx2.HTTPStatusError`——
        那是 D7 要收敛掉的东西。一旦漏出去，换 Provider 就不再是
        "改一个适配器"，而是"改所有 except 子句"。

        用 `/nope` 作基地址：SDK 会 POST 到 `/nope/chat/completions`，
        桩服务上没有这个路由 → 真实的 404 → 必须被归成 `LLMError`。
        """
        settings = make_settings(llm_base_url=f"{stub_llm}/nope", llm_max_retries=0)
        client = build_llm_http_client(settings)
        provider = QwenProvider(settings, http_client=client)

        try:
            with pytest.raises(LLMError):
                await provider.generate(prompt="hi", schema=_Greeting)
        finally:
            await client.aclose()


class TestTheTwoEgressPathsDifferOnPurpose:
    """LLM 路径**不做 SR-2（内网阻断）**，目标路径做——这是有意的差异。

    理由在 `guardrail/async_client.py` 的模块文档字符串里：威胁模型不同。
    LLM 宿主来自应用配置（不由用户决定），所以 DNS rebinding 对它没有意义，
    白名单就够了；而被测目标的地址来自**用户上传的 Spec**，是不可信输入，
    所以那条路要额外做"解析全部 IP + 校验 + 钉死 + 对端二次校验"。

    这条测试让那个差异**可执行**——否则"LLM 那条路没查内网"看起来就像个疏忽，
    某天会被"顺手补上"；而补上之后，`llm_base_url` 指向本机或内网的模型
    （自建推理服务是很常见的部署）就会全部失败。

    LLM 那一侧的可执行证据在 `test_generate_succeeds_and_is_audited`：
    它用的基地址是 `127.0.0.1:9101`，而配置里**没有**任何内网例外。
    """

    def test_a_loopback_target_is_denied_without_an_explicit_exception(self, make_settings) -> None:
        """同一个 `127.0.0.1`，作为**目标**时必须被 SR-2 拦下。"""
        settings = make_settings(llm_base_url=f"{STUB_BASE}/v1")
        assert settings.guardrail_allow_private_targets == [], "前提：没有配任何内网例外"

        decision = evaluate(
            RequestContext(url=f"{STUB_BASE}/ok", method="GET"),
            [STUB_HOST],
            whitelist=Whitelist.from_urls([f"{STUB_BASE}/"]),
        )
        assert isinstance(decision, Decision)
        assert not decision.allowed
        assert decision.reason.value == "private_ip"
