"""LogAnalyzer：规则优先、LLM 兜底、以及"每条失败都必须有结论"。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from testflow.agents.log_analyzer import LogAnalyzer
from testflow.domain.attribution import (
    AttributionCategory,
    FailedCase,
    FailureContext,
    FallbackAttribution,
)


class _StubAttributor:
    """记录调用次数的假归因器。"""

    model_name = "stub-model"

    def __init__(
        self,
        *,
        result: FallbackAttribution | None = None,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.result = result or FallbackAttribution(
            category=AttributionCategory.ASSERTION_MISMATCH,
            confidence="high",
            evidence=["响应体字段与期望不同"],
            reasoning="HTTP 2xx 但内容不符",
        )
        self.error = error
        self.delay_s = delay_s
        self.calls: list[FailureContext] = []

    async def attribute(self, ctx: FailureContext) -> FallbackAttribution:
        self.calls.append(ctx)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.result


def _ctx(**overrides: Any) -> FailureContext:
    base: dict[str, Any] = {
        "status": "failed",
        "http_status": 200,
        "error_kind": None,
        "response_body": '{"ok": false}',
        "truncated": False,
        "assertion_outcomes": (
            {"index": 0, "type": "json_path", "outcome": "failed", "expected": "a", "actual": "b"},
        ),
        "case_confidence": "high",
        "responses_schema": None,
    }
    base.update(overrides)
    return FailureContext(**base)


def _item(**overrides: Any) -> FailedCase:
    return FailedCase(case_id=uuid.uuid4(), context=_ctx(**overrides))


async def test_rule_hit_never_calls_the_model() -> None:
    """能用规则判定的绝不调模型（LA §2.1）——这是成本与确定性的共同来源。"""
    stub = _StubAttributor()
    analyzer = LogAnalyzer(stub)
    item = FailedCase(
        case_id=uuid.uuid4(),
        context=_ctx(http_status=503),
    )

    outcome = await analyzer.analyze([item])

    assert stub.calls == []
    decision = outcome.decisions[item.case_id]
    assert decision.method == "rule"
    assert decision.category is AttributionCategory.SERVER_ERROR
    assert decision.rule_ref == "status.server_error"
    assert decision.evidence["http_status"] == 503


async def test_rule_miss_goes_to_the_model() -> None:
    stub = _StubAttributor()
    analyzer = LogAnalyzer(stub)
    item = _item()

    outcome = await analyzer.analyze([item])

    assert len(stub.calls) == 1
    decision = outcome.decisions[item.case_id]
    assert decision.method == "llm"
    assert decision.llm_model == "stub-model"
    assert decision.category is AttributionCategory.ASSERTION_MISMATCH
    assert decision.evidence["signals"] == ["响应体字段与期望不同"]


async def test_without_a_model_every_miss_becomes_unknown() -> None:
    """没有 LLM 时也不能让失败"没有归因"——`unknown` 至少是可见的缺陷信号。"""
    analyzer = LogAnalyzer(None)
    item = _item()

    outcome = await analyzer.analyze([item])

    decision = outcome.decisions[item.case_id]
    assert decision.category is AttributionCategory.UNKNOWN
    assert decision.confidence == "low"
    assert decision.evidence["reason"] == "no_rule_matched_and_no_llm_configured"


async def test_model_failure_becomes_unknown_and_a_node_failure() -> None:
    """LLM 调用失败不能中断整个 N6，也不能让这条失败凭空消失。"""
    stub = _StubAttributor(error=RuntimeError("boom"))
    analyzer = LogAnalyzer(stub)
    item = _item()

    outcome = await analyzer.analyze([item])

    decision = outcome.decisions[item.case_id]
    assert decision.category is AttributionCategory.UNKNOWN
    assert decision.method == "llm"
    assert decision.confidence == "low"
    assert "RuntimeError" in decision.evidence["reason"]
    assert len(outcome.failures) == 1
    assert outcome.failures[0].kind == "llm_attribution_failed"


async def test_assertion_mismatch_confidence_is_capped_by_case_confidence() -> None:
    """LA3：低置信度用例的断言失败更可能是用例本身的问题。"""
    analyzer = LogAnalyzer(_StubAttributor())
    low = _item(case_confidence="low")
    high = _item(case_confidence="high")

    outcome = await analyzer.analyze([low, high])

    assert outcome.decisions[low.case_id].confidence == "low"
    assert outcome.decisions[high.case_id].confidence == "medium"


async def test_truncated_response_caps_confidence_at_medium() -> None:
    """模型看不到被截掉的部分，"字段缺失"可能只是"没看到"（DM6）。"""
    analyzer = LogAnalyzer(_StubAttributor())
    item = _item(truncated=True, case_confidence="high")

    outcome = await analyzer.analyze([item])

    assert outcome.decisions[item.case_id].confidence == "medium"


async def test_budget_exhaustion_still_yields_a_decision() -> None:
    """超预算的用例落 `unknown`，并整体记一条 NodeFailure（不逐条刷屏）。"""
    analyzer = LogAnalyzer(_StubAttributor(delay_s=5.0), llm_budget_s=0.01)
    items = [_item(), _item()]

    outcome = await analyzer.analyze(items)

    assert set(outcome.decisions) == {item.case_id for item in items}
    assert all(
        decision.category is AttributionCategory.UNKNOWN for decision in outcome.decisions.values()
    )
    assert len(outcome.failures) == 1
    assert outcome.failures[0].kind == "attribution_deadline_exceeded"


async def test_every_input_gets_exactly_one_decision() -> None:
    """输入 N 条，输出必须 N 条。少一条就是"失败原因被悄悄吞掉"。"""
    analyzer = LogAnalyzer(_StubAttributor())
    items = [
        _item(),
        FailedCase(case_id=uuid.uuid4(), context=_ctx(http_status=500)),
        _item(case_confidence="low"),
    ]

    outcome = await analyzer.analyze(items)

    assert set(outcome.decisions) == {item.case_id for item in items}
    assert len(outcome.decisions) == len(items)


async def test_empty_input_is_a_noop() -> None:
    analyzer = LogAnalyzer(_StubAttributor())
    outcome = await analyzer.analyze([])
    assert outcome.decisions == {}
    assert outcome.failures == ()


@pytest.mark.parametrize("truncated", [False, True])
async def test_unknown_is_always_low_confidence(truncated: bool) -> None:
    stub = _StubAttributor(
        result=FallbackAttribution(
            category=AttributionCategory.UNKNOWN,
            confidence="high",
            evidence=[],
            reasoning="信息不足",
        )
    )
    analyzer = LogAnalyzer(stub)
    item = _item(truncated=truncated)

    outcome = await analyzer.analyze([item])

    assert outcome.decisions[item.case_id].confidence == "low"
