"""对抗性测试集（TS4）——**每个用例对应一种已知的绕过手法**。

对应决策：`09-测试策略.md` §4（TS4 表）、`03/模块详细设计/Guardrail.md` §4、
ADR-0019、GR1~GR4。

## 这个文件与 `tests/unit/test_guardrail_policy.py` 的分工

两边都测 Guardrail，但**组织方式不同**，而这个差别正是它们在同一个仓库里
并存而不冗余的理由：

* 单测**按函数组织**（`classify_ip`、`check_whitelist`、`evaluate`……），
  关心"这个函数的每个分支对不对"
* 本文件**按攻击手法组织**（元数据直取、IPv6 封装、端口扫描、子域混淆……），
  关心"某一种已知的绕过，在这个系统上可行不可行"

后者是安全审查时真正要看的那张清单：改动 Guardrail 之后，重读 TS4 表
逐行确认"这一条还没被打开"。按函数组织的测试做不到这件事——它会把
"IPv4 映射绕过"拆散到三个不同的类里，而审查者不知道它们是一件事。

所以这里有**刻意的重复**：同一段判定逻辑，在单测里被逐分支验证，
在这里被当作一条"攻击是否可行"的结论再验一次。

## 为什么这类测试值得单独存在

它们覆盖的都是**在被利用前没有任何症状**的漏洞。缺少 IPv4 映射检查的实现，
正常测试全绿、功能一切正确——只是 `::ffff:169.254.169.254` 能读到实例凭证。
功能测试永远发现不了它，只有专门构造的对抗性输入能。
"""

from __future__ import annotations

import pytest

from testflow.guardrail.pipeline import EgressPolicy, RequestContext, SendPlan, evaluate
from testflow.guardrail.policy import (
    Decision,
    DenyReason,
    IPClass,
    Whitelist,
    check_target_address,
    classify_ip,
    parse_target,
)
from testflow.guardrail.transport import (
    PeerVerificationError,
    _verify_peer,
    _VerifyingSyncBackend,
)

pytestmark = pytest.mark.security

#: 一个确实属于公网、不会被任何网段规则命中的地址。
PUBLIC = ["93.184.216.34"]


def _policy(*urls: str, allowed_private: frozenset[str] = frozenset()) -> EgressPolicy:
    return EgressPolicy(whitelist=Whitelist.from_urls(urls), allowed_private=allowed_private)


# --------------------------------------------------------------------- 元数据


class TestCloudMetadata:
    """云元数据是 SSRF 的头号目标：拿到它等于拿到实例的身份。"""

    @pytest.mark.parametrize(
        ("url", "resolved"),
        [
            # AWS / OpenStack 经典地址（169.254.0.0/16 链路本地）
            (
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                ["169.254.169.254"],
            ),
            # 阿里云（100.64.0.0/10 CGNAT 段内的 100.100.100.200）
            ("http://100.100.100.200/latest/meta-data/", ["100.100.100.200"]),
            # AWS 的 IPv6 元数据端点
            ("http://[fd00:ec2::254]/latest/meta-data/", ["fd00:ec2::254"]),
        ],
    )
    def test_direct_access_is_denied_as_metadata(self, url: str, resolved: list[str]) -> None:
        """★ 判成 `METADATA` 而不是 `PRIVATE`，这个区别是安全性质。

        `PRIVATE` 可以被配置例外放行（内网自建服务是常见需求），
        而 `METADATA` **没有例外通道**（GR4）。归错类就等于给这条攻击
        开了一个配置开关——而配置一份内网例外看起来完全无害。
        """
        decision = check_target_address(parse_target(url), resolved)
        assert isinstance(decision, Decision) and not decision.allowed
        assert decision.reason is DenyReason.METADATA

    @pytest.mark.parametrize(
        "resolved",
        [["169.254.169.254"], ["100.100.100.200"], ["fd00:ec2::254"]],
    )
    def test_even_a_permissive_exception_does_not_open_it(self, resolved: list[str]) -> None:
        """**把例外配成"什么都能访问"也拦得住。**

        这是整个 Guardrail 里唯一一条没有例外通道的规则。一份被误配的
        `guardrail_allow_private_targets` 不该变成"可以读实例凭证"。
        """
        decision = check_target_address(
            parse_target(
                f"http://[{resolved[0]}]/" if ":" in resolved[0] else f"http://{resolved[0]}/"
            ),
            resolved,
            # 故意把所有可能的写法都放进例外名单
            allowed_private=[resolved[0], f"{resolved[0]}:80", f"[{resolved[0]}]"],
        )
        assert not decision.allowed
        assert decision.reason is DenyReason.METADATA


