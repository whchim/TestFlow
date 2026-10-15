"""把请求**钉死**到已校验 IP 的传输层（GR2 / ADR-0019）。

对应决策：`03-模块详细设计/Guardrail.md` §4.2 步骤 ③④、GR2、ADR-0019（Q3）。

## 做法：改宿主 + 单独告诉 TLS 真名

一次出站要同时满足两件看起来矛盾的事：

* **TCP 必须连到我们已经校验过的那个 IP**——否则 DNS rebinding 的窗口还开着
* **TLS 必须仍按原始域名做 SNI 与证书校验**——否则证书必然对不上，HTTPS 直接失败

`httpcore` 恰好提供了这个缝：它从 `request.extensions["sni_hostname"]` 取 TLS 的
`server_hostname`（`httpcore/_sync/connection.py:107`），而 TCP 连的是 URL 里的宿主。
两者可以不同。

    URL 宿主   = 已校验的 IP   →  TCP 连到这里
    sni_hostname = 原始域名     →  TLS 按它做 SNI 与证书校验
    Host 头     = 原始域名      →  服务端看到的是正常请求

这就是 `Guardrail.md` 里 Q3（"钉死后 HTTPS 的 SNI 与证书校验如何处理"）的答案。

## 为什么自己实现传输层，而不是用 `httpx.HTTPTransport` 的扩展点

对端二次校验（步骤 ④）需要**已连接 socket 的对端地址**，而 `httpx` 的传输层
在返回响应时已经不再暴露那个 socket。

有两种绕法，**都不够好**：

* *在 `httpx.HTTPTransport` 上事后替换它的连接池*——依赖一个私有属性。
  一旦上游改名，替换会静默失效：**peer 校验悄悄不再执行，而没有任何报错**。
  安全机制最坏的失效方式就是这种。
* *放弃校验*——那就少了一道防线。

所以这里直接用 `httpcore` 构造传输层。代价是多写几十行；收益是
**每一处依赖都是公开 API，坏掉会大声报错**，而不是安静地退化。
"""

from __future__ import annotations

import contextlib
import ipaddress
from collections.abc import Iterable, Iterator
from typing import Any, cast

import httpcore
import httpx

from testflow.guardrail.errors import (
    GuardrailError,
    GuardrailTimeout,
    GuardrailTransportError,
    PeerVerificationError,
    classify_transport_error,
)

__all__ = [
    "PinnedAsyncTransport",
    "PinnedTransport",
    "PeerVerificationError",
    "PinnedPlanExtension",
]

#: 请求扩展里携带 `SendPlan` 的键。传输层据此知道该连哪里。
PinnedPlanExtension = "testflow_pinned_plan"

#: `httpcore` 的异常 → `httpx` 的异常。
#:
#: 调用方（执行器）期望的是 `httpx` 家族的异常——它要据此填 `case_results.error_kind`
#: （`dns_failure` / 连接被拒 / TLS 失败）。让 `httpcore` 的异常直接漏出去，
#: 执行器就得认识两个库的异常体系。
#:
#: **这里刻意逐个列出，而不是用 `getattr` 动态构造。** 前者在 httpcore 改名时
#: 会在 **import 阶段**就 `AttributeError`（写这段时真的撞上了：httpcore 没有
#: `CloseError`，`httpx` 有）；后者会安静地少映射几个类型，
#: 直到某个罕见错误发生时才表现为"执行器收到了一个不认识的异常"。
#:
#: 测试 `test_error_map_covers_httpcore_exceptions` 会保证新版本引入的异常
#: 不会漏掉。
_ERROR_MAP: dict[type[BaseException], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ProtocolError: httpx.ProtocolError,
}


@contextlib.contextmanager
def _map_errors() -> Iterator[None]:
    """把 `httpcore` 的异常转成本项目自己的类型。

    **对外只暴露 `guardrail` 的异常**（见 `errors.GuardrailTransportError`）——
    调用方（`execution/`）不该知道我们用了哪个 HTTP 库，更不该为了
    `except httpx.ConnectError` 而 import httpx：架构规则禁止
    `execution/` 里出现 HTTP 客户端，而这条规则**实测确实拦下过**
    ——写完执行器后 CI 立刻报 `testflow.execution.runner` 导入了 httpx。

    具体的失败原因（超时 / DNS / TLS / 连接被拒）在这里就分类好挂在异常上
    带出去。那是**传输层的知识**，放在这里比让执行器再走一遍
    `__cause__` 链更合适。
    """
    try:
        yield
    except Exception as exc:
        if isinstance(exc, GuardrailError):
            raise
        for core_type in _ERROR_MAP:
            if isinstance(exc, core_type):
                raise _translate(exc) from exc
        raise


