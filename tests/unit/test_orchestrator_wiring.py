"""装配层的防回归断言：**LLM 出站必须经 Guardrail。**

对应决策：ADR-0002（唯一出站通道）、`08-工程规范.md` §2.3、`02` D2/D7。

## 为什么这条需要单独一条测试

`llm/qwen.py` 的 `http_client` 参数类型是 `Any`，而且 `llm/` **本来就允许**
import LLM SDK——于是"少传一个参数就自建客户端"这件事在 AST 层面完全合法，
架构测试（`tests/architecture/`）一个字都说不出来。

后果是 ADR-0002 多了一条没人看着的出站路径：流量照发、测试全绿，
只有"审计表里没有 `direction='llm'` 的记录"这一件事暴露了它。
而**不查审计表就看不见**——这一课在本项目上已经吃过一次
（见 `storage/audit_sink.py` 的模块文档字符串）。

所以这里断言的是**装配结果**：默认装配出来的那个客户端，
必须对外部地址报 `GuardrailError`，且必须带着审计回调。
删掉 `http_client=` 或 `audit_sink=`，这条测试立刻红。
"""

from __future__ import annotations

import httpx2
import pytest

from testflow.domain.cases import DimensionSpec
from testflow.graph import tasks
from testflow.guardrail import async_client as ac
from testflow.guardrail.errors import GuardrailError

__all__ = []


class _Context:
    """`_OrchestratorContext` 的最小替身。

    真的那个会建数据库引擎与 Redis 连接池——而本测试只关心
    "装配出来的客户端长什么样"，起那两个东西纯属浪费，
    还会让测试依赖外部服务（于是它在没有 Docker 的机器上被跳过，
    而跳过和通过看起来一样绿）。
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        #: `make_audit_sink` 只把它闭包起来，本测试不会真的写审计，
        #: 所以不需要真的 sessionmaker。
        self.sessions = None
        #: 真上下文里的进程级 LLM 客户端缓存（`tasks._default_llm_provider`）。
        self.llm_provider = None


@pytest.fixture
def built_client(monkeypatch, make_settings):
    """跑一遍真实装配，把交出去的客户端与参数截回来。"""
    captured: dict[str, object] = {}
    real = ac.build_llm_http_client

    def _spy(settings, **kwargs):
        client = real(settings, **kwargs)
        captured["client"] = client
        captured["kwargs"] = kwargs
        return client

    monkeypatch.setattr(ac, "build_llm_http_client", _spy)
    settings = make_settings(llm_base_url="https://llm.example.com/v1")
    provider = tasks._default_case_generator(_Context(settings))
    captured["provider"] = provider
    return captured


async def test_the_client_refuses_any_host_outside_the_allowlist(built_client) -> None:
    """★ 这个是本文件的核心。

    "对外部地址报 `GuardrailError`"这件事**只有一个来源**：
    那个客户端被套上了 Guardrail 的传输层。一个 SDK 自建的客户端
    会老老实实去连 `evil.example.com`——于是这条断言就是那句
    "http_client 被正确注入了"的可执行版本。
    """
    client = built_client["client"]
    assert isinstance(client, httpx2.AsyncClient)

    with pytest.raises(GuardrailError, match="不在允许范围内"):
        await client.get("https://evil.example.com/v1/chat")


def test_the_audit_sink_is_wired(built_client) -> None:
    """GR6：LLM 那条路径也要留痕——**没接回调等于没审计**。"""
    kwargs = built_client["kwargs"]
    assert callable(kwargs.get("audit_sink")), (
        "LLM 出站没有审计回调。这个疏漏不会报错，只会让审计表里永远没有 direction='llm' 的记录。"
    )


def test_the_model_name_reaches_the_database_field(built_client, make_settings) -> None:
    """装配出来的是**真 Provider**（不是占位），且模型名能传到 `cases.llm_model`。

    这条挡两件事：

    * "把 `case_generator` 默认值改回 `None`"——那时 `generate_cases` 会静默
      退回纯规则生成器，复杂接口不被生成用例，而报告看起来一切正常
    * 适配器把 `model_name` 吞掉——`09` §5.3 的 L3 Eval 靠这个字段比对
      模型档位，答错会让"生成质量是否随档位下降"的结论整个失真

    断言的是适配器**对外暴露的** `model_name`，而不是它内部那个 Provider：
    写进库的是前者，而前者才需要在换了任何一层之后仍然正确。
    """
    assert built_client["provider"].model_name == make_settings().llm_model


async def test_the_generator_is_callable_the_way_the_graph_calls_it(built_client) -> None:
    """★★ **本文件最重要的一条，也是 M2 漏掉的那个 bug 的守门人。**

    装配出 `LlmCaseGenerator(QwenProvider(...))` 之后，图上那句调用是

        await active.generate(operation, dimensions)      # 位置参数

    而 `QwenProvider.generate(*, prompt, schema)` 是**关键字专用**的——
    直接把它当 `case_generator` 传进去，每次调用都抛 `TypeError`，
    然后被 `generate_for_operations` 的"部分失败不阻塞全局"接住：
    Run 跑完、报告出来、那个接口一条用例都没有。

    ## 断言的是"抛了什么错"，而不是"没抛错"

    这里连的是一个**不存在**的 LLM 主机，所以它必然失败。关键在于
    **失败的方式**：

    * 契约对了 → 走到网络层 → 抛 `LLMError`（`gateway_unreachable` 那一类）
    * 契约错了 → 抛 `TypeError`

    只断言"抛了异常"会让两种情形都通过——那正是这个 bug 当年藏身的地方。
    """
    from testflow.agents.llm_case_gen import LlmCaseGenerator
    from testflow.llm.client import LLMError

    dimensions = [DimensionSpec(dimension="value_valid", category="normal", expected_status=[200])]
    operation = _FakeOperation()

    generator = built_client["provider"]
    assert isinstance(generator, LlmCaseGenerator), "图要的是 CaseGenerator，不是 LLMClient"

    with pytest.raises(LLMError):
        await generator.generate(operation, dimensions)


class _FakeOperation:
    """`Operation` 行的最小替身（装配测试不该需要数据库）。"""

    method = "POST"
    path = "/echo"
    parameters_schema = None
    request_body_schema = {"content": {"application/json": {"schema": {"type": "object"}}}}
    responses_schema = {"200": {"description": "ok"}}
