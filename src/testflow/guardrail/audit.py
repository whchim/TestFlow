"""审计记录的**构造**——含脱敏（SR-6 / GR6）。

对应决策：`03-模块详细设计/Guardrail.md` §10、`04-数据模型.md` §2.9。

## 脱敏在这里，不在存储层

`storage/repositories/audit.py` 只负责把拿到的东西写进去。清洗发生在**构造记录时**
——也就是这个模块。这样"审计里会不会有凭证"这个问题只有一个答案，
而不是"看调用方记不记得"。

## `allow` 与 `deny` 都要记（GR6）

只记拒绝，就无法回答**"这个 Run 到底访问了哪些 host"**——而那正是安全复盘
时最有价值的问题。一个只有拒绝记录的审计表，出事之后帮不上忙：
你能看到谁被拦了，看不到谁进来了。
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from testflow.guardrail.pipeline import RequestContext, SendPlan
from testflow.guardrail.policy import DenyReason

__all__ = ["AuditRecord", "allow_record", "deny_record", "redact_url", "request_digest"]

_REDACTED = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一条待落库的审计。字段与 `audit_logs` 表对齐（`04` §2.9）。"""

    direction: str
    target_host: str
    target_url: str | None
    decision: str
    deny_reason: str | None
    run_id: uuid.UUID | None
    request_digest: str | None
    trace_id: str | None


def redact_url(url: str) -> str:
    """**保留全部查询参数名，抹掉全部取值。**

    不区分"敏感"与"不敏感"——我们无法知道**任意一个用户**的 API 里哪个参数
    敏感。`?user_id=42` 在大多数接口里无所谓，在医疗或金融接口里就是 PII。
    按字段名去猜（`token` / `secret` / `password`…）必然漏，
    而**漏一次就是一次真实的泄露**，猜错的代价远大于多擦几个无害的值。

    保留参数名则是纯粹的信息收益：`?[名字]=[REDACTED]` 仍然告诉你
    **这个接口发了哪些参数**，而"这里面有一个叫 token 的参数"本身就是一条
    值得记录下来的设计缺陷——抹掉名字会让它变成一个看不见的事实。
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted = [(name, _REDACTED) for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(redacted)))


def request_digest(method: str, url: str, body: bytes | None = None) -> str:
    """请求的指纹，用于把审计记录与执行结果对上。

    **是哈希，不是原文**——所以即便 URL 里混进了没被识别的敏感值，
    它也不会以可读形式落库。这让脱敏多了一道不依赖模式匹配的兜底：
    脱敏靠"猜到哪些字段敏感"，而哈希不靠猜。
    """
    payload = f"{method.upper()} {url}".encode()
    if body:
        payload += b"\n" + body
    return hashlib.sha256(payload).hexdigest()


def _base(ctx: RequestContext, *, decision: str, deny_reason: DenyReason | None) -> AuditRecord:
    from testflow.observability.tracing import current_trace_id

    return AuditRecord(
        direction=ctx.direction,
        # `target_host` 记**原始域名**而不是钉死的 IP：审计要回答的是
        # "访问了哪个服务"，而不是"打到了哪台机器"。而 IP 是变化的
        # （负载均衡、迁移），把它记成主键会让历史记录失去可比性。
        target_host=urlsplit(ctx.url).hostname or "",
        target_url=redact_url(ctx.url),
        decision=decision,
        deny_reason=deny_reason.value if deny_reason else None,
        run_id=ctx.run_id,
        request_digest=None,
        trace_id=current_trace_id(),
    )


def allow_record(ctx: RequestContext, plan: SendPlan) -> AuditRecord:
    """放行的记录。"""
    return dataclasses.replace(
        _base(ctx, decision="allow", deny_reason=None),
        request_digest=request_digest(ctx.method, ctx.url),
        target_host=plan.target.host,
    )


def deny_record(ctx: RequestContext, reason: DenyReason, detail: str = "") -> AuditRecord:
    """拒绝的记录。

    `detail` **刻意不落库**——它可能存在 `audit_logs` 没有的字段里，
    而为了"多存一句话"去改表结构不值得。它由调用方写进日志。
    """
    return _base(ctx, decision="deny", deny_reason=reason)
