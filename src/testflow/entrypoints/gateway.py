"""Gateway 进程入口。

对应决策：`08-工程规范.md` EN1、`02-架构设计.md` §2.2、`05-API契约.md`（接口契约）。

职责：REST 接口、SSE 单通道、内部唤醒端点、对外响应脱敏。
**不含业务逻辑**——网关是薄层，规则判断下沉到编排层或模块层，
否则会形成绕过 Guardrail 的第二条路径。

## ⚠️ `create_app()` 必须能在**没有任何外部服务**的情况下被调用

`scripts/export-openapi.sh` 在 CI 里调用它导出接口契约快照。
那一步的目的是"比对契约与代码是否一致"——**不是"检查数据库在不在"**。

所以这里只装配路由与中间件，**一个连接都不建**。真正的连接发生在第一次
请求时（`gateway/deps.GatewayContext` 全是惰性属性）。

这条约束有一个附带的好处：它把"导入时产生副作用"这件事挡在门外。
一个能在 import 时连数据库的模块，会在你只想读一下它的常量时
意外地产生连接——而那种问题的表现是"某个不相关的测试偶尔超时"。
"""

from __future__ import annotations

import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from testflow import __version__
from testflow.config import Settings, load_settings
from testflow.gateway.deps import GatewayContext, Producer
from testflow.gateway.internal import router as internal_router
from testflow.gateway.problems import ProblemDetail, problem_from_request, problem_response
from testflow.gateway.routes import runs, specs
from testflow.gateway.schemas import HealthResponse
from testflow.observability.logging import configure_logging, get_logger

__all__ = ["create_app", "main"]

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    context: GatewayContext | None = None,
    producer: Producer | None = None,
) -> FastAPI:
    """构造 Gateway 应用。**不建任何连接。**

    Args:
        settings: 配置。`None` 时用 `load_settings()`——**注意这仍然是惰性的**：
            它只读环境变量，不连数据库。
        context: 完整的上下文（测试用，可以塞一个假的 session 工厂）。
        producer: 编排队列的生产者。`None` 时 `POST /api/runs` 会返回 503——
            这是刻意的：**没有生产者时"已接受"是个谎言**。

    Returns:
        装配好的 `FastAPI` 实例。它可以直接被 `openapi()` 调用导出契约。
    """
    app = FastAPI(
        title="TestFlow",
        version=__version__,
        description=(
            "基于多智能体编排的 API 自动化测试平台。\n\n"
            "**接口契约以代码为准**（ADR-0023）：这份文档由 FastAPI 从路由与 "
            "Pydantic 模型自动生成，`openapi.json` 是它的产物而非来源。"
        ),
        # 不设 `servers`：那是**被测文档**才需要的字段（`SpecParser` 从它提取
        # 白名单）。给自己的接口文档写一个 servers 只会让人误以为
        # TestFlow 也支持"只提供文档内容"的用法——它不支持。
        docs_url="/docs",
        redoc_url=None,
    )

    ctx = context or GatewayContext(settings=settings or load_settings(), producer=producer)
    app.state.ctx = ctx

    app.include_router(specs.router)
    app.include_router(runs.router)
    app.include_router(internal_router)

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"], summary="存活检查")
    async def healthz() -> HealthResponse:
        """**只报告"进程活着"，不报告"依赖可达"。**

        这条区分很重要：如果它去连数据库，那么数据库抖动时 Gateway 会被
        健康检查判死并重启——而重启一个正常的进程解决不了数据库的问题，
        只会让正在处理的请求全部中断。

        依赖的连通性由**启动期自检**与运维监控负责，不是这条端点。
        """
        return HealthResponse(status="ok")

    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """统一错误响应为 `application/problem+json`。

    覆盖三类：

    * **`HTTPException`** —— FastAPI 自己抛的（422 校验失败、401 等）。
      不接管的话它们会返回 `{"detail": ...}`，与我们的 problem+json 格式不一致，
      而调用方要为两种格式各写一份解析。
    * **`RequestValidationError`** —— 请求体不符合 Pydantic 模型。
      它的默认响应会把**用户的输入原样回显**，而那可能包含凭证
      （例如有人把 API key 放错了字段）。这里只回显位置与原因。
    * **其他一切** —— 兜底。**不返回异常消息**：里面可能有 SQL、路径、凭证。
    """

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return problem_from_request(request, exc)

    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            ProblemDetail(
                type="invalid_request",
                title="请求不符合接口约定",
                status=422,
                code="invalid_request",
                errors=[
                    {
                        # 只回显**位置**与**原因**，不回显 `input`——
                        # 用户的输入可能包含凭证（有人把 key 放错了字段）。
                        "pointer": "/" + "/".join(str(p) for p in err.get("loc", ())),
                        "message": str(err.get("msg", "")),
                    }
                    for err in exc.errors()
                ],
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        titles = {
            401: "未认证",
            403: "无权限",
            404: "资源不存在",
            405: "方法不被允许",
            413: "请求体过大",
            503: "服务暂时不可用",
        }
        return problem_response(
            ProblemDetail(
                type=f"http_{exc.status_code}",
                title=titles.get(exc.status_code, "请求失败"),
                status=exc.status_code,
                code=f"http_{exc.status_code}",
                # 401 的 `detail` 是我们自己写的固定文案，可以安全回显。
                detail=str(exc.detail) if exc.status_code < 500 else "",
            )
        )


def main() -> None:
    """启动 Gateway。

    生产者在这里注入——**这是唯一知道"队列是怎么实现的"的地方**。
    网关主体（`gateway/`）只认一个 `orchestrate(run_id, reason)` 协议。
    """
    settings = load_settings()
    configure_logging(settings.log_level)

    app = create_app(settings, producer=_celery_producer(settings))
    _log_bind_warning(settings)

    import uvicorn

    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # 用我们自己配的 handler，不要 uvicorn 再装一套
    )


def _celery_producer(settings: Settings):  # noqa: ANN202 —— 返回一个局部协议实现
    """构造 Celery 生产者。

    用 `send_task(name, args, queue)` 而不是 import 任务函数：
    **那会让 Gateway 依赖 `graph/`**（任务定义在那边）。而网关是薄层，
    它只需要知道"有个叫 `testflow.orchestrate` 的队列消息"。
    队列名与任务名是契约，定义在 `celery_app.py` 里。
    """
    from testflow.celery_app import ORCHESTRATE_QUEUE, build_celery_app

    app = build_celery_app(settings)

    class _Producer:
        def orchestrate(self, run_id, reason: str) -> None:
            app.send_task(
                "testflow.orchestrate", args=[str(run_id), reason], queue=ORCHESTRATE_QUEUE
            )

    return _Producer()


def _log_bind_warning(settings: Settings) -> None:
    """绑定到非环回地址时**显式提醒**。

    `config.py` 的启动期校验已经保证了"非环回必有令牌"。
    这条提醒说的是另一件事：**你现在对整个网络开放了，而那里存着加密凭证。**
    一个必然通过的安全检查不代表使用者知道自己在做什么。
    """
    from testflow.config import LOOPBACK_HOSTS

    if settings.bind_host not in LOOPBACK_HOSTS:
        logger.warning(
            "Gateway 绑定在 %s:%d —— **对网络开放**，其中含加密凭证存储。确认这是有意的。",
            settings.bind_host,
            settings.bind_port,
        )


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)
