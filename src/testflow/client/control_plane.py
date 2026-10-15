"""控制面客户端——访问**我们自己**的服务，不是被测目标。

对应决策：`08-工程规范.md` §2.2（`client/` 的定位）、`05-API契约.md` §4.7（内部唤醒）、
ADR-0002（Guardrail 是**代替用户发出的**请求的唯一通道）。

## 为什么它不经过 Guardrail

Guardrail 的白名单是"这个 Run 允许访问哪些**目标 API**"——那些地址来自
用户上传的 Spec，是不可信输入。而控制面地址来自**应用配置**，
并且只指向我们自己的 Gateway。

把它塞进 Guardrail 会造成两处混淆：

* **白名单污染**——一个和被测系统无关的地址进了名单，而名单越宽 SR-1 越弱
* **信任级别混同**——唤醒端点需要 `internal_token`（`05` §5.3 明确它
  与用户 Token 权限不同）。把内部凭证交给一个"面向目标 API"的通道，
  会让两种信任边界之间的隔离失效

## 但 `client/` 也不是随便用的

架构测试限制 `testflow.client` **只能被 `entrypoints/cli` 引用**。
M1 把这个名单扩到 `entrypoints/worker`——因为唤醒请求是 Worker 发出的。

**这个扩展是刻意的、有界的**：`client/` 里只有"打固定配置地址"的客户端，
没有任何接收 URL 参数的通用方法。要再扩大引用范围，得先改那条架构规则，
而那是一次需要写理由的修改。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from testflow.config import Settings
from testflow.observability.logging import get_logger

__all__ = ["ControlPlaneClient", "WakeResult"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WakeResult:
    """`POST /internal/runs/{id}/wake` 的响应。

    只有两种取值（`05` §4.7）：

    * `pending` —— 还有用例没跑完，Gateway 什么都没做
    * `resumed` —— 全部到齐，Gateway 已经投递了推进指令

    **Worker 不据此做任何判断**——它只是通知者，完成判定在 Gateway 那边
    （`06` §6.2）。区分这两个值只是为了让日志能回答"唤醒有没有生效"。
    """

    status: str

    @property
    def resumed(self) -> bool:
        return self.status == "resumed"


class ControlPlaneClient:
    """只打自己 Gateway 的客户端。

    **没有任何接收 URL 参数的方法。** 这不是"暂时没实现"，是设计：
    一个能接受任意地址的方法会把 `client/` 变成第二条通用出站路径，
    而那正是架构规则要防的事。方法签名里只出现 `run_id`，
    地址由 `gateway_url` 与固定的路径模板拼出。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        gateway_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # 默认指向本机 Gateway。容器部署时由 `gateway_url` 覆盖成服务名。
        self._base = (gateway_url or f"http://127.0.0.1:{settings.bind_port}").rstrip("/")
        self._token = settings.internal_token
        self._client = httpx.Client(
            timeout=settings.http_timeout_s,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ControlPlaneClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wake(self, run_id: object) -> WakeResult:
        """通知 Gateway"这个 Run 又有用例完成了"。

        **幂等**（`06` §6.1）：重复调用无害，Gateway 自己查 PG 决定要不要推进。
        所以调用方**不需要**为失败做任何补偿——兜底轮询会收尾。

        **不发 cookie、不带 `Authorization` 之外的身份信息**：
        内部端点用独立 Token（`05` §5.3），而它比用户 Token 权限更高——
        泄露一份用户凭证不该能触发图推进。
        """
        response = self._client.post(
            f"{self._base}/internal/runs/{run_id}/wake",
            headers={"x-internal-token": self._token},
        )
        response.raise_for_status()
        return WakeResult(status=str(response.json().get("status", "unknown")))

    def health(self) -> bool:
        """Gateway 是否可达。**入口点启动时用它做一次快速自检**——
        一个连不上自己的 Worker 会写一堆结果、然后全部依赖兜底轮询收尾，
        而那看起来像"系统很慢"而不是"配置错了"。"""
        try:
            return self._client.get(f"{self._base}/healthz").status_code == 200
        except httpx.HTTPError:
            return False
