"""可控的被测对象（fixture 服务）。

对应决策：`09-测试策略.md` §3.2（TS3）、§3.3。

**为什么必须有**

端到端测试需要一个"可以打"的 API。若打真实外部接口：结果不稳定、需要真实
凭证、可能污染对方数据，而且**无法构造特定的失败场景**——你怎么让一个正常
的服务返回"200 但 body 不是 JSON"？

**设计原则**

每个端点对应一条**已经在设计文档里写死的行为**。它不是"随便一个测试用的
API"，而是那些设计行为的**可执行规格**。表格见 `09` §3.2。

**它同时是 Spec 的来源**：FastAPI 自动生成的 `/openapi.json` 可以直接喂给
TestFlow 当作被测接口文档，省掉手工维护一份测试用 Spec。

⚠️ **只用于测试，绝不部署到生产**。它监听 127.0.0.1，本身就是内网目标，
因此测试环境必须配置 `guardrail_allow_private_targets` 例外（ADR-0018）——
这反过来正好验证了例外机制本身可用。

用法::

    uv run testflow-fixture                 # 监听 127.0.0.1:9000
    curl localhost:9000/bad-json
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Annotated, Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------- 数据模型


class OkResponse(BaseModel):
    status: str
    data: dict[str, Any]


class TreeNode(BaseModel):
    """自引用模型——FastAPI 会为它生成递归的 `$ref` 结构。

    这正是 `SpecParser` SP1（`$ref` 展开深度上限）需要处理的情形。
    """

    name: str
    child: TreeNode | None = None


class ValidationErrorItem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ValidationErrorResponse(BaseModel):
    detail: list[ValidationErrorItem]


# --------------------------------------------------------------- 命中计数

#: 记录各路径被真正请求到的次数。
#:
#: 存在的理由很具体：验证 SR-3 的**第二层防护**时，只断言"Guardrail 返回了
#: 拒绝"是不够的——那只能说明我们决定不发出请求，不能说明请求**确实没到达**。
#: 有了计数，测试可以断言"该端点的命中数为 0"。
_hits: Counter[str] = Counter()


def _record(path: str) -> None:
    _hits[path] += 1


# ------------------------------------------------------------------ 应用

app = FastAPI(
    title="TestFlow Fixture API",
    version="0.1.0",
    description=(
        "TestFlow 的可控被测对象。每个端点对应一条设计文档中写死的行为，"
        "是那些行为的可执行规格。见 `docs/09-测试策略.md` §3.2。"
    ),
    # ⚠️ **`servers` 不能省。** FastAPI 默认不生成它，而 TestFlow 的
    # `declared_targets` 正是从 `servers[].url` 提取的（`SpecParser` SP2/§5）。
    # 缺了它，白名单为空 → 所有请求被 Guardrail 拒绝 → 端到端测试根本跑不起来。
    #
    # 这也顺带构成了一个真实用例：fixture 监听 127.0.0.1，本身是内网地址，
    # 因此测试环境必须配置 `guardrail_allow_private_targets` 例外。
    # 例外机制因此被日常测试持续使用——比专门为它写一条单测有力得多。
    servers=[{"url": "http://127.0.0.1:9000"}],
)


@app.get("/ok", response_model=OkResponse, summary="正常路径")
async def ok() -> OkResponse:
    """200 + 合法 JSON。用于验证最基本的正常流程。"""
    _record("/ok")
    return OkResponse(status="ok", data={"id": 1, "name": "example"})


@app.get("/echo", operation_id="echo_get", summary="回显请求")
@app.post("/echo", operation_id="echo_post", summary="回显请求")
async def echo(request: Request) -> JSONResponse:
    """回显请求内容。用于验证 `json_path` / `header` 各 operator 的求值。

    **只注册 GET 与 POST**：PUT / PATCH 属于破坏性方法，会被 Guardrail 拦下
    （SR-3），挂在这里也打不到。而显式的 `operation_id` 是必需的——用
    `api_route` 挂多个方法时 FastAPI 会给每个方法生成**同一个** operationId，
    而本服务的 `/openapi.json` 要作为测试输入喂给 SpecParser，不能带这种脏数据。
    """
    _record("/echo")
    body: Any = None
    raw = await request.body()
    if raw:
        try:
            import json

            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", errors="replace")
    return JSONResponse(
        {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": {
                k.lower(): v
                for k, v in request.headers.items()
                if k.lower() not in {"host", "content-length"}
            },
            "body": body,
        }
    )


@app.get("/no-content", status_code=204, summary="204 无响应体")
async def no_content() -> Response:
    """无 body。用于验证 `json_path` 断言在空 body 上的行为。"""
    _record("/no-content")
    return Response(status_code=204)


@app.get("/bad-json", summary="200 但响应体不是 JSON")
async def bad_json() -> Response:
    """**返回 200，声称是 JSON，实际是 HTML。**

    这是 `03/TestExecutor.md` TE2（断言级三态）**唯一**的验证手段。

    如果实现把"断言无法求值"（`error`）与"断言不成立"（`failed`）合并成
    一态——那是更短的代码、看起来更干净——**其他所有测试仍然是绿的**，
    只有这个端点能暴露它。

    真实场景中它对应"服务返回了一个错误页面，却声称 Content-Type 是 JSON"。
    """
    _record("/bad-json")
    return Response(
        content="<html><body>502 Bad Gateway</body></html>",
        media_type="application/json",  # 刻意说谎，模拟真实世界的坏服务
        status_code=200,
    )


@app.get("/error-500", summary="服务端异常")
async def error_500() -> JSONResponse:
    """500。用于验证 `server_error` 归因规则。"""
    _record("/error-500")
    return JSONResponse({"error": "internal", "message": "something broke"}, status_code=500)


@app.post(
    "/invalid-echo",
    status_code=400,
    response_model=ValidationErrorResponse,
    summary="请求校验失败",
)
async def invalid_echo() -> JSONResponse:
    """400 + 校验错误结构。用于验证 `request_invalid` 归因规则。

    形状模仿 FastAPI 的校验错误（`03/LogAnalyzer.md` Q2：MVP 先支持
    FastAPI / Spring 的常见结构）。
    """
    _record("/invalid-echo")
    return JSONResponse(
        {
            "detail": [
                {
                    "loc": ["body", "email"],
                    "msg": "field required",
                    "type": "value_error.missing",
                }
            ]
        },
        status_code=400,
    )


@app.get("/slow", summary="响应慢于 HTTP 超时")
async def slow(seconds: Annotated[float, Field(gt=0)] = 30.0) -> OkResponse:
    """延迟返回。用于验证 TE7 的三层超时与 `timeout` 状态。

    默认 30 秒——刻意超过 `http_timeout_s`（默认 10s），使请求必然超时。
    """
    _record("/slow")
    await asyncio.sleep(seconds)
    return OkResponse(status="ok", data={"slept": seconds})


@app.get("/redirect", summary="返回 3xx 重定向")
async def redirect(to: str = "/ok") -> Response:
    """302 重定向。用于验证 SR-4（不跟随重定向）。

    `?to=` 可指向任意地址——包括元数据地址（`http://169.254.169.254/`），
    用来验证"即使目标在白名单内，重定向也不会被跟随"。
    """
    _record("/redirect")
    return Response(status_code=302, headers={"Location": to})


@app.get("/large", summary="响应体超过截断上限")
async def large(kb: Annotated[int, Field(gt=0, le=4096)] = 512) -> PlainTextResponse:
    """返回超过 256 KB 的响应体。用于验证 `04` DM6（截断 + `truncated` 标记）。

    默认 512 KB，是截断上限（256 KB）的两倍。
    """
    _record("/large")
    return PlainTextResponse("x" * (kb * 1024))


@app.get("/recursive", response_model=TreeNode, summary="递归嵌套结构")
async def recursive(depth: Annotated[int, Field(ge=0, le=32)] = 3) -> TreeNode:
    """返回递归嵌套的树。

    它的**接口文档**（`/openapi.json`）中含有自引用的 `$ref`，是 `SpecParser`
    SP1（展开深度上限）的验证素材。
    """
    _record("/recursive")
    node = TreeNode(name=f"leaf-{depth}")
    for level in reversed(range(depth)):
        node = TreeNode(name=f"level-{level}", child=node)
    return node


@app.delete("/destructive", summary="破坏性方法")
async def destructive() -> OkResponse:
    """接受 DELETE。用于验证 SR-3 的**第二层拦截**。

    N4 会在投递前过滤掉破坏性用例（第一层）。本端点用于验证第二层：
    即使有人绕过了应用层逻辑、真的把 DELETE 发了出去，Guardrail 也应拦下。

    断言方式不是"收到了拒绝"，而是**查询 `/__hits` 确认本端点从未被访问**。
    """
    _record("/destructive")
    return OkResponse(status="deleted", data={})


# ------------------------------------------------------- 测试辅助端点
# 这两个端点前缀为 `__`，用于测试自省，不出现在设计文档描述的 11 个端点中。


@app.get("/__hits", summary="[测试辅助] 各端点被访问次数")
async def hits() -> dict[str, int]:
    """返回命中计数。

    核心用途：验证"Guardrail 拦截后请求**确实没到达**目标"，而不只是
    "我们决定不发出"。这两件事的区别就是两层防护是否都成立。
    """
    return dict(_hits)


@app.post("/__reset", summary="[测试辅助] 清零计数")
async def reset_hits() -> dict[str, str]:
    _hits.clear()
    return {"status": "reset"}


@app.get("/__slow-hits", summary="[测试辅助] 等待并返回 slow 端点命中数")
async def slow_hits() -> dict[str, Any]:
    """配合 `/slow` 使用：等待足够长时间后确认请求确实到达过。"""
    return {"slow": _hits.get("/slow", 0), "at": time.time()}


def main() -> None:
    """启动 fixture 服务。端口固定 9000（`09` §3.3 的验证命令依此）。

    监听地址可由 `FIXTURE_HOST` 覆盖，**默认是环回**。

    默认值选环回而不是 `0.0.0.0` 是刻意的：这个服务**没有任何认证**，
    它的全部设计目的就是"可以被随意打"。绑到 `0.0.0.0` 会让同网段的
    任何人拿到一个开放的、无认证的 API——而它里面还有一个
    `/destructive` 端点。**"默认安全"与"默认可用"之间，这里选前者。**

    容器里必须显式设 `FIXTURE_HOST=0.0.0.0`，否则同网络的其他容器连不上。
    那个动作是**可见的**——它出现在 compose 文件里，而不是某个默认值里。
    """
    import os

    import uvicorn

    host = os.environ.get("FIXTURE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=9000, log_level="warning")


if __name__ == "__main__":
    main()
