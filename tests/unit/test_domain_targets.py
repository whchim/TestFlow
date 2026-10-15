"""`DeclaredTarget` 的单元测试。

对应决策：`Guardrail.md` GR3（白名单是 `scheme + host + port` 三元组精确匹配）、
`04-数据模型.md` §2.1 `declared_targets`。

**为什么"归一化"这件事值得测**

白名单是**精确匹配**（不算子域、不通配）。精确匹配的脆弱之处在于：
只要同一个目标能写出两种字面量，它们就会被当成两个不同的条目——
于是一个被放进名单，另一个被拒绝，而**两边看起来都对**。

端口默认值、尾点、大小写这三件事各能造成这种分裂。所以逐条测。
"""

from __future__ import annotations

import pytest

from testflow.domain.targets import DeclaredTarget


class TestNormalization:
    def test_defaults_port_by_scheme(self) -> None:
        """没写端口时补默认值——否则 `...` 与 `...:443` 会分成两条。"""
        assert DeclaredTarget.parse("https://api.example.com").port == 443
        assert DeclaredTarget.parse("http://api.example.com").port == 80

    def test_explicit_and_implicit_port_are_the_same_target(self) -> None:
        """这是本类存在的理由：两种写法必须**相等**，不是"看起来一样"。"""
        assert DeclaredTarget.parse("https://api.example.com") == DeclaredTarget.parse(
            "https://api.example.com:443"
        )
        assert DeclaredTarget.parse("http://api.example.com") == DeclaredTarget.parse(
            "http://api.example.com:80"
        )

    def test_host_is_lowercased(self) -> None:
        assert DeclaredTarget.parse("https://API.Example.COM").host == "api.example.com"

    def test_trailing_dot_is_stripped(self) -> None:
        """末尾的 `.` 是 DNS 的绝对写法，指向同一个地方。"""
        assert DeclaredTarget.parse("https://api.example.com./v1") == DeclaredTarget.parse(
            "https://api.example.com/v1"
        )

    def test_path_and_query_do_not_affect_the_target(self) -> None:
        """白名单按**目标主机**授权，不按接口。路径由用例决定。"""
        assert DeclaredTarget.parse("https://api.example.com/v1/users?page=2") == (
            DeclaredTarget.parse("https://api.example.com:443/")
        )

    def test_userinfo_is_stripped(self) -> None:
        """`https://user:pass@host` 里的凭证不进入目标——它在白名单里毫无意义。"""
        assert DeclaredTarget.parse("https://user:pw@api.example.com").host == "api.example.com"

    def test_ipv6_host_unbracketed(self) -> None:
        t = DeclaredTarget.parse("http://[::1]:9000")
        assert t.host == "::1"
        assert t.port == 9000


class TestRejection:
    """解析不了的目标必须抛错，不能"尽力而为"。

    一个解析成空字符串的目标进到白名单里，等于放行了一个未知的地方。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "/relative/path",  # 相对 URL：没有基准地址，无法确定 host（SP3）
            "api.example.com",  # 缺 scheme
            "ftp://api.example.com",  # scheme 不受支持
            "file:///etc/passwd",
            "https://",  # 缺 host
            "https://api.example.com:99999",  # 端口越界
            "https://api.example.com:abc",  # 端口非数字
        ],
    )
    def test_invalid_urls_raise(self, url: str) -> None:
        with pytest.raises(ValueError):
            DeclaredTarget.parse(url)


class TestStr:
    def test_str_is_the_round_trip_form(self) -> None:
        """`str()` 与解析互为逆运算——落库的就是这个形式。"""
        s = "https://api.example.com:443"
        assert str(DeclaredTarget.parse(s)) == s
        assert DeclaredTarget.parse(str(DeclaredTarget.parse(s))) == DeclaredTarget.parse(s)

    def test_is_hashable_and_orderable(self) -> None:
        """能放进 set（去重）——SpecParser 产出的是一个集合。"""
        a = DeclaredTarget.parse("https://a.example.com")
        b = DeclaredTarget.parse("https://b.example.com")
        assert len({a, b, a}) == 2
        assert sorted([b, a]) == [a, b]
