"""出站校验策略——**纯函数，不做任何 I/O**。

对应决策：`03-模块详细设计/Guardrail.md`（GR1 ~ GR5）、ADR-0019、ADR-0018、
ADR-0020。`09-测试策略.md` §2.2 把本模块列为"必须充分覆盖（≥ 90%）"，
理由是它是**安全边界**，一个边界条件的漏洞就是 SSRF。

本模块只回答"**允不允许**"，不负责"**怎么发**"。真正的请求发出、连接层
校验（对端地址二次校验）在 `sync_client` / `async_client` 中，它们调用这里
的判定结果。

⚠️ 安全底线不在此处暴露为配置项（ADR-0022）：元数据地址拒绝、重定向不跟随
等都是硬编码行为，没有开关。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "DESTRUCTIVE_METHODS",
    "Decision",
    "DenyReason",
    "IPClass",
    "Target",
    "Whitelist",
    "check_method",
    "check_target_address",
    "check_whitelist",
    "classify_ip",
    "parse_target",
]

#: 允许出现在出站目标中的 scheme。其余一律拒绝——见 `_DEFAULT_PORTS`。
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

#: 破坏性方法。SR-3 默认禁用（`03/Guardrail.md` §5）。
DESTRUCTIVE_METHODS: Final = frozenset({"DELETE", "PUT", "PATCH"})


# --------------------------------------------------------------- 禁止网段
#
# 完整清单与逐条理由见 `03/Guardrail.md` §4.3。这里只重复最容易搞错的一条：
# `::ffff:0:0/96`（IPv4 映射）必须显式列出，否则 `::ffff:169.254.169.254`
# 会绕过只检查 IPv4 网段的实现——见 ADR-0019 陷阱一。

_FORBIDDEN_V4: Final = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",  # 本网络
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # CGNAT —— 含阿里云元数据 100.100.100.200
        "127.0.0.0/8",  # 环回
        "169.254.0.0/16",  # 链路本地 —— 含云元数据 169.254.169.254
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF 协议分配
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # 基准测试
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # 组播
        "240.0.0.0/4",  # 保留
        "255.255.255.255/32",  # 广播
    )
)

_FORBIDDEN_V6: Final = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/128",  # 未指定
        "::1/128",  # 环回
        # IPv4 兼容地址（已废弃）``::a.b.c.d``。属 `::/96`，与映射地址同类绕过。
        # 文档 §4.3 未单列，但**同类风险必须同等处理**——`::127.0.0.1` 在
        # Linux 上仍可能被路由到环回。此处作为防御性补充。
        "::/96",
        "::ffff:0:0/96",  # IPv4 映射 —— 见 ADR-0019 陷阱一
        "fc00::/7",  # 唯一本地
        "fe80::/10",  # 链路本地
        "ff00::/8",  # 组播
        "2002::/16",  # 6to4 —— 封装 IPv4
        "64:ff9b::/96",  # NAT64 —— 封装 IPv4
    )
)

#: 云元数据地址：**即使配置了内网例外也拒绝**（GR4 / §4.5）。
#: 一次成功的 SSRF 即可能获取实例凭证，因此这里没有例外通道。
_METADATA_V4: Final = frozenset(map(ipaddress.IPv4Address, ("169.254.169.254", "100.100.100.200")))
_METADATA_V6: Final = frozenset(map(ipaddress.IPv6Address, ("fd00:ec2::254",)))

_MAPPED_V4: Final = ipaddress.IPv6Network("::ffff:0:0/96")
_SIXTOFOUR: Final = ipaddress.IPv6Network("2002::/16")
_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


# --------------------------------------------------------------- 判定结果


class DenyReason(StrEnum):
    """拒绝原因。取值与 `04-数据模型.md` §2.9 的 `audit_logs.deny_reason` 对齐。

    用 `StrEnum` 而非 `(str, Enum)`：后者的 `str()` 返回 `'DenyReason.WHITELIST'`
    而不是 `'whitelist'`，一旦某处忘记写 `.value`，落库或进 JSON 的就是那个
    带类名的字符串——而它**看起来像是对的**。
    """

    WHITELIST = "whitelist"  # SR-1
    METADATA = "metadata"  # SR-2，不可例外
    PRIVATE_IP = "private_ip"  # SR-2，可配置例外
    DESTRUCTIVE = "destructive"  # SR-3
    REDIRECT = "redirect"  # SR-4
    RATE_LIMIT = "rate_limit"  # SR-5


class IPClass(Enum):
    METADATA = "metadata"  # 永不例外
    PRIVATE = "private"  # 可被显式配置例外
    PUBLIC = "public"


@dataclass(frozen=True, slots=True)
class Decision:
    """一次校验的结论。`detail` 用于审计日志与排障，不参与控制流。"""

    allowed: bool
    reason: DenyReason | None = None
    detail: str = ""

    @classmethod
    def allow(cls) -> Decision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: DenyReason, detail: str = "") -> Decision:
        return cls(allowed=False, reason=reason, detail=detail)


# --------------------------------------------------------------- 目标解析


@dataclass(frozen=True, slots=True)
class Target:
    """出站目标的三元组。**白名单的匹配单位**（GR3）。

    刻意不含路径与查询串：白名单是**按目标主机**授权的，不是按接口授权的。
    路径参数由用例决定，不参与安全判定。
    """

    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_target(url: str) -> Target:
    """把 URL 解析为 `Target`。

    端口在此**必须显式化**（§5.4）：否则 `https://api.example.com` 与
    `https://api.example.com:443` 会被当成两个不同的白名单条目。

    Raises:
        ValueError: URL 无法解析、scheme 不受支持，或端口超出范围。
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname  # urlsplit 已做：小写、去方括号、剥离 userinfo

    if not scheme or not host:
        raise ValueError(f"无法解析的目标 URL：{url!r}")
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"不支持的 scheme：{scheme!r}（仅允许 http / https）")

    try:
        port = parts.port
    except ValueError as exc:  # 端口非数字或超出 0..65535
        raise ValueError(f"目标 URL 的端口无效：{url!r}") from exc

    return Target(scheme=scheme, host=host.rstrip("."), port=port or _DEFAULT_PORTS[scheme])


