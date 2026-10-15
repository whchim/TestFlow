"""域名解析。**产出全部地址，不是"一个地址"。**

对应决策：`03-模块详细设计/Guardrail.md` GR1、ADR-0019 §4.2 步骤 ①。

## 为什么必须返回全部

一个域名可以解析出**多条 A 记录**。若只取第一条去校验，而把解析工作留给
HTTP 客户端，客户端可能选中**没被校验过的那一条**——

    我们校验：api.example.com → 1.2.3.4        ✅ 公网，放行
    客户端连接：api.example.com → 127.0.0.1    ❌ 环回，没人检查过

这不是理论问题：轮询 DNS、多 A 记录、以及攻击者控制的 DNS 都能制造这种分裂。

所以这里的契约是：**返回全部，调用方校验全部**（`policy.check_target_address`
会逐个走一遍）。校验完之后连哪一条都行，因为每一条都被验过了。

## GR1：校验作用于解析出的 IP，不作用于域名

这是整个 SSRF 防护的地基。"先解析→再校验域名"这种做法挡不住 DNS Rebinding
（见 `policy.py` 与 ADR-0019）：解析和连接是两次独立的查询，
中间那零点几秒足以让攻击者换掉答案。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Protocol

from testflow.guardrail.policy import IPAddress

__all__ = ["AsyncResolver", "Resolver", "SyncResolver", "system_resolver"]


class Resolver(Protocol):
    """解析器的契约。**同步版本。**

    做成 Protocol 而不是直接调 `socket`：测试要能不碰真实 DNS 就注入
    "这个域名解析到 127.0.0.1"这种场景，而那正是 SSRF 防护的核心用例。
    """

    def resolve(self, host: str) -> list[IPAddress]:
        """返回该主机的**全部** IP 地址。解析失败时抛 `OSError`。"""
        ...


class SyncResolver:
    """基于 `socket.getaddrinfo` 的实现。Celery Worker（prefork）使用。"""

    def resolve(self, host: str) -> list[IPAddress]:
        # `getaddrinfo` 会对同一个地址返回多条记录（按 socktype 区分），
        # 必须去重——否则"校验全部"会做无用功，而告警里也会出现重复项。
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return _dedupe(str(info[4][0]) for info in infos)


class AsyncResolver:
    """基于 `asyncio.getaddrinfo` 的实现。LangGraph 节点使用。

    `asyncio.getaddrinfo` 会把阻塞的解析丢进线程池——直接用同步版本会卡住
    整个事件循环，而编排层是单事件循环的（`02` D1）。
    """

    async def resolve(self, host: str) -> list[IPAddress]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return _dedupe(str(info[4][0]) for info in infos)


def _dedupe(values: Iterable[str]) -> list[IPAddress]:
    """去重并解析成 `ipaddress` 对象。保持首次出现的顺序。

    顺序有意义：调用方按顺序选取**第一个通过校验的** IP 去连接。
    让顺序随解析库内部实现变化，会让"同一个域名连到哪台机器"变成随机事件。
    """
    seen: dict[str, IPAddress] = {}
    for raw in values:
        # IPv4-mapped 的 IPv6 形式（`::ffff:1.2.3.4`）与 IPv4 是同一个地方。
        # 不归一化的话，同一个地址会以两种形式进入"全部地址"列表，
        # 校验做两遍，而且错误信息里会出现一个看起来陌生的写法。
        address = ipaddress.ip_address(str(raw).split("%")[0])
        if isinstance(address, ipaddress.IPv6Address):
            mapped = _mapped_or_none(address)
            if mapped is not None:
                address = mapped
        seen.setdefault(str(address), address)
    return list(seen.values())


def _mapped_or_none(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """只归一化 **IPv4 映射**地址（`::ffff:a.b.c.d`）。

    刻意不含 6to4 / NAT64：那两种形式承载的地址**不一定是我们要连的地方**
    （6to4 需要中继才能到达），把它们归一会让"校验"和"连接"指向不同的目标。
    `policy._embedded_ipv4` 对那两种的处理是"一律按内网拒绝"，那是正确的做法。
    """
    if address in ipaddress.IPv6Network("::ffff:0:0/96"):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def system_resolver() -> SyncResolver:
    """默认解析器。单独一个函数，方便测试里替换、也方便将来做 DNS 缓存。"""
    return SyncResolver()
