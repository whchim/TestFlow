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

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from testflow.domain.graph import RunPhase
from testflow.gateway.auth import require_user_token
from testflow.gateway.deps import GatewayContext
from testflow.gateway.problems import ProblemDetail, problem_response
from testflow.gateway.schemas import (
    AttributionItem,
    AttributionListResponse,
    CaseListItem,
    CaseListResponse,
    CreateRunRequest,
    DecisionApplied,
    DecisionRequest,
    DecisionResponse,
    HumanVerdictRequest,
    ReportResponse,
    RunCreatedResponse,
    RunStatusResponse,
)
from testflow.observability.logging import get_logger
from testflow.storage.repositories import attributions as attributions_repo
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import reports as reports_repo
from testflow.storage.repositories import results as results_repo
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

#: 用例列表的分页默认值与上限。
#:
#: 上限不是"性能调优"而是**契约**：一个 Run 最多 2000 条用例
#: （`max_cases_per_run`），不设上限的话一次请求就能把它们全拉出来
#: ——而确认界面一屏只显示几十条。上限让"一次拉全量"变成请求
#: 方必须显式分页的事，而不是一个会随着数据增长悄悄变慢的默认行为。
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500


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
    "/{run_id}/cases",
    response_model=CaseListResponse,
    dependencies=[Depends(require_user_token)],
    summary="列出该运行的用例（按置信度排序，分页）",
    responses={
        404: {"description": "Run 不存在（`problem+json`，`code=run_not_found`）"},
    },
)
async def get_run_cases(
    run_id: str,
    request: Request,
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> Any:
    """用例列表。**排序是接口语义的一部分，不是实现细节**（`05` §4.3）。

    低置信度在前——理由见 `cases_repo._CONFIDENCE_RANK`：那些用例的期望值
    有一部分是推断出来的，正是"期望写错"这一类问题的所在。

    ⚠️ **前端不该自己重排。** 排序放在服务端是刻意的：分页与排序必须由同一个
    地方决定（否则"第 2 页"的含义取决于客户端按什么排的，而那个契约
    没人写得下来）。PRD F9 也要求前端不重复实现业务规则。

    `limit` 超上限由 FastAPI 直接拒绝（422，走统一的 problem+json 处理器）
    ——而不是静默截断到上限：一个"我请求了 5000 条，拿到 500 条且没有提示"
    的接口，会让调用方以为自己拿全了。
    """
    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()

        total = cases_repo.count_by_run(session, run.id)
        rows = cases_repo.list_page(session, run.id, limit=limit, offset=offset)
        return CaseListResponse(
            run_id=str(run.id),
            total=total,
            limit=limit,
            offset=offset,
            items=[_to_case_item(row) for row in rows],
        )
    finally:
        session.close()


@router.post(
    "/{run_id}/decisions",
    response_model=DecisionResponse,
    dependencies=[Depends(require_user_token)],
    summary="提交人工确认决策（恢复 IP-1）",
    responses={
        404: {"description": "Run 不存在（`problem+json`，`code=run_not_found`）"},
        409: {
            "description": "Run 不在待确认状态，或已无待确认用例"
            "（`problem+json`，`code=run_not_awaiting_review`）"
        },
        422: {
            "description": "决策不完整——有未决用例未出现在请求中"
            "（`problem+json`，`code=decision_incomplete`）"
        },
    },
)
async def submit_decisions(run_id: str, payload: DecisionRequest, request: Request) -> Any:
    """提交批准 / 拒绝，然后把 Run 推离确认门（`05` §4.5）。

    ## 决策先落库，再投递——顺序不能反（ADR-0009）

    `advance()` **不带载荷**：图恢复时只收到"决策已提交"这个信号，
    真正的决策从 PG 读（`apply_decisions` 查 `review_status == 'approved'`）。

    若把决策放进 resume 载荷，就同时有了"载荷里的决策"和"PG 里的
    `review_status`"两个事实来源——恢复时以谁为准又成了新问题，
    而它的表现是"用户批了 40 条，结果跑了另外 40 条"。

    所以 `commit()` **必须在**投递之前：投递只是叫醒推进者，
    它醒来后读的是库。

    ## 为什么 200、以及为什么第二次提交是 409

    提交返回时 `review_status` 已经落库，用户的决定**已经生效**——
    202 的"已受理、尚未处理"反而不准确。

    第二次提交（两个标签页、双击）会命中 409：要么阶段已经离开
    `awaiting_review`，要么待确认集合已经空了（决策已生效、正在恢复）。
    两种情形的共同含义是"**这个 Run 现在不需要你确认了**"，
    所以共用同一个错误码，而 `detail` 把具体情况说清楚。
    """
    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()

        if run.phase != RunPhase.AWAITING_REVIEW:
            return _not_awaiting_review(f"当前阶段：{run.phase}")

        pending = cases_repo.list_by_run(session, run.id, review_status="pending")
        if not pending:
            # 决策已经落库、只是推进者还没取走（resume 是异步的）。
            # 这正是"双击提交"打中的窗口。
            return _not_awaiting_review("该运行已无待确认的用例，决策正在生效")

        requested = _parse_decisions(payload)
        if requested is None:
            return problem_response(
                ProblemDetail(
                    type="invalid_request",
                    title="case_id 不是合法的 UUID",
                    status=422,
                    code="invalid_request",
                )
            )

        pending_ids = {case.id for case in pending}
        if set(requested) != pending_ids:
            missing = len(pending_ids - set(requested))
            unknown = len(set(requested) - pending_ids)
            return problem_response(
                ProblemDetail(
                    type="decision_incomplete",
                    title="决策不完整",
                    status=422,
                    code="decision_incomplete",
                    detail=(
                        f"该运行还有 {missing} 条待确认用例未出现在请求中，"
                        f"另有 {unknown} 条不属于待确认集合。"
                        "必须一次提交全部待确认用例的决策——"
                        "没被提到的用例会停在 pending，既不执行也不出现在报告里。"
                    ),
                )
            )

        approve = [cid for cid, decision in requested.items() if decision == "approve"]
        reject = [cid for cid, decision in requested.items() if decision == "reject"]
        approved = cases_repo.set_review_status(session, approve, "approved")
        rejected = cases_repo.set_review_status(session, reject, "rejected")
        # ★ commit 必须在投递之前：推进者醒来时读的是库，不是这次请求的内存。
        session.commit()
        # 把 id 取成普通值再离开作用域——`session.close()` 之后 `run` 是游离对象，
        # 读它的属性在 `expire_on_commit=False` 下恰好能用，但那依赖一个
        # 别处的配置项。取出来就不依赖了。
        run_uuid = run.id
    finally:
        session.close()

    try:
        context.require_producer().orchestrate(run_uuid, "decisions_submitted")
    except Exception as exc:  # noqa: BLE001 —— 队列不可用是运行时状态，不是编程错误
        # **决策已经生效，所以这不是失败。** 返回 503 会诱导用户重试，
        # 而重试会命中 409——那时他会以为自己的决策丢了。
        # 兜底轮询会在审阅超时前扫到它（`06` §6.4），所以这里只记日志。
        logger.error("决策已落库但投递推进指令失败 run=%s：%s", run_uuid, exc)

    logger.info("人工决策已提交 run=%s 批准=%d 拒绝=%d", run_uuid, approved, rejected)
    return DecisionResponse(
        run_id=str(run_uuid),
        applied=DecisionApplied(approved=approved, rejected=rejected),
    )


@router.get(
    "/{run_id}/report",
    dependencies=[Depends(require_user_token)],
    summary="取报告（JSON 或 Markdown）",
    # ★ 用 `response_model=ReportResponse` 而不是 `None`：
    # 端点按 `Accept` 返回两种内容类型，返回 Markdown 时是 `PlainTextResponse`
    # （FastAPI 对 Response 实例跳过校验）。声明 response_model 的唯一目的是
    # 让 ReportResponse 进入 `components.schemas`——否则手动写在 `responses`
    # 里的 `$ref` 会悬空，而解析器会（正确地）拒绝这种文档。
    response_model=ReportResponse,
    responses={
        200: {
            "description": "报告内容。`application/json` 返回结构化数据，"
            "`text/markdown` 返回写入时的渲染快照（RA4）。",
            # `response_model` 已经生成了 application/json 的 schema；
            # 这里只补第二种内容类型。
            "content": {"text/markdown": {"schema": {"type": "string"}}},
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


# ---------------------------------------------------------------- 归因（M3）


@router.get(
    "/{run_id}/attributions",
    response_model=AttributionListResponse,
    dependencies=[Depends(require_user_token)],
    summary="列出失败归因（分页）",
    responses={404: {"description": "运行不存在（`problem+json`，`code=run_not_found`）"}},
)
async def list_attributions(
    run_id: str,
    request: Request,
    limit: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> Any:
    """列出某个 Run 的归因。分页约定与 `/cases` 一致。

    **一次批量取用例与结果**，不逐条查——一页最多 500 条，
    逐条查就是 1500 次往返（归因 + 用例 + 结果）。
    """
    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()

        rows = attributions_repo.list_page(session, run.id, limit=limit, offset=offset)
        total = attributions_repo.count_by_run(session, run.id)
        cases = {case.id: case for case in cases_repo.list_by_run(session, run.id)}
        results = {row.case_id: row for row in results_repo.list_by_run(session, run.id)}
        return AttributionListResponse(
            run_id=str(run.id),
            total=total,
            limit=limit,
            offset=offset,
            items=[
                _attribution_item(row, cases.get(row.case_id), results.get(row.case_id))
                for row in rows
            ],
        )
    finally:
        session.close()


@router.patch(
    "/{run_id}/attributions/{attribution_id}",
    response_model=AttributionItem,
    dependencies=[Depends(require_user_token)],
    summary="记录人工归因表态",
    responses={
        404: {"description": "运行或归因不存在（`problem+json`）"},
        422: {"description": "表态与类别不匹配（`problem+json`）"},
    },
)
async def submit_human_verdict(
    run_id: str,
    attribution_id: str,
    payload: HumanVerdictRequest,
    request: Request,
) -> Any:
    """记录 `agree` / `disagree`，以及（`disagree` 时**必须**给出的）正确类别。

    ## 为什么 LA6 在路由里校验而不是 Pydantic 里

    在 Pydantic 里加 `model_validator` 也能拦住，但它返回的是 FastAPI 默认
    错误体（`{"detail": ...}`），而契约要求 `problem+json` 且 `code` 可区分
    （`human_category_required` / `human_category_not_allowed`）。
    调用方需要按 `code` 做分支，而不是解析中文句子。

    ## 为什么返回 200 而不是 202

    表态是同步且权威的：返回之前已经提交，人的判断**已经生效**。
    """
    if payload.verdict == "disagree" and payload.human_category is None:
        return problem_response(
            ProblemDetail(
                type="human_category_required",
                title="缺少正确类别",
                status=422,
                code="human_category_required",
                detail="表态为 disagree 时必须给出你认为正确的类别——只记"
                "「不同意」无法回答「该补哪条规则」（LA6）。",
            )
        )
    if payload.verdict == "agree" and payload.human_category is not None:
        return problem_response(
            ProblemDetail(
                type="human_category_not_allowed",
                title="多余的类别",
                status=422,
                code="human_category_not_allowed",
                detail="表态为 agree 时不应再给出另一个类别——那会让"
                "「同意这个归因」与「我认为是另一类」两种含义混在一行里。",
            )
        )

    context: GatewayContext = request.app.state.ctx
    session = context.session()
    try:
        run = _find_run(session, run_id)
        if run is None:
            return _not_found()
        try:
            parsed_id = uuid.UUID(attribution_id)
        except ValueError:
            return _attribution_not_found()
        row = attributions_repo.get_by_id(session, parsed_id)
        # 归属别的 Run 的归因一律当作"不存在"——区分它们等于泄漏
        # "这个 id 存在，只是不属于你"。
        if row is None or row.run_id != run.id:
            return _attribution_not_found()

        updated = attributions_repo.set_human_verdict(
            session,
            parsed_id,
            verdict=payload.verdict,
            human_category=(
                payload.human_category.value if payload.human_category is not None else None
            ),
        )
        session.commit()
        assert updated is not None  # 刚查到的行，不可能在这里消失

        case = cases_repo.get(session, updated.case_id)
        result = results_repo.get(session, updated.run_id, updated.case_id)
        return _attribution_item(updated, case, result)
    finally:
        session.close()


def _attribution_item(row: Any, case: Any, result: Any) -> AttributionItem:
    return AttributionItem(
        attribution_id=str(row.id),
        case_id=str(row.case_id),
        case_name=case.name if case else "(用例已删除)",
        dimension=case.dimension if case else "",
        status=result.status if result else "unknown",
        http_status=result.http_status if result else None,
        category=row.category,
        method=row.method,
        rule_ref=row.rule_ref,
        llm_model=row.llm_model,
        confidence=row.confidence,
        evidence=row.evidence,
        human_verdict=row.human_verdict,
        human_category=row.human_category,
    )


def _attribution_not_found() -> JSONResponse:
    return problem_response(
        ProblemDetail(
            type="attribution_not_found",
            title="归因不存在",
            status=404,
            code="attribution_not_found",
        )
    )


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


def _not_awaiting_review(detail: str) -> JSONResponse:
    """409。两种情形共用：**"这个 Run 现在不需要你确认了"**。

    分两个错误码会让调用方多写一个分支，而他能做的事完全一样
    （刷新状态、别再提交）。差异放在 `detail` 里给人看。
    """
    return problem_response(
        ProblemDetail(
            type="run_not_awaiting_review",
            title="该运行不在待确认状态",
            status=409,
            code="run_not_awaiting_review",
            detail=detail,
        )
    )


def _parse_decisions(payload: DecisionRequest) -> dict[uuid.UUID, str] | None:
    """把请求里的 case_id 转成 UUID。**有一个非法就整批拒绝**（返回 `None`）。

    ## 为什么是"整批拒绝"而不是"跳过非法的"

    跳过的话，那个 case_id 会落进"未出现在请求中"的差额里，于是错误信息说
    "有 1 条待确认用例未提交"——而用户分明提交了它。他会去数自己的请求，
    数出 118 条，然后完全不知道问题在哪。

    返回 `None`（而不是抛异常）与 `GuardrailDenied` 同理：
    **这是对输入的正常判断，不是平台故障。**

    ## 为什么用 dict 而不是 list

    同一个 case_id 出现两次时，dict 让"后写的生效"成为一个确定的行为
    （而不是"两条都写库，`UPDATE ... WHERE id IN (...)` 一次命中两遍"）。
    重复提交同一条决策是用户会做的事（两个标签页），
    而它能有一个明确的语义好过有一个依赖实现细节的语义。
    """
    parsed: dict[uuid.UUID, str] = {}
    for item in payload.decisions:
        try:
            parsed[uuid.UUID(item.case_id)] = item.decision
        except ValueError:
            return None
    return parsed


def _to_case_item(row: Any) -> CaseListItem:
    """`Case` 行 → 接口模型。

    显式逐字段构造，不用 `model_validate(row, from_attributes=True)`：
    后者会把模型上**所有**字段都当候选，于是将来给 `cases` 表加一列
    （例如凭证相关的东西）时，它会**自动出现在接口响应里**——
    而"多暴露了一个字段"不会让任何测试失败。
    """
    return CaseListItem(
        case_id=str(row.id),
        name=row.name,
        category=row.category,
        dimension=row.dimension,
        confidence=row.confidence,
        review_status=row.review_status,
        is_destructive=row.is_destructive,
        request_method=row.request_method,
        request_path=row.request_path,
        assertions=row.assertions or [],
    )
