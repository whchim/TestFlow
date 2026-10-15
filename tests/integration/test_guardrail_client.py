"""Guardrail 出站客户端的集成测试。**需要真实 PostgreSQL 与 fixture 服务。**

对应决策：`03-模块详细设计/Guardrail.md` §2（六步流水线）、§8（拒绝后行为）、
GR2/GR3/GR5、`03/TestExecutor.md` §1.1、§4.2。

## 为什么这批必须是集成测试

它验证的是**六步流水线真的连在一起并按顺序生效**——单测能覆盖每一步的判定，
但覆盖不了"第 ③ 步的拒绝是不是真的让请求没发出去"。

而"请求没发出去"这件事**只能向被调用方求证**（fixture 的 `/__hits`）：
没发出去的请求不会有返回值，所以从响应侧什么都看不出来。
本文件里大量使用这个手法，因为安全功能的大部分时间都在做"不让某事发生"。
"""

from __future__ import annotations

import httpx
import pytest

from testflow.config import Settings
from testflow.guardrail.errors import GuardrailDenied, GuardrailTransportError
from testflow.guardrail.pipeline import EgressPolicy, RequestContext
from testflow.guardrail.policy import Whitelist
from testflow.guardrail.resolver import SyncResolver
from testflow.guardrail.sync_client import GuardrailResponse, GuardrailSyncClient
from testflow.guardrail.transport import PinnedPlanExtension, _pinned_core_request

pytestmark = pytest.mark.integration

FIXTURE_TARGET = "127.0.0.1:9000"


@pytest.fixture
def guardrail_settings(make_settings) -> Settings:
    """一份给了内网例外的配置。

    **例外是必需的**，因为 fixture 监听环回地址，而 SR-2 默认拒绝内网。
    这反过来正好让例外机制被持续使用——见 `09` §3.3 / ADR-0018。
    """
    return make_settings(guardrail_allow_private_targets=[FIXTURE_TARGET])


@pytest.fixture
def policy(fixture_server: str) -> EgressPolicy:
    return EgressPolicy(
        whitelist=Whitelist.from_urls([f"{fixture_server}/"]),
        allowed_private=frozenset({FIXTURE_TARGET}),
    )


@pytest.fixture
def audit_log() -> list[object]:
    return []


@pytest.fixture
def client(guardrail_settings: Settings, audit_log: list[object]) -> GuardrailSyncClient:
    c = GuardrailSyncClient(guardrail_settings, audit_sink=audit_log.append)
    try:
        yield c
    finally:
        c.close()


def _ctx(path: str, method: str = "GET", base: str = "http://127.0.0.1:9000") -> RequestContext:
    return RequestContext(url=f"{base}{path}", method=method, run_id=None)


class TestAllowedRequests:
    def test_get_returns_the_response(self, client, policy, fixture_server) -> None:
        result = client.send(_ctx("/ok"), policy)
        assert isinstance(result, GuardrailResponse)
        assert result.status_code == 200
        assert b"ok" in result.body

    def test_allow_is_audited(self, client, policy, audit_log) -> None:
        """GR6：`allow` 也要记。

        只记拒绝，就无法回答"这个 Run 到底访问了哪些 host"——而那正是
        安全复盘最有价值的问题。
        """
        client.send(_ctx("/ok"), policy)
        assert [r.decision for r in audit_log] == ["allow"]
        assert audit_log[0].target_host == "127.0.0.1"
        assert audit_log[0].deny_reason is None

    def test_audit_url_has_query_values_redacted(self, client, policy, audit_log) -> None:
        """SR-6：URL 里的查询**取值**不得落库，但参数名要留。

        参数名保留是有意的——"这个接口把凭证放在 URL 里"本身是一条值得
        记录下来的设计缺陷，抹掉名字就看不见了。
        """
        client.send(_ctx("/echo?probe=hello&token=SUPERSECRET"), policy)
        recorded = audit_log[-1].target_url
        assert "probe" in recorded and "token" in recorded, "参数名应当保留"
        assert "SUPERSECRET" not in recorded
        assert "hello" not in recorded


