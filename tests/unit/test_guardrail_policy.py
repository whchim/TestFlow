"""Guardrail 策略层的单元测试。

对应决策：`09-测试策略.md` §2.2（必须充分覆盖，≥ 90%）、§4（对抗性测试集）。

这些测试的**目的不是"验证功能正确"，而是"验证绕过不可行"**——这是安全
组件与其余模块在测试策略上的区别。表中每一行对应一种已知的绕过手法。

涉及 I/O 的绕过（DNS Rebinding 需要可控解析器）在 M2 的
`tests/security/adversarial/` 中实现；此处只覆盖**纯函数可达**的那些。
"""

from __future__ import annotations

import ipaddress

import pytest

from testflow.guardrail.policy import (
    Decision,
    DenyReason,
    IPClass,
    Target,
    Whitelist,
    check_method,
    check_target_address,
    check_whitelist,
    classify_ip,
    parse_target,
)

# ------------------------------------------------------------------ IP 判定


class TestClassifyIpv4:
    """SR-2：IPv4 禁止网段。清单见 `03/Guardrail.md` §4.3。"""

    @pytest.mark.parametrize(
        "ip",
        [
            "0.0.0.0",
            "0.1.2.3",
            "10.0.0.1",
            "10.255.255.255",
            "100.64.0.1",  # CGNAT 段内的普通地址
            # 注意 `100.100.100.200` 不在此列表：它虽同属 CGNAT 禁止段，
            # 但是阿里云元数据地址，应先被判为 METADATA。见 TestMetadataPriority。
            "127.0.0.1",
            "127.255.255.254",
            "169.254.1.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.0.0.1",
            "192.0.2.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
        ],
    )
    def test_forbidden_ranges(self, ip: str) -> None:
        assert classify_ip(ip) is IPClass.PRIVATE

    @pytest.mark.parametrize(
        "ip",
        ["8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1"],
    )
    def test_public(self, ip: str) -> None:
        """边界外的地址不应被误判——过严的规则会让平台无法使用。"""
        assert classify_ip(ip) is IPClass.PUBLIC

    def test_accepts_address_object(self) -> None:
        assert classify_ip(ipaddress.IPv4Address("10.0.0.1")) is IPClass.PRIVATE

    @pytest.mark.parametrize("bad", ["", "not-an-ip", "999.999.999.999", "1.2.3"])
    def test_invalid_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            classify_ip(bad)


class TestClassifyIpv6:
    """SR-2：IPv6 禁止网段。"""

    @pytest.mark.parametrize(
        "ip",
        [
            "::",
            "::1",
            "fc00::1",
            "fd00::1",
            "fe80::1",
            "ff02::1",
        ],
    )
    def test_forbidden_ranges(self, ip: str) -> None:
        assert classify_ip(ip) is IPClass.PRIVATE

    @pytest.mark.parametrize("ip", ["2606:4700:4700::1111", "2001:4860:4860::8888"])
    def test_public(self, ip: str) -> None:
        assert classify_ip(ip) is IPClass.PUBLIC


class TestEmbeddedIpv4Bypass:
    """**对抗性测试的核心**：IPv6 中封装的 IPv4。

    只检查 IPv4 网段的实现会被这些地址绕过，而**在被利用前没有任何症状**——
    正常测试全绿。见 ADR-0019 §陷阱一。
    """

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:192.168.1.1",
            "::ffff:169.254.1.1",
            "::ffff:8.8.8.8",  # 即便是公网地址，映射形式本身也在禁止网段内
        ],
    )
    def test_ipv4_mapped_is_private(self, ip: str) -> None:
        assert classify_ip(ip) is IPClass.PRIVATE

    def test_ipv4_mapped_metadata_escalates(self) -> None:
        """映射形式的元数据地址必须升级为 METADATA，而非普通内网。

        否则它会落进"可配置例外"那一类，被例外机制放行——违反 GR4。
        """
        assert classify_ip("::ffff:169.254.169.254") is IPClass.METADATA
        assert classify_ip("::ffff:100.100.100.200") is IPClass.METADATA

    @pytest.mark.parametrize(
        "ip",
        [
            "2002:a9fe:a9fe::",  # 6to4 封装 169.254.169.254
            "2002:0a00:0001::",  # 6to4 封装 10.0.0.1
            "2002:7f00:0001::",  # 6to4 封装 127.0.0.1
        ],
    )
    def test_sixtofour(self, ip: str) -> None:
        assert classify_ip(ip) in (IPClass.PRIVATE, IPClass.METADATA)

    def test_sixtofour_metadata_escalates(self) -> None:
        assert classify_ip("2002:a9fe:a9fe::") is IPClass.METADATA

    @pytest.mark.parametrize(
        "ip",
        [
            "64:ff9b::a9fe:a9fe",  # NAT64 封装 169.254.169.254
            "64:ff9b::7f00:1",  # NAT64 封装 127.0.0.1
            "64:ff9b::0a00:1",  # NAT64 封装 10.0.0.1
        ],
    )
    def test_nat64(self, ip: str) -> None:
        assert classify_ip(ip) in (IPClass.PRIVATE, IPClass.METADATA)

    def test_nat64_metadata_escalates(self) -> None:
        assert classify_ip("64:ff9b::a9fe:a9fe") is IPClass.METADATA

    @pytest.mark.parametrize("ip", ["::127.0.0.1", "::10.0.0.1"])
    def test_ipv4_compatible_is_private(self, ip: str) -> None:
        """已废弃的 IPv4 兼容地址，与映射地址同类绕过。"""
        assert classify_ip(ip) is IPClass.PRIVATE


