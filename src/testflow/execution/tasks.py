"""Celery 任务：执行一条用例。**Worker 进程的入口。**

对应决策：`03-模块详细设计/TestExecutor.md` §8（任务设计）、TE1（载荷只带 ID）、
TE7（三层超时）、`02` §1.3（队列拓扑）。

## 载荷只有两个 ID（TE1）

    testflow.exec_case(run_id, case_id)

**没有请求体、没有断言、没有任何业务数据。** 三个理由：

1. **消息大小**——2000 条用例的完整定义会把 broker 撑爆，而 broker 是内存队列
2. **数据陈旧**——从投递到执行之间可能隔着一次人工确认（IP-1），
   那段时间里用例可能被改过
3. **重试正确性**——重投的任务应当读到**最新**状态，而不是它出生时的快照

代价是每个任务多一次数据库往返。在一个本来就要写数据库的任务里，
那次往返可以忽略。

## 进程级资源用惰性单例

Worker 是**长驻进程**，连接池与出站客户端的构造代价要付一次而不是每次任务。
所以这里用 `_context()` 缓存它们。

**但不用模块级 `= build(...)`**：那会在 import 时读配置、建连接池，
而 `celery` 会 import 任务模块来发现任务——于是"只是想看看有哪些任务"
就会触发一次数据库连接。惰性初始化把这件事推到真正执行的那一刻。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from celery import shared_task
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from testflow.celery_app import EXEC_QUEUE
from testflow.config import Settings, load_settings
from testflow.execution.credentials import CredentialError, CredentialMaterial, decrypt_value
from testflow.execution.runner import WakeCallback, run_case
from testflow.guardrail.audit import AuditRecord
from testflow.guardrail.pipeline import EgressPolicy
from testflow.guardrail.policy import Whitelist
from testflow.guardrail.sync_client import GuardrailSyncClient
from testflow.observability.logging import get_logger
from testflow.observability.tracing import bind_trace_id
from testflow.storage.db import create_db_engine, create_session_factory
from testflow.storage.repositories import audit as audit_repo
from testflow.storage.repositories import credentials as credentials_repo
from testflow.storage.repositories import runs as runs_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = ["exec_case", "reset_context", "set_wake_callback"]

logger = get_logger(__name__)


@dataclass
class _WorkerContext:
    """Worker 进程级的资源。**每个进程一份**（prefork 之后各自持有自己的）。"""

    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    guardrail: GuardrailSyncClient


_context: _WorkerContext | None = None


def _build_context() -> _WorkerContext:
    settings = load_settings()
    engine = create_db_engine(settings)
    sessions = create_session_factory(engine)
    return _WorkerContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        # ★ 必须传 `audit_sink`。
        #
        # `03/Guardrail.md` GR6 要求 **`allow` 与 `deny` 都要记**。
        # 而第一版这里没传——于是审计表一条记录都没有，而**没有任何东西会报错**：
        # 请求照常发出、测试照常通过，只是"这个 Run 访问了哪些 host"
        # 这个问题永远答不上来。
        #
        # 发现它靠的是**跑完 demo 之后去查那张表**——那一步不做，
        # 这个洞会一直留着。写完一个"记录审计"的功能，要去看记录有没有真的产生。
        guardrail=GuardrailSyncClient(settings, audit_sink=_audit_sink(sessions)),
    )


def _audit_sink(sessions: sessionmaker[Session]) -> Callable[[AuditRecord], None]:
    """把审计记录写进库。**用独立的短事务。**

    不复用执行任务的事务：审计要回答的是"我们放没放行这次请求"，
    而那件事在请求发出的那一刻就已经确定了。把它绑进任务事务的话，
    任务因为任何原因回滚，**连同那条日志一起消失**——
    而"某次出站曾经发生过"不依赖任务成功与否。
    """

    def _write(record: AuditRecord) -> None:
        try:
            with sessions() as session:
                audit_repo.record(
                    session,
                    direction=record.direction,
                    target_host=record.target_host,
                    target_url=record.target_url,
                    decision=record.decision,
                    deny_reason=record.deny_reason,
                    run_id=record.run_id,
                    request_digest=record.request_digest,
                    trace_id=record.trace_id,
                )
                session.commit()
        except Exception:  # noqa: BLE001
            # **审计写失败不能带崩执行。** 一次业务请求的成功与否不该取决于
            # 日志存储的可用性——但必须留下痕迹，否则"审计静默失效"这件事
            # 会以"表一直是空的"这种最难注意到的形式存在。
            logger.exception(
                "审计写入失败 host=%s decision=%s", record.target_host, record.decision
            )

    return _write


def _get_context() -> _WorkerContext:
    global _context
    if _context is None:
        _context = _build_context()
    return _context


def reset_context() -> None:
    """丢弃缓存并关闭资源。**供测试使用。**

    测试之间共享一个连接池会让"上一个测试留下的连接"影响下一个——
    而那类失败只在特定顺序下出现，极难排查。
    """
    global _context
    if _context is not None:
        _context.guardrail.close()
        _context.engine.dispose()
        _context = None


@shared_task(
    name="testflow.exec_case",
    queue=EXEC_QUEUE,
    # `max_retries=0`（TE7）：超时与连接失败**是测试结果，不是系统故障**。
    # 重试它们会让报告的耗时与结论都失真——而"超时"本身就是用户想知道的信息。
    #
    # 真正需要重试的只有"Worker 被强杀"那种情况，而那是靠 `acks_late`
    # 重投、不是靠 Celery 的 retry 机制。
    max_retries=0,
)
def exec_case(run_id: str, case_id: str) -> dict[str, Any]:
    """执行一条用例。**任务载荷只有这两个 ID**（TE1）。

    Returns:
        一个小的结果摘要。**只用于日志与调试**——权威结果在 `case_results` 里
        （`task_ignore_result=True`，Celery 不存它）。
    """
    context = _get_context()
    run_uuid = uuid.UUID(run_id)
    case_uuid = uuid.UUID(case_id)

    # 绑定 trace_id 让这个任务的全部日志能串起来。Celery 的进程是复用的，
    # 所以**必须**在这里设一个新的——否则上一个任务的 trace_id 会粘过来。
    bind_trace_id()

    policy = _policy_for(context, run_uuid)

    session = context.sessions()
    try:
        outcome = run_case(
            session,
            run_id=run_uuid,
            case_id=case_uuid,
            client=context.guardrail,
            policy=policy,
            settings=context.settings,
            load_credential=lambda host: _load_credential(session, context.settings, host),
            wake=_wake,
        )
        # **先落库再回调**（D4）。`run_case` 内部已经 flush 过回调，
        # 这里 commit 把整条用例的结果固化下来。
        session.commit()
    except Exception:
        session.rollback()
        # 不吞异常：`acks_late=True` 下，异常会让任务以失败告终，
        # 而"任务失败了"是可观测的——静默吞掉只会让用例永久悬空，
        # 直到兜底轮询把它标成超时。
        logger.exception("用例执行失败 run=%s case=%s", run_id, case_id)
        raise
    finally:
        session.close()

    return {"run_id": run_id, "case_id": case_id, "status": outcome.status}


def _policy_for(context: _WorkerContext, run_id: uuid.UUID) -> EgressPolicy:
    """按该 Run 的 Spec 构造出站授权。

    **白名单从 Spec 派生，内网例外只从配置读**（ADR-0018）。
    这个区分很关键：Spec 是用户提供的、不可信的，而配置是我们自己写的。
    把例外也做成从 Spec 派生，就等于"用户在文档里写了什么地址，就自动获得
    访问它的授权"——SR-2 会退化成"检查文档有没有写明自己要访问哪里"。
    """
    with context.sessions() as session:
        run = runs_repo.get(session, run_id)
        if run is None:
            raise ValueError(f"Run 不存在：{run_id}")
        spec = specs_repo.get(session, run.spec_id)
        if spec is None:
            raise ValueError(f"Run {run_id} 指向的 Spec 不存在")

        return EgressPolicy(
            whitelist=Whitelist.from_urls(list(spec.declared_targets)),
            # 例外**只来自配置**。它是一份显式的名单，永不从 Spec 派生。
            allowed_private=frozenset(context.settings.guardrail_allow_private_targets),
        )


def _load_credential(session: Session, settings: Settings, host: str) -> CredentialMaterial | None:
    """按 host 取凭证并解密（TE4）。

    没有配置凭证时返回 `None`——执行器**照常发送**（不加认证头）。
    那个 401 是有效信息：它告诉用户"这个接口需要凭证，而你还没配"。
    静默跳过执行会让报告显示一个不存在的"通过"。
    """
    row = credentials_repo.get_by_host(session, host)
    if row is None:
        return None

    try:
        secret = decrypt_value(row.encrypted_value, settings.credential_encryption_key)
    except CredentialError:
        # 解密失败是**配置问题**，不是被测系统的问题。记日志、返回 None，
        # 让请求以无凭证的形式发出——结果会是 401，而归因会指向 `auth_error`。
        # **不把异常详情往上抛**：它可能被 Worker 的错误处理写进日志，
        # 而错误消息是凭证最容易泄露的地方。
        logger.error("凭证解密失败 host=%s name=%s（已刻意不输出内容）", host, row.name)
        return None

    return CredentialMaterial(auth_type=row.auth_type, secret=secret, key_version=row.key_version)


def _wake(run_id: uuid.UUID) -> None:
    """唤醒回调。**由入口点注入**（见 `set_wake_callback`）。

    做成注册而不是在这里直接发 HTTP，是为了守住一条架构边界：

    * 唤醒请求打的是**我们自己的 Gateway**，它需要一个 HTTP 客户端
    * 而 `execution/` 里不许出现任何 HTTP 客户端
      （架构测试强制——那正是"Guardrail 是唯一出站通道"的落点）

    两者看似矛盾，其实不是：架构约束管的是**代替用户发出的**出站请求。
    进程间的控制面通信是另一回事，而它应当由**进程入口**去接线——
    入口点是唯一知道"我跑在哪个 Gateway 后面"的地方。

    于是依赖方向变成：`entrypoints/worker` → 注册回调 → `execution/tasks` 使用它。
    `execution/` 依然不认识任何 HTTP 库。

    回调为 `None` 时**什么都不做**——此时只能靠兜底轮询收尾。
    那条路径必须能独立工作（M1 专项检查 a 就验证它），
    因为回调可能全部丢失。
    """
    if _wake_callback is None:
        logger.debug("未注册唤醒回调，本任务的结果将依赖兜底轮询收尾 run=%s", run_id)
        return
    _wake_callback(run_id)


_wake_callback: WakeCallback | None = None


def set_wake_callback(callback: WakeCallback | None) -> None:
    """注册唤醒回调。**由 Worker 入口点在启动时调用一次。**"""
    global _wake_callback
    _wake_callback = callback
