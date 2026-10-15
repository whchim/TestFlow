"""`/api/specs` —— 提交 OpenAPI 文档。

对应决策：`05-API契约.md` §4.1、`03/SpecParser.md` SP4/SP5、DM2（指纹去重）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from testflow.agents.spec_parser import SpecParseError, parse_spec
from testflow.gateway.auth import require_user_token
from testflow.gateway.deps import GatewayContext
from testflow.gateway.problems import ProblemDetail, problem_response
from testflow.gateway.schemas import SpecCreatedResponse, WarningItem
from testflow.observability.logging import get_logger
from testflow.storage.models import Operation, Spec
from testflow.storage.repositories import operations as operations_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = ["router"]

logger = get_logger(__name__)
router = APIRouter(prefix="/api/specs", tags=["specs"])

#: 上传大小上限（`07` NF4）。在**读之前**拦，而不是读完了再检查——
#: 后者已经把那份内容拉进内存了，而攻击者可以发一个 10GB 的文档。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _extract_content(payload: object) -> tuple[str, str | None]:
    """从请求体里取出文档原文与文件名。

    接受两种形态（`05` §4.1）：`multipart/form-data` 的文件上传，
    或 `application/json` 的粘贴内容。两者最终都归一到"一段文本"。

    **不在这里做大小校验**——那要在读之前做，见 `upload_spec`。
    """
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str):
            return content, payload.get("filename")
    raise ValueError("请求体必须包含 `content` 字段（文档原文）")


@router.post(
    "",
    response_model=SpecCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_user_token)],
    summary="提交 OpenAPI 文档",
)
async def upload_spec(request: Request) -> SpecCreatedResponse | object:
    """解析并保存一份 OpenAPI 文档。

    **幂等（DM2）**：同一份内容重复提交不会重复解析——指纹命中已有 Spec 时
    直接复用（`reused=true`），不重新落 `operations`。

    **硬失败 vs 降级（SP4）**：语法错误、不合规、悬空 `$ref` 返回 400；
    而"信息不全"（相对 URL、没有 `servers`）**降级继续**并在 `warnings`
    里说明——用户能修的必须让他修，不能修的尽力而为并如实标注。
    """
    context: GatewayContext = request.app.state.ctx

    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BYTES:
        return problem_response(
            ProblemDetail(
                type="payload_too_large",
                title="文档过大",
                status=413,
                code="payload_too_large",
                detail=f"上限为 {MAX_UPLOAD_BYTES} 字节，实际收到 {len(raw)} 字节。",
            )
        )

    try:
        content, filename = _extract_content(await _decode(request, raw))
    except ValueError as exc:
        return problem_response(
            ProblemDetail(
                type="invalid_request",
                title="请求格式不正确",
                status=422,
                code="invalid_request",
                detail=str(exc),
            )
        )

    try:
        parsed = parse_spec(content)
    except SpecParseError as exc:
        # **文档缺陷是硬失败**（SP4）。降级继续的后果是用户拿到一堆
        # 莫名其妙的 `blocked_by_policy` 归因，而真正的原因
        # （文档里引了不存在的组件）在解析阶段就被掩盖了。
        return problem_response(
            ProblemDetail(
                type="spec_invalid",
                title="文档无法解析",
                status=400,
                code=exc.code,
                detail=exc.detail,
                errors=[{"pointer": "", "message": e} for e in exc.errors],
            )
        )

    session = context.session()
    try:
        existing = specs_repo.get_by_fingerprint(session, parsed.fingerprint)
        if existing is not None:
            # ★ 指纹命中 → **复用**。不重新解析、不重复落 operations。
            # 这是 SP 幂等性的落点，也让"同一个 Run 被唤醒多次"无害。
            operations = operations_repo.list_by_spec(session, existing.id)
            return _response(existing, operations, reused=True, warnings=parsed.warnings)

        spec = specs_repo.insert(
            session,
            Spec(
                fingerprint=parsed.fingerprint,
                filename=filename,
                openapi_version=parsed.openapi_version,
                raw_content=content,
                byte_size=len(content.encode("utf-8")),
                declared_targets=[str(t) for t in parsed.declared_targets],
                operation_count=len(parsed.operations),
            ),
        )
        rows = [
            Operation(
                spec_id=spec.id,
                method=op.method,
                path=op.path,
                operation_id=op.operation_id,
                summary=op.summary,
                parameters_schema=op.parameters_schema,
                request_body_schema=op.request_body_schema,
                responses_schema=op.responses_schema,
                has_unresolved_ref=op.has_unresolved_ref,
                is_destructive=op.is_destructive,
            )
            for op in parsed.operations
        ]
        operations_repo.insert_many(session, rows)
        session.commit()
        # ⚠️ **响应必须在 session 还活着的时候构造。**
        #
        # 第一版把这一句放在 `finally` 之后——那时 session 已经 `close()`，
        # ORM 对象处于 detached 状态。若属性已被 expire（默认的
        # `expire_on_commit=True` 就会），读任何一个字段都会抛
        # `DetachedInstanceError`。
        #
        # 而 `create_session_factory` 恰好设了 `expire_on_commit=False`，
        # 所以**生产环境不会暴露这个问题**——只有测试用别的工厂构造时才炸。
        # 那种"只在测试里出现"的差异最容易被归成"测试写错了"而放过，
        # 但它实际上是代码在依赖一个不属于自己契约的配置项。
        return _response(spec, rows, reused=False, warnings=parsed.warnings)
    finally:
        session.close()


async def _decode(request: Request, raw: bytes) -> dict[str, object]:
    """把请求体解成 dict。支持 JSON 与 multipart 两种形态。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        uploaded = form.get("file") or form.get("content")
        payload: dict[str, object] = {}
        if uploaded is not None and hasattr(uploaded, "read"):
            payload["content"] = (await uploaded.read()).decode("utf-8", errors="replace")
            payload["filename"] = getattr(uploaded, "filename", None)
        else:
            payload["content"] = str(form.get("content") or "")
            payload["filename"] = form.get("filename")
        return payload

    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # YAML 原文直接贴在 body 里的情况——当成"整段内容"处理。
        return {"content": raw.decode("utf-8", errors="replace")}


def _response(
    spec: Spec, operations: list[Operation], *, reused: bool, warnings: list
) -> SpecCreatedResponse:
    return SpecCreatedResponse(
        spec_id=str(spec.id),
        fingerprint=spec.fingerprint,
        openapi_version=spec.openapi_version,
        operation_count=len(operations),
        declared_targets=list(spec.declared_targets),
        reused=reused,
        warnings=[WarningItem(code=w.code, detail=w.detail) for w in warnings],
    )