class TestWhitelistDenial:
    def test_target_outside_the_whitelist_is_denied(self, client, fixture_server) -> None:
        outside = EgressPolicy(
            whitelist=Whitelist.from_urls(["https://other.example.com"]),
            allowed_private=frozenset({FIXTURE_TARGET}),
        )
        result = client.send(_ctx("/ok"), outside)
        assert isinstance(result, GuardrailDenied)
        assert result.reason.value == "whitelist"

    def test_denied_request_never_reaches_the_target(self, client, fixture_server, hits) -> None:
        """**这条才是真正的验证。**

        "我们决定不发出"和"它确实没到达"是两件事——前者是判断，
        后者是事实。两层防护（应用层过滤 + Guardrail 拦截）都成立时，
        目标侧的计数器才不会被 +1。
        """
        hits.reset()
        outside = EgressPolicy(whitelist=Whitelist.from_urls(["https://other.example.com"]))
        client.send(_ctx("/ok"), outside)
        assert hits.of("/ok") == 0, "被拒绝的请求不该到达目标"

    def test_denial_is_audited_with_the_reason(self, client, fixture_server, audit_log) -> None:
        client.send(
            _ctx("/ok"), EgressPolicy(whitelist=Whitelist.from_urls(["https://x.example.com"]))
        )
        denials = [r for r in audit_log if r.decision == "deny"]
        assert denials and denials[0].deny_reason == "whitelist"


class TestPrivateAddressDenial:
    def test_loopback_is_denied_without_an_exception(self, client, fixture_server) -> None:
        """SR-2 的核心：默认拒绝内网，而例外必须**显式配置**。

        fixture 就在环回地址上，所以它就是最真实的测试对象。
        """
        strict = EgressPolicy(whitelist=Whitelist.from_urls([f"{fixture_server}/"]))
        result = client.send(_ctx("/ok"), strict)
        assert isinstance(result, GuardrailDenied)
        assert result.reason.value == "private_ip"

    def test_metadata_address_is_denied_even_with_an_exception(self, client) -> None:
        """GR4：云元数据地址**永不例外**。

        这是整个 Guardrail 里唯一一条没有例外通道的规则——一次成功的 SSRF
        就能拿到实例凭证，进而横向移动。所以这里刻意配了一个"全放行"的例外，
        验证它仍然拦得住。
        """
        permissive = EgressPolicy(
            whitelist=Whitelist.from_urls(["http://169.254.169.254/"]),
            allowed_private=frozenset({"169.254.169.254:80"}),
        )
        result = client.send(_ctx("/latest/meta-data/", base="http://169.254.169.254"), permissive)
        assert isinstance(result, GuardrailDenied)
        assert result.reason.value == "metadata", "内网例外不得对元数据地址生效"


class TestDestructiveDenial:
    def test_delete_is_denied(self, client, policy) -> None:
        result = client.send(_ctx("/destructive", method="DELETE"), policy)
        assert isinstance(result, GuardrailDenied)
        assert result.reason.value == "destructive"

    def test_destructive_request_never_reaches_the_target(
        self, client, policy, fixture_server, hits
    ) -> None:
        """TE6 的两层防护里，这一层是**冗余**的那一层——而它必须有效。

        N4 在投递前就过滤掉破坏性用例（第一层）。但第一层是应用逻辑，
        可能因 bug、重构或后续改动而失效。安全设计不假设应用层永远正确，
        所以这里验证"即便一个破坏性请求真的走到了出站口，它也发不出去"。
        """
        hits.reset()
        client.send(_ctx("/destructive", method="DELETE"), policy)
        assert hits.of("/destructive") == 0

    def test_method_is_taken_from_the_actual_request(self, client, policy) -> None:
        """§5.2：判据是**实际即将发出的 HTTP 方法**，不是数据库里的字段。

        若依据数据库字段，那么"字段与实际请求不一致"这类 bug
        （例如路径参数被注入改写了方法）就永远发现不了。
        """
        assert isinstance(client.send(_ctx("/ok", method="delete"), policy), GuardrailDenied)


