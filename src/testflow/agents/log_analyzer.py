"""N6 归因：规则优先，LLM 兜底。

对应决策：`03-模块详细设计/LogAnalyzer.md` §2（规则优先）、§4（规则库）、
§5（LLM 兜底）、§6（LA3 置信度耦合）、§7（输出契约）。

## 为什么规则排在 LLM 前面

四个理由（LA §2.1）：确定性、可解释、零成本零延迟、可回归。
更重要的是**它保住了归因的可测试性**——规则路径同输入必得同输出，
可以写断言；LLM 路径只能测"契约没被违反"，不能测结论对不对。

## 为什么每条失败都必须有结论

`AnalysisOutcome.decisions` 的约定是"输入多少条，输出多少条"。
LLM 调用失败、超时、返回了残缺结果——都不能让那条失败在报告里
**没有归因地消失**。宁可写一条 `unknown`：它是缺陷信号，
会出现在规则覆盖率与"未归类"统计里（LA §3.4）。

## 预算与并发为什么要在这里管

单次 LLM 调用可能耗时几十秒（`llm_timeout_s` 默认 120），而一个 Run
可能失败几十条。串行叠加会让 N6 超过 Run 锁的 TTL（400 秒），
于是锁到期、另一个推进者进来重跑同一个节点——**同样的钱花两遍，
还可能写出两份不同的结论**。

所以：并发受限（默认 4）、总预算受限（默认 300 秒，留出余量），
到点未完成的直接落 `unknown`。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from testflow.agents.prompts import build_attribution_prompt
from testflow.agents.rules.attribution import RuleResult, evaluate
from testflow.domain.attribution import (
    AnalysisOutcome,
    AttributionCategory,
    AttributionDecision,
    FailedCase,
    FailureContext,
    FallbackAttribution,
    cap_confidence,
)
from testflow.domain.graph import NodeFailure
from testflow.llm.client import LLMClient
from testflow.observability.logging import get_logger
from testflow.observability.tracing import bind_llm_purpose, reset_llm_purpose

__all__ = ["LlmAttributor", "LogAnalyzer"]

logger = get_logger(__name__)


class LlmAttributor:
    """把 `LLMClient` 适配成归因器。

    与 `LlmCaseGenerator` 同理：**构造时只接收窄协议**，不 import 任何 SDK。
    这样 `agents/` 仍然与厂商无关（`08` 的依赖白名单）。
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    @property
    def model_name(self) -> str | None:
        """落库用的模型名。**从 Provider 委托**，不在这里重复一份配置。"""
        return getattr(self._llm, "model_name", None)

    async def attribute(self, ctx: FailureContext) -> FallbackAttribution:
        """调用模型。**不吞异常**——由 `LogAnalyzer` 决定怎么兜底。"""
        return await self._llm.generate(
            prompt=build_attribution_prompt(ctx),
            schema=FallbackAttribution,
        )


