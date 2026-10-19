"""Guardrail 客户端的单元测试（不需要网络）。

对应决策：`03/模块详细设计/Guardrail.md` GR8（两形态共用校验）、§7（限流熔断）、
GR6（审计脱敏）、`02` D2（LLM 出站同样经 Guardrail）。

**这批测试的价值在于"便宜"**：判定逻辑是纯函数，所以可以在毫秒级跑完
几百个地址分类用例。真正需要网络的只剩集成测试里那几条——
而它们验的是"判定结果有没有被正确执行"。

`_dedupe` 那条顺带验了一个容易漏的边界：`::ffff:127.0.0.1` 与 `127.0.0.1`
是**同一个地方**，不归一化会让同一个地址以两种形式被校验两遍。
"""

from __future__ import annotations

import httpcore2
import httpx2
import pytest

from testflow.guardrail.audit import redact_url, request_digest
from testflow.guardrail.errors import (
    GuardrailError,
    GuardrailTimeout,
    GuardrailTransportError,
)
from testflow.guardrail.pipeline import EgressPolicy, RequestContext, SendPlan, evaluate
from testflow.guardrail.policy import Decision, Whitelist
from testflow.guardrail.ratelimit import RateLimiter
from testflow.guardrail.resolver import _dedupe

ANY_PUBLIC = ["93.184.216.34"]


def _policy(*urls: str, allowed_private: frozenset[str] = frozenset()) -> EgressPolicy:
    return EgressPolicy(whitelist=Whitelist.from_urls(urls), allowed_private=allowed_private)


class TestEvaluateStepOrder:
    """GR8：判定是一个链条，**顺序本身是设计的一部分**。"""

    def test_whitelist_is_checked_first(self) -> None:
        """白名单最便宜且最硬（精确匹配、无例外通道），放最前。

        一个不在名单里的目标**不该触发 DNS 解析**——那本身就是一次外发，
        而为一个注定被拒的目标做外发毫无意义。
        """
        result = evaluate(
            RequestContext(url="http://not-listed.example.com/", method="GET"),
            ANY_PUBLIC,
            whitelist=Whitelist.from_urls(["http://listed.example.com"]),
        )
        assert isinstance(result, Decision) and result.reason.value == "whitelist"

    def test_destructive_method_is_checked_before_address(self) -> None:
        """SR-3 在地址校验之前：破坏性方法无需解析域名就能判。"""
        result = evaluate(
            RequestContext(url="http://listed.example.com/", method="DELETE"),
            ANY_PUBLIC,
            whitelist=Whitelist.from_urls(["http://listed.example.com"]),
        )
        assert isinstance(result, Decision) and result.reason.value == "destructive"

    def test_all_checks_passing_yields_a_send_plan(self) -> None:
        result = evaluate(
            RequestContext(url="https://listed.example.com/v1", method="GET"),
            ANY_PUBLIC,
            whitelist=Whitelist.from_urls(["https://listed.example.com"]),
        )
        assert isinstance(result, SendPlan)
        assert result.pinned_ip == "93.184.216.34"
        assert result.original_host == "listed.example.com"

    def test_unparseable_target_is_a_whitelist_denial(self) -> None:
        """解析不了的目标一律拒绝——"解析不了"在这里等于"明确不能用"。

        `Whitelist.from_urls` 本身也会拒绝非 http(s) 的条目，
        所以这里的白名单用正常 URL 构造，而被判定的 URL 是异常的。
        """
        result = evaluate(
            RequestContext(url="ftp://listed.example.com/", method="GET"),
            ANY_PUBLIC,
            whitelist=Whitelist.from_urls(["http://listed.example.com"]),
        )
        assert isinstance(result, Decision) and result.reason.value == "whitelist"