# ------------------------------------------------------- IPv6 封装的 IPv4


class TestIpv6EncapsulatedIPv4:
    """把 IPv4 塞进 IPv6——**只写 IPv4 网段检查的实现必然漏掉这一整类**。

    `03/Guardrail.md` §4.4 指出这是同类实现的结构性漏洞，而不是某个实现的
    疏忽。三种封装形式都必须拆开看内层到底是谁（`policy._embedded_ipv4`）。
    """

    @pytest.mark.parametrize(
        ("host", "note"),
        [
            ("::ffff:169.254.169.254", "IPv4 映射（后 32 位）"),
            ("2002:a9fe:a9fe::", "6to4（第 2、3 个 hextet）"),
            ("64:ff9b::a9fe:a9fe", "NAT64（后 32 位）"),
        ],
    )
    def test_encapsulated_metadata_is_still_metadata(self, host: str, note: str) -> None:
        """★ 三条路径封装的都是 `169.254.169.254`，结论必须**升级为 METADATA**。

        只判成 `PRIVATE` 是不够的——那时它能被内网例外放行。
        封装形式没有任何合法用途，所以拆开后看到元数据就一定按元数据拒绝。
        """
        assert classify_ip(host) is IPClass.METADATA, f"{note} 没有被拆开"

        decision = check_target_address(
            parse_target(f"http://[{host}]/latest/meta-data/"),
            [host],
            allowed_private=[host, f"[{host}]"],
        )
        assert not decision.allowed
        assert decision.reason is DenyReason.METADATA

    @pytest.mark.parametrize("host", ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:0a00:0001::"])
    def test_encapsulated_private_is_at_least_private(self, host: str) -> None:
        """封装的是普通内网地址时按 `PRIVATE` 处理（可例外，但默认拒）。

        这里**不要求**升格成 METADATA——它确实只是一台内网机器。
        要求的是"不能被当成公网"。
        """
        assert classify_ip(host) is IPClass.PRIVATE


# --------------------------------------------------------- 白名单的边界


class TestWhitelistPrecision:
    """GR3：白名单是 `scheme + host + port` 的**精确匹配**。"""

    def test_port_scanning_via_the_whitelisted_host_is_denied(self) -> None:
        """白名单里有 `api.example.com:443`，不代表能访问它的 22 端口。

        只校验 host 的实现会把平台变成一台端口扫描器——而用户会以为
        "我只白名单了自己的 API"。
        """
        policy = _policy("https://api.example.com:443")
        result = evaluate(
            RequestContext(url="https://api.example.com:22/", method="GET"),
            PUBLIC,
            whitelist=policy.whitelist,
        )
        assert isinstance(result, Decision) and not result.allowed
        assert result.reason is DenyReason.WHITELIST

    @pytest.mark.parametrize(
        "attacker_url",
        [
            "https://evil.example.com/",  # 子域（父域在白名单里）
            "https://api.example.com.evil.example.com/",  # 后缀混淆
            "https://api.example.co/",  # TLD 截断
            "http://api.example.com/",  # 只有 scheme 不同
        ],
    )
    def test_subdomain_confusion_is_denied(self, attacker_url: str) -> None:
        """**不做子域继承、不做通配。** 白名单含 `api.example.com` 时，
        上面每一个都必须被拒。允许通配等于"攻击者只需控制一个子域"。
        """
        result = evaluate(
            RequestContext(url=attacker_url, method="GET"),
            PUBLIC,
            whitelist=Whitelist.from_urls(["https://api.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed
        assert result.reason is DenyReason.WHITELIST


# ---------------------------------------------------- 字面量地址的形态


class TestLiteralAddressForms:
    """**字形不同的同一个地址。**

    ⚠️ 这一类的防线不是"认得出这些写法"，而是"**从不信任字面量**"（GR1）：
    我们校验的是域名解析之后的 IP，所以 `2130706433`（十进制写法的
    127.0.0.1）即便被解析器认出来，也是在**解析之后**被拦下的。

    这正是 `evaluate` 把 `resolved_ips` 作为参数收进来的理由——
    校验发生在"知道真实地址"之后，而不是"看名字像不像"。
    """

    @pytest.mark.parametrize(
        ("url", "resolved", "note"),
        [
            ("http://2130706433/", ["127.0.0.1"], "十进制整型"),
            ("http://0x7f000001/", ["127.0.0.1"], "十六进制"),
            ("http://017700000001/", ["127.0.0.1"], "八进制"),
            ("http://127.1/", ["127.0.0.1"], "短式（缺省段补零）"),
            ("http://[0:0:0:0:0:0:0:1]/", ["::1"], "IPv6 全写"),
            ("http://[::ffff:127.0.0.1]/", ["127.0.0.1"], "IPv4 映射的环回"),
        ],
    )
    def test_an_odd_literal_still_resolves_to_a_denied_address(
        self, url: str, resolved: list[str], note: str
    ) -> None:
        """★ 白名单里**写的就是这个奇怪字面量**，也依然过不去。

        这条设置的是最坏情况：用户（或攻击者控制的 Spec）把
        `http://2130706433/` 声明成了目标，于是白名单精确匹配通过。
        但地址校验看的是它解析出的 `127.0.0.1`，照样拒绝。

        若某天有人把校验改成"看 host 字段像不像内网"，这些用例会全部变红
        ——而那就是 IPv4 字面量绕过（`03/Guardrail.md` §4.4）回来了。
        """
        result = evaluate(
            RequestContext(url=url, method="GET"),
            resolved,
            whitelist=Whitelist.from_urls([url]),
        )
        assert isinstance(result, Decision), f"{note}：{url} 竟然被放行了"
        assert not result.allowed

    def test_userinfo_cannot_smuggle_a_metadata_host(self) -> None:
        """`http://169.254.169.254@evil.example.com/` 的宿主是 **evil.example.com**。

        URL 里的 userinfo 段（`@` 之前）是**凭证**，不是目标地址。
        凭直觉读 URL 的人会以为目标是元数据服务；解析器不会。
        这里断言解析结果与直觉无关——而且这个方向是"安全的那一侧"：
        真实宿主是 evil，所以它连白名单都过不去。
        """
        target = parse_target("http://169.254.169.254@evil.example.com/")
        assert target.host == "evil.example.com"

        result = evaluate(
            RequestContext(url="http://169.254.169.254@evil.example.com/", method="GET"),
            PUBLIC,
            # 即便有人真把元数据地址写进了白名单
            whitelist=Whitelist.from_urls(["http://169.254.169.254"]),
        )
        assert isinstance(result, Decision) and not result.allowed

    def test_a_fragment_cannot_hide_the_real_host(self) -> None:
        """`#` 之后是片段，不参与请求——但它常常骗过看日志的人。"""
        target = parse_target("http://evil.example.com#@169.254.169.254/")
        assert target.host == "evil.example.com"


# ------------------------------------------------------------ 重定向穿透


class TestRedirectPassthrough:
    """SR-4：不跟随重定向（`follow_redirects=False` 是构造期开关）。

    真正"没跟随"的断言在集成测试里（`test_guardrail_client.py::
    TestRedirectIsNotFollowed`，靠 fixture 的访问计数求证——没发出去的
    请求从响应侧看不出来）。这里验的是**纵深防御的第二层**：
    万一某天它被跟了，`Location` 指向的目标本身也过不去。
    """

    @pytest.mark.parametrize(
        ("location", "resolved", "reason"),
        [
            ("http://169.254.169.254/latest/meta-data/", ["169.254.169.254"], DenyReason.METADATA),
            ("http://127.0.0.1:9000/admin", ["127.0.0.1"], DenyReason.PRIVATE_IP),
            ("http://10.0.0.5:8080/internal", ["10.0.0.5"], DenyReason.PRIVATE_IP),
        ],
    )
    def test_an_internal_redirect_target_is_denied_at_the_address_layer(
        self, location: str, resolved: list[str], reason: DenyReason
    ) -> None:
        """把 `Location` 当成一次**新的**出站请求来判定——指向内网的必须被拒。

        ⚠️ 这里刻意把**重定向目标自己**放进白名单，把白名单这一层"喂饱"。
        否则这些用例会因为"宿主不在白名单"而通过——那验的是白名单，
        跟"重定向指向内网"这件事毫无关系，是一次**空洞的通过**。

        喂饱之后，拒绝的原因只可能来自地址校验，而断言里写明了是哪一种：
        元数据（无例外）还是内网（可配置例外）。
        """
        result = evaluate(
            RequestContext(url=location, method="GET"),
            resolved,
            whitelist=Whitelist.from_urls([location]),
        )
        assert isinstance(result, Decision), f"{location} 竟然被放行了"
        assert result.reason is reason

    def test_a_subdomain_confused_redirect_target_fails_the_whitelist(self) -> None:
        """指向"看起来像自己人"的外部域名——**这里拦住它的是白名单**。

        与上一条刻意分开写：那个宿主解析到的是公网地址，地址校验不会拦它，
        唯一的防线是白名单的精确匹配（GR3，不做子域继承）。
        合成一条来测会让"到底哪一层在起作用"变得说不清。
        """
        result = evaluate(
            RequestContext(url="https://api.example.com.evil.example.com/", method="GET"),
            PUBLIC,
            whitelist=Whitelist.from_urls(["https://api.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed
        assert result.reason is DenyReason.WHITELIST


# --------------------------------------------------------- 破坏性方法


class TestDestructiveBypass:
    """SR-3 的**第二层**：应用层过滤（N4）失效时，出站口仍然拦得住。"""

    @pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "delete", "Delete"])
    def test_destructive_methods_are_denied_even_if_a_case_asks_for_one(self, method: str) -> None:
        """大小写与变形都不该成为缺口（`check_method` 先做 `.upper()`）。

        这里**不经过** N4 的过滤：直接构造一次出站，模拟"应用层出了 bug，
        一条破坏性用例走到了出站口"。安全设计不假设应用层永远正确。
        """
        result = evaluate(
            RequestContext(url="https://api.example.com/v1/items/1", method=method),
            PUBLIC,
            whitelist=Whitelist.from_urls(["https://api.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed
        assert result.reason is DenyReason.DESTRUCTIVE

    def test_the_mvp_has_no_authorization_switch(self) -> None:
        """ADR-0021：MVP **没有**破坏性方法的授权入口。

        `destructive_authorized` 参数存在是为了 V1.0 引入授权步骤时不必改调用点
        ——所以同一个 URL 在显式授权下会放行。这条测试说明的是"授权是一个
        显式参数，不是某个会被顺手套上的默认值"。
        """
        url = "https://api.example.com/v1/items/1"
        whitelist = Whitelist.from_urls(["https://api.example.com"])

        assert not evaluate(
            RequestContext(url=url, method="DELETE"), PUBLIC, whitelist=whitelist
        ).allowed
        assert isinstance(
            evaluate(
                RequestContext(url=url, method="DELETE"),
                PUBLIC,
                whitelist=whitelist,
                destructive_authorized=True,
            ),
            SendPlan,
        )


# ------------------------------------------------------------ DNS rebinding


class _FakeStream:
    """一个只回答"我连到了谁"的假 socket 流。"""

    def __init__(self, peer: object, *, raise_on_probe: bool = False) -> None:
        self._peer = peer
        self._raise = raise_on_probe
        self.closed = False

    def get_extra_info(self, name: str) -> object:
        if self._raise:
            raise RuntimeError("这个流不支持 server_addr 探针")
        return self._peer if name == "server_addr" else None

    def close(self) -> None:
        self.closed = True


class TestDnsRebinding:
    """`09` §4 表里的最后一行，也是 ADR-0019 的核心。

    ## 攻击是什么样的

    我们解析 `api.example.com` → 校验得到公网 IP → 认为可以发。
    但在"解析"与"连接"之间，攻击者把 DNS 记录换成了 `127.0.0.1`，
    于是我们连到了自己家里。**校验过的东西和实际连的东西不是一回事**，
    这就是 TOCTOU。

    ## 防线是两步

    1. **钉死**：把连接目标固定为已校验的那个 IP（不给二次解析的机会）
    2. **连接后二次校验**：连上之后 `getpeername()` 核对对端是不是它
       ——防的是透明代理 / iptables REDIRECT 这类"连的是 A 实际到了 B"

    这里直接测第 2 步。第 1 步（`_pinned_core_request` 把宿主换成 IP）
    在 `test_guardrail_client.py::TestPinning` 里验。

    ⚠️ **为什么不 xfail 掉**：`09` 允许先标可选，理由是"成本高"。
    但这里并不需要真的搭一个会变的 DNS——`_verify_peer` 是一个纯函数式
    的核对，喂它一个假的流就能走到全部分支。成本低到没有理由不做，
    而标 xfail 会让这个文件看起来"有一个已知缺口"，反而掩盖了它其实被覆盖了。
    """

    def test_a_peer_mismatch_is_detected(self) -> None:
        """★ 要求连 A、实际连到 B → 必须报错。

        证书校验**拦不住**这种情况：中间是一层持有该域名合法证书的
        MITM 代理时，TLS 会正常通过。只有 `getpeername()` 骗不了人。
        """
        with pytest.raises(PeerVerificationError, match="连接目标被篡改"):
            _verify_peer(_FakeStream(("127.0.0.1", 443)), "93.184.216.34")

    def test_a_matching_peer_passes(self) -> None:
        _verify_peer(_FakeStream(("93.184.216.34", 443)), "93.184.216.34")

    @pytest.mark.parametrize(
        "peer",
        [
            ("::ffff:93.184.216.34", 443, 0, 0),  # IPv6 socket 上的 IPv4 映射形式
            ("93.184.216.34", 443),
        ],
    )
    def test_the_comparison_survives_address_notation(self, peer: tuple) -> None:
        """`getpeername()` 在 IPv6 socket 上返回 `('::ffff:1.2.3.4', port, 0, 0)`。

        不归一化就会把**正确的连接**判成劫持——于是每一条 HTTPS 请求都失败，
        而失败信息说"可能存在透明代理"。误报会让人把这条检查关掉，
        而它关掉之后就没人在看连接到底去哪了。
        """
        _verify_peer(_FakeStream(peer), "93.184.216.34")

    def test_the_whole_connection_is_dropped_on_a_mismatch(self) -> None:
        """**不只是拒绝，还要断开。**

        不断开的话，那条指向未授权对端的连接会留在连接池里被后续请求复用
        ——"这次没发出去"变成了"下一条用它发出去"。
        """

        class _Delegate:
            def __init__(self, stream: _FakeStream) -> None:
                self._stream = stream

            def connect_tcp(
                self, host, port, timeout=None, local_address=None, socket_options=None
            ):
                return self._stream

        stream = _FakeStream(("127.0.0.1", 443))
        backend = _VerifyingSyncBackend(_Delegate(stream))  # type: ignore[arg-type]

        with pytest.raises(PeerVerificationError):
            backend.connect_tcp("93.184.216.34", 443)

        assert stream.closed, "对端不符时那条连接必须被关掉"

    @pytest.mark.parametrize(
        "stream",
        [
            _FakeStream(None),  # 拿不到对端地址
            _FakeStream(None, raise_on_probe=True),  # 探针本身不支持
        ],
    )
    def test_a_probe_failure_does_not_break_the_request(self, stream: _FakeStream) -> None:
        """探针取不到对端地址时**放行**，不阻断。

        两条分支都要走到：`get_extra_info` 返回空，以及它**抛异常**
        （有些 stream 实现不支持 `server_addr`）。

        选择放行而不是拒绝，是因为拒绝的后果是"全部 HTTPS 请求失败"，
        而放行的后果只是"这一步没查"。权衡写在 `_verify_peer` 的 docstring 里。
        """
        _verify_peer(stream, "93.184.216.34")


# --------------------------------------------------------- 无地址可校验


class TestFailClosed:
    def test_an_empty_resolution_is_denied(self) -> None:
        """解析不出任何地址 → 无法校验 → 拒绝。

        "校验不了"绝对不能等于"放行"——那正好是攻击者想要的：
        让解析返回空，然后等着代码走进 `if not ips: return allow`。
        """
        result = evaluate(
            RequestContext(url="https://api.example.com/", method="GET"),
            [],
            whitelist=Whitelist.from_urls(["https://api.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed

    def test_one_bad_address_among_good_ones_denies_everything(self) -> None:
        """DNS 返回多条 A 记录时，**只校验第一条是不够的**。

        客户端可能选中没被校验的那一条——而那正是 SSRF 的入口。
        """
        result = evaluate(
            RequestContext(url="https://api.example.com/", method="GET"),
            ["93.184.216.34", "169.254.169.254"],
            whitelist=Whitelist.from_urls(["https://api.example.com"]),
        )
        assert isinstance(result, Decision) and not result.allowed
        assert result.reason is DenyReason.METADATA
