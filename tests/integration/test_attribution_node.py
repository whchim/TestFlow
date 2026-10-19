"""N6 归因节点：写入、幂等、清理、补记超时、LLM 失败兜底。

这些测试要连数据库，所以没有 Docker 时会被 `session` fixture 跳过——
**跳过和通过看起来一样绿**，所以断言都写成"行数/内容"而不是"没抛异常"。
"""

from __future__ import annotations

from typing import Any

import pytest

from testflow.agents.log_analyzer import LogAnalyzer
from testflow.domain.attribution import AnalysisOutcome, AttributionCategory, FailedCase
from testflow.graph.nodes import analyze_failures
from testflow.storage.models import CaseResult
from testflow.storage.repositories import attributions as attributions_repo


class _SpyAnalyzer:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0
        self.batch_sizes: list[int] = []

    async def analyze(self, items: list[FailedCase]) -> AnalysisOutcome:
        self.calls += 1
        self.batch_sizes.append(len(items))
        return await self.inner.analyze(items)


async def _fail(
    session,
    *,
    run,
    case,
    status: str = "failed",
    http_status: int | None = 500,
    error_kind: str | None = None,
    assertion_outcomes: list[dict[str, Any]] | None = None,
) -> CaseResult:
    row = CaseResult(
        run_id=run.id,
        case_id=case.id,
        status=status,
        http_status=http_status,
        error_kind=error_kind,
        assertion_outcomes=assertion_outcomes or [],
    )
    session.add(row)
    session.flush()
    return row


async def test_every_failure_gets_exactly_one_attribution(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    spec = make_spec()
    operation = make_operation(spec, method="GET", path="/ok")
    run = make_run(spec)
    server = make_case(run, operation, name="500")
    assertion = make_case(run, operation, name="2xx 断言不符")
    passed = make_case(run, operation, name="通过")

    await _fail(session, run=run, case=server, http_status=500)
    await _fail(
        session,
        run=run,
        case=assertion,
        http_status=200,
        assertion_outcomes=[
            {"index": 0, "type": "json_path", "outcome": "failed", "expected": "a", "actual": "b"}
        ],
    )
    await _fail(session, run=run, case=passed, status="passed", http_status=200)

    result = await analyze_failures(
        {"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None)
    )

    rows = attributions_repo.list_by_run(session, run.id)
    assert len(rows) == 2
    by_case = {row.case_id: row for row in rows}
    assert by_case[server.id].category == "server_error"
    assert by_case[server.id].method == "rule"
    assert by_case[assertion.id].category == "unknown"  # 规则不判 assertion_mismatch
    assert passed.id not in by_case
    assert len(result["attribution_ids"]) == 2


async def test_zero_failures_short_circuits_without_calling_the_analyzer(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(session, run=run, case=case, status="passed", http_status=200)

    spy = _SpyAnalyzer(LogAnalyzer(None))
    result = await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=spy)

    assert spy.calls == 0
    assert result["attribution_ids"] == []
    assert attributions_repo.list_by_run(session, run.id) == []


async def test_synthesized_stall_is_not_attributed(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    """补记的超时不是测试结论——它不该被归因成"网络故障"。"""
    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(
        session,
        run=run,
        case=case,
        status="timeout",
        http_status=None,
        error_kind="executor_stall",
    )

    spy = _SpyAnalyzer(LogAnalyzer(None))
    result = await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=spy)

    assert spy.calls == 0
    assert result["attribution_ids"] == []
    assert attributions_repo.list_by_run(session, run.id) == []


async def test_rerun_preserves_the_human_verdict(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    """重算只能覆盖机器结论，**不能抹掉人的判断**。"""
    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(session, run=run, case=case, http_status=500)

    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None))
    row = attributions_repo.list_by_run(session, run.id)[0]
    attributions_repo.set_human_verdict(
        session, row.id, verdict="disagree", human_category="server_error"
    )

    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None))

    rows = attributions_repo.list_by_run(session, run.id)
    assert len(rows) == 1
    assert rows[0].human_verdict == "disagree"
    assert rows[0].human_category == "server_error"


async def test_rerun_deletes_attribution_for_a_case_that_now_passes(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    """否则报告会显示"失败 0 条、归因 1 条"。"""
    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    result_row = await _fail(session, run=run, case=case, http_status=500)

    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None))
    assert len(attributions_repo.list_by_run(session, run.id)) == 1

    result_row.status = "passed"
    result_row.http_status = 200
    session.flush()

    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None))
    assert attributions_repo.list_by_run(session, run.id) == []


async def test_llm_failure_is_recorded_as_unknown_not_dropped(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    from testflow.agents.log_analyzer import LlmAttributor
    from testflow.testing.fake_llm import FakeLLMProvider

    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(
        session,
        run=run,
        case=case,
        http_status=200,
        assertion_outcomes=[
            {"index": 0, "type": "json_path", "outcome": "failed", "expected": "a", "actual": "b"}
        ],
    )

    analyzer = LogAnalyzer(LlmAttributor(FakeLLMProvider(fail_attribution=True)))
    result = await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=analyzer)

    rows = attributions_repo.list_by_run(session, run.id)
    assert len(rows) == 1
    assert rows[0].category == "unknown"
    assert rows[0].method == "llm"
    assert rows[0].confidence == "low"
    assert any(f.kind == "llm_attribution_failed" for f in result["node_failures"])


async def test_analyzer_that_omits_a_decision_is_reported(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    """分析器少给一条结论 = 一条失败的原因被吞掉。

    节点必须自己补 `unknown` 并记一条 NodeFailure，而不是让行数少一条。
    """

    class _DropsEverything:
        async def analyze(self, items: list[FailedCase]) -> AnalysisOutcome:
            return AnalysisOutcome(decisions={})

    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(session, run=run, case=case, http_status=500)

    result = await analyze_failures(
        {"run_id": str(run.id)}, session=session, analyzer=_DropsEverything()
    )

    rows = attributions_repo.list_by_run(session, run.id)
    assert len(rows) == 1
    assert rows[0].category == "unknown"
    assert any(f.kind == "attribution_incomplete" for f in result["node_failures"])


@pytest.mark.parametrize(
    ("http_status", "expected"),
    [
        (400, "request_invalid"),
        (401, "auth_error"),
        (404, "resource_not_found"),
        (409, "conflict"),
        (429, "rate_limited"),
        (503, "server_error"),
    ],
)
async def test_rule_catalog_end_to_end(
    session, make_spec, make_operation, make_run, make_case, http_status, expected
) -> None:
    spec = make_spec()
    operation = make_operation(spec)
    run = make_run(spec)
    case = make_case(run, operation)
    await _fail(session, run=run, case=case, http_status=http_status)

    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=LogAnalyzer(None))

    row = attributions_repo.list_by_run(session, run.id)[0]
    assert row.category == expected
    assert row.category in {c.value for c in AttributionCategory}
    assert row.evidence["http_status"] == http_status