class LogAnalyzer:
    """归因器。`llm=None` 时退化为"只跑规则"。"""

    def __init__(
        self,
        llm: LlmAttributor | None = None,
        *,
        llm_budget_s: float = 300.0,
        llm_concurrency: int = 4,
    ) -> None:
        self._llm = llm
        self._budget_s = llm_budget_s
        self._concurrency = max(1, llm_concurrency)

    async def analyze(self, items: Sequence[FailedCase]) -> AnalysisOutcome:
        # 归因不需要思考模式（`02` §5.3）：它只是从 11 个封闭类别里选一个。
        # 开思考只会增加延迟与 token，不会提高这个判断的质量。
        token = bind_llm_purpose("attribution")
        try:
            return await self._analyze(items)
        finally:
            reset_llm_purpose(token)

    async def _analyze(self, items: Sequence[FailedCase]) -> AnalysisOutcome:
        decisions: dict[uuid.UUID, AttributionDecision] = {}
        pending: list[FailedCase] = []

        for item in items:
            hit = evaluate(item.context)
            if hit is None:
                pending.append(item)
            else:
                decisions[item.case_id] = _from_rule(item.context, hit)

        if not pending:
            return AnalysisOutcome(decisions=decisions)

        if self._llm is None:
            for item in pending:
                decisions[item.case_id] = _unknown(
                    method="rule",
                    reason="no_rule_matched_and_no_llm_configured",
                    ctx=item.context,
                )
            return AnalysisOutcome(decisions=decisions)

        fallback_decisions, failures = await self._run_fallback(pending)
        decisions.update(fallback_decisions)
        return AnalysisOutcome(decisions=decisions, failures=tuple(failures))

    # ────────────────────────────────────────── LLM 兜底

    async def _run_fallback(
        self, pending: Sequence[FailedCase]
    ) -> tuple[dict[uuid.UUID, AttributionDecision], list[NodeFailure]]:
        semaphore = asyncio.Semaphore(self._concurrency)
        attributor = self._llm
        assert attributor is not None  # 调用方已判空；这里只是给类型检查看

        async def guarded(
            item: FailedCase,
        ) -> tuple[uuid.UUID, FallbackAttribution | None, Exception | None]:
            async with semaphore:
                try:
                    return item.case_id, await attributor.attribute(item.context), None
                except Exception as exc:  # noqa: BLE001 —— 失败要变成记录，不是崩溃
                    return item.case_id, None, exc

        tasks = {asyncio.create_task(guarded(item)): item for item in pending}
        done, not_done = await asyncio.wait(tasks, timeout=self._budget_s)
        for task in not_done:
            task.cancel()
        if not_done:
            await asyncio.gather(*not_done, return_exceptions=True)

        decisions: dict[uuid.UUID, AttributionDecision] = {}
        failures: list[NodeFailure] = []
        for task in done:
            item = tasks[task]
            case_id, raw, error = task.result()
            if error is not None or raw is None:
                decisions[case_id] = _unknown(
                    method="llm",
                    reason=f"llm_attribution_failed:{type(error).__name__ if error else 'empty'}",
                    ctx=item.context,
                )
                failures.append(
                    NodeFailure(
                        node="analyze_failures",
                        kind="llm_attribution_failed",
                        detail=f"LLM 归因失败：{type(error).__name__ if error else '空结果'}",
                        retryable=True,
                    )
                )
                continue

            decisions[case_id] = _from_llm(item.context, raw, model_name=self._model_name())

        # 预算耗尽：这些用例没有结论，落 `unknown` 并**整体**记一条失败。
        # 不逐条记 NodeFailure：50 条超时会产 50 条一模一样的记录。
        for task in not_done:
            item = tasks[task]
            decisions[item.case_id] = _unknown(
                method="llm", reason="attribution_deadline_exceeded", ctx=item.context
            )
        if not_done:
            failures.append(
                NodeFailure(
                    node="analyze_failures",
                    kind="attribution_deadline_exceeded",
                    detail=f"{len(not_done)} 条失败用例未在 {self._budget_s:.0f} 秒预算内完成归因",
                    retryable=True,
                )
            )
        return decisions, failures

    def _model_name(self) -> str | None:
        return getattr(self._llm, "model_name", None)


# ────────────────────────────────────────────── 结论构造


def _rule_evidence(ctx: FailureContext, rule_ref: str) -> dict[str, Any]:
    """规则路径的证据。**刻意不含响应体**——它可能含凭证或用户数据。"""
    return {
        "rule_ref": rule_ref,
        "status": ctx.status,
        "http_status": ctx.http_status,
        "error_kind": ctx.error_kind,
        "truncated": ctx.truncated,
    }


def _from_rule(ctx: FailureContext, hit: RuleResult) -> AttributionDecision:
    return AttributionDecision(
        category=hit.category,
        method="rule",
        confidence=cap_confidence(
            hit.confidence,
            category=hit.category,
            case_confidence=ctx.case_confidence,
            truncated=ctx.truncated,
        ),
        rule_ref=hit.rule_ref,
        evidence=_rule_evidence(ctx, hit.rule_ref),
    )


def _from_llm(
    ctx: FailureContext, raw: FallbackAttribution, *, model_name: str | None
) -> AttributionDecision:
    return AttributionDecision(
        category=raw.category,
        method="llm",
        confidence=cap_confidence(
            raw.confidence,
            category=raw.category,
            case_confidence=ctx.case_confidence,
            truncated=ctx.truncated,
        ),
        llm_model=model_name,
        evidence={
            "signals": list(raw.evidence),
            "reasoning": raw.reasoning,
            "truncated": ctx.truncated,
            "case_confidence": ctx.case_confidence,
        },
    )


def _unknown(
    *, method: Literal["rule", "llm"], reason: str, ctx: FailureContext
) -> AttributionDecision:
    return AttributionDecision(
        category=AttributionCategory.UNKNOWN,
        method=method,
        confidence="low",
        evidence={
            "reason": reason,
            "truncated": ctx.truncated,
            "case_confidence": ctx.case_confidence,
        },
    )
