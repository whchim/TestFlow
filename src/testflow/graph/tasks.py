"""编排层的 Celery 任务。**推进图，然后释放进程。**

对应决策：`06-Agent编排.md` G1（编排层独立进程）、`02` D1（编排层异步）、
`10-部署运维.md` §2.2（单副本）。

## 为什么用 `asyncio.run` 而不是"编排层也写同步代码"

ADR-0001 说"编排层 async、执行层 sync"。这条任务的写法看似与"不要每任务起事件循环"
的建议冲突，其实不是——**那条建议针对的是执行层**：

    执行层：每个用例一个任务，一次运行有几千个。每个都起一个事件循环，
            开销与信号处理的复杂度都不划算。**同步多进程更简单、隔离更干净。**

    编排层：每个**图跳**一个任务，一次 Run 只有几个。起事件循环的开销
            可以忽略，而 LangGraph 的原生接口就是异步的——
            非要绕开它就得用 `graph.invoke()`，那会在一次 LLM 调用里
            阻塞整个进程几十秒。

## 单副本 + `--pool=solo`

编排层不是"处理负载"，而是"推动状态机往前走一步"。per-Run 的串行由 Redis
锁保证（`06` §6.3），**不靠池并发**——所以单副本、单进程完全够用，
而且避免在 prefork 子进程里 fork 一个活的事件循环（那是已知会出事的行为）。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Coroutine
from typing import Any

from celery import shared_task
from redis.asyncio import Redis
from sqlalchemy.orm import Session, sessionmaker

from testflow.celery_app import ORCHESTRATE_QUEUE
from testflow.config import Settings, load_settings
from testflow.graph.runner import GraphDeps, advance, build_graph
from testflow.observability.logging import get_logger
from testflow.observability.tracing import (
    bind_run_id,
    bind_trace_id,
    reset_run_id,
    reset_trace_id,
)
from testflow.storage.db import create_db_engine, create_session_factory

__all__ = ["orchestrate", "reset_graph_context", "set_dispatch"]

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _background_loop() -> asyncio.AbstractEventLoop:
    """进程内**唯一**的事件循环，跑在一个后台线程上。

    ## 为什么不能每个任务 `asyncio.run()`

    `asyncio.run()` 每次都建一个新的循环。而编排层持有**有状态**的异步资源
    ——Redis 连接池、checkpointer 的连接。它们的连接绑定在"创建它的那个循环"上，
    换循环就会得到：

        RuntimeError: Event loop is closed

    而那不会在测试里出现（测试每个用例一个循环，资源也是每用例新建的），
    **只在真的跑起来时出现**——每个任务都换一次循环，于是第二个任务开始
    就再也用不了那个 Redis 连接。

    ## 代价与收益

    多了一个后台线程与一个长期存活的循环。换来的是：图只编译一次、
    Redis 连接被复用、以及"连接绑错循环"这一类问题从根上消失。

    `--pool=solo` 保证了同时只有一个任务在跑，所以这个共享循环
    不需要额外的并发保护。
    """
    global _loop, _loop_thread
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, name="testflow-orchestrator-loop", daemon=True
        )
        _loop_thread.start()
    return _loop


def _run_blocking[T](coro: Coroutine[Any, Any, T]) -> T:
    """把协程提交到那个循环上执行，并**阻塞当前线程**等结果。

    阻塞是对的：Celery 的任务体是同步的（`--pool=solo`），
    它需要"跑完图再返回"，而不是"投出去就不管"。
    """
    return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()


logger = get_logger(__name__)


class _OrchestratorContext:
    """编排层的进程级资源。

    图**编译一次、长期复用**——编译代价不低（构建图、解析节点签名），
    而它本身是无状态的：状态全在 Checkpoint 里。每次任务重新编译会让
    每次推进都多花几十毫秒，而那正好是这个任务的主要成本。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_db_engine(settings)
        self.sessions: sessionmaker[Session] = create_session_factory(self.engine)
        self.redis: Redis = Redis.from_url(
            f"{settings.redis_url.rstrip('/')}/{settings.redis_checkpoint_db}"
        )
        self.deps: GraphDeps | None = None
        self.graph: Any = None
        #: 进程级 LLM 客户端。**用例生成与失败归因共用同一个**——
        #: 两个客户端意味着两份连接池，而连接池复用的收益本来就建立在
        #: "流量稀疏、连接要留住"上。它们也不会同时跑（N2 与 N6 相隔整个
        #: 执行阶段）。
        self.llm_provider: Any = None


_context: _OrchestratorContext | None = None


