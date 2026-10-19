"""图的构造与推进。

对应决策：`06-Agent编排.md` §4（主链）、G1（编排层独立进程）、G6（中断机制）、
G8（完成判定）、G9（per-Run 锁）、`02` D1（编排层异步）、D4。

## 推进是**短跳**，不是长驻负载

`advance()` 做的事是：从 Checkpoint 恢复 → 跑到下一个中断点 → 释放。
一次调用通常在毫秒到几秒之间（唯一的例外是 N2 里那几次 LLM 调用）。

这也是为什么编排层用 `--pool=solo`、单副本就够（`10` §2.2）：
它不是在"处理负载"，而是在"推动状态机往前走一步"。

## 为什么 `advance()` 要拿锁

同一个 Run 会被**多个来源同时唤醒**：每条用例完成时回调一次（N 次），
兜底轮询还会调用同一个函数。不串行的话，两个推进者会同时从同一个
Checkpoint 恢复、同时跑同一个节点。

锁**不保证**独占（TTL 会到期），所以每个节点自己也要幂等。见 `locks.py`。
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from testflow.domain.graph import RunPhase
from testflow.graph import nodes as N
from testflow.graph.checkpoint import RedisCheckpointSaver
from testflow.graph.locks import acquire_run_lock
from testflow.graph.routing import (
    after_collect,
    after_decisions,
    after_generate,
    after_parse,
)
from testflow.graph.state import GraphState, initial_state
from testflow.observability.logging import get_logger
from testflow.storage.repositories import runs as runs_repo

__all__ = ["GraphDeps", "advance", "build_graph"]

logger = get_logger(__name__)

#: 挂在编译结果上的属性名——用来把"每次调用会变的 session"传给节点。
#:
#: 为什么用一个动态属性而不是把 session 绑进 `partial`：图**编译一次、
#: 长期复用**，而 session 每次调用都要换新的。绑进 partial 就等于
#: 每次调用都得重新编译整张图。
_HOLDER_ATTR = "_testflow_session_holder"

#: 投递一条用例到 `exec` 队列。由入口点注入（编排层不认识 Celery）。
Dispatch = Callable[[uuid.UUID, uuid.UUID], None]

#: 构造并落库报告。避免 `graph/` 依赖 `agents/report_agent` 的具体实现。
ReportBuilder = Callable[[Session, uuid.UUID], Any]


@dataclasses.dataclass(frozen=True, slots=True)
class GraphDeps:
    """图运行所需的外部依赖。**全部注入，没有一个是 import 来的。**

    理由与 `GuardrailSyncClient` 的审计回调相同：注入让 `graph/` 能在一张
    纯内存的图上被测试（给一个假的 dispatch 就行），而不必起 Celery、起 Redis。
    """

    dispatch: Dispatch
    report_builder: ReportBuilder
    #: 阶段 B 的生成器。`None` 时用纯规则生成器（离线路径）。
    case_generator: Any = None
    #: N6 的归因器。`None` 时 N6 只跑规则（`unknown` 会变多）。
    failure_analyzer: Any = None


def build_graph(checkpointer: RedisCheckpointSaver, deps: GraphDeps) -> Any:
    """构造并编译主链。

    ## 节点函数为什么要 `partial`

    LangGraph 的节点只接收 `(state)`，而我们的节点还需要 `session`、
    `dispatch` 这些注入的依赖。用 `functools.partial` 把后者绑上，
    比让每个节点自己去取（模块级单例 / 上下文变量）更明确：
    **依赖从哪来，在编译这张图的地方一眼能看完。**

    ## 关于 IP-1

    IP-1 是插在 `generate_cases` 与 `apply_decisions` 之间的**独立节点**
    （`review_gate`）——与 IP-2（`wait_for_results`）同理：`interrupt()`
    独占一个节点，被中断的节点在恢复时从头重跑（实测确认），
    所以它不能和 N2/N3 的生成或决策逻辑混在同一个节点里。

    相比 M1，这是一次**图结构变更**：旧 checkpoint 无法在新图上恢复。
    `10` §8 允许停机升级，所以 M1→M2 的升级是一次停机操作。
    """
    session_holder: dict[str, Session] = {}

    graph = StateGraph(GraphState)

    graph.add_node("parse_spec", _with_session(N.parse_spec_node, session_holder))
    graph.add_node(
        "generate_cases",
        _with_session(N.generate_cases, session_holder, generator=deps.case_generator),
    )
    # ★ IP-1：与 IP-2 一样，整个节点体就是一次 `interrupt`，必须是独立节点。
    graph.add_node("review_gate", _with_session(N.review_gate, session_holder))
    graph.add_node("apply_decisions", _with_session(N.apply_decisions, session_holder))
    graph.add_node(
        "dispatch_cases",
        _with_session(N.dispatch_cases, session_holder, dispatch=deps.dispatch),
    )
    # ★ IP-2：整个节点体就是一次 `interrupt`。**必须是独立节点**——
    # 被中断的节点在恢复时会从头重跑（实测确认），塞进别的节点里
    # 会让那个节点的全部逻辑跟着重跑一遍。
    graph.add_node("wait_for_results", _with_session(N.wait_for_results, session_holder))
    graph.add_node("collect_results", _with_session(N.collect_results, session_holder))
    graph.add_node(
        "analyze_failures",
        _with_session(N.analyze_failures, session_holder, analyzer=deps.failure_analyzer),
    )
    graph.add_node(
        "build_report",
        _with_session(N.build_report, session_holder, builder=deps.report_builder),
    )

    graph.add_edge(START, "parse_spec")
    graph.add_conditional_edges(
        "parse_spec", after_parse, {"generate_cases": "generate_cases", "__end__": END}
    )
    graph.add_conditional_edges(
        "generate_cases", after_generate, {"review_gate": "review_gate", "__end__": END}
    )
    graph.add_edge("review_gate", "apply_decisions")
    graph.add_conditional_edges(
        "apply_decisions", after_decisions, {"dispatch_cases": "dispatch_cases", "__end__": END}
    )
    graph.add_edge("dispatch_cases", "wait_for_results")
    graph.add_edge("wait_for_results", "collect_results")
    graph.add_conditional_edges(
        "collect_results",
        after_collect,
        {"analyze_failures": "analyze_failures", "build_report": "build_report"},
    )
    graph.add_edge("analyze_failures", "build_report")
    graph.add_edge("build_report", END)

    compiled = graph.compile(checkpointer=checkpointer)
    # 把 session 的容器挂在编译结果上，供 `advance` 填。
    # 用一个可变容器而不是 `partial`：`advance` 每次调用要换一个新的 session，
    # 而图是编译一次、长期复用的。
    setattr(compiled, _HOLDER_ATTR, session_holder)
    return compiled


def _with_session(
    fn: Callable[..., Any], holder: dict[str, Session], **extra: Any
) -> Callable[..., Any]:
    """把一个需要 `session` 的节点包成 LangGraph 认的一元函数。"""

    async def _node(state: GraphState) -> dict[str, Any]:
        session = holder.get("session")
        if session is None:  # pragma: no cover —— advance 一定会填
            raise RuntimeError("图被调用时没有绑定 session")
        return await fn(state, session=session, **extra)

    _node.__name__ = getattr(fn, "__name__", "node")
    return _node


async def _mark_done_if_finished(
    graph: Any, config: dict[str, Any], session: Session, run_id: uuid.UUID
) -> None:
    """图跑到 END 之后，把 Run 标成 `done`。

    **这一步不能省，而且它不该放在 N7 里。** N7 的职责是"产出报告"，
    而"整个 Run 结束了"是**图层面**的事实——将来在 N7 之后再加节点
    （M3 可能要加"推送通知"），把 DONE 写在 N7 里就会让那个新节点
    在一个已标记完成的 Run 上运行。

    判据是"没有下一步了"，而不是某个节点的返回值：那是 LangGraph
    对"图已跑完"的定义，也是唯一不会随节点增减而失效的判据。
    """
    state = await graph.aget_state(config)
    if state.next:
        return
    current = runs_repo.get(session, run_id)
    if current is None or RunPhase(current.phase).is_terminal:
        return
    runs_repo.set_phase(session, run_id, RunPhase.DONE)


async def advance(
    run_id: uuid.UUID,
    *,
    graph: Any,
    redis: Redis,
    session: Session,
    reason: str = "wake",
) -> str:
    """把一个 Run 推进到下一个中断点。

    Args:
        reason: 唤醒来源，用于日志。取值如 `results_ready` / `fallback_poll` /
            `decisions_submitted`。它**不影响行为**——不同来源做的是同一件事。

    Returns:
        做了什么。取值：`started` / `resumed` / `lock_held` / `terminal` / `failed`。
        **返回值是给日志和测试看的**，调用方不需要据此做分支——
        拿不到锁不是错误，终态不是错误。
    """
    async with acquire_run_lock(redis, run_id) as acquired:
        if not acquired:
            # 别人正在推进。**这不是错误**：N 条用例完成会有 N 次唤醒，
            # 其中 N-1 次会遇到这个分支。
            logger.debug("Run %s 已有推进者在跑，跳过（reason=%s）", run_id, reason)
            return "lock_held"

        run = runs_repo.get(session, run_id)
        if run is None:
            logger.error("Run 不存在：%s", run_id)
            return "failed"
        if RunPhase(run.phase).is_terminal:
            # 终态之后不再推进。**这条检查不能省**：兜底轮询可能在
            # Run 已经完成之后才扫到它（扫描是周期性的，而推进是即时的），
            # 那时再唤醒一次会把整个图从头跑一遍。
            return "terminal"

        holder = getattr(graph, _HOLDER_ATTR)
        holder["session"] = session

        config = {"configurable": {"thread_id": str(run_id)}}
        existing = await graph.aget_state(config)

        if not existing.next and not existing.values:
            # 没有任何 checkpoint → 这是第一次推进，从头开始。
            await graph.ainvoke(
                initial_state(
                    run_id=str(run_id),
                    spec_id=str(run.spec_id),
                    spec_fingerprint=run.spec_fingerprint,
                ),
                config,
            )
            await _mark_done_if_finished(graph, config, session, run_id)
            session.commit()
            return "started"

        if not existing.next:
            # 有 checkpoint 但已经没有下一步 → 图已经跑完了。
            return "terminal"

        # 从挂起处恢复。载荷是"结果到齐了"这个信号本身——
        # **完成判定查 PG，不靠载荷**（`06` §6.2）：载荷可能陈旧，
        # 而 PG 是权威。
        await graph.ainvoke(Command(resume={"reason": reason}), config)
        await _mark_done_if_finished(graph, config, session, run_id)
        session.commit()
        return "resumed"
