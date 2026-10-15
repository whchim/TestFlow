"""LLM 出站（异步）。**`openai` SDK 用的那个 `http_client` 从这里来。**

对应决策：`02` D2（两条出站路径都要经 Guardrail）、D7（LLM 调用收敛到 Provider 接口后）、
`08-工程规范.md` §2.3、ADR-0005。

## 为什么 `llm/` 拿不到 httpx，而这个文件在 `guardrail/`

架构测试禁止 `llm/` 之外的模块 import LLM SDK，也禁止 `guardrail/` 之外
（和 `client/`）的模块 import HTTP 客户端。两者合起来推出一个结论：

    `llm/qwen.py` 构造 `AsyncOpenAI(...)` 时**必须**有人把 `http_client=` 递进来，
    而它自己不能 import httpx/httpx2 去造一个。

那个"有人"就是这里。于是 `llm/qwen.py` 的构造参数类型只能是 `Any`
——一旦写成具体类型，它就得 import httpx，架构测试立刻变红。

## 为什么用 `httpx2` 而不是 `httpx`

`openai` 3.29 依赖的是 **`httpx2>=2.12,<3`**——一个**独立于 `httpx` 的包**
（内含自己的 `httpcore2`）。把 `httpx.AsyncClient` 递进去会类型不符。

这个事实是实测确认的，不是推断：
`importlib.metadata.requires("openai")` → `['httpx2<3,>=2.12.0', ...]`

**顺带一提**：M0 的架构测试原本只禁止 `{httpx, requests, aiohttp, urllib3, httpcore}`
——`httpx2` 不在名单里。那是一个真实的洞：任何模块都能 import 它而不被发现。
已在 M1 补上。

## 与目标 API 那条路径的差异：**不钉死**

被测目标的地址来自**用户上传的 Spec**，是不可信输入，所以要走完整的
IP 钉死 + 对端校验。而 LLM 提供商的地址来自**应用配置**（`llm_base_url`），
`02` D2 明确把它列为"固定的域名，用户不可控"。

威胁模型不同，防护力度也就不同：
* 目标路径：白名单 + 解析全部 IP + 校验 + 钉死 + 对端二次校验
* LLM 路径：**白名单**（只允许配置里那一个 host:port）+ 审计

**这不是"偷工减料"**：LLM 宿主不由用户决定，DNS rebinding 对它没有意义
（攻击者无法让我们去解析一个他控制的域名）。而白名单仍然必要——
它保证"即便某处 bug 让 LLM 客户端被用于访问别的地址，也发不出去"。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx2

from testflow.config import Settings
from testflow.guardrail.audit import AuditRecord, allow_record
from testflow.guardrail.errors import GuardrailError
from testflow.guardrail.pipeline import EgressPolicy, RequestContext

__all__ = ["build_llm_http_client", "llm_egress_policy"]

AuditSink = Callable[[AuditRecord], None]


def llm_egress_policy(settings: Settings) -> EgressPolicy:
    """LLM 出站的授权范围：**只有配置里那一个地址**。

    从 `llm_base_url` 派生，而不是另外加一个配置项——两个配置项描述同一件事，
    迟早会不一致，而不一致的表现是"模型调用全部失败"，排查方向还指向网络。
    """
    from testflow.guardrail.policy import Whitelist

    return EgressPolicy(whitelist=Whitelist.from_urls([settings.llm_base_url]))


class _AllowlistAsyncTransport(httpx2.AsyncBaseTransport):
    """只放行白名单内地址的异步传输层。

    不做 IP 钉死（见模块文档字符串），但**白名单与审计一个不少**——
    `Guardrail.md` §1.2 的第一设计目标是"绕不过去"，而一份"LLM 那条路没查"
    的设计等于留了一条绕过的路。
    """

    def __init__(
        self,
        *,
        allowed_host_port: str,
        audit_sink: AuditSink | None = None,
        run_id: uuid.UUID | None = None,
        delegate: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed = allowed_host_port
        self._audit_sink = audit_sink
        self._run_id = run_id
        self._delegate = delegate or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host_port = f"{request.url.host}:{request.url.port or _default_port(request.url.scheme)}"

        if host_port != self._allowed:
            # **这是平台故障，不是"请求被拒绝"。** LLM 宿主来自配置，
            # 走到这里说明有人把客户端用错了地方——那必须立刻暴露，
            # 而不是变成一条安静的审计记录。
            raise GuardrailError(
                f"LLM 出站目标 {host_port!r} 不在允许范围内（仅允许 {self._allowed!r}）。"
                "这可能意味着某个模块错误地复用了 LLM 客户端去访问别的地址。"
            )

        self._emit(
            allow_record(
                RequestContext(
                    url=str(request.url),
                    method=request.method,
                    direction="llm",
                    run_id=self._run_id,
                ),
                _plan_for(str(request.url)),
            )
        )
        return await self._delegate.handle_async_request(request)

    def _emit(self, record: AuditRecord) -> None:
        if self._audit_sink is not None:
            self._audit_sink(record)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _plan_for(url: str):
    """构造一个仅用于审计的 `SendPlan`（LLM 路径不钉死，IP 无意义）。"""
    from testflow.guardrail.pipeline import SendPlan
    from testflow.guardrail.policy import parse_target

    target = parse_target(url)
    return SendPlan(
        url=url,
        target=target,
        candidate_ips=(target.host,),
        original_host=target.host,
    )


def build_llm_http_client(
    settings: Settings,
    *,
    audit_sink: AuditSink | None = None,
    run_id: uuid.UUID | None = None,
) -> httpx2.AsyncClient:
    """构造交给 `openai.AsyncOpenAI(http_client=...)` 的客户端。

    **这是 `llm/qwen.py` 唯一能拿到 HTTP 客户端的途径。** 它返回的具体类型是
    `httpx2.AsyncClient`，但 `qwen.py` 不该知道这一点——它把参数标注成 `Any`
    并原样传递，于是"LLM 出站经过 Guardrail"这件事在**架构上**成立，
    而不是靠"记得注入"。

    ⚠️ 这条约束**静态检查不到**（`llm/` 允许 import LLM SDK，看不出它有没有
    正确注入）。`08` §2.3 因此要求一条行为测试：构造适配器 → 发一次真实调用 →
    断言请求出现在 Guardrail 的审计里。那条测试属于 M2（见路线图）。
    """
    base = urlsplit(settings.llm_base_url)
    allowed = f"{base.hostname}:{base.port or _default_port(base.scheme)}"

    return httpx2.AsyncClient(
        transport=_AllowlistAsyncTransport(
            allowed_host_port=allowed,
            audit_sink=audit_sink,
            run_id=run_id,
        ),
        timeout=settings.llm_timeout_s,
        # 与目标路径同理：不跟随重定向。LLM API 的重定向没有任何合理用途，
        # 而跟随即绕过白名单。
        follow_redirects=False,
    )
