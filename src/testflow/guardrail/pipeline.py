"""六步校验流水线的**判定核心**——sync 与 async 共用（GR8）。

对应决策：`03-模块详细设计/Guardrail.md` §2（流水线）、GR1/GR2（IP 校验与钉死）、
GR3（白名单）、GR8（两形态共用校验）、ADR-0018（例外只能来自配置）。

## 为什么判定与发送必须分开

`evaluate()` 是**纯函数**：给定同样的输入，永远得到同样的结论。它不发请求、
不读时钟、不碰随机数。

而"发送"这一步是有状态的（连接池、限流器、超时）。把两者混在一起，
校验逻辑就再也不能被彻底测试——每个用例都得起一个 HTTP 服务。

分开之后，`guardrail/policy.py` 与这里的 `evaluate` 能用毫秒级的速度跑几百个
边界用例（含 `::ffff:169.254.169.254` 这类必须精确覆盖的），
而真正的发送逻辑只在一个集成测试里验。

## 六步与它们各自在哪

| 步 | 内容 | 在哪 |
| :--- | :--- | :--- |
| ① | 白名单（SR-1） | `evaluate()` |
| ② | 解析 + 校验全部 IP + 选出钉死目标（SR-2） | `evaluate()`（解析与校验之间只隔一次函数调用） |
| ③ | 破坏性方法（SR-3） | `evaluate()` |
| ④ | 不跟随重定向（SR-4） | **客户端构造时**——见下 |
| ⑤ | 限流与熔断（SR-5） | 客户端（有状态） |
| ⑥ | 发送 + 审计 | 客户端 |

**第 ④ 步不在判定里，是因为它没有"判定"可言。** `follow_redirects=False`
是一个构造期开关：设了它，3xx 就只是一个响应，不会被跟随——不存在
"这个请求该不该跟随"这个逐请求问题。`DenyReason.REDIRECT` 因此是**保留取值**
（对齐 `04` §2.9 的 CHECK 约束），当前实现不会产生它。
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from testflow.guardrail.policy import (
    Decision,
    DenyReason,
    IPAddress,
    Target,
    Whitelist,
    check_method,
    check_target_address,
    check_whitelist,
    parse_target,
)

__all__ = ["Direction", "EgressPolicy", "RequestContext", "SendPlan", "evaluate"]

Direction = Literal["target", "llm"]


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """**一次 Run 的**出站授权。与进程级的限流器/传输层分开。

    分开的理由很实际：传输层和限流器是每进程一份、创建代价高的；而白名单和
    内网例外是每次 Run 都不同的（白名单从该 Run 的 Spec 派生）。
    把两者混在一个对象里，就会被迫为每个 Run 重建一次连接池。
    """

    #: 该 Run 允许出站的目标（SR-1）。
    whitelist: Whitelist
    #: 内网例外的**唯一来源**（ADR-0018）。**永不从 Spec 派生**——
    #: 若"Spec 声明了就等于授权"，攻击者只需在文档里写上
    #: `http://169.254.169.254/` 就拿到了授权，SR-2 就退化成
    #: "检查文档是否声明了自己要访问的地址"。
    allowed_private: frozenset[str] = frozenset()
    #: MVP 恒为 `False`——没有授权入口（ADR-0021 / TE3）。
    #: 保留它是为了 V1.0 引入独立授权步骤时不必改调用点。
    destructive_authorized: bool = False


@dataclass(frozen=True, slots=True)
class RequestContext:
    """一次出站的上下文。**判定所需的全部输入都在这里**（除了已解析的 IP）。"""

    url: str
    method: str
    #: `target` = 打被测 API；`llm` = 调模型提供商。两者写进审计的 `direction` 列。
    direction: Direction = "target"
    run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SendPlan:
    """校验通过后的**发送指令**。

    它不是"原来的请求"，而是"该往哪里发、用什么身份"的结论——
    传输层照着它改写实际的连接目标。
    """

    url: str
    target: Target
    #: **已全部校验过的**候选连接目标，按顺序尝试。传输层只认字面量 IP，
    #: 不做任何解析。
    candidate_ips: tuple[str, ...]
    #: 原域名。用于两个地方：`Host` 头，以及 TLS 的 SNI / 证书校验。
    original_host: str

    @property
    def pinned_ip(self) -> str:
        """本次尝试要连的 IP。

        单元素时就是它自己；多元素时是第一个（客户端在重试时会逐次收窄
        `candidate_ips`，见 `sync_client._send_pinned`）。
        """
        return self.candidate_ips[0]

    @property
    def host_port(self) -> str:
        """限流器的键。与 `specs.declared_targets` 的粒度一致（都含端口）。"""
        return f"{self.target.host}:{self.target.port}"


def evaluate(
    ctx: RequestContext,
    resolved_ips: Sequence[IPAddress],
    *,
    whitelist: Whitelist,
    allowed_private: Collection[str] = (),
    destructive_authorized: bool = False,
) -> Decision | SendPlan:
    """执行 ①②③ 三步判定。

    Args:
        resolved_ips: 由调用方解析好的**全部**地址。为什么不在这里解析：
            解析是 I/O，而本函数要保持纯粹；更重要的是，sync 与 async 的
            解析方式不同（`getaddrinfo` vs `await getaddrinfo`），
            把解析留在外面，两条路径就能共用这里全部的判定逻辑（GR8）。
        allowed_private: 内网例外的**唯一来源**（ADR-0018）。
            调用方从配置读，**任何情况下不得从 Spec 派生**。

    Returns:
        `SendPlan` 表示放行；`Decision`（`allowed=False`）表示拒绝。
        刻意不用异常表示拒绝——拒绝是正常结果，见 `errors.py`。

    Note:
        判定顺序**不可调换**：白名单 → 地址 → 方法。白名单最便宜且最"硬"
        （精确匹配，无例外通道），放在最前能让最明确的拒绝最先返回，
        也避免为一个不在名单里的目标去做 DNS 解析（那本身就是一次外发）。
    """
    try:
        target = parse_target(ctx.url)
    except ValueError as exc:
        # 解析不了的目标一律拒绝。`parse_target` 已经排除了非 http(s) 的 scheme、
        # 非法端口——这些都不是"暂时不确定"，而是"明确不能用"。
        return Decision.deny(DenyReason.WHITELIST, f"无法解析的出站目标：{exc}")

    whitelist_decision = check_whitelist(target, whitelist)
    if not whitelist_decision.allowed:
        return whitelist_decision

    # SR-3 在地址校验**之前**：破坏性方法无需解析域名就能判，
    # 而拒绝一次破坏性请求时顺带做一次 DNS 查询是没必要的（也是一次外发）。
    method_decision = check_method(ctx.method, destructive_authorized=destructive_authorized)
    if not method_decision.allowed:
        return method_decision

    address_decision = check_target_address(target, resolved_ips, allowed_private=allowed_private)
    if not address_decision.allowed:
        return address_decision

    return SendPlan(
        url=ctx.url,
        target=target,
        candidate_ips=tuple(str(ip) for ip in resolved_ips),
        original_host=target.host,
    )
