"""`/api/runs` —— 发起与查询测试运行。

对应决策：`05-API契约.md` §4.2~§4.6、`06-Agent编排.md` §5.1（中断态）。

## 网关是**纯生产者**

`POST /api/runs` 只做三件事：落一行 `runs`、把 run_id 丢进 `orchestrate` 队列、
返回 202。**它不 import `graph/`，也不 import `execution/`**——
"图长什么样"的变化不该波及到网关（`02` §2.2）。

## 202 而不是 201

Run 是**异步**创建的：202 的意思是"已接受，正在处理"。用 201（"已创建"）
会让调用方以为可以立刻查询结果。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from testflow.domain.graph import RunPhase
from testflow.gateway.auth import require_user_token
from testflow.gateway.deps import GatewayContext
from testflow.gateway.problems import ProblemDetail, problem_response
from testflow.gateway.schemas import (
    CreateRunRequest,
    ReportResponse,
    RunCreatedResponse,
    RunStatusResponse,
)
from testflow.observability.logging import get_logger
from testflow.storage.repositories import reports as reports_repo
from testflow.storage.repositories import runs as runs_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = ["router"]

logger = get_logger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])

#: SSE 的轮询间隔与心跳间隔（秒）。
#:
#: M1 的 SSE 是**短间隔轮询数据库**，不是真推送——见 `stream_events` 的说明。
_POLL_INTERVAL_S = 1.0
_HEARTBEAT_EVERY_TICKS = 15


@router.post(
    "",
    response_model=RunCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_user_token)],
    summary="发起一次测试运行",
)
async def create_run(
    payload: CreateRunRequest, request: Request
) -> RunCreatedResponse | JSONResponse:
    """创建 Run 并投递推进指令。

    **503 而不是静默成功**：队列不可用时返回 503（`05` §4.2）。
    返回 202 而实际什么都没投递，会让用户等一个永远不会开始的 Run——
    那比"明确失败"糟得多。
    """
    context: GatewayContext = request.app.state.ctx
    try:
        spec_id = uuid.UUID(payload.spec_id)
    except ValueError:
        return problem_response(
            ProblemDetail(
                type="invalid_request",
                title="spec_id 不是合法的 UUID",
                status=422,
                code="invalid_request",
            )
        )

    session = context.session()
    try:
        spec = specs_repo.get(session, spec_id)
        if spec is None:
            return problem_response(
                ProblemDetail(
                    type="spec_not_found", title="Spec 不存在", status=404, code="spec_not_found"
                )
            )
        run = runs_repo.create(session, spec_id=spec.id, spec_fingerprint=spec.fingerprint)
        run_id, phase = run.id, run.phase
        session.commit()
    finally:
        session.close()

    try:
        context.require_producer().orchestrate(run_id, "run_created")
    except Exception as exc:  # noqa: BLE001 —— 队列不可用是运行时状态，不是编程错误
        logger.error("投递推进指令失败 run=%s：%s", run_id, exc)
        return problem_response(
            ProblemDetail(
                type="orchestrator_unavailable",
                title="编排器暂时不可用",
                status=503,
                code="orchestrator_unavailable",
                detail="运行已创建但未能投递到队列。请稍后重试，或检查 Redis 是否可达。",
            )
        )

    return RunCreatedResponse(run_id=str(run_id), phase=str(phase))


@router.get(
    "/{run_id}",
    response_model=RunStatusResponse,
    dependencies=[Depends(require_user_token)],
    summary="查询运行状态",
)
async def get_run(run_id: str, request: Request) -> Any:
    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()
        return RunStatusResponse(
            run_id=str(run.id),
            phase=run.phase,
            spec_fingerprint=run.spec_fingerprint,
            expected_case_count=run.expected_case_count,
            settled_case_count=run.settled_case_count,
            failure_reason=run.failure_reason,
            # `interrupt` **只在中断状态下出现**（`05` §4.3）。
            # 前端据此决定要不要显示确认界面，而不是去猜 `phase` 字符串的语义。
            interrupt={"kind": "review"} if run.phase == RunPhase.AWAITING_REVIEW else None,
        )
    finally:
        session.close()


@router.get(
    "/{run_id}/report",
    dependencies=[Depends(require_user_token)],
    summary="取报告（JSON 或 Markdown）",
    # `response_model=None`：这个端点按 `Accept` 返回两种内容类型，
    # 而 FastAPI 不接受 `JSONResponse | PlainTextResponse` 作为响应模型。
    # 两种形态在 `responses` 里显式声明——**契约不能因为注解写不了就消失**。
    response_model=None,
    responses={
        200: {
            "description": "报告内容。`application/json` 返回结构化数据，"
            "`text/markdown` 返回写入时的渲染快照（RA4）。",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ReportResponse"}},
                "text/markdown": {"schema": {"type": "string"}},
            },
        },
        404: {"description": "报告尚未生成（`problem+json`，`code=report_not_ready`）"},
    },
)
async def get_report(run_id: str, request: Request) -> Any:
    """按 `Accept` 返回 JSON 或 Markdown。

    Markdown 返回的是**写入时的快照**，不是重新渲染（RA4）：
    报告会被引用（评审材料、问题工单），三个月前那份应当保持原样——
    "当时看到的"和"现在查到的"必须一致。
    """
    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()

        report = reports_repo.get_by_run(session, run.id)
        if report is None:
            # 404 而不是空报告：**"还没生成"与"零用例"含义完全不同**，
            # 用同一个响应表达会让用户以为测试跑完了。
            return problem_response(
                ProblemDetail(
                    type="report_not_ready",
                    title="报告尚未生成",
                    status=404,
                    code="report_not_ready",
                    detail=f"当前阶段：{run.phase}",
                )
            )

        accepts = (request.headers.get("accept") or "").lower()
        if "text/markdown" in accepts:
            return PlainTextResponse(report.rendered or "", media_type="text/markdown")
        return JSONResponse(
            content=ReportResponse(run_id=str(run.id), content=report.content).model_dump()
        )
    finally:
        session.close()


@router.get(
    "/{run_id}/events",
    dependencies=[Depends(require_user_token)],
    summary="运行事件流（SSE）",
    response_model=None,
    responses={
        200: {
            "description": "SSE 事件流。事件里**只有信号，没有数据**（API7）："
            "`run.phase` / `run.completed` / `run.failed` / 心跳注释。",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_events(run_id: str, request: Request) -> Any:
    """SSE 事件流。**只传信号，不传数据**（API7）。

    设计上的一条硬约束：事件里只有"阶段变了"这种信号，**没有用例内容、
    没有响应体**。两条理由：

    * 数据走两条路会立刻产生"哪个是权威"的问题——而 REST 是权威
    * SSE 连接可能很长，把数据塞进去意味着它会被缓存在中间层
      （代理、浏览器），而那些缓存不受我们的留存策略管辖

    ## ⚠️ M1 的实现方式：**短间隔轮询数据库**

    真正的"事件推送"要求 Gateway 知道编排器/Worker 侧的进展，而那需要一条
    跨进程的事件总线。M1 没有它——所以这里按固定间隔查 `runs` 表，
    阶段变了就发一条事件。

    **这是一个偏差，要记录在案**：延迟是轮询间隔的量级（秒），
    而真推送是毫秒。代价换来的是不引入一套总线——Redis pub/sub 会因为
    断线丢消息，而"REST 是权威"这条退路让我们**丢了消息也不影响正确性**，
    只是晚一点收到通知。
    """
    context: GatewayContext = request.app.state.ctx

    async def _events():
        last_phase: str | None = None
        ticks = 0
        while True:
            if await request.is_disconnected():
                return
            session = context.session()
            try:
                run = _find_run(session, run_id)
                if run is None:
                    return
                phase = run.phase
            finally:
                session.close()

            if phase != last_phase:
                last_phase = phase
                yield f"event: run.phase\ndata: {json.dumps({'phase': phase})}\n\n"
                if RunPhase(phase).is_terminal:
                    name = "run.completed" if phase == RunPhase.DONE else "run.failed"
                    yield f"event: {name}\ndata: {json.dumps({'phase': phase})}\n\n"
                    return

            # 心跳：让中间的代理不要因为"太久没数据"而掐断连接。
            # 没有它，一个跑十分钟的 Run 的事件流会在中途静默断开，
            # 而前端看起来只是"没有新事件"。
            ticks += 1
            if ticks % _HEARTBEAT_EVERY_TICKS == 0:
                yield ": heartbeat\n\n"

            await asyncio.sleep(_POLL_INTERVAL_S)

    return StreamingResponse(_events(), media_type="text/event-stream")


# ---------------------------------------------------------------- 助手


def _find_run(session, run_id: str):
    """按 id 取 Run。**非法 UUID 与"不存在"返回同一个结果**（`None`）。

    区分它们等于告诉调用者"这个 id 格式对但不存在"——
    对一个不该存在的东西，这种区分没有价值，只增加了探测面。
    """
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        return None
    return runs_repo.get(session, parsed)


def _not_found() -> JSONResponse:
    return problem_response(
        ProblemDetail(type="run_not_found", title="运行不存在", status=404, code="run_not_found")
    )
