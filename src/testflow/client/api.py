"""CLI 用的 Gateway 客户端（PRD F9）。

对应决策：`01-TestFlow_PRD.md` F9、`08-工程规范.md` §2.2、`05-API契约.md`。

## 它是**纯 HTTP 客户端**

CLI 不解析 OpenAPI、不生成用例、不执行请求——它只把 HTTP 请求发出去、
把响应读回来。PRD F9 要求这一点，而理由不是"分层好看"：

    如果 CLI 能自己解析 Spec，它就必须能读文件、能理解 OpenAPI——
    而**它跑在用户的机器上**。那时"这个文档会被怎么解释"就有了两个实现，
    而两者不一致的部分会表现为"CLI 说没问题，平台说解析失败"。

`tests/architecture/` 强制这条：`cli.py` 只能 import `testflow.client`
与 `testflow.domain`，不能碰 `graph/` / `execution/` / `storage/`。

## 它**不从 `Settings` 读配置**

`Settings` 有五个必填项（`database_url`、`internal_token`、
`credential_encryption_key`……），而它们是**服务端**的配置。
一个只想上传一份文档的用户不该需要知道数据库在哪，更不该把
凭证加密密钥放进自己的环境变量里。

所以这个客户端只接收两个东西：Gateway 地址与 Token。它们也是 CLI
那两个全局选项唯一映射到的配置。

## 与 `control_plane.py` 的分工

| | `ControlPlaneClient` | 本模块 |
| :-- | :-- | :-- |
| 调用者 | Worker（平台内部） | CLI（用户） |
| 认证 | `x-internal-token` | `Authorization: Bearer` |
| 端点 | `/internal/*`（能触发图推进） | `/api/*`（用户权限） |

**两者不能合并。** 合并意味着用户 Token 那条路径也能触发图推进，
而那正是 `05` §5.3 的双令牌设计要防的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "GatewayApiClient",
    "GatewayError",
    "ReportReady",
    "RunStatus",
    "SpecUploaded",
]

#: 默认的 Gateway 地址。与 `Settings.bind_port` 的默认值一致——
#: 但**刻意不 import 它**：那会把服务端配置拖进 CLI 的依赖里（见模块文档串）。
DEFAULT_BASE_URL = "http://127.0.0.1:3000"

#: 请求超时（秒）。
#:
#: 比 `Settings.http_timeout_s`（10 秒，那是**打被测目标**的超时）宽松得多：
#: 这里的每次调用都可能在服务端触发一次解析或查询。上传一份大文档解析几秒
#: 是正常的，而把那个正常耗时判成超时，用户会以为平台挂了。
DEFAULT_TIMEOUT_S = 30.0


class GatewayError(Exception):
    """Gateway 拒绝了这次请求，或它不可达。

    ## 为什么不用 `httpx` 自己的异常

    Gateway 的错误体是 `application/problem+json`（API4），里面有
    `code` 与**给用户看的** `detail`。`httpx.HTTPStatusError` 的消息是
    `"Client error '409 Conflict' for url '...'"`——它把最有用的两个字段
    （为什么、怎么办）丢掉，只留下状态码。

    所以这里把 problem 的内容**提出来**变成一个可读的异常，
    而 CLI 只要 `print(exc)` 就够了。
    """

    def __init__(
        self,
        *,
        status: int | None,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass(frozen=True, slots=True)
class SpecUploaded:
    """`POST /api/specs` 的结果。"""

    spec_id: str
    fingerprint: str
    operation_count: int
    declared_targets: list[str]
    #: 指纹命中已有 Spec 时为 `True`——**没有重新解析、没有重复落库**（DM2）。
    reused: bool
    #: 降级提示。**CLI 必须把它们打出来**：`declared_targets` 为空意味着
    #: 所有请求都会被 Guardrail 拒绝，而用户不看 `warnings` 就不知道为什么。
    warnings: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunStatus:
    """`GET /api/runs/{id}` 的结果。"""

    run_id: str
    phase: str
    expected_case_count: int
    settled_case_count: int
    failure_reason: str | None = None
    #: 仅在中断态出现（`05` §4.3）。**非空意味着"轮到你了"**。
    interrupt: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        """是否已经结束。

        与 `RunPhase.is_terminal` 保持一致（`done` / `failed` / `abandoned`）。
        **这里刻意复制一份字面量而不 import 那个枚举**：CLI 只能依赖
        `client` 与 `domain`，而 `RunPhase` 在 `domain` 里——按理可以 import。
        但客户端不该**解释**服务端的状态机，只该如实转达它看到的字符串；
        枚举一改名，这里静默漏掉一个终态，`--wait` 就会永远转下去。
        所以复制加断言（见 `test_cli.py::test_terminal_phases_match_the_domain`）。
        """
        return self.phase in {"done", "failed", "abandoned"}

    @property
    def awaiting_review(self) -> bool:
        return self.phase == "awaiting_review"


@dataclass(frozen=True, slots=True)
class ReportReady:
    """`GET /api/runs/{id}/report` 的结果，两种形态各取所需。"""

    content: dict[str, Any]
    rendered: str


class GatewayApiClient:
    """只打自己 Gateway 的用户端点。**没有任何接收 URL 参数的方法。**

    地址由 `base_url` 加固定的路径模板拼出，`run_id` / `spec_id` 只作为
    路径片段被填入。这不是"暂时没实现通用方法"，是设计：一个能接受任意
    地址的方法会把 `client/` 变成一条通用出站路径，而它不经过 Guardrail
    （见 `control_plane.py` 里对这条边界的说明）。
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout,
            # 与 Guardrail 的客户端同理：**不跟随重定向**。
            # Gateway 的接口没有"请去别处"这个语义，而跟随会让
            # Bearer Token 被送到重定向目标——那是一个凭空的凭证泄露。
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    @property
    def base_url(self) -> str:
        """Gateway 地址。**CLI 用它拼给人看的确认链接**（`/runs/{id}/confirm`）。

        那个页面由前端提供（S6），路径模板在 CLI 这一侧——
        而 Gateway 的 API 里并没有 `confirm` 这个端点，它是一个**前端路由**。
        """
        return self._base

    # ----------------------------------------------------------- 生命周期

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GatewayApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- 接口

    def upload_spec(self, content: str, *, filename: str | None = None) -> SpecUploaded:
        """提交一份 OpenAPI 文档。

        走 `application/json` 而不是 `multipart/form-data`：文档已经是内存里的
        一个字符串了（CLI 自己读的文件），而 multipart 要再编一次码。
        两种形态服务端都接受（`05` §4.1），选代价小的那个。
        """
        body = self._request("POST", "/api/specs", body={"content": content, "filename": filename})
        return SpecUploaded(
            spec_id=body["spec_id"],
            fingerprint=body["fingerprint"],
            operation_count=body["operation_count"],
            declared_targets=list(body.get("declared_targets", [])),
            reused=bool(body.get("reused")),
            warnings=list(body.get("warnings", [])),
        )

    def create_run(self, spec_id: str) -> str:
        """发起一次运行，返回 `run_id`。

        **不返回 phase**：那是异步创建的，此刻读到的 `init` 只是起点，
        没有任何信息量。要状态就调 `get_run`——一个来源，一个答案。
        """
        body = self._request("POST", "/api/runs", body={"spec_id": spec_id})
        return str(body["run_id"])

    def get_run(self, run_id: str) -> RunStatus:
        body = self._request("GET", f"/api/runs/{run_id}")
        return RunStatus(
            run_id=str(body["run_id"]),
            phase=str(body["phase"]),
            expected_case_count=int(body.get("expected_case_count", 0)),
            settled_case_count=int(body.get("settled_case_count", 0)),
            failure_reason=body.get("failure_reason"),
            interrupt=body.get("interrupt"),
        )

    def get_report(self, run_id: str) -> ReportReady:
        """取报告。

        **一次请求拿两种形态是不行的**——服务端按 `Accept` 二选一。
        所以要两次请求，而两次拿到的仍是**同一次生成的结果**：
        `reports.content` 与 `reports.rendered` 是一行里的两个字段，
        不会因为请求格式不同而重新计算（RA4/RA5）。

        只请求一次、由调用方决定形态也是可行的，但那会让"JSON 形态"
        带上一个 `rendered` 字段——而它可能有几百行 Markdown，
        对读 JSON 的程序来说是纯噪音。
        """
        content = self._request("GET", f"/api/runs/{run_id}/report")
        rendered = self._request("GET", f"/api/runs/{run_id}/report", markdown=True)
        return ReportReady(content=content, rendered=rendered)

    # ----------------------------------------------------------- 内部

    def _request(
        self, method: str, path: str, *, body: dict | None = None, markdown: bool = False
    ) -> Any:
        """发一次请求，把响应归一成 Python 值或 `GatewayError`。

        **一处统一处理 problem+json**：每个方法各自解析错误体的话，
        迟早有一个漏掉——而它表现为"某个端点的错误信息是一串英文的
        httpx 消息"，其余端点都正常。

        参数叫 `body` 而不是 `json`：`json` 是 `httpx.request` 的**关键字**
        参数名，同名会让"哪个 json 是哪个"多一层歧义。
        """
        headers = {"Accept": "text/markdown"} if markdown else None
        try:
            response = self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            # 连不上 / 超时 —— 也是 `GatewayError` 的一种，因为对 CLI 来说
            # "服务器拒绝了"和"服务器不在"是同一件事：这次操作没成功，
            # 而用户要做的事也一样（看一眼地址配对了没有）。
            raise GatewayError(
                status=None,
                code="gateway_unreachable",
                message=f"无法连接 Gateway（{self._base}）：{type(exc).__name__}",
            ) from exc

        # `>= 300` 而不是 `>= 400`：Gateway 的接口**不返回重定向**
        # （路径都是直的）。收到一个 3xx 说明我们打的根本不是 Gateway——
        # 中间有一层代理、或者地址配错了。把它当成功处理的话，下一步
        # `response.json()` 会在一个 HTML 上抛 `ValueError`，
        # 而那个异常看起来像"我们的代码坏了"。
        if response.status_code >= 300:
            raise self._to_error(response)

        if markdown:
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(
                status=response.status_code,
                code="unexpected_response",
                message=(
                    f"期望 JSON，实际收到 {response.headers.get('content-type', '未知类型')}。"
                    "这可能意味着你打的不是 Gateway。"
                ),
            ) from exc

    @staticmethod
    def _to_error(response: httpx.Response) -> GatewayError:
        """把 `problem+json` 转成可读的异常。

        **解析失败时回落到原文**，而不是抛一个解析异常：一个 500 的响应体
        可能根本不是 JSON（例如中间有一层代理返回了 HTML 错误页）。
        那时用户需要看到的是那段原文——它能告诉他"你打的不是 Gateway"。
        """
        try:
            problem = response.json()
        except ValueError:  # JSONDecodeError 是它的子类
            return GatewayError(
                status=response.status_code,
                code=f"http_{response.status_code}",
                message=response.text[:200],
            )

        if not isinstance(problem, dict):
            return GatewayError(
                status=response.status_code,
                code=f"http_{response.status_code}",
                message=str(problem)[:200],
            )

        parts = [str(problem.get("title") or "请求失败")]
        if problem.get("detail"):
            parts.append(str(problem["detail"]))
        for item in problem.get("errors") or []:
            parts.append(f"{item.get('pointer', '?')}: {item.get('message', '')}")

        return GatewayError(
            status=response.status_code,
            code=str(problem.get("code") or f"http_{response.status_code}"),
            message="；".join(parts),
        )
