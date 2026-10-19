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

import asyncio
import contextlib
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from testflow import __version__
from testflow.config import Settings, load_settings
from testflow.gateway.deps import GatewayContext, Producer
from testflow.gateway.internal import router as internal_router
from testflow.gateway.problems import ProblemDetail, problem_from_request, problem_response
from testflow.gateway.routes import runs, specs
from testflow.gateway.schemas import HealthResponse, ReadinessCheck, ReadinessResponse
from testflow.observability.logging import configure_logging, get_logger

__all__ = ["create_app", "main", "DEFAULT_FRONTEND_DIST"]

logger = get_logger(__name__)

#: 前端构建产物的默认位置。**相对进程的工作目录**。
#:
#: 这是一个刻意的取舍：包内相对路径在两种部署下都对不上——
#: 装成 wheel 之后 `src/testflow/` 会落到 `site-packages/`，而 `frontend/dist`
#: 在仓库根。所以工作目录成了契约的一部分，而它由两处保证：
#: 本地开发从仓库根运行，容器里 `WORKDIR /app` 且 dist 被 COPY 到
#: `/app/frontend/dist`。两处都不是隐含的假设。
DEFAULT_FRONTEND_DIST = Path("frontend/dist")

#: **绝不能被 SPA 兜底吞掉**的路径前缀。
#:
#: 少了这一条，`GET /api/runs/不存在的id` 会返回 `index.html` 与 200——
#: 于是接口契约里"404 + problem+json"被一条 HTML 悄悄替换掉了，而
#: 调用方（CLI、前端自己、将来的外部集成）拿到的是一个解析不了的响应。
#: 更糟的是它**只在 dist 存在时**才发生，本地纯后端开发时一切正常。
_API_PREFIXES = ("api/", "internal/")