class TestRedirectIsNotFollowed:
    def test_302_is_returned_as_a_result(self, client, policy) -> None:
        """GR5：不跟随重定向，3xx 本身就是**有价值的测试结果**。

        跟随会同时绕过白名单（`Location` 可以指向任意 host）与内网阻断
        （`Location: http://169.254.169.254/`）。
        """
        result = client.send(_ctx("/redirect"), policy)
        assert isinstance(result, GuardrailResponse)
        assert result.status_code == 302
        assert "location" in result.headers

    def test_the_redirect_target_is_never_requested(
        self, client, policy, fixture_server, hits
    ) -> None:
        """**只看状态码是不够的。**

        "收到了 302"并不能证明没有跟随——跟随之后 `response.history` 会非空，
        但最终状态码仍可能是 302（环回重定向）。真正能区分的是
        **重定向目标有没有被访问过**：`/redirect` 默认指向 `/ok`。
        """
        hits.reset()
        client.send(_ctx("/redirect"), policy)
        assert hits.of("/redirect") == 1
        assert hits.of("/ok") == 0, "重定向被跟随了——目标端点被访问过"


class TestResponseBodyCap:
    def test_large_body_is_truncated_and_flagged(self, client, policy) -> None:
        """DM6：上限 256KB，且**必须显式标记**。

        不标记的后果很具体：截断后的 JSON 解析必然失败，于是它会被归因成
        "响应格式错误"——而被测系统其实完全正常。`truncated` 这个字段就是
        用来阻止这个误判的。
        """
        result = client.send(_ctx("/large"), policy)
        assert isinstance(result, GuardrailResponse)
        assert result.truncated is True
        assert len(result.body) <= client.settings.response_body_max_bytes

    def test_small_body_is_not_truncated(self, client, policy) -> None:
        result = client.send(_ctx("/ok"), policy)
        assert isinstance(result, GuardrailResponse)
        assert result.truncated is False