def _translate(exc: Exception) -> GuardrailTransportError:
    kind = classify_transport_error(exc)
    if kind == "timeout":
        return GuardrailTimeout(str(exc))
    error = GuardrailTransportError(str(exc))
    error.error_kind = kind
    return error


def _normalize_host(value: str) -> str:
    """归一化成便于比较的字符串。

    `getpeername()` 在 IPv6 socket 上返回 `('::ffff:1.2.3.4', port, 0, 0)`
    这种 IPv4 映射形式，而我们要比对的是 `1.2.3.4`——不归一化会误报。
    """
    raw = value.split("%")[0]  # 去掉 zone id（`fe80::1%eth0`）
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw.lower()
    if isinstance(address, ipaddress.IPv6Address) and address in ipaddress.IPv6Network(
        "::ffff:0:0/96"
    ):
        address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return str(address)


def _verify_peer(
    stream: httpcore.NetworkStream | httpcore.AsyncNetworkStream, expected_host: str
) -> None:
    """连接建立**之后**、发数据**之前**，核对对端地址。

    这一步防的是什么：**透明的连接劫持**。若中间有一层代理或 iptables 的
    REDIRECT 规则把我们发往 A 的连接悄悄转到了 B，那么"我们连的是已校验的 IP"
    这个前提就不成立了——而 TLS 证书校验**拦不住**这种情况：
    证书是按域名验的，代理只要持有该域名的合法证书（企业 MITM 代理正是如此）
    就能让校验通过。

    `getpeername()` 拿到的地址则骗不了人：它就是内核实际连上的那个对端。

    Args:
        expected_host: 传给 `connect_tcp` 的宿主。本项目的传输层**总是**
            传一个字面量 IP（已校验过的那个），所以这里是精确比较。
            若是域名则跳过——那种情况下比较无从谈起，
            而它意味着钉死没有生效，那是一个应当在别处暴露的 bug。
    """
    try:
        expected = ipaddress.ip_address(_normalize_host(expected_host))
    except ValueError:
        return  # 不是 IP 字面量：不做比较（见 docstring）

    try:
        peer = stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 —— 探针失败不该让请求失败
        return

    if not peer:
        return

    actual = _normalize_host(str(peer[0]))
    if actual != str(expected):
        # **只抛，不关。** 关闭流在本模块里是异步的（`aclose`），
        # 而本函数是同步的——试图在这里关掉同步流、跳过异步流，
        # 会造成"同步路径断了、异步路径还开着"的不一致。
        # 由两条 `connect_tcp` 各自在自己的语境里关，两边行为一致。
        raise PeerVerificationError(_peer_mismatch_message(str(expected), actual))


def _peer_mismatch_message(expected: str, actual: str) -> str:
    return (
        f"连接目标被篡改：要求连接 {expected}，实际连到了 {actual}。"
        "可能存在透明代理或连接劫持，请求已被中止。"
    )