def create_app(
    settings: Settings | None = None,
    *,
    context: GatewayContext | None = None,
    producer: Producer | None = None,
    frontend_dist: Path | None = None,
) -> FastAPI:
    """构造 Gateway 应用。**不建任何连接。**

    Args:
        settings: 配置。`None` 时用 `load_settings()`——**注意这仍然是惰性的**：
            它只读环境变量，不连数据库。
        context: 完整的上下文（测试用，可以塞一个假的 session 工厂）。
        producer: 编排队列的生产者。`None` 时 `POST /api/runs` 会返回 503——
            这是刻意的：**没有生产者时"已接受"是个谎言**。
        frontend_dist: 前端构建产物目录。`None` 时用
            `DEFAULT_FRONTEND_DIST`（相对工作目录）。

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

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse, "description": "依赖不可达"}},
        tags=["ops"],
        summary="就绪检查",
    )
    async def readyz() -> JSONResponse:
        """**会碰依赖**的就绪检查：PG 与 Redis 都能用才返回 200。

        与 `/healthz` 的分工是刻意的（`10` §5）：

        * `/healthz` 不碰任何依赖——否则数据库抖一下，编排器会把一个
          **健康的 Gateway** 判死并重启，而重启解决不了数据库的问题
        * `/readyz` 明确回答"现在能不能干活"，供人、负载均衡与部署脚本使用

        依赖不可达时返回 **503 + problem 形状的 JSON**，而不是 200 加一个
        布尔字段——后者会让所有只看状态码的调用方以为一切正常。

        `detail` 只写**异常类型**。异常消息里可能有连接串、库名或路径，
        而这个端点不需要认证（运维探针要能直接打）。
        """
        checks = list(
            await asyncio.gather(
                _check_database(ctx),
                _check_redis(ctx.settings),
            )
        )
        ready = all(check.ok for check in checks)
        payload = ReadinessResponse(
            status="ok" if ready else "degraded",
            checks=checks,
        )
        return JSONResponse(
            status_code=200 if ready else 503,
            content=payload.model_dump(mode="json"),
        )

    # ★ 前端托管**必须注册在所有 API 路由之后**：路由是按注册顺序匹配的，
    # 而下面那条兜底会接住"前面谁都没匹配上"的路径。
    _install_frontend(app, frontend_dist or DEFAULT_FRONTEND_DIST)

    _install_error_handlers(app)
    return app


#: 就绪检查的超时。**必须远小于任何调用方的超时**：探针要的是"现在能不能
#: 干活"，而不是"等多久之后能不能"。2 秒足够本机/同网络的 PG 与 Redis 应答。
_READY_TIMEOUT_S = 2.0


async def _check_database(ctx: GatewayContext) -> ReadinessCheck:
    """`SELECT 1`。用线程跑同步驱动，避免阻塞事件循环。"""

    def _ping() -> None:
        with ctx.engine.connect() as connection:
            connection.execute(text("select 1"))

    try:
        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=_READY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 —— 健康检查要把所有失败都变成"不健康"
        return ReadinessCheck(name="database", ok=False, detail=type(exc).__name__)
    return ReadinessCheck(name="database", ok=True)


async def _check_redis(settings: Settings) -> ReadinessCheck:
    """`PING` broker 所在的库。

    只检查 broker 库而不检查 checkpoint 库：后者是图状态的缓存，丢了可以
    从 PG 重跑（ADR-0009）；前者不通则整个系统无法投递任务，那才是"不就绪"。
    """
    from redis.asyncio import Redis

    client = Redis.from_url(
        f"{settings.redis_url.rstrip('/')}/{settings.redis_broker_db}",
        socket_connect_timeout=_READY_TIMEOUT_S,
        socket_timeout=_READY_TIMEOUT_S,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        return ReadinessCheck(name="redis", ok=False, detail=type(exc).__name__)
    finally:
        # 关闭本身失败不该把一个"已经判定为不健康"的检查变成 500。
        with contextlib.suppress(Exception):
            await client.aclose()
    return ReadinessCheck(name="redis", ok=True)


def _install_frontend(app: FastAPI, dist: Path) -> None:
    """托管前端构建产物 + SPA 兜底路由。

    ## 为什么不用 `app.mount("/", StaticFiles(directory=dist, html=True))`

    它两件事都做不了：

    1. **`html=True` 的 404 行为**只覆盖"目录里没有这个文件"，而 SPA 需要
       `/runs/01J.../confirm` 这种**磁盘上根本不存在**的路径也返回
       `index.html`（那是前端路由，不是文件）
    2. 更糟的是它会**吞掉 API 的 404**：挂载在 `/` 上意味着任何没匹配上的
       路径都交给它，包括 `/api/runs/不存在的id`——于是契约里那条
       "404 + problem+json" 变成了一份 HTML。

    所以这里手写兜底：先看是不是真文件，再看是不是 API 路径，最后才
    回退到 `index.html`。

    ## dist 不存在时**不跳过注册，而是返回 404**

    两种做法都能让本地纯后端开发正常工作。选后者是因为它让**路由集合与
    工作目录无关**：CI 第 ④ 步导出契约快照时不依赖任何文件的存在，
    而"开发机上有 dist、CI 上没有"造成的差异会以
    "契约在本地和 CI 不一致"这种最难查的形式出现。
    """

    index_file = dist / "index.html"

    # `response_model=None`：这个端点返回 `FileResponse` 或 `JSONResponse`，
    # 而 FastAPI 不接受它们的联合作为响应模型（同 `GET /report`）。
    # 它是兜底路由、不进契约（`include_in_schema=False`），
    # 所以这里没有"契约会因此缺一块"的问题。
    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def _spa(full_path: str) -> FileResponse | JSONResponse:
        if full_path.startswith(_API_PREFIXES):
            # 前面所有 API 路由都没匹配上 —— 那就是真的不存在。
            # 走统一的 problem+json，与没有前端时**逐字节一致**。
            return problem_response(
                ProblemDetail(
                    type="not_found",
                    title="路径不存在",
                    status=404,
                    code="not_found",
                )
            )

        if not index_file.is_file():
            # 没有前端产物（纯后端部署 / CI）。明确说清，而不是返回一个
            # 空白的 200——那会让人以为前端坏了。
            return problem_response(
                ProblemDetail(
                    type="frontend_not_built",
                    title="前端尚未构建",
                    status=404,
                    code="frontend_not_built",
                    detail=f"未找到 {index_file}。请先运行 `npm ci && npm run build`。",
                )
            )

        # 真文件（`/assets/index-abc123.js` 这类）直接给；
        # 其余（`/upload`、`/runs/xxx/confirm`）交给前端路由，
        # 所以返回 `index.html` 让浏览器端的路由接手。
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_file)


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