class TestMetadataPriority:
    """GR4 / §4.5：元数据地址永不可例外。"""

    @pytest.mark.parametrize(
        "ip",
        ["169.254.169.254", "100.100.100.200", "fd00:ec2::254"],
    )
    def test_classified_as_metadata(self, ip: str) -> None:
        assert classify_ip(ip) is IPClass.METADATA

    def test_metadata_checked_before_private(self) -> None:
        """顺序反了会让元数据落进"可配置例外"，从而被放行。

        `169.254.169.254` 同时属于 `169.254.0.0/16`（禁止网段）；
        只有先判元数据才能拦住它。
        """
        assert classify_ip("169.254.169.254") is IPClass.METADATA
        assert classify_ip("169.254.1.1") is IPClass.PRIVATE  # 同网段但非元数据


# ------------------------------------------------------------ 目标解析


class TestParseTarget:
    def test_default_ports(self) -> None:
        assert parse_target("https://api.example.com") == Target("https", "api.example.com", 443)
        assert parse_target("http://api.example.com") == Target("http", "api.example.com", 80)

    def test_explicit_port_and_path_stripped(self) -> None:
        assert parse_target("https://api.example.com:8443/v1/users?q=1") == Target(
            "https", "api.example.com", 8443
        )

    def test_host_normalized(self) -> None:
        assert parse_target("https://API.Example.COM/").host == "api.example.com"
        assert parse_target("https://api.example.com./").host == "api.example.com"

    def test_userinfo_stripped(self) -> None:
        """`http://user@evil.com/` 的 host 是 evil.com，不是 user。"""
        assert parse_target("http://user:pass@evil.com/").host == "evil.com"

    def test_ipv6_host(self) -> None:
        assert parse_target("http://[::1]:9000/") == Target("http", "::1", 9000)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://api.example.com",  # scheme 不受支持
            "api.example.com",  # 无 scheme
            "https://",  # 无 host
            "https://api.example.com:99999",  # 端口越界
            "https://api.example.com:notaport",  # 端口非数字
        ],
    )
    def test_invalid_raises(self, url: str) -> None:
        with pytest.raises(ValueError):
            parse_target(url)


# --------------------------------------------------------------- 白名单


class TestWhitelist:
    """SR-1 / GR3：精确匹配，不通配、不继承子域。"""

    @pytest.fixture
    def wl(self) -> Whitelist:
        return Whitelist.from_urls(["https://api.example.com", "http://internal.test:8080"])

    def test_exact_match_allowed(self, wl: Whitelist) -> None:
        assert wl.allows(parse_target("https://api.example.com/v1"))
        assert wl.allows(parse_target("https://api.example.com:443/v1"))

    def test_scheme_must_match(self, wl: Whitelist) -> None:
        """允许 http 意味着允许明文传输凭证。"""
        assert not wl.allows(parse_target("http://api.example.com"))

    def test_port_must_match(self, wl: Whitelist) -> None:
        """只校验 host 会允许 `http://whitelisted-host:22`，把平台变成端口扫描器。"""
        assert not wl.allows(parse_target("https://api.example.com:8443"))
        assert not wl.allows(parse_target("https://api.example.com:22"))

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com",  # 不同子域
            "https://api.example.com.evil.com",  # 后缀混淆
            "https://example.com",  # 父域
            "https://xapi.example.com",  # 前缀混淆
        ],
    )
    def test_no_subdomain_inheritance(self, wl: Whitelist, url: str) -> None:
        assert not wl.allows(parse_target(url))

    def test_check_whitelist_denies_with_reason(self, wl: Whitelist) -> None:
        d = check_whitelist(parse_target("https://evil.com"), wl)
        assert not d.allowed
        assert d.reason is DenyReason.WHITELIST

    def test_check_whitelist_allows(self, wl: Whitelist) -> None:
        d = check_whitelist(parse_target("https://api.example.com/v1"), wl)
        assert d.allowed
        assert d.reason is None

    def test_unparseable_entry_raises(self) -> None:
        """白名单宁可不完整，不可放宽——构造失败应直接抛错。"""
        with pytest.raises(ValueError):
            Whitelist.from_urls(["ftp://x.test"])