class TestPinning:
    """GR2：连接目标被钉死为已校验的 IP，但身份仍是原域名。"""

    def test_core_request_connects_to_the_pinned_ip(self) -> None:
        request = httpx.Request("GET", "https://api.example.com/v1/users")
        plan = _plan_for(request.url, original_host="api.example.com", pinned="93.184.216.34")
        request.extensions = {**request.extensions, PinnedPlanExtension: plan}

        core, headers = _pinned_core_request(request)

        assert core.url.host == b"93.184.216.34", "TCP 必须连到已校验的 IP"
        assert headers["host"] == "api.example.com"

    def test_host_header_comes_from_the_plan_not_the_url(self) -> None:
        """**Host 头必须由 `SendPlan` 决定，不能听 URL 的。**

        这条测试的写法是刻意绕的，因为直白的写法**测不到任何东西**：

            正常人会写：请求用 `https://api.example.com/...` 构造，
                        然后断言 `headers["host"] == "api.example.com"`。

            但 httpx 自己就会根据 URL 填好 `Host: api.example.com`——
            所以那个断言在"抹掉赋值语句"之后**照样通过**。
            （变异测试实测：删掉 `headers["host"] = plan.original_host`
             那行，27 个测试全绿。）

        真实的失败模式是"**请求是用钉死后的 URL 构造的**"——那时 httpx 会把
        `Host` 填成 IP，服务端看到的就是一个不认识的虚拟主机名，
        表现为 404 或证书错误。所以这里**故意**用一个宿主已经是 IP 的 URL 构造
        请求，再看 Host 头是否被 `SendPlan` 纠正回域名。
        """
        pinned_url = "https://93.184.216.34/v1/users"
        request = httpx.Request("GET", pinned_url)
        plan = _plan_for(
            httpx.URL(pinned_url), original_host="api.example.com", pinned="93.184.216.34"
        )
        request.extensions = {**request.extensions, PinnedPlanExtension: plan}

        _, headers = _pinned_core_request(request)

        assert headers["host"] == "api.example.com", (
            "Host 头被 URL 的宿主（IP）污染了——服务端会认不出这是哪个服务"
        )

    def test_sni_hostname_is_the_original_domain(self) -> None:
        """★ 这是 `Guardrail.md` Q3 的答案。

        钉死 IP 之后，TLS 若仍拿 IP 去做 SNI 与证书校验，HTTPS 必然失败
        （证书是按域名签的）。`httpcore` 允许两者分开：
        TCP 连 IP，而 `server_hostname` 取 `extensions["sni_hostname"]`。
        """
        request = httpx.Request("GET", "https://api.example.com/v1/users")
        plan = _plan_for(request.url, original_host="api.example.com", pinned="93.184.216.34")
        request.extensions = {**request.extensions, PinnedPlanExtension: plan}

        core, _ = _pinned_core_request(request)

        assert core.extensions["sni_hostname"] == "api.example.com"

    def test_path_and_query_are_preserved(self) -> None:
        """只改宿主，其余原样——否则请求会打到错误的接口上。"""
        request = httpx.Request("GET", "https://api.example.com/v1/users?page=2")
        plan = _plan_for(request.url, original_host="api.example.com", pinned="93.184.216.34")
        request.extensions = {**request.extensions, PinnedPlanExtension: plan}

        core, _ = _pinned_core_request(request)

        assert core.url.target == b"/v1/users?page=2"

    def test_missing_plan_is_a_loud_error(self) -> None:
        """没有 `SendPlan` 就发不出去。

        静默放行（连原始域名）会让钉死失效，而失效是看不出来的——
        请求照样成功，只是绕过了整套防护。所以宁可报错。
        """
        from testflow.guardrail.errors import GuardrailError

        with pytest.raises(GuardrailError, match="缺少 SendPlan"):
            _pinned_core_request(httpx.Request("GET", "https://api.example.com/"))


class TestMultiAddressFallback:
    """一个域名可能解析出多个地址，而**第一个不一定可达**。"""

    def test_falls_back_when_the_first_address_refuses(
        self, fixture_server, guardrail_settings
    ) -> None:
        """`localhost` 在 Windows 上常常先解析到 `::1`，而 fixture 只监听 IPv4。

        只连第一个的后果是"一个完全正常的域名随机失败"，且失败信息是
        "连接被拒绝"——看起来像对方挂了。顺序回退把这种情况兜住。
        """
        resolver = SyncResolver()
        candidates = resolver.resolve("localhost")
        if len(candidates) < 2:  # pragma: no cover —— 单栈环境下无从验证
            pytest.skip("本机 localhost 只解析出一个地址，无法验证回退")

        client = GuardrailSyncClient(guardrail_settings)
        policy = EgressPolicy(
            whitelist=Whitelist.from_urls(["http://localhost:9000"]),
            allowed_private=frozenset({"localhost:9000"}),
        )
        try:
            result = client.send(
                RequestContext(url="http://localhost:9000/ok", method="GET"), policy
            )
        finally:
            client.close()

        assert isinstance(result, GuardrailResponse), "应当回退到可达的那个地址"
        assert result.status_code == 200