class TestSendPlanCarriesAllValidatedAddresses:
    """GR2：**校验全部地址**，连接时按顺序尝试。"""

    def test_every_resolved_address_becomes_a_candidate(self) -> None:
        plan = evaluate(
            RequestContext(url="https://listed.example.com/", method="GET"),
            ["93.184.216.34", "93.184.216.35"],
            whitelist=Whitelist.from_urls(["https://listed.example.com"]),
        )
        assert isinstance(plan, SendPlan)
        assert plan.candidate_ips == ("93.184.216.34", "93.184.216.35")

    def test_one_bad_address_among_good_ones_denies_everything(self) -> None:
        """**只校验第一个是不够的。**

        一个域名返回多条 A 记录时，客户端可能选中**没被校验的那一条**：

            我们校验：api.example.com → 1.2.3.4        ✅ 公网
            实际连接：api.example.com → 127.0.0.1      ❌ 没人检查过

        所以任意一条不合规就必须整体拒绝。
        """
        result = evaluate(
            RequestContext(url="https://listed.example.com/", method="GET"),
            ["93.184.216.34", "127.0.0.1"],
            whitelist=Whitelist.from_urls(["https://listed.example.com"]),
        )
        assert isinstance(result, Decision) and result.reason.value == "private_ip"

    def test_no_resolved_addresses_is_denied(self) -> None:
        """解析不出任何地址 → 无法校验 → 按拒绝处理（fail-closed）。"""
        result = evaluate(
            RequestContext(url="https://listed.example.com/", method="GET"),
            [],
            whitelist=Whitelist.from_urls(["https://listed.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed


class TestPrivateExceptionIsScoped:
    """ADR-0018：例外**只作用于 SR-2**，且只能来自配置。"""

    def test_exception_allows_a_private_target(self) -> None:
        result = evaluate(
            RequestContext(url="http://127.0.0.1:9000/ok", method="GET"),
            ["127.0.0.1"],
            whitelist=Whitelist.from_urls(["http://127.0.0.1:9000"]),
            allowed_private=frozenset({"127.0.0.1:9000"}),
        )
        assert isinstance(result, SendPlan)

    def test_exception_does_not_bypass_the_whitelist(self) -> None:
        """**例外只放开 SR-2。** 若它能顺带放开 SR-1，那份例外名单就变成了
        "可以访问任意地址"——而配置它的人以为自己只是开了个调试后门。"""
        result = evaluate(
            RequestContext(url="http://127.0.0.1:9000/ok", method="GET"),
            ["127.0.0.1"],
            whitelist=Whitelist.from_urls(["http://127.0.0.1:9999"]),
            allowed_private=frozenset({"127.0.0.1:9000"}),
        )
        assert isinstance(result, Decision) and result.reason.value == "whitelist"

    def test_exception_never_covers_metadata_addresses(self) -> None:
        """GR4：元数据地址**永不例外**。

        这是整个 Guardrail 里唯一一条没有例外通道的规则。一次成功的 SSRF
        就能拿到实例凭证。即便有人真的把它写进了例外名单，也必须拦下。
        """
        result = evaluate(
            RequestContext(url="http://169.254.169.254/", method="GET"),
            ["169.254.169.254"],
            whitelist=Whitelist.from_urls(["http://169.254.169.254"]),
            allowed_private=frozenset({"169.254.169.254", "169.254.169.254:80"}),
        )
        assert isinstance(result, Decision) and result.reason.value == "metadata"


class TestResolverNormalization:
    def test_ipv4_mapped_and_ipv4_are_one_address(self) -> None:
        """`::ffff:127.0.0.1` 与 `127.0.0.1` 是同一个地方。

        不归一化 → 同一个地址以两种形式出现 → 校验两遍，
        而且错误信息里会冒出一个用户看不懂的写法。
        """
        assert [str(a) for a in _dedupe(["127.0.0.1", "::ffff:127.0.0.1"])] == ["127.0.0.1"]

    def test_order_is_preserved(self) -> None:
        """顺序决定"先试哪个地址"，随机化会让失败变得不可复现。"""
        assert [str(a) for a in _dedupe(["93.184.216.35", "93.184.216.34"])] == [
            "93.184.216.35",
            "93.184.216.34",
        ]

    def test_zone_id_is_stripped(self) -> None:
        """`fe80::1%eth0` 里的 `%eth0` 是链路本地地址的作用域标识，不是地址的一部分。"""
        assert [str(a) for a in _dedupe(["fe80::1%eth0", "fe80::1%eth1"])] == ["fe80::1"]


class TestRateLimiter:
    def test_allows_up_to_the_qps_then_denies(self) -> None:
        limiter = RateLimiter(
            qps_per_host=2.0,
            max_concurrency_per_host=10,
            breaker_threshold=5,
            breaker_cooldown_s=60,
        )
        assert limiter.acquire("a:80").allowed
        assert limiter.acquire("a:80").allowed
        assert not limiter.acquire("a:80").allowed

    def test_hosts_are_independent(self) -> None:
        """限流是**按 host** 的——一个被限住的域名不该影响其他域名。"""
        limiter = RateLimiter(
            qps_per_host=1.0,
            max_concurrency_per_host=10,
            breaker_threshold=5,
            breaker_cooldown_s=60,
        )
        limiter.acquire("a:80")
        assert not limiter.acquire("a:80").allowed
        assert limiter.acquire("b:80").allowed

    def test_concurrency_limit(self) -> None:
        limiter = RateLimiter(
            qps_per_host=100.0,
            max_concurrency_per_host=1,
            breaker_threshold=5,
            breaker_cooldown_s=60,
        )
        assert limiter.acquire("a:80").allowed
        second = limiter.acquire("a:80")
        assert not second.allowed and "在途" in second.detail

    def test_release_frees_the_slot(self) -> None:
        limiter = RateLimiter(
            qps_per_host=100.0,
            max_concurrency_per_host=1,
            breaker_threshold=5,
            breaker_cooldown_s=60,
        )
        limiter.acquire("a:80")
        limiter.release("a:80", success=True)
        assert limiter.acquire("a:80").allowed

    def test_consecutive_failures_open_the_breaker(self) -> None:
        limiter = RateLimiter(
            qps_per_host=100.0,
            max_concurrency_per_host=10,
            breaker_threshold=2,
            breaker_cooldown_s=60,
        )
        for _ in range(2):
            limiter.acquire("a:80")
            limiter.release("a:80", success=False)

        verdict = limiter.acquire("a:80")
        assert not verdict.allowed and "熔断" in verdict.detail

    def test_success_resets_the_failure_count(self) -> None:
        """偶发失败不该累积成熔断——只有**连续**失败才算目标挂了。"""
        limiter = RateLimiter(
            qps_per_host=100.0,
            max_concurrency_per_host=10,
            breaker_threshold=3,
            breaker_cooldown_s=60,
        )
        limiter.acquire("a:80")
        limiter.release("a:80", success=False)
        limiter.acquire("a:80")
        limiter.release("a:80", success=True)  # 成功 → 计数清零
        limiter.acquire("a:80")
        limiter.release("a:80", success=False)

        assert limiter.acquire("a:80").allowed, "未达连续阈值，不该熔断"

    def test_breaker_half_opens_after_cooldown(self) -> None:
        limiter = RateLimiter(
            qps_per_host=100.0,
            max_concurrency_per_host=10,
            breaker_threshold=1,
            breaker_cooldown_s=0,  # 冷却立即结束，模拟时间流逝
        )
        limiter.acquire("a:80")
        limiter.release("a:80", success=False)
        assert limiter.acquire("a:80").allowed, "冷却结束后应当放一个请求去探路"


class TestAuditRedaction:
    def test_all_values_are_replaced_but_all_names_kept(self) -> None:
        """SR-6。**全部取值都擦，全部参数名都留。**

        不区分"敏感/不敏感"是刻意的：我们无法知道任意一个用户的 API 里
        哪个参数敏感（`?user_id=42` 在医疗接口里就是 PII）。按字段名去猜必然漏，
        而漏一次就是一次真实的泄露。

        参数名全留则是纯粹的信息收益——"这个接口发了哪些参数"仍然可见。
        """
        assert redact_url("https://a.com/p?token=SECRET&page=2") == (
            "https://a.com/p?token=%5BREDACTED%5D&page=%5BREDACTED%5D"
        )

    def test_no_query_value_survives(self) -> None:
        """反向断言：**秘密片段不得以任何形式留在结果里**。

        正向断言（"结果包含 [REDACTED]"）在掩码没盖住秘密时**照样通过**——
        这个教训在 `observability/logging.py` 上已经吃过一次。
        """
        result = redact_url("https://a.com/p?api_key=sk-abcdef123456&q=secret")
        assert "sk-abcdef123456" not in result
        assert "secret" not in result

    def test_url_without_query_is_unchanged(self) -> None:
        assert redact_url("https://a.com/p") == "https://a.com/p"

    def test_digest_is_stable_and_not_the_plaintext(self) -> None:
        """指纹是**哈希**——即便脱敏漏掉了什么，它也不会以可读形式落库。

        这让脱敏多了一道不依赖模式匹配的兜底：模式匹配靠"猜到哪些字段敏感"，
        而哈希不靠猜。
        """
        d = request_digest("GET", "https://a.com/p?token=SECRET")
        assert len(d) == 64
        assert "SECRET" not in d
        assert d == request_digest("GET", "https://a.com/p?token=SECRET")
        assert d != request_digest("POST", "https://a.com/p?token=SECRET")


class TestLlmEgressAllowlist:
    """`02` D2：LLM 那条出站路径同样要经过 Guardrail。"""

    @pytest.fixture
    def captured(self) -> list[object]:
        return []

    def _client(self, settings, captured, base_url: str):
        """用 MockTransport 顶掉真实传输层——**测试不该真的外发请求**。"""
        from testflow.guardrail.async_client import _AllowlistAsyncTransport
        from testflow.guardrail.audit import allow_record
        from testflow.guardrail.pipeline import RequestContext

        recorded: list[str] = []

        class _Spy(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
                recorded.append(str(request.url))
                return httpx2.Response(200, json={"ok": True})

        def sink(record) -> None:
            captured.append(record)

        transport = _AllowlistAsyncTransport(
            allowed_host_port=base_url,
            audit_sink=sink,
            delegate=_Spy(),
        )
        _ = (allow_record, RequestContext)  # 保持导入语义清晰
        return httpx2.AsyncClient(transport=transport, follow_redirects=False), recorded

    async def test_configured_address_is_allowed_and_audited(self, make_settings, captured) -> None:
        settings = make_settings(llm_base_url="https://llm.example.com/v1")
        client, recorded = self._client(settings, captured, "llm.example.com:443")

        response = await client.get("https://llm.example.com/v1/chat")

        assert response.status_code == 200
        assert recorded == ["https://llm.example.com/v1/chat"]
        assert len(captured) == 1
        assert captured[0].direction == "llm"
        assert captured[0].decision == "allow"

    async def test_other_addresses_raise_a_platform_error(self, make_settings) -> None:
        """**必须报错，不能安静地放过。**

        LLM 宿主来自配置、不由用户决定。走到这个分支说明有模块把 LLM 客户端
        用错了地方——那是一个需要立刻暴露的 bug，而不是一条安静的拒绝记录。
        """
        settings = make_settings(llm_base_url="https://llm.example.com/v1")
        client, recorded = self._client(settings, [], "llm.example.com:443")

        with pytest.raises(GuardrailError, match="不在允许范围内"):
            await client.get("https://evil.example.com/v1/chat")
        assert recorded == [], "被拒绝的请求不该到达底层传输层"

    async def test_denied_address_is_audited_before_raising(self, make_settings, captured) -> None:
        """GR6：**`deny` 也要记。**

        "必须报错"和"必须留痕"不冲突。抛出去之后就没有第二次机会了，
        而"我们曾试图往哪儿发"恰恰是安全复盘时最该能回答的问题——
        一条被拒绝的 LLM 出站说明某个模块在用错客户端，值得留下时间点。
        """
        settings = make_settings(llm_base_url="https://llm.example.com/v1")
        client, _ = self._client(settings, captured, "llm.example.com:443")

        with pytest.raises(GuardrailError, match="不在允许范围内"):
            await client.get("https://evil.example.com/v1/chat")

        assert [r.decision for r in captured] == ["deny"]
        assert captured[0].deny_reason == "whitelist"
        assert captured[0].direction == "llm", "两条出站路径靠这个字段区分"
        assert captured[0].target_host == "evil.example.com"

    def test_policy_is_derived_from_the_configured_base_url(self, make_settings) -> None:
        """白名单从 `llm_base_url` **派生**，而不是另加一个配置项。

        两个配置项描述同一件事迟早会不一致，而不一致的表现是
        "模型调用全部失败"——排查方向还会指向网络。
        """
        from testflow.guardrail.async_client import llm_egress_policy

        settings = make_settings(llm_base_url="https://llm.example.com/v1")
        targets = {str(t) for t in llm_egress_policy(settings).whitelist.targets}
        assert targets == {"https://llm.example.com:443"}


def _raising_client(exc: BaseException) -> httpx2.AsyncClient:
    """构造一个"一请求就抛"的客户端。**用 MockTransport 顶掉真实传输层。**"""

    class _Raiser(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise exc

    from testflow.guardrail.async_client import _AllowlistAsyncTransport

    return httpx2.AsyncClient(
        transport=_AllowlistAsyncTransport(
            allowed_host_port="llm.example.com:443",
            delegate=_Raiser(),
        ),
        follow_redirects=False,
    )


class TestLlmTransportErrorMapping:
    """异步路径的传输异常必须**归类成本项目自己的类型**（对齐 sync）。

    ## 为什么这不是"整洁"问题

    实测确认：原始 `httpcore2.ConnectError` 会**原样穿透 `openai` SDK**
    ——它的 `request_exceptions()` 只认 `httpx2` 的类型，而 `httpcore2`/`httpx2`
    是两个独立的异常体系。所以少了这层翻译，调用方拿到的既没有 `error_kind`、
    也匹配不上 `GuardrailTransportError`。

    而 `llm/qwen.py` 的**重试策略正挂在异常类型上**。少翻译一种异常的表现不是
    报错，而是"这个错从不重试"——它只会表现为偶发失败率偏高，看不出来。
    """

    async def test_network_error_is_translated_with_a_kind(self) -> None:
        """连接被拒要能归出 `connection_refused`——执行器据此填 `error_kind`。"""
        refused = ConnectionRefusedError(10061, "connection refused")
        exc = httpcore2.ConnectError("refused")
        exc.__cause__ = refused

        with pytest.raises(GuardrailTransportError) as caught:
            await _raising_client(exc).get("https://llm.example.com/v1/chat")

        assert caught.value.error_kind == "connection_refused"

    async def test_timeout_is_translated_to_the_timeout_subtype(self) -> None:
        """**超时必须落进 `GuardrailTimeout` 这个子类**，而不是笼统的传输错误。

        这个区分决定重试与否（`qwen.generate` 不重试超时：三次 120 秒超时
        = 360 秒，超过编排层的锁 TTL）。归错了的表现是"超时也会重试"，
        而那正是设 `max_retries=0` 要避免的事。

        `__cause__` 链上的内建 `TimeoutError` 是真实链路的样子：
        `httpcore2` 用 `raise TimeoutException(exc) from exc` 包装
        （见 `httpcore2/_exceptions.py::map_exceptions`）。
        """
        exc = httpcore2.ConnectTimeout("timed out")
        exc.__cause__ = TimeoutError("timed out")

        with pytest.raises(GuardrailTimeout) as caught:
            await _raising_client(exc).get("https://llm.example.com/v1/chat")

        assert caught.value.error_kind == "timeout"

    async def test_httpx2_errors_are_also_translated(self) -> None:
        """默认 delegate 是 `httpx2.AsyncHTTPTransport`，它把 httpcore2 包成
        `httpx2.*` 再抛——所以**两套类型都得认**。

        只认 httpcore2 的话，真实路径（默认 delegate）上的错误反而漏掉。
        """
        with pytest.raises(GuardrailTransportError):
            await _raising_client(httpx2.ConnectError("refused")).get(
                "https://llm.example.com/v1/chat"
            )

    async def test_unrelated_errors_are_not_translated(self) -> None:
        """**不能吞掉一切。** 一个编程错误被归成"网络故障"，
        会让排查方向从"我们的 bug"变成"网线有问题"。"""
        with pytest.raises(RuntimeError, match="我们自己写错了"):
            await _raising_client(RuntimeError("我们自己写错了")).get(
                "https://llm.example.com/v1/chat"
            )

    async def test_guardrail_errors_pass_through_untouched(self) -> None:
        """Guardrail 自己的异常不该被再包一层——那会让 `error_kind` 丢掉。"""
        with pytest.raises(GuardrailError, match="平台故障") as caught:
            await _raising_client(GuardrailError("平台故障")).get("https://llm.example.com/v1/chat")
        assert type(caught.value) is GuardrailError
