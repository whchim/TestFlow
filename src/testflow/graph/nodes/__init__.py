"""图的八个节点。

对应决策：`06-Agent编排.md` §3（节点表）、§4（主链与条件边）、G4/G5。

    START → N1 → N2 → **IP-1** → N3 → N4 → **IP-2** → N5 → N6 → N7 → END
            解析   生成    人工确认   决策   投递    等待     汇集  归因  报告

## 与计划里的文件布局有一处出入

计划写的是一个节点一个文件。实现时放在了这个包的 `__init__.py` 里——
八个节点每个都只有二三十行，拆开之后"它们如何串起来"这件事反而要跨
八个文件去读。**串起来看比分开看更重要**，所以先按后者组织。

（包本身是 M0 脚手架留下的，与 `08` EN1 的目录约定一致，所以继续用它。
将来某个节点长到几百行时再单独拆出去——那时它才有独立成文件的价值。）

## 关于 `phase`

每个节点都写 `runs.phase`。但**路由一律依据业务字段**（G4）——
`phase` 是派生值，用它做路由等于用缓存做判断，而缓存会落后。

## 关于幂等

所有节点都必须能安全重跑。原因不是"以防万一"，而是**必然发生**：

* 中断的节点在恢复时**从头重跑**（实测确认：`interrupt()` 所在节点的
  函数体在 resume 时会完整执行第二遍）
* 锁的 TTL 到期、Redis 抖动都可能让两个推进者并行

所以 N2 跳过已成功的、N4 跳过已有结果的、N3/N5/N7 都是幂等的写。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from testflow.agents.spec_parser import SpecParseError, parse_spec
from testflow.agents.test_case_gen import RuleBasedGenerator, generate_for_operations
from testflow.domain.attribution import (
    AttributionCategory,
    AttributionDecision,
    FailedCase,
    FailureContext,
    is_synthesized_stall,
)
from testflow.domain.graph import NodeFailure, RunPhase
from testflow.graph.state import GraphState, failures_of
from testflow.observability.logging import get_logger
from testflow.storage.repositories import attributions as attributions_repo
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import operations as operations_repo
from testflow.storage.repositories import results as results_repo
from testflow.storage.repositories import runs as runs_repo
from testflow.storage.repositories import specs as specs_repo

__all__ = [
    "analyze_failures",
    "apply_decisions",
    "build_report",
    "collect_results",
    "dispatch_cases",
    "generate_cases",
    "parse_spec_node",
    "review_gate",
    "wait_for_results",
]

logger = get_logger(__name__)

#: 节点名 → 中文职责。用于错误消息与日志，让人一眼看出是**哪一步**出了问题。
_NODE_LABELS: dict[str, str] = {
    "parse_spec": "解析 Spec",
    "generate_cases": "生成用例",
    "review_gate": "人工确认",
    "apply_decisions": "落库决策",
    "dispatch_cases": "投递执行",
    "wait_for_results": "等待结果",
    "collect_results": "汇集结果",
    "analyze_failures": "失败归因",
    "build_report": "生成报告",
}


# ═══════════════════════════════════════════════ N1 解析 Spec


async def parse_spec_node(state: GraphState, *, session: Session) -> dict[str, Any]:
    """N1：解析 Spec，产出目标 Operation 清单。

    **不调用 LLM**（`02` D3）。整个解析是确定性的，因此这个节点是全系统里
    唯一"真幂等"的——同输入必得同输出，可以安全重放。

    幂等键是 `spec_fingerprint`：同指纹的文档解析结果一致，直接复用已落库的
    Operation（SP 幂等性）。
    """
    run_id = uuid.UUID(state["run_id"])
    spec_id = uuid.UUID(state["spec_id"])
    runs_repo.set_phase(session, run_id, RunPhase.PARSING)

    spec = specs_repo.get(session, spec_id)
    if spec is None:
        return {
            "node_failures": [
                *failures_of(state),
                NodeFailure("parse_spec", "spec_missing", f"Spec 不存在：{spec_id}"),
            ]
        }

    operations = operations_repo.list_by_spec(session, spec_id)
    if not operations:
        # 正常情况下 Operation 在 Run 创建时就落库了（`SpecParser` 的产出）。
        # 走到这里说明数据不完整——**不静默继续**：一个没有目标接口的 Run
        # 会一路跑到 N7 并产出一份"0 条用例、通过率未知"的报告，
        # 而那份报告看起来像"测试通过了"。
        #
        # 这里重新解析一次，是为了让"上传了 Spec 但没有 Run"这条路径
        # （M2 的 CLI/前端可能这么用）也能工作，而不是硬失败。
        try:
            parse_spec(spec.raw_content)
        except SpecParseError as exc:
            return {
                "node_failures": [
                    *failures_of(state),
                    NodeFailure("parse_spec", exc.code, exc.detail),
                ]
            }
        operations = operations_repo.list_by_spec(session, spec_id)

    if not operations:
        return {
            "node_failures": [
                *failures_of(state),
                NodeFailure("parse_spec", "no_operations", "这份 Spec 里没有解析出任何接口"),
            ]
        }

    return {
        "target_operation_ids": [str(op.id) for op in operations],
        "spec_fingerprint": spec.fingerprint,
        "node_failures": failures_of(state),
    }


# ═══════════════════════════════════════════════ N2 生成用例


async def generate_cases(
    state: GraphState, *, session: Session, generator: Any = None
) -> dict[str, Any]:
    """N2：为每个目标 Operation 生成用例。

    **幂等语义是"跳过"，不是"重放"**（DM4）。见 `agents/test_case_gen.py`
    的模块文档字符串——那里解释了为什么对调 LLM 的节点来说，
    "重放"等于静默删除用户已确认的工作。
    """
    run_id = uuid.UUID(state["run_id"])
    runs_repo.set_phase(session, run_id, RunPhase.GENERATING)
    operation_ids = [uuid.UUID(x) for x in state.get("target_operation_ids", [])]

    if not operation_ids:
        # 没有可用接口——硬失败。空 Spec 不该产出一份"全绿"的报告。
        return {
            "node_failures": [
                *failures_of(state),
                NodeFailure("generate_cases", "no_operations", "Spec 里没有可测试的接口"),
            ]
        }

    outcome = await generate_for_operations(
        session,
        run_id=run_id,
        operation_ids=operation_ids,
        generator=generator or RuleBasedGenerator(),
        # TG5：无信息接口走纯规则，**不调 LLM**。demo 的 fixture 里
        # 一半以上属于这类，所以这条路径决定了 demo 能不能离线跑。
        fallback_rule_generator=RuleBasedGenerator(),
    )

    failures = failures_of(state)
    for operation_id in outcome.failed_operations:
        # **部分失败不阻塞全局**：一个接口的 schema 特别难处理，
        # 不该让整份 Spec 的生成全部作废。失败会在报告里作为
        # "未覆盖的接口"呈现。
        failures = [
            *failures,
            NodeFailure("generate_cases", "operation_failed", str(operation_id), retryable=True),
        ]

    return {
        "case_ids": [str(cid) for cid in outcome.generated_case_ids],
        "generated_case_count": len(outcome.generated_case_ids),
        "node_failures": failures,
    }


# ═══════════════════════════════════════════════ IP-1 人工确认


async def review_gate(state: GraphState, *, session: Session) -> dict[str, Any]:
    """IP-1：人工确认门。**整个函数体就是一次 `interrupt`。**

    与 `wait_for_results`（IP-2）同理，必须是**独立节点**：被中断的节点在
    恢复时会从头重跑，若把 `interrupt()` 塞进 N2 或 N3，恢复时会把生成或
    决策逻辑再跑一遍——那会静默改写用户已确认的用例。

    挂起期间用户在决策接口逐条批准 / 拒绝用例，那些决定写进
    `cases.review_status`（PG 是权威）。本节点不读、也不决定任何东西——
    它只负责把图**停在这里**，等一个"决策已提交"的唤醒信号。
    """
    from langgraph.types import interrupt

    # 挂起前必须把 phase 写成 `awaiting_review`：兜底轮询与审阅超时扫描
    # 看的正是这个状态（与 IP-2 写 `awaiting_results` 是同一个道理）。
    runs_repo.set_phase(session, uuid.UUID(state["run_id"]), RunPhase.AWAITING_REVIEW)

    interrupt(
        {
            "kind": "review",
            "run_id": state["run_id"],
            "expected": state.get("generated_case_count", 0),
        }
    )
    return {"phase": RunPhase.DISPATCHING.value}


# ═══════════════════════════════════════════════ N3 落库决策


async def apply_decisions(state: GraphState, *, session: Session) -> dict[str, Any]:
    """N3：确定"哪些用例允许执行"。**只读人工决策，不在此处批准任何东西。**

    M2 起，批准 / 拒绝是 IP-1（`review_gate`）之后、由决策接口写进
    `cases.review_status` 的事实（`06` §5.1）。本节点只**读**"已批准"的集合，
    供下游路由判断。

    路由依据一直是 `approved_case_ids` 是否为空，不是"有没有人审过"——
    所以把 M1 的"无条件批准"换成"读人工决策"之后，**下游一行都不用改**。
    """
    run_id = uuid.UUID(state["run_id"])
    approved_ids = [
        str(c.id) for c in cases_repo.list_by_run(session, run_id, review_status="approved")
    ]
    logger.info("N3 读取人工决策：批准 %d 条用例", len(approved_ids))
    return {"approved_case_ids": approved_ids}


# ═══════════════════════════════════════════════ N4 投递执行


async def dispatch_cases(state: GraphState, *, session: Session, dispatch: Any) -> dict[str, Any]:
    """N4：把已批准的用例投递到 `exec` 队列。

    ## 幂等（`06` §5.3：恢复安全性最关键的一点）

    投递前先查 `case_results`，**跳过已有结果的用例**。少了这一步，
    恢复时会把全部用例再投一遍——幂等性保证结果不会写重，
    但白做一遍工，而且会给目标服务凭空多打一倍的请求。

    ## 破坏性用例在这里过滤

    **这是主防线**，Worker 侧的 Guardrail 是冗余的那一层（TE6）。
    两层都要有：本层是应用逻辑，可能因 bug、重构或后续改动而失效；
    Guardrail 不受业务代码影响。**安全设计不假设应用层永远正确。**
    """
    run_id = uuid.UUID(state["run_id"])
    runs_repo.set_phase(session, run_id, RunPhase.DISPATCHING)

    # ★★ **投递之前必须先把数据提交。**
    #
    # N2 生成用例、N3 批准——但它们和 N4 在**同一个事务**里，而那个事务要到
    # `advance()` 跑完才提交。N4 一旦把任务投出去，Worker 就是一个
    # 完全独立的进程，它会立刻按 `case_id` 去查库——**而那时数据还没提交**。
    #
    # 症状：`ValueError: 用例不存在`，随机出现在一部分任务上
    # （取决于 Worker 有没有抢在提交之前查到）。
    #
    # **这个 bug 集成测试发现不了**：那边投递和读取用的是同一个 session，
    # 未提交的数据对它自己是可见的。只有真的把编排器和 Worker 分成两个进程
    # 跑起来才会撞上。
    #
    # 提交在这里而不是"N2 里顺手提交"，是因为**这条边界有确切的位置**：
    # "当我们要把数据的引用交给另一个进程时，它必须已经持久化"。
    session.commit()

    approved = [uuid.UUID(x) for x in state.get("approved_case_ids", [])]
    already_done = {row.case_id for row in results_repo.list_by_run(session, run_id)}

    dispatched = 0
    for case_id in approved:
        if case_id in already_done:
            continue
        case = cases_repo.get(session, case_id)
        if case is None:
            continue
        if case.is_destructive:
            # TE3：MVP 不执行破坏性用例，也不提供授权入口。
            logger.info("跳过破坏性用例 case=%s（TE3：MVP 无授权入口）", case_id)
            continue
        dispatch(run_id, case_id)
        dispatched += 1

    # 一次投递 2000 条用例可能花几十秒。若不更新 `updated_at`，
    # 兜底轮询会把一个**正在正常工作**的 Run 判成停滞并强行推进。
    #
    # ★ **派生缓存也在这里维护**（DM5：节点出口更新，供前端展示）。
    # `expected` 的口径是"应当有结果的用例数"，所以它是 `count_dispatchable`
    # 而不是 `len(approved)`——两者在破坏性用例上会分叉，而那些按 TE3
    # 永远不会被执行，也就永远不会有结果。用 `len(approved)` 当分母，
    # 完成判定就永远差那几条。
    settled_now = results_repo.count_results(session, run_id)
    runs_repo.set_case_counts(
        session,
        run_id,
        expected=cases_repo.count_dispatchable(session, run_id),
        settled=settled_now,
    )
    # 投递完也要提交一次：上面这几个写（`updated_at`、两个计数）同样要在
    # 下一个进程（编排器的下一次唤醒）之前可见。
    session.commit()
    logger.info("N4 投递 %d 条用例（跳过 %d 条已有结果的）", dispatched, len(already_done))
    # 只返回 state 里存在的键——多出来的键会被 LangGraph 合并进状态，
    # 而一个不在 "契约" 里的字段会在 checkpoint 里越攒越多。
    return {"settled_case_count": settled_now}


# ═══════════════════════════════════════════════ IP-2 等待


async def wait_for_results(state: GraphState, *, session: Session) -> dict[str, Any]:
    """IP-2：等待执行结果。**整个函数体就是一次 `interrupt`。**

    为什么必须是**独立节点**而不是塞进 N4 或 N5 里：**被中断的节点在恢复时
    会从头重跑**（实测确认）。若 `interrupt()` 写在 `dispatch_cases` 里面，
    恢复时会把整个投递逻辑再跑一遍——那本身是幂等的（N4 会跳过已有结果的），
    但完全没必要，而且让"重跑哪些代码"这件事变得难以推理。

    拆成独立节点之后，"恢复 = 从等待之后继续"这件事在图上就看得见了。
    """
    from langgraph.types import interrupt

    # ⚠️ **挂起前必须把 phase 写成 `awaiting_results`。**
    #
    # 少了这一步，phase 会停在 N4 写的 `dispatching`，而**兜底轮询扫的正是
    # `awaiting_results`**——于是它永远扫不到卡住的 Run，那道安全网直接失效。
    # 症状是"回调丢了的 Run 永远转圈"，而日志里一切正常。
    #
    # 这个写入在 `interrupt()` **之前**，所以节点在恢复重跑时会再写一次。
    # 无害：写的是同一个值。而这不是"不小心多写了一次"，是**必然如此**——
    # 中断的节点从头重跑是 LangGraph 的语义（实测确认过）。
    runs_repo.set_phase(session, uuid.UUID(state["run_id"]), RunPhase.AWAITING_RESULTS)

    # `interrupt()` 的返回值是恢复时传入的载荷。M1 的载荷是"结果到齐了"
    # 这个信号本身，没有别的信息——**完成判定查 PG，不靠载荷**
    # （`06` §6.2：载荷可能是陈旧的，而 PG 是权威）。
    interrupt(
        {
            "kind": "awaiting_results",
            "run_id": state["run_id"],
            "expected": state.get("generated_case_count", 0),
        }
    )
    return {"phase": RunPhase.ANALYZING.value}


# ═══════════════════════════════════════════════ N5 汇集结果


async def collect_results(state: GraphState, *, session: Session) -> dict[str, Any]:
    """N5：校验结果完整性，判定部分失败。

    ## 完成判定的权威依据是 PG，不是聚合字段

    `06` §6.2：查 `case_results` 的行数，而不是读 `runs.settled_case_count`。
    后者是派生缓存（DM5），会因为崩溃、重投、并发而落后——而它落后导致的最坏
    情况正是"该推进时不推进"，那恰好是兜底轮询要解决的问题，
    它自己不能再依赖一个可能不准的值。

    ## 部分失败是正常结果，不是错误

    有用例没跑完时，把它们补记为 `timeout` 并继续推进。用户的报告里会
    明确标出"部分用例未完成"——那比一个永远转圈的任务有用得多。
    """
    run_id = uuid.UUID(state["run_id"])
    runs_repo.set_phase(session, run_id, RunPhase.ANALYZING)

    # ★ **分母是"应当有结果"的用例数，不是"生成了多少条"。**
    #
    # 第一版用的是 `state["expected_case_count"]`——那是 N2 生成的条数。
    # 两者在有被拒绝或破坏性用例时会分叉，而分叉的后果很具体：
    #
    #     settled(2) < expected(3)  → 判定"结果不完整" → 记一条 partial_results，
    #                                  并去补记缺失的那条为 timeout
    #
    # 于是每一次运行都会多出一条假的"部分失败"记录——**而 Run 的最终结论
    # 仍然是 done、通过率仍然正确**，所以它看起来完全正常。
    # 只有翻日志（或翻 `node_failures`）才会看到那句
    # "N5 结果不完整：期望 3 条，已有 2 条"。
    #
    # 补记那一步在 M2 之前会真的写出一条假的 `timeout` 结果
    # （把一个用户拒绝的用例说成"目标接口超时"）——那才是这个分母错误的
    # 真正危害。它现在被 `mark_unfinished_as_timeout` 的过滤挡住了，
    # 但分母本身仍然是错的，会继续制造假告警。
    expected = cases_repo.count_dispatchable(session, run_id)
    settled = results_repo.count_results(session, run_id)

    failures = failures_of(state)
    if settled < expected:
        # 走这条路径说明"被唤醒了但结果没到齐"。正常情况下图不会被唤醒
        # （唤醒端点自己会判定），所以走到这里意味着**兜底轮询**触发的。
        marked = results_repo.mark_unfinished_as_timeout(session, run_id)
        logger.warning(
            "N5 结果不完整：期望 %d 条，已有 %d 条，补记 %d 条为 timeout",
            expected,
            settled,
            marked,
        )
        failures = [
            *failures,
            NodeFailure(
                "collect_results",
                "partial_results",
                f"期望 {expected} 条结果，实际 {settled + marked} 条；缺口的已标为超时",
            ),
        ]
        settled = results_repo.count_results(session, run_id)

    # 派生缓存（DM5）：这是"结果到齐"之后的最终值，前端与 CLI 读它。
    # **判定仍然不读它**——`gateway/internal.py` 与下一次唤醒都查
    # `case_results`，因为缓存会因为崩溃/重投而落后。
    runs_repo.set_case_counts(session, run_id, settled=settled)
    return {"settled_case_count": settled, "node_failures": failures}


# ═══════════════════════════════════════════════ N6 归因


def _rule_only_analyzer() -> Any:
    """没有注入分析器时的兜底：只跑规则。

    这不是"半成品模式"，而是**保证每条失败都有归因**的最后一道防线。
    生产环境由 `graph.tasks` 注入带 LLM 的 `LogAnalyzer`；图测试与离线运行
    走这里，结论仍然确定（只是规则覆盖率会低，`unknown` 会多）。

    为什么不直接不归因：失败用例在报告里没有归因，等于失败原因被悄悄吞掉。
    `unknown` 至少是一个**可见的缺陷信号**（LA §3.4）。
    """
    from testflow.agents.log_analyzer import LogAnalyzer

    logger.warning("未注入归因分析器，N6 只跑规则（LLM 兜底不可用）")
    return LogAnalyzer()


async def analyze_failures(
    state: GraphState, *, session: Session, analyzer: Any = None
) -> dict[str, Any]:
    """N6：对失败用例归因。

    `LogAnalyzer`（规则 + LLM 兜底）由装配层注入（`graph/tasks.py`）。
    节点本身**不 import 具体实现**——`graph/` 只依赖协议，这样"换一个分析器"
    不需要改图（`02` §2.2 的分层理由）。

    ## 两处容易写错的地方

    1. **补记的超时不是失败**（`executor_stall`）。它们不参与归因、
       也不该有归因行——否则报告里会出现一条"网络故障"，而事实是
       我们根本没拿到结果（见 `is_synthesized_stall`）。
    2. **重算要清理过期归因**。一条用例上次失败、这次通过，旧的归因行
       必须删掉，否则报告会出现"失败 0 条、归因 3 条"。
    """
    run_id = uuid.UUID(state["run_id"])
    runs_repo.set_phase(session, run_id, RunPhase.ANALYZING)

    results = results_repo.list_by_run(session, run_id)
    failing = [
        row
        for row in results
        if row.status != "passed" and not is_synthesized_stall(row.status, row.error_kind)
    ]

    cases = cases_repo.list_by_run(session, run_id)
    case_by_id = {case.id: case for case in cases}
    failing_case_ids = {row.case_id for row in failing}
    stale_case_ids = [case.id for case in cases if case.id not in failing_case_ids]
    attributions_repo.delete_for_cases(session, run_id, stale_case_ids)

    # 零失败：不构造 LLM 客户端、不写行（D10）。
    if not failing:
        return {"attribution_ids": []}

    operation_ids = sorted(
        {case_by_id[row.case_id].operation_id for row in failing if row.case_id in case_by_id}
    )
    operation_by_id = {op.id: op for op in operations_repo.list_by_ids(session, operation_ids)}

    items: list[FailedCase] = []
    for row in failing:
        case = case_by_id.get(row.case_id)
        if case is None:  # pragma: no cover —— 外键保证不会发生
            continue
        operation = operation_by_id.get(case.operation_id)
        items.append(
            FailedCase(
                case_id=row.case_id,
                context=FailureContext(
                    status=row.status,
                    http_status=row.http_status,
                    error_kind=row.error_kind,
                    response_body=row.response_body,
                    truncated=row.response_body_truncated,
                    assertion_outcomes=row.assertion_outcomes or (),
                    case_confidence=case.confidence,
                    responses_schema=operation.responses_schema if operation else None,
                    elapsed_ms=row.elapsed_ms,
                ),
            )
        )

    outcome = await (analyzer or _rule_only_analyzer()).analyze(items)

    rows: list[dict[str, Any]] = []
    missing: list[uuid.UUID] = []
    for item in items:
        decision = outcome.decisions.get(item.case_id)
        if decision is None:
            missing.append(item.case_id)
            decision = AttributionDecision(
                category=AttributionCategory.UNKNOWN,
                method="rule",
                confidence="low",
                evidence={"reason": "analysis_incomplete"},
            )
        rows.append(
            {
                "run_id": run_id,
                "case_id": item.case_id,
                "category": decision.category.value,
                "method": decision.method,
                "rule_ref": decision.rule_ref,
                "llm_model": decision.llm_model,
                "confidence": decision.confidence,
                "evidence": decision.evidence,
            }
        )

    attributions_repo.upsert_many(session, rows)

    stored = {row.case_id: str(row.id) for row in attributions_repo.list_by_run(session, run_id)}
    attribution_ids = [stored[item.case_id] for item in items if item.case_id in stored]

    failures = [*failures_of(state), *outcome.failures]
    if missing:
        failures.append(
            NodeFailure(
                node="analyze_failures",
                kind="attribution_incomplete",
                detail=f"{len(missing)} 条失败用例没有拿到归因结论",
                retryable=True,
            )
        )
    return {"attribution_ids": attribution_ids, "node_failures": failures}


# ═══════════════════════════════════════════════ N7 报告


async def build_report(state: GraphState, *, session: Session, builder: Any) -> dict[str, Any]:
    """N7：聚合指标并渲染报告。

    **不调用 LLM**（D5）——数据层是确定性的集合运算，模板只负责呈现（RA5）。

    报告里的通过率分母**只含已执行的用例**（RA3）：未执行/未确认/被拒绝的
    不进分母，否则"我们没跑的用例"会稀释通过率，把一个真实的失败率说小。
    """
    run_id = uuid.UUID(state["run_id"])
    runs_repo.set_phase(session, run_id, RunPhase.REPORTING)

    report = builder(session, run_id)
    return {"report_id": str(report.id)}


def _label(node: str) -> str:
    """节点名的中文说明。用在错误消息里——"collect_results 失败"远不如
    "汇集结果失败"直观，而日志的读者不一定记得住节点名。"""
    return _NODE_LABELS.get(node, node)