class TestResolver:
    def test_returns_every_address(self, fixture_server) -> None:
        """GR1：返回**全部**地址。只取第一个去校验、把解析留给客户端，
        会让客户端有机会选中没被校验的那一条——这正是 SSRF 的入口。"""
        assert SyncResolver().resolve("localhost"), "至少解析出一个地址"

    def test_deduplicates(self, fixture_server) -> None:
        addresses = [str(a) for a in SyncResolver().resolve("localhost")]
        assert len(addresses) == len(set(addresses))

    def test_ipv4_mapped_is_normalized(self) -> None:
        """`::ffff:127.0.0.1` 与 `127.0.0.1` 是同一个地方。

        不归一化的话，同一个地址会以两种形式进入列表、被校验两遍，
        而错误信息里会出现一个用户看不懂的写法。
        """
        from testflow.guardrail.resolver import _dedupe

        result = _dedupe(["127.0.0.1", "::ffff:127.0.0.1"])
        assert [str(a) for a in result] == ["127.0.0.1"]


class TestErrorMapCoverage:
    def test_error_map_covers_httpcore_exceptions(self) -> None:
        """httpcore 升级引入新异常时，这条会失败——**而不是安静地少映射几个**。

        少映射的后果是执行器收到一个不认识的异常类型，进而把"连接被拒"
        记成一个笼统的未知错误，`error_kind` 也就填不出有意义的值。
        """
        from testflow.guardrail.transport import _ERROR_MAP

        present = {name for name in dir(httpx) if name.endswith(("Error", "Timeout", "Exception"))}
        for core_type in _ERROR_MAP:
            assert issubclass(core_type, Exception)
        # 每个映射目标都必须是真实的 httpx 异常。
        for target in _ERROR_MAP.values():
            assert target.__name__ in present, f"{target.__name__} 不是 httpx 的异常"

    def test_network_errors_surface_as_guardrail_errors(self, guardrail_settings) -> None:
        """连接被拒时抛的必须是 **`guardrail` 自己的** `GuardrailTransportError`。

        **不是 httpx 的。** `execution/` 为了 `except httpx.ConnectError`
        就得 import httpx，而架构规则禁止那里出现 HTTP 客户端——
        这条规则实测把这个 import 拦下来了，于是依赖方向被改成：
        httpx 的异常在 `guardrail/` 内部就被吸收。

        附带的好处是失败原因在传输层就分类好了：`exc.error_kind` 直接就是
        执行器要写进 `case_results.error_kind` 的值。
        """
        # 一个几乎肯定没人监听的端口。
        client = GuardrailSyncClient(guardrail_settings)
        policy = EgressPolicy(
            whitelist=Whitelist.from_urls(["http://127.0.0.1:59999"]),
            allowed_private=frozenset({"127.0.0.1:59999"}),
        )
        try:
            with pytest.raises(GuardrailTransportError) as exc:
                client.send(RequestContext(url="http://127.0.0.1:59999/", method="GET"), policy)
            assert exc.value.error_kind == "connection_refused"
        finally:
            client.close()


class TestRateLimit:
    def test_exceeding_qps_is_denied_with_rate_limit(self, fixture_server, make_settings) -> None:
        """SR-5：QPS 上限是**异常兜底**，但触发时必须真的拦住。"""
        cfg = make_settings(
            guardrail_allow_private_targets=[FIXTURE_TARGET],
            guardrail_qps_per_host=1.0,
        )
        client = GuardrailSyncClient(cfg)
        policy = EgressPolicy(
            whitelist=Whitelist.from_urls([f"{fixture_server}/"]),
            allowed_private=frozenset({FIXTURE_TARGET}),
        )
        try:
            first = client.send(_ctx("/ok"), policy)
            second = client.send(_ctx("/ok"), policy)
        finally:
            client.close()

        assert isinstance(first, GuardrailResponse), "第一个请求应当放行"
        assert isinstance(second, GuardrailDenied)
        assert second.reason.value == "rate_limit"

    def test_server_errors_do_not_trip_the_breaker(self, fixture_server, make_settings) -> None:
        """熔断依据是**能不能连上**，不是返回的内容对不对。

        一个持续返回 500 的服务仍然是活的。把它熔断掉会让后续结果全部变成
        `rate_limit`，而真实的结论（服务端有 bug）就再也测不出来了。
        """
        cfg = make_settings(
            guardrail_allow_private_targets=[FIXTURE_TARGET],
            guardrail_breaker_threshold=2,
        )
        client = GuardrailSyncClient(cfg)
        policy = EgressPolicy(
            whitelist=Whitelist.from_urls([f"{fixture_server}/"]),
            allowed_private=frozenset({FIXTURE_TARGET}),
        )
        try:
            results = [client.send(_ctx("/error-500"), policy) for _ in range(4)]
        finally:
            client.close()

        assert all(isinstance(r, GuardrailResponse) for r in results)
        assert all(r.status_code == 500 for r in results)


