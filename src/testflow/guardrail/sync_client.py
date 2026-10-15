"""服务端**唯一**的出站客户端（同步）。Celery Worker 使用。

对应决策：`02` D2（唯一出站通道）、`03/Guardrail.md` §1.3（两种客户端形态）、
§2（六步流水线）、`03/TestExecutor.md` §1.1（执行器不持有客户端构造权）。

## 这个类的存在意义

`Guardrail.md` §1.2 把 Guardrail 定为系统唯一的出站路径，并写明第一设计目标是
**"绕不过去"** 而非"校验得准"。这个类就是那句话的落点：

    Worker ──▶ GuardrailSyncClient ──▶ 目标 API
                    ▲
                    └─ 白名单、IP 校验、钉死、对端校验、限流、审计，全在这里

**执行器拿不到 httpx**——架构测试（`tests/architecture/`）在 CI 里强制这一点。
即便有人往 `execution/` 里写 `import httpx`，构建就会失败。

## 拒绝是返回值，不是异常

被策略拦下的请求对平台来说**是正常工作**。见 `errors.py` 的说明。
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import httpx

from testflow.config import Settings
from testflow.guardrail.audit import AuditRecord, allow_record, deny_record
from testflow.guardrail.errors import GuardrailDenied, GuardrailTransportError
from testflow.guardrail.pipeline import EgressPolicy, RequestContext, SendPlan, evaluate
from testflow.guardrail.policy import Decision, DenyReason
from testflow.guardrail.ratelimit import RateLimiter
from testflow.guardrail.resolver import Resolver, system_resolver
from testflow.guardrail.transport import PinnedPlanExtension, PinnedTransport

__all__ = ["GuardrailResponse", "GuardrailSyncClient"]

#: 审计的落点。由调用方注入——`guardrail/` **不依赖 `storage/`**。
#:
#: 这个方向很要紧：让 guardrail 去 import 仓储层，就等于把这个安全模块
#: 绑在数据库上，它的单元测试从此需要 PG。注入一个回调，
#: 测试里传个 list.append 就够了。
AuditSink = Callable[[AuditRecord], None]


@dataclass(frozen=True, slots=True)
class GuardrailResponse:
    """一次成功的出站。**已经是结果，不是流。**

    读全了才返回，是因为随后的断言求值需要一个完整的响应体
    （JSON 解析、schema 校验都要整体）。流式留给 V1.0 的大响应场景。
    """

    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    #: 响应体是否因超过上限被截断（`04` DM6）。
    #: **必须显式标记**：截断后的 JSON 解析必然失败，若不标记，
    #: 它会被归因成"响应格式错误"——而那是被测系统的问题，不是我们的。
    truncated: bool
    #: 截断前的原始字节数。
    body_bytes: int

    @property
    def text(self) -> str:
        """按 UTF-8 尽力解码。解码失败不抛错——非 UTF-8 的响应体本身就是一种结果。"""
        return self.body.decode("utf-8", errors="replace")


@dataclass
class GuardrailSyncClient:
    """同步出站客户端。**进程级复用**（连接池与限流器都是有状态的）。"""

    settings: Settings
    audit_sink: AuditSink | None = None
    resolver: Resolver = field(default_factory=system_resolver)
    limiter: RateLimiter | None = None
    _client: httpx.Client | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = RateLimiter(
                qps_per_host=self.settings.guardrail_qps_per_host,
                max_concurrency_per_host=self.settings.guardrail_max_concurrency_per_host,
                breaker_threshold=self.settings.guardrail_breaker_threshold,
                breaker_cooldown_s=self.settings.guardrail_breaker_cooldown_s,
            )

    @property
    def client(self) -> httpx.Client:
        """惰性构造。**不在 `__init__` 里建**——构造客户端会创建连接池，
        而"只是想读个配置"的调用方不该付出这个代价（同一个理由见
        `config.load_settings` 不做模块级单例）。"""
        if self._client is None:
            self._client = httpx.Client(
                transport=PinnedTransport(),
                # GR5：不跟随重定向。3xx 会作为**测试结果**返回。
                # 显式写出来而不是依赖默认值——它是安全底线（ADR-0022），
                # 而"默认值是 False"这件事不写下来就没人会去核对。
                follow_redirects=False,
                timeout=self.settings.http_timeout_s,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> GuardrailSyncClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- 主流程

    def send(
        self,
        ctx: RequestContext,
        policy: EgressPolicy,
        *,
        content: bytes | None = None,
    ) -> GuardrailResponse | GuardrailDenied:
        """执行 ①~⑥ 全部六步。

        Returns:
            放行并拿到响应 → `GuardrailResponse`；
            被拒绝 → `GuardrailDenied`（**不是异常**，见 `errors.py`）。
        """
        # ① 白名单 → 解析 → ② 校验全部 IP 并选出钉死目标
        host = _host_of(ctx.url)
        try:
            resolved = self.resolver.resolve(host)
        except OSError as exc:
            # 解析失败：目标根本不存在。这**不是**策略拒绝，而是目标侧的问题，
            # 所以不记审计（审计回答的是"我们放没放行"，而这里根本没走到放行判断）。
            return GuardrailDenied(
                DenyReason.PRIVATE_IP,
                f"无法解析主机 {host!r}：{exc}",
            )

        outcome = evaluate(
            ctx,
            resolved,
            whitelist=policy.whitelist,
            allowed_private=policy.allowed_private,
            destructive_authorized=policy.destructive_authorized,
        )
        if isinstance(outcome, Decision):
            self._audit(deny_record(ctx, outcome.reason or DenyReason.WHITELIST, outcome.detail))
            return GuardrailDenied(outcome.reason or DenyReason.WHITELIST, outcome.detail)

        plan: SendPlan = outcome

        # ⑤ 限流与熔断
        verdict = self.limiter.acquire(plan.host_port)  # type: ignore[union-attr]
        if not verdict.allowed:
            self._audit(deny_record(ctx, DenyReason.RATE_LIMIT, verdict.detail))
            return GuardrailDenied(DenyReason.RATE_LIMIT, verdict.detail)

        # ⑥ 发送
        #
        # ★★ **`allow` 审计必须在发送之前写。**
        #
        # "放行"这个决定在发送之前就已经做出了——它与请求最终成不成功无关。
        # 写在发送之后（第一版就是这样）会漏掉**所有**超时与报错的请求：
        # 异常一抛，后面那行审计就永远执行不到。
        #
        # 而漏掉的恰是最该被审计的那几条：一个打不通的地址、一个超时的目标，
        # 正是安全复盘时要问"我们到底往哪儿发了请求"的场合。
        # 症状是审计表里只有成功的请求，看起来一切都很安分。
        self._audit(allow_record(ctx, plan))

        success = False
        try:
            response = self._send_pinned(plan, ctx.method, content)
            success = True
        except httpx.HTTPError:
            # 连接层错误**计入熔断**（`success` 保持 False）。
            # 注意判据是"能不能连上"，而不是"返回的内容对不对"——
            # 一个持续返回 500 的服务仍然是活的，把它熔断掉会让测试结果
            # 全部变成 `rate_limit` 而不是真实的 `failed`。
            raise
        finally:
            self.limiter.release(plan.host_port, success=success)  # type: ignore[union-attr]

        return response

    def _send_pinned(self, plan: SendPlan, method: str, content: bytes | None) -> GuardrailResponse:
        """按候选 IP 依次尝试，返回第一个连上的响应。

        ## 为什么不是"只连第一个"

        一个域名可以同时有 A 记录（IPv4）和 AAAA 记录（IPv6）。系统解析器
        返回的**顺序不保证可达性**：Windows 上 `localhost` 常常先返回 `::1`，
        而只监听 IPv4 的服务根本连不上。

        只连第一个的后果是：**一个完全正常的域名会随机失败**，且失败信息是
        "连接被拒绝"——看起来像对方挂了，实际上是我们在一个它没监听的地址上敲门。

        ## 为什么是顺序而不是并行（Happy Eyeballs）

        并行发起（同时连 IPv6 与 IPv4，取先成功的）能把延迟降到最低，
        代价是每个请求**凭空多打一次目标**。对一个"测量接口行为"的工具来说，
        这个代价不可接受：多出来的那次连接会出现在审计里、会计入目标的
        QPS、可能触发对方的告警。

        顺序尝试最坏情况下多一次连接，而那一次只发生在第一个地址确实不通时。

        ## 安全性没有被削弱

        进入本函数的每个 IP **都已经过 `check_target_address` 逐个校验**，
        所以换一个连不会绕开 SR-2——它们都是"可以连的地方"，区别只在可达性。
        传输层仍会对实际连上的那个做对端二次校验。
        """
        last_error: GuardrailTransportError | None = None

        for index in range(len(plan.candidate_ips)):
            attempt = dataclasses.replace(plan, candidate_ips=plan.candidate_ips[index:])
            request = self.client.build_request(method, plan.url, content=content)
            # 把结论交给传输层。**这是"钉死"实际发生的地方**——
            # 传输层据 plan 改写连接目标，并设置 TLS 的 server_hostname。
            request.extensions = {**request.extensions, PinnedPlanExtension: attempt}

            started = time.monotonic()
            try:
                response = self.client.send(request, stream=True)
            except GuardrailTransportError as exc:
                # ⚠️ **这里必须捕获 guardrail 自己的异常类型，不是 httpx 的。**
                # 传输层已经把 httpcore 的异常转换过了（见 `transport._map_errors`），
                # 所以 `httpx.ConnectError` 永远不会到达这里——写错类型的后果是
                # **多地址回退静默失效**：第一个地址连不上就直接抛出去，
                # 而双栈主机名的第一个地址经常连不上（`::1` 而服务只监听 IPv4）。
                # 这个错误被集成测试当场抓住了。
                last_error = exc
                continue

            try:
                body, truncated, total = _read_capped(
                    response.iter_bytes(), self.settings.response_body_max_bytes
                )
            finally:
                response.close()
            elapsed_ms = int((time.monotonic() - started) * 1000)

            return GuardrailResponse(
                status_code=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                elapsed_ms=elapsed_ms,
                truncated=truncated,
                body_bytes=total,
            )

        assert last_error is not None, "候选列表非空时循环至少执行一次"
        raise last_error

    def _audit(self, record: AuditRecord) -> None:
        if self.audit_sink is not None:
            self.audit_sink(record)


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def _read_capped(chunks: Iterable[bytes], cap: int) -> tuple[bytes, bool, int]:
    """按上限读取响应体。

    Returns:
        `(截断后的内容, 是否被截断, 原始总字节数)`。

    **读到上限就停**，不是读完再截：一个恶意的或被配错的接口返回 10GB 时，
    "读完再截"已经把 10GB 拉进了内存。而 256KB 的上限（`04` DM6）存在的
    理由正是防这个。

    总字节数（`body_bytes`）也只在必要时才准确——被截断时它截至停止读取的那一刻，
    而不是响应的真实长度。这是刻意的取舍：真实长度要靠读完才知道，
    而那正是我们要避免的。
    """
    buffer = bytearray()
    total = 0
    for chunk in chunks:
        total += len(chunk)
        remaining = cap - len(buffer)
        if remaining <= 0:
            return bytes(buffer), True, total
        buffer.extend(chunk[:remaining])
        if len(buffer) >= cap:
            return bytes(buffer), True, total
    return bytes(buffer), False, total