class _VerifyingSyncBackend(httpcore.NetworkBackend):
    """给 `connect_tcp` 套上对端校验的同步后端。"""

    def __init__(self, delegate: httpcore.NetworkBackend | None = None) -> None:
        self._delegate = delegate or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        stream = self._delegate.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        try:
            _verify_peer(stream, host)
        except PeerVerificationError:
            # 连上了一个不该连的地方——**先断开再上报**。
            # 不断开的话这次连接会留在池里，而它指向的是一个未经授权的对端。
            stream.close()
            raise
        return stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        # Unix socket 不经过 TCP，没有"对端 IP"可言。Guardrail 也不会用到它
        # （白名单只允许 http/https），保留是为了满足接口。
        return self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class _VerifyingAsyncBackend(httpcore.AsyncNetworkBackend):
    """异步版本。"""

    def __init__(self, delegate: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._delegate.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        try:
            _verify_peer(stream, host)
        except PeerVerificationError:
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


# ------------------------------------------------------------- 请求改写


def _pinned_core_request(
    request: httpx.Request,
) -> tuple[httpcore.Request, dict[str, str]]:
    """把 `httpx.Request` 改写成"连到 IP、但按域名说话"的 `httpcore.Request`。

    Returns:
        `(core_request, headers)`。headers 单独返回是为了让调用方在错误信息里
        用得上——`core_request.headers` 是 bytes 元组列表，不便阅读。

    Raises:
        GuardrailError: 请求上没有携带 `SendPlan`。**这是编程错误**：
            传输层只能被 Guardrail 的客户端使用，而客户端一定会附上它。
            静默放行（连原始域名）会让钉死失效，所以宁可报错。
    """
    plan = request.extensions.get(PinnedPlanExtension)
    if plan is None:
        raise GuardrailError(
            "出站请求缺少 SendPlan。传输层只能由 guardrail 的客户端构造的请求使用——"
            "否则钉死与对端校验都不会发生。"
        )

    headers = dict(request.headers)
    # Host 必须是**原始域名**：服务端据此做虚拟主机路由与 SNI。
    # 留着 IP 会让几乎所有真实服务返回 404 或证书错误。
    headers["host"] = plan.original_host

    # 只改宿主，路径与查询串原样保留。
    target_url = request.url.copy_with(host=plan.pinned_ip)

    core_request = httpcore.Request(
        method=request.method,
        url=httpcore.URL(
            scheme=target_url.raw_scheme,
            host=target_url.raw_host,
            port=target_url.port,
            target=target_url.raw_path,
        ),
        headers=[(k.lower(), v) for k, v in headers.items()],
        content=request.stream,
        extensions={
            **request.extensions,
            # ★ 这一行就是 Q3 的答案：TLS 仍按原始域名做 SNI 与证书校验。
            "sni_hostname": plan.original_host,
        },
    )
    return core_request, headers


class _ResponseStream(httpx.SyncByteStream):
    """把 `httpcore` 的响应流包成 `httpx` 认识的形态。

    `httpx` 内部有一个同名私有类。不 import 它（私有 API），自己写这十几行——
    它的全部职责就是"迭代时把底层流关掉"，而那个语义值得写明白。
    """

    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._stream
        finally:
            self.close()

    def close(self) -> None:
        # `close` 不写在协议里（`Iterable` 没有它），但 httpcore 给我们的每个
        # 流都实现了它。用 `getattr` 而不是 `cast` 是因为**语义上确实不保证**
        # ——而"不确定就跳过"在这里是安全的：不关流只会多占一条连接，
        # 不会造成正确性问题，而强行调用一个不存在的方法会直接崩。
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> Any:
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self._stream.aclose()


class PinnedTransport(httpx.BaseTransport):
    """同步传输层。Celery Worker 使用。

    刻意**不暴露代理设置**：`trust_env=False` 的效果由"不实现代理"天然达成。
    读环境变量里的 `HTTP_PROXY` 会让"唯一出站通道"取决于运行时环境——
    一个没人审查过的变量就能把全部流量导向别处。
    """

    def __init__(self, *, verify: bool = True, retries: int = 0) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpcore.default_ssl_context() if verify else None,
            network_backend=_VerifyingSyncBackend(),
            retries=retries,
            # 不复用连接：钉死是**逐请求**的，而连接池的键是 `host:port`。
            # 两条不同的请求钉到同一个 IP 时复用同一条连接本身没问题，
            # 但复用会让"这次连接到底连到哪"变得不易追溯。代价可接受
            # （测试流量本来就稀疏），换来的是每次连接都真的经过校验。
            max_keepalive_connections=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request, _ = _pinned_core_request(request)
        with _map_errors():
            response = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(cast("Iterable[bytes]", response.stream)),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """异步传输层。LLM 出站使用。"""

    def __init__(self, *, verify: bool = True, retries: int = 0) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpcore.default_ssl_context() if verify else None,
            network_backend=_VerifyingAsyncBackend(),
            retries=retries,
            max_keepalive_connections=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request, _ = _pinned_core_request(request)
        with _map_errors():
            response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_AsyncResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