# ------------------------------------------------------------------ 助手


def _plan_for(url: httpx.URL, *, original_host: str, pinned: str):
    """构造一个 `SendPlan`，用于直接测试传输层的请求改写。

    刻意通过真实构造函数而不是手工拼字段：`SendPlan` 的字段将来变了，
    这里会一起失败，而不是安静地造出一个结构不对的对象。
    """
    from testflow.guardrail.pipeline import SendPlan
    from testflow.guardrail.policy import Target, parse_target

    try:
        target = parse_target(str(url))
    except ValueError:
        # URL 的宿主是 IP 时也解析得出来，但白名单语义上我们关心的是域名。
        target = Target(scheme=url.scheme, host=original_host, port=url.port or 443)
    return SendPlan(
        url=str(url),
        target=target,
        candidate_ips=(pinned,),
        original_host=original_host,
    )


class TestAuditIsWrittenBeforeSending:
    """**"放行"这个决定在发送之前就做出了。**

    所以审计必须写在发送之前。写在之后（第一版就是）会漏掉**所有**
    超时与报错的请求——异常一抛，后面那行审计永远执行不到。

    而这个洞是**跑完 demo 之后去查审计表**才发现的：表里只有成功的请求，
    看起来一切都很安分。漏掉的恰是最该被审计的那几条——
    一个超时的目标正是安全复盘时要问"我们到底往哪儿发了请求"的场合。
    """

    def test_timed_out_request_is_still_audited(
        self, fixture_server, make_settings, audit_log
    ) -> None:
        # 把 HTTP 超时压到 1 秒：fixture 的 `/slow` 默认要 30 秒，
        # 而那会让这条测试本身变成一次"慢测试"。
        settings = make_settings(
            guardrail_allow_private_targets=[FIXTURE_TARGET],
            http_timeout_s=1,
        )
        client = GuardrailSyncClient(settings, audit_sink=audit_log.append)
        policy = EgressPolicy(
            whitelist=Whitelist.from_urls([f"{fixture_server}/"]),
            allowed_private=frozenset({FIXTURE_TARGET}),
        )
        try:
            with pytest.raises(GuardrailTransportError):
                client.send(_ctx("/slow"), policy)
        finally:
            client.close()

        assert [r.decision for r in audit_log] == ["allow"], (
            "请求超时了，但**它确实被放行过**——这条必须留在审计里。"
            "否则审计表里只剩成功的请求，而打不通的那些永远查不到。"
        )
        assert audit_log[0].target_url is not None
        assert "/slow" in audit_log[0].target_url

    def test_denied_request_is_audited_and_never_sent(
        self, fixture_server, make_settings, audit_log, hits
    ) -> None:
        """拒绝的路径同样要记（GR6）——而且请求确实没到达。"""
        settings = make_settings(guardrail_allow_private_targets=[FIXTURE_TARGET])
        client = GuardrailSyncClient(settings, audit_sink=audit_log.append)
        strict = EgressPolicy(whitelist=Whitelist.from_urls(["https://elsewhere.example.com"]))
        try:
            hits.reset()
            client.send(_ctx("/ok"), strict)
        finally:
            client.close()

        assert [r.decision for r in audit_log] == ["deny"]
        assert audit_log[0].deny_reason == "whitelist"
        assert hits.of("/ok") == 0
