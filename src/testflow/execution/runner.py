"""执行一条用例。**Worker 侧的全部工作。**

对应决策：`03-模块详细设计/TestExecutor.md` §2（执行流程）、TE1/TE2/TE4/TE5、
`02` D4（结果先落库，再推进 Checkpoint）、`06-Agent编排.md` §6.1（回调唤醒）。

## 执行流程

```
① 从 PG 读 case / operation / spec        ← 任务载荷里只有一个 id（TE1）
        ↓
② 组装请求（request_builder）
        ↓
③ 注入凭证（credentials，按 host）
        ↓
④ 经 Guardrail 发出（唯一出站通道）
        ↓
⑤ 求值断言（三态）
        ↓
⑥ 落库（ON CONFLICT DO UPDATE，幂等）
        ↓
⑦ 回调唤醒
```

## 为什么这是同步代码（ADR-0001）

Worker 用 Celery prefork 池——一个进程跑一个任务。同步代码在这里更简单、
更好调试，崩溃隔离也更干净。异步只用在真正需要高并发的两个地方
（Gateway 与编排层）。

## D4：先落库，再回调

**顺序不能反。** 回调只是"去叫醒编排层"，而编排层醒来之后**从 PG 读结果**
（`06` §6.2）。若先回调再落库，编排层可能在结果还没写进去时就完成判定，
看到"没完成"而继续等——直到兜底轮询才发现。反过来，落库成功但回调丢了，
只是晚一点推进（兜底轮询会收尾）。**两种失败模式的代价完全不同。**
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from testflow.config import Settings
from testflow.domain.assertions import Assertion, AssertionOutcome, AssertionResult
from testflow.execution.assertions import (
    CaseStatus,
    RequestOutcome,
    ResponseContext,
    derive_case_status,
    evaluate,
)
from testflow.execution.credentials import CredentialMaterial, apply_credential
from testflow.execution.request_builder import build_request, pick_base_url
from testflow.guardrail.audit import request_digest
from testflow.guardrail.errors import (
    GuardrailDenied,
    GuardrailTimeout,
    GuardrailTransportError,
)
from testflow.guardrail.pipeline import EgressPolicy, RequestContext
from testflow.guardrail.sync_client import GuardrailResponse, GuardrailSyncClient
from testflow.observability.logging import get_logger
from testflow.storage.models import Case, Operation, Spec
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import results as results_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = ["CaseOutcome", "WakeCallback", "execute_case", "persist_outcome", "run_case"]

logger = get_logger(__name__)

#: 唤醒回调：告诉 Gateway"这条用例完成了"。**幂等**——重复调用无害
#: （`06` §6.2 的完成判定查的是 PG，不靠"最后一个任务自我识别"）。
WakeCallback = Callable[[uuid.UUID], None]


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """一条用例的完整执行结果。对应 `case_results` 的一行。"""

    status: CaseStatus
    assertion_outcomes: list[AssertionResult] = field(default_factory=list)
    http_status: int | None = None
    response_headers: dict[str, str] | None = None
    response_body: str | None = None
    response_body_truncated: bool = False
    response_body_bytes: int | None = None
    elapsed_ms: int | None = None
    error_kind: str | None = None
    request_digest_hex: str | None = None


@dataclass(frozen=True, slots=True)
class CaseSpec:
    """执行一条用例所需的全部输入。

    刻意不用 ORM 对象：这样 `execute_case` 是纯粹的"给定输入 → 给定输出"，
    单元测试不必构造五张表的关联数据。数据库读取被压在 `run_case` 里。
    """

    method: str
    path: str
    base_url: str
    assertions: tuple[Assertion, ...]
    query: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    body: Any | None = None
    requires_auth: bool = True
    responses_schema: dict[str, Any] | None = None


def execute_case(
    case: CaseSpec,
    *,
    client: GuardrailSyncClient,
    policy: EgressPolicy,
    credential: CredentialMaterial | None = None,
    run_id: uuid.UUID | None = None,
) -> CaseOutcome:
    """跑一条用例，返回结果。**不碰数据库。**

    Args:
        credential: 该用例目标 host 的凭证。`None` 表示没有可用凭证——
            此时若 `case.requires_auth` 为真，我们会**照常发送**（不加认证头），
            而不是跳过执行。理由是那个 401 是有效信息：它告诉用户
            "这个接口需要凭证，而你还没配"。静默跳过会让报告显示一个
            不存在的"通过"。
    """
    request = build_request(
        method=case.method,
        path=case.path,
        base_url=case.base_url,
        query=case.query,
        headers=case.headers,
        body=case.body,
    )

    if case.requires_auth and credential is not None:
        request = apply_credential(request, credential)

    # ★ `run_id` 必须带上。
    #
    # `03/Guardrail.md` GR6 说审计要回答的是**"这个 Run 访问了哪些 host"**——
    # 而第一版这里没传，于是审计记录的 `run_id` 全是 NULL。
    # 表里有数据、字段也在，只是**关联断了**：按 Run 查一条都查不到。
    #
    # 这类"数据写了但关联丢了"的 bug 不会报错，只会让审计在最需要它的时候
    # （复盘某一次运行）变得没用。
    ctx = RequestContext(url=request.url, method=request.method, direction="target", run_id=run_id)
    digest = request_digest(request.method, request.url, request.body)

    try:
        result = client.send(ctx, policy, content=request.body)
    except GuardrailTimeout:
        # 超时是**测试结果**，不是系统故障（TE7），所以它不重试、
        # 也不用 `error` 表示——两者的归因完全不同。
        return CaseOutcome(status="timeout", error_kind="timeout", request_digest_hex=digest)
    except GuardrailTransportError as exc:
        return CaseOutcome(
            status="error",
            error_kind=exc.error_kind,
            request_digest_hex=digest,
        )

    if isinstance(result, GuardrailDenied):
        # §8.1：被策略拦下 → `error` + `guardrail_denied`。
        # **`http_status` 保持 `None`**——请求根本没发出去，
        # 填一个状态码会让报告里出现一个不存在的响应。
        #
        # M3 的规则归因据此判成 `blocked_by_policy`，而不是"服务端错误"。
        return CaseOutcome(
            status="error",
            error_kind=result.error_kind,
            request_digest_hex=digest,
        )

    return _evaluate_response(result, case, digest)


def _evaluate_response(response: GuardrailResponse, case: CaseSpec, digest: str) -> CaseOutcome:
    context = ResponseContext(
        status_code=response.status_code,
        headers=response.headers,
        body=response.body,
        elapsed_ms=response.elapsed_ms,
        truncated=response.truncated,
        responses_schema=case.responses_schema,
    )
    outcomes = evaluate(list(case.assertions), context)
    status = derive_case_status(RequestOutcome(kind="completed"), outcomes)

    return CaseOutcome(
        status=status,
        assertion_outcomes=outcomes,
        http_status=response.status_code,
        # 响应头里可能含 `set-cookie` 等敏感值。落库的是**响应**头，
        # 不是请求头——请求头里的凭证我们自己不存（TE4），
        # 而响应头是被测系统给的，它是测试结论的一部分。
        response_headers=dict(response.headers),
        response_body=response.text,
        response_body_truncated=response.truncated,
        response_body_bytes=response.body_bytes,
        elapsed_ms=response.elapsed_ms,
        request_digest_hex=digest,
    )


# ═══════════════════════════════════════════════════════ 与数据库相关


def parse_assertions(raw: object) -> tuple[Assertion, ...]:
    """把 `cases.assertions` 里的 JSONB **还原成 Pydantic 模型**。

    ⚠️ **这一步不能省。** 数据库里存的是 JSON（JSONB 列），读出来是
    `list[dict]`——而求值引擎按类型分派（`isinstance(x, JsonPathAssertion)`），
    拿到 dict 会走进最后的 `raise AssertionError("未处理的断言类型")`。

    这个缺口**单元测试发现不了**：那里测试直接构造 Pydantic 模型，
    类型天然正确。只有真正从库里读一次的集成测试才会撞上它。

    Raises:
        ValueError: 断言结构不合法。**硬失败**是有意的——一条读不出来的
            断言意味着这条用例的结果无法解释，静默跳过它会得到一个
            "通过"的假象（断言数为零时用例级判定是 `failed`，
            但那个 `failed` 的原因会指向一个不存在的问题）。
    """
    try:
        return tuple(TypeAdapter(list[Assertion]).validate_python(raw or []))
    except ValidationError as exc:
        raise ValueError(f"用例的断言结构不合法：{exc}") from exc


def load_case_spec(session: Session, case: Case, operation: Operation, spec: Spec) -> CaseSpec:
    """从 ORM 对象构造 `CaseSpec`。

    基地址取自 Spec 的 `declared_targets`——见 `pick_base_url` 里关于
    "M1 取第一个"这个限制的说明。
    """
    base_url = pick_base_url(list(spec.declared_targets))
    if base_url is None:
        raise ValueError(
            "这份 Spec 没有声明任何出站目标（servers）。"
            "相关请求会被 Guardrail 拒绝——见 `05` §4.1 的 warnings 字段。"
        )

    return CaseSpec(
        method=case.request_method,
        path=case.request_path,
        base_url=base_url,
        assertions=parse_assertions(case.assertions),
        query=case.request_query,
        headers=case.request_headers,
        body=case.request_body,
        requires_auth=case.requires_auth,
        responses_schema=operation.responses_schema,
    )


def persist_outcome(
    session: Session, *, run_id: uuid.UUID, case_id: uuid.UUID, outcome: CaseOutcome
) -> None:
    """把结果写进 `case_results`。**幂等**（TE5）。

    用 `ON CONFLICT DO UPDATE` 而不是"先查后写"：消息队列会重发、
    Worker 会被强杀后重投。天真的"先查有没有再插入"在并发下必然有竞态
    （两个进程都查到"没有"，然后都插入）。

    `assertion_outcomes` 序列化成纯 dict 列表——直接塞 Pydantic 对象
    会让 SQLAlchemy 的 JSONB 序列化依赖 `pydantic` 的 `default=` 钩子，
    而那是一个不在本模块控制范围内的隐式行为。
    """
    results_repo.upsert_many(
        session,
        [
            {
                "run_id": run_id,
                "case_id": case_id,
                "status": outcome.status,
                "http_status": outcome.http_status,
                "response_headers": outcome.response_headers,
                "response_body": outcome.response_body,
                "response_body_truncated": outcome.response_body_truncated,
                "response_body_bytes": outcome.response_body_bytes,
                "elapsed_ms": outcome.elapsed_ms,
                "error_kind": outcome.error_kind,
                "assertion_outcomes": [_outcome_to_json(o) for o in outcome.assertion_outcomes],
                "request_digest": outcome.request_digest_hex,
            }
        ],
    )


def _outcome_to_json(result: AssertionResult) -> dict[str, Any]:
    """断言结果 → 可落库的 JSON。

    `outcome` 是 `StrEnum`，显式取 `.value` 是刻意的：`(str, Enum)` 那种
    写法会序列化出 `"AssertionOutcome.PASSED"`，而它**看起来像是对的**——
    落库之后要等到有人按 `outcome == "passed"` 去查才会发现对不上。
    """
    return {
        "index": result.index,
        "type": result.type,
        "outcome": result.outcome.value
        if isinstance(result.outcome, AssertionOutcome)
        else result.outcome,
        "expected": result.expected,
        "actual": result.actual,
        "reason": result.reason,
    }


def run_case(
    session: Session,
    *,
    run_id: uuid.UUID,
    case_id: uuid.UUID,
    client: GuardrailSyncClient,
    policy: EgressPolicy,
    settings: Settings,
    load_credential: Callable[[str], CredentialMaterial | None] | None = None,
    wake: WakeCallback | None = None,
) -> CaseOutcome:
    """Worker 侧的完整流程：读 → 执行 → 落库 → 回调。

    **调用方负责事务边界。** 本函数只 flush，不 commit——`commit` 是一个
    跨越"数据库"与"消息队列"两个系统的决定（先落库再回调，见 D4），
    把它藏在执行函数里会让那个顺序无处可见。

    Args:
        load_credential: 按 host 取凭证。做成回调而不是直接 import 仓储，
            是为了让凭证来源可替换（测试里返回一个固定的 `CredentialMaterial`
            就够了）。`None` 表示"这个 Run 没用凭证"。
        wake: 唤醒回调。**在落库之后调用**——顺序不能反（见模块文档字符串）。
            传 `None` 表示不回调，此时只能靠兜底轮询收尾
            （那正是 M1 的专项检查 a 要验证的路径）。

    Returns:
        执行结果。**调用方拿它做断言或日志**——返回值本身已经落库了。

    Raises:
        ValueError: 数据不完整（用例/接口/规格缺失，或 Spec 没有声明出站目标）。
            这类错误意味着数据被破坏或被绕过流程写入，不该静默跳过。
    """
    case = cases_repo.get(session, case_id)
    if case is None:
        raise ValueError(f"用例不存在：{case_id}")

    operation = session.get(Operation, case.operation_id)
    if operation is None:
        raise ValueError(f"用例 {case_id} 指向的接口不存在：{case.operation_id}")

    # Spec 从**接口**上取：`cases` 表只有 `operation_id`，没有 `spec_id`
    # （`04` §2.5）。多绕一层是有意的——让"这条用例属于哪份文档"
    # 只有一个来源，而不是在两张表上各存一份、迟早不一致。
    spec = specs_repo.get(session, operation.spec_id)
    if spec is None:
        raise ValueError(f"接口 {operation.id} 指向的 Spec 不存在")

    case_spec = load_case_spec(session, case, operation, spec)

    credential: CredentialMaterial | None = None
    if case_spec.requires_auth and load_credential is not None:
        credential = load_credential(_host_of(case_spec.base_url))

    outcome = execute_case(
        case_spec, client=client, policy=policy, credential=credential, run_id=run_id
    )

    outcome = _apply_truncation(outcome, settings)

    persist_outcome(session, run_id=run_id, case_id=case_id, outcome=outcome)
    session.flush()

    logger.info(
        "用例执行完成 run=%s case=%s status=%s http=%s",
        run_id,
        case_id,
        outcome.status,
        outcome.http_status,
    )

    # ★ D4：**落库之后**才回调。反过来的话，编排层可能在结果写进去之前
    # 就完成判定，看到"还没完成"而继续等待——直到兜底轮询才发现。
    if wake is not None:
        wake(run_id)

    return outcome


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def _apply_truncation(outcome: CaseOutcome, settings: Settings) -> CaseOutcome:
    """按配置的上限再截断一次响应体。

    `GuardrailSyncClient` 已经按 `response_body_max_bytes` 读取
    （只读到上限就停，不把 10GB 拉进内存）。这里处理的是**已经拿到**的
    结果：若某个调用方绕过了客户端自带的截断，这里保证落库的内容
    仍在上限之内——数据库列是 TEXT，而"存一个 10GB 的响应体"会先打爆磁盘。

    重复截断是幂等的，所以两道都留着比只留一道安全。
    """
    if outcome.response_body is None:
        return outcome

    limit = settings.response_body_max_bytes
    encoded = outcome.response_body.encode("utf-8")
    if len(encoded) <= limit:
        return outcome

    return dataclasses.replace(
        outcome,
        response_body=encoded[:limit].decode("utf-8", errors="ignore"),
        response_body_truncated=True,
        response_body_bytes=outcome.response_body_bytes or len(encoded),
    )