def _get_context() -> _OrchestratorContext:
    global _context
    if _context is None:
        _context = _OrchestratorContext(load_settings())

    if _context.graph is None:
        if _context.deps is None:
            raise RuntimeError(
                "编排层的依赖尚未注册。入口点必须在启动时调用 `set_dispatch()`——"
                "否则图里的 N4 无路可投递，而它的表现是「Run 停在 dispatching」"
                "而不是一个明确的错误。"
            )
        from testflow.graph.checkpoint import RedisCheckpointSaver

        _context.graph = build_graph(
            RedisCheckpointSaver(_context.redis),
            _context.deps,
        )
    return _context


def set_dispatch(
    dispatch: Any,
    *,
    report_builder: Any = None,
    case_generator: Any = None,
    analyzer: Any = None,
) -> None:
    """注册图的外部依赖。**由编排器入口点在启动时调用一次。**

    为什么不让 `graph/` 直接 import 这些实现：那会让 `graph/` 认识 Celery
    （`dispatch` 最终要 `send_task`），而编排层不该知道任务是怎么被投递的——
    它只需要知道"有人会把这条用例送去执行"。M2 换成别的队列实现时，
    改的只是入口点里那一行。

    两个 `None` 都会**填上真正的实现**（而不是留空位）——见
    `_default_case_generator` 的文档字符串。测试要注入假实现时传参覆盖即可。
    """
    global _context
    if _context is None:
        _context = _OrchestratorContext(load_settings())
    ctx = _context

    if report_builder is None:
        import functools

        from testflow.agents.report_agent import build_report
        from testflow.llm.pricing import price_table_from_overrides

        # 单价覆盖只在装配点解析一次；报告拿到的是**已合并的表**。
        # 这样 `report_agent` 不必知道配置从哪来（它也不该知道）。
        report_builder = functools.partial(
            build_report,
            price_table=price_table_from_overrides(ctx.settings.llm_pricing_overrides),
        )

    if case_generator is None:
        case_generator = _default_case_generator(ctx)

    if analyzer is None:
        analyzer = _default_failure_analyzer(ctx)

    ctx.deps = GraphDeps(
        dispatch=dispatch,
        report_builder=report_builder,
        case_generator=case_generator,
        failure_analyzer=analyzer,
    )
    ctx.graph = None  # 依赖变了，图要重建


def _default_case_generator(context: _OrchestratorContext) -> Any:
    """装配真正的 LLM 生成器（M2）。

    ## 为什么默认装配，而不是留 `None` 让别人记着传

    `None` 的表现是**静默降级**：`generate_cases` 里 `generator or RuleBasedGenerator()`
    会退回纯规则生成器，于是复杂接口根本不生成用例——而报告看起来一切正常，
    只是覆盖少了一块，且没有任何东西提示。M1 就停在这个状态（`case_generator=None`）。

    与 `report_builder` 同理：编排层跑起来就必须有的东西，默认值应当是它本身。

    ## ★ 关键：`http_client` 必须来自 Guardrail

    不给 `AsyncOpenAI` 传 `http_client`，SDK 会**自己建一个客户端**——
    那条出站不经过 Guardrail，绕开了 ADR-0002 的"唯一出站通道"。

    而**架构测试抓不到这件事**：`llm/` 本来就允许 import LLM SDK，
    `import openai` 与 `AsyncOpenAI(http_client=..., ...)` 在 AST 层面都合法。
    唯一的防线是那条行为测试（`tests/integration/test_llm_egress_behavior.py`）：
    发一次真实调用，断言审计里出现了 `direction='llm'` 的记录。
    **删掉这里传的 `http_client`，它就会红。**

    `run_id` 传 `None`：这个客户端是**进程级**的，一个客户端服务所有 Run，
    所以审计记不到具体 Run 上——这是 `audit_logs.run_id` 可空的设计用途之一
    （见 `repositories/audit.record`）。要按 Run 归因得改成每 Run 一个客户端，
    而连接池就没法复用了；LLM 流量本来就稀疏，这条取舍可以随时重来。

    ## ★ 必须包一层 `LlmCaseGenerator`

    图要的是 `CaseGenerator`（`generate(operation, dimensions)`），而
    `QwenProvider` 实现的是 `LLMClient`（`generate(*, prompt, schema)`）——
    **同名不同契约**。直接传 Provider 的后果实测过，很隐蔽：

        TypeError: QwenProvider.generate() takes 1 positional argument but 3 were given

    它被 `generate_for_operations` 的"部分失败不阻塞全局"接住了，于是
    Run 正常跑完、报告正常生成，只有**那个接口的用例是空的**。
    所以要断言的不只是"装上了 Provider"，还有"装上的是能用的那个东西"
    ——见 `tests/unit/test_orchestrator_wiring.py` 里那条**真的调用一次**的测试。
    """
    from testflow.agents.llm_case_gen import LlmCaseGenerator

    return LlmCaseGenerator(_default_llm_provider(context))