# --------------------------------------------------------------- 白名单


@dataclass(frozen=True, slots=True)
class Whitelist:
    """出站白名单（SR-1）。

    **精确匹配，且是 `scheme + host + port` 三元组**（GR3）：

    - 禁止通配符——通配会让校验失去意义，攻击者只需控制一个子域
    - 不做子域继承——`api.example.com` 在名单中**不**意味着 `evil.example.com` 允许
    - 必须含端口——只校验 host 会允许 `http://whitelisted-host:22`，把平台变成端口扫描器
    - 必须含 scheme——允许 `http` 意味着允许明文传输凭证

    这三条都是安全要求，不是"实现上的选择"。
    """

    targets: frozenset[Target]

    @classmethod
    def from_urls(cls, urls: Iterable[str]) -> Whitelist:
        """从完整 URL 构造。无法解析的条目直接抛错——白名单宁可不完整，不可放宽。"""
        return cls(targets=frozenset(parse_target(u) for u in urls))

    def allows(self, target: Target) -> bool:
        return target in self.targets


def check_whitelist(target: Target, whitelist: Whitelist) -> Decision:
    """SR-1。未在白名单中**直接拒绝**，不做"警告后放行"（§3.3）。"""
    if whitelist.allows(target):
        return Decision.allow()
    return Decision.deny(
        DenyReason.WHITELIST,
        f"{target} 不在出站白名单中",
    )


# --------------------------------------------------------------- IP 判定


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """提取 IPv6 地址中封装的 IPv4（若有）。

    三种封装形式都构成绕过路径（ADR-0019），必须都能拆开：

    ====================  ==========================
    ``::ffff:a.b.c.d``    IPv4 映射（后 32 位）
    ``2002:AABB:CCDD::``  6to4（第 2、3 个 hextet）
    ``64:ff9b::a.b.c.d``  NAT64（后 32 位）
    ====================  ==========================

    **刻意手算而非用 `IPv6Address.sixtofour`**：该属性在 Python 3.13 起对
    非 6to4 地址返回 `None`，而在 3.12 及以前**抛 `AddressValueError`**。
    本项目声明支持 `>=3.12`，用它会变成"在 3.12 上崩溃"的版本陷阱。
    手动位运算无此问题，且把位布局写在了明处。
    """
    raw = int(ip)
    if ip in _MAPPED_V4:
        return ipaddress.IPv4Address(raw & 0xFFFFFFFF)
    if ip in _SIXTOFOUR:
        return ipaddress.IPv4Address((raw >> 80) & 0xFFFFFFFF)
    if ip in _NAT64:
        return ipaddress.IPv4Address(raw & 0xFFFFFFFF)
    return None