# ------------------------------------------------------- 目标地址校验


class TestCheckTargetAddress:
    """SR-2 的完整判定，含例外机制。"""

    TARGET = Target("http", "internal.test", 8080)

    def test_public_allowed(self) -> None:
        assert check_target_address(self.TARGET, ["8.8.8.8"]).allowed

    def test_private_denied(self) -> None:
        d = check_target_address(self.TARGET, ["10.0.0.1"])
        assert not d.allowed
        assert d.reason is DenyReason.PRIVATE_IP

    def test_private_allowed_by_config_exception(self) -> None:
        """ADR-0018：例外是显式配置的独立动作。"""
        d = check_target_address(self.TARGET, ["10.0.0.1"], allowed_private=["internal.test:8080"])
        assert d.allowed

    def test_exception_can_be_bare_host(self) -> None:
        d = check_target_address(self.TARGET, ["10.0.0.1"], allowed_private=["internal.test"])
        assert d.allowed

    def test_exception_does_not_apply_to_other_host(self) -> None:
        d = check_target_address(self.TARGET, ["10.0.0.1"], allowed_private=["other.test:8080"])
        assert not d.allowed

    def test_exception_does_not_apply_to_other_port(self) -> None:
        d = check_target_address(self.TARGET, ["10.0.0.1"], allowed_private=["internal.test:9999"])
        assert not d.allowed

    # ---- 以下三条是本模块最重要的测试 ----

    def test_metadata_denied_without_exception(self) -> None:
        d = check_target_address(self.TARGET, ["169.254.169.254"])
        assert not d.allowed
        assert d.reason is DenyReason.METADATA

    def test_metadata_denied_EVEN_WITH_exception(self) -> None:
        """GR4：元数据地址没有例外通道。

        这条若失败，意味着"配置了内网例外就能读云元数据"——一次成功的
        SSRF 即可获取实例凭证。
        """
        d = check_target_address(
            self.TARGET,
            ["169.254.169.254"],
            allowed_private=["internal.test:8080", "169.254.169.254", "169.254.169.254:80"],
        )
        assert not d.allowed
        assert d.reason is DenyReason.METADATA

    def test_all_resolved_ips_are_checked(self) -> None:
        """只校验第一条是不够的——返回多条 A 记录时客户端可能选中未校验的那条。"""
        d = check_target_address(self.TARGET, ["8.8.8.8", "10.0.0.1"])
        assert not d.allowed
        assert d.reason is DenyReason.PRIVATE_IP

        d2 = check_target_address(self.TARGET, ["8.8.8.8", "169.254.169.254"])
        assert not d2.allowed
        assert d2.reason is DenyReason.METADATA

    def test_empty_resolution_denied(self) -> None:
        """解析不出任何地址时校验无从进行——fail-closed。"""
        d = check_target_address(self.TARGET, [])
        assert not d.allowed

    def test_unparseable_ip_denied(self) -> None:
        d = check_target_address(self.TARGET, ["not-an-ip"])
        assert not d.allowed


# ------------------------------------------------------------ 方法判定


class TestCheckMethod:
    """SR-3：破坏性方法默认禁用。"""

    @pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "delete", "Put"])
    def test_destructive_denied(self, method: str) -> None:
        d = check_method(method)
        assert not d.allowed
        assert d.reason is DenyReason.DESTRUCTIVE

    @pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS", "get"])
    def test_safe_methods_allowed(self, method: str) -> None:
        assert check_method(method).allowed

    def test_explicit_authorization_allows(self) -> None:
        """MVP 无授权入口，但参数保留给 V1.0（ADR-0021）。"""
        assert check_method("DELETE", destructive_authorized=True).allowed


# --------------------------------------------------------------- Decision


class TestDecision:
    def test_allow_has_no_reason(self) -> None:
        d = Decision.allow()
        assert d.allowed and d.reason is None

    def test_deny_carries_reason_and_detail(self) -> None:
        d = Decision.deny(DenyReason.WHITELIST, "原因")
        assert not d.allowed
        assert d.reason is DenyReason.WHITELIST
        assert d.detail == "原因"

    def test_deny_reason_values_match_audit_log(self) -> None:
        """取值必须与 `04-数据模型.md` §2.9 的 `audit_logs.deny_reason` 对齐。"""
        assert {r.value for r in DenyReason} == {
            "whitelist",
            "metadata",
            "private_ip",
            "destructive",
            "redirect",
            "rate_limit",
        }