def _default_llm_provider(context: _OrchestratorContext) -> Any:
    """进程内**唯一**的 LLM 客户端。用例生成与失败归因共用。

    ## ★ `http_client` 必须来自 Guardrail

    不给 `AsyncOpenAI` 传 `http_client`，SDK 会**自己建一个客户端**——
    那条出站不经过 Guardrail，绕开了 ADR-0002 的"唯一出站通道"。

    而**架构测试抓不到这件事**：`llm/` 本来就允许 import LLM SDK，
    `import openai` 与 `AsyncOpenAI(http_client=..., ...)` 在 AST 层面都合法。
    唯一的防线是那条行为测试（`tests/integration/test_llm_egress_behavior.py`）：
    发一次真实调用，断言审计里出现了 `direction='llm'` 的记录。
    **删掉这里传的 `http_client`，它就会红。**

    `run_id` 传 `None`：这个客户端是**进程级**的，一个客户端服务所有 Run，
    所以审计记不到具体 Run 上——这是 `audit_logs.run_id` 可空的设计用途之一
    （见 `repositories/audit.record`）。要按 Run 归因得改成每 Run 一个客户端，
    而连接池就没法复用了；LLM 流量本来就稀疏，这条取舍可以随时重来。
    """
    if context.llm_provider is not None:
        return context.llm_provider

    from testflow.guardrail.async_client import build_llm_http_client
    from testflow.llm.qwen import QwenProvider
    from testflow.storage.audit_sink import make_audit_sink
    from testflow.storage.usage_sink import make_usage_sink

    context.llm_provider = QwenProvider(
        context.settings,
        http_client=build_llm_http_client(
            context.settings,
            audit_sink=make_audit_sink(context.sessions),
        ),
        # ★ 用量必须真的接上。漏了它的表现是报告里"LLM 调用 0 次、成本 ¥0"，
        # 而 Run 一切正常——与 M2 漏掉 `audit_sink` 是同一种静默失败。
        on_usage=make_usage_sink(context.sessions),
    )
    return context.llm_provider


def _default_failure_analyzer(context: _OrchestratorContext) -> Any:
    """装配 N6 的分析器：规则 + LLM 兜底。

    与 `_default_case_generator` 同理，**默认值必须是真实现**：
    留 `None` 的表现是"归因静默退化成只跑规则"，报告里 `unknown` 变多，
    而不会有任何东西提示少了一整条路径。
    """
    from testflow.agents.log_analyzer import LlmAttributor, LogAnalyzer

    return LogAnalyzer(LlmAttributor(_default_llm_provider(context)))


def reset_graph_context() -> None:
    """丢弃缓存的图与连接。**供测试使用。**

    测试之间共享一张编译好的图，会让"上一个测试留下的 checkpoint"
    影响下一个——而那类失败只在特定顺序下出现。
    """
    global _context
    if _context is not None:
        _context.engine.dispose()
        _context = None


@shared_task(name="testflow.orchestrate", queue=ORCHESTRATE_QUEUE, max_retries=0)
def orchestrate(run_id: str, reason: str = "wake") -> dict[str, Any]:
    """推进一个 Run。**短跳操作**——跑到下一个中断点就返回。

    Returns:
        一个小摘要（`action` 字段）。**仅供日志**：推进的结果（成功 / 拿不到锁 /
        终态）不是"任务失败"，所以不能用任务的成败来表达——那会让
        "别人正在推进"在 Celery 的监控里变成一串失败任务。
    """
    context = _get_context()

    async def _advance() -> str:
        # ★ 上下文必须在**协程内部**绑定。这个协程会被
        # `run_coroutine_threadsafe` 提交到后台线程的事件循环；
        # 在提交方线程里 bind 不保证能跨线程传过去。
        #
        # `run_id` 是 M4 的成本归集依据：LLM 客户端是进程级的，
        # 构造时不知道任何 Run；审计与用量都从当前上下文取它。
        trace_token = bind_trace_id()
        run_token = bind_run_id(uuid.UUID(run_id))
        session = context.sessions()
        try:
            return await advance(
                uuid.UUID(run_id),
                graph=context.graph,
                redis=context.redis,
                session=session,
                reason=reason,
            )
        finally:
            session.close()
            reset_run_id(run_token)
            reset_trace_id(trace_token)

    action = _run_blocking(_advance())
    logger.info("推进完成 run=%s reason=%s action=%s", run_id, reason, action)
    return {"run_id": run_id, "action": action}