def classify_ip(value: str | IPAddress) -> IPClass:
    """判定一个 IP 属于元数据 / 内网 / 公网。

    顺序很重要：**先判元数据，再判禁止网段**。因为元数据地址本身就在禁止
    网段内（`169.254.169.254` ∈ `169.254.0.0/16`），顺序反了会让它落进
    "可配置例外"那一类，从而被例外机制放行——违反 GR4。

    Raises:
        ValueError: 输入不是合法的 IP 字面量。
    """
    ip: IPAddress = (
        value
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
        else ipaddress.ip_address(value.strip())
    )

    # 先拆 IPv6 的封装，再看它到底代表哪个 IPv4。
    if isinstance(ip, ipaddress.IPv6Address) and (inner := _embedded_ipv4(ip)) is not None:
        # 封装形式一律按"内网"处理——它们是绕过的载体，没有任何合法用途。
        # 但若封装的正是元数据地址，升级为不可例外的 METADATA。
        if inner in _METADATA_V4:
            return IPClass.METADATA
        return IPClass.PRIVATE

    if ip in _METADATA_V4 or ip in _METADATA_V6:
        return IPClass.METADATA

    networks = _FORBIDDEN_V4 if ip.version == 4 else _FORBIDDEN_V6
    if any(ip in net for net in networks):
        return IPClass.PRIVATE

    return IPClass.PUBLIC


def _is_excepted(target: Target, allowed_private: Collection[str]) -> bool:
    """判断目标是否命中显式配置的内网例外。

    例外条目形如 ``"127.0.0.1:9000"``，**只作用于 SR-2，不绕过 SR-1**。

    例外的唯一来源是配置，**永不从 Spec 派生**（ADR-0018）——若"Spec 声明了
    就等于授权"，攻击者只需在文档里写 ``http://169.254.169.254/`` 即获得授权，
    SR-2 就退化成"检查文档是否声明了自己要访问的地址"。
    """
    if not allowed_private:
        return False
    host_port = f"{target.host}:{target.port}"
    bare_host = target.host
    return host_port in allowed_private or bare_host in allowed_private


def check_target_address(
    target: Target,
    resolved_ips: Sequence[str | IPAddress],
    *,
    allowed_private: Collection[str] = (),
) -> Decision:
    """SR-2。校验域名解析出的**全部** IP。

    Args:
        target: 出站目标（提供 host:port 用于匹配例外）。
        resolved_ips: 已解析的 IP 列表。**必须全部校验**——只查第一个是不够的，
            返回多条 A 记录时客户端可能选中未校验的那条。
        allowed_private: 配置中的内网例外（host:port 或裸 host）。

    Note:
        本函数只回答"这些 IP 允不允许连"。**真正的防护还差一步**：校验通过后
        必须把连接目标钉死为已校验的 IP，并在连接建立后二次校验对端地址——
        见 ADR-0019（DNS Rebinding / TOCTOU）。那部分属于 I/O 层，不在本模块。
    """
    if not resolved_ips:
        return Decision.deny(
            DenyReason.PRIVATE_IP,
            f"{target} 未解析出任何 IP 地址——无法校验，按拒绝处理",
        )

    excepted = _is_excepted(target, allowed_private)

    for raw in resolved_ips:
        try:
            ip_class = classify_ip(raw)
        except ValueError as exc:
            # 防御性分支：IP 来自我们自己的 DNS 解析，正常不应触发。
            # 校验不了就拒绝（fail-closed），不要放行。
            return Decision.deny(
                DenyReason.PRIVATE_IP,
                f"无法解析的 IP 字面量 {raw!r}——无法校验，按拒绝处理（{exc}）",
            )

        if ip_class is IPClass.METADATA:
            # GR4：没有例外通道，配置了内网例外也不例外。
            return Decision.deny(
                DenyReason.METADATA,
                f"{target} 解析到云元数据地址 {raw}——该地址不可例外",
            )

        if ip_class is IPClass.PRIVATE and not excepted:
            return Decision.deny(
                DenyReason.PRIVATE_IP,
                f"{target} 解析到内网地址 {raw}——如需访问，请在 "
                "guardrail_allow_private_targets 中显式配置",
            )

    return Decision.allow()


# --------------------------------------------------------------- 方法判定


def check_method(method: str, *, destructive_authorized: bool = False) -> Decision:
    """SR-3。破坏性方法默认禁用。

    MVP **没有授权入口**（ADR-0021），因此 `destructive_authorized` 在
    当前实现中恒为 False——保留参数是为了让 V1.0 引入独立授权步骤时
    不必改动调用点。

    注意：判定依据必须是**实际即将发出的 HTTP 方法**，而非数据库中的字段
    （`03/Guardrail.md` §5.2）。若字段与实际请求不一致，依据字段就发现不了。
    """
    normalized = method.upper()
    if normalized in DESTRUCTIVE_METHODS and not destructive_authorized:
        return Decision.deny(
            DenyReason.DESTRUCTIVE,
            f"{normalized} 是破坏性方法，默认禁用；MVP 不提供授权入口",
        )
    return Decision.allow()
