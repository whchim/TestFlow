"""M3 验收：路线图的三条检查 + 归因链路。

路线图 M3 的验收标准（`11-开发路线图.md`）：

1. 规则覆盖率**有数字**（被规则归因 / 总失败）
2. 手工让一条用例超时 → 报告显式标注未完成，且**不稀释通过率**
3. 报告含"未执行的破坏性用例"章节

再加三条本里程碑必须成立的性质：

4. 每条失败都有且只有一条归因
5. 低置信度用例的 `assertion_mismatch` 带显式提示（LA3）
6. 补记的超时既不归因、也不进通过率分母
"""

from __future__ import annotations

import pytest

from testflow.agents.log_analyzer import LlmAttributor, LogAnalyzer
from testflow.agents.report_agent import build_report
from testflow.graph.nodes import analyze_failures
from testflow.storage.models import CaseResult
from testflow.storage.repositories import attributions as attributions_repo
from testflow.testing.fake_llm import FakeLLMProvider

pytestmark = pytest.mark.integration


def _result(
    session,
    run,
    case,
    *,
    status: str = "failed",
    http_status: int | None = 500,
    error_kind: str | None = None,
    outcomes: list[dict] | None = None,
) -> CaseResult:
    row = CaseResult(
        run_id=run.id,
        case_id=case.id,
        status=status,
        http_status=http_status,
        error_kind=error_kind,
        assertion_outcomes=outcomes or [],
    )
    session.add(row)
    session.flush()
    return row


def _seed(session, make_spec, make_operation, make_run, make_case):
    spec = make_spec()
    op = make_operation(spec, method="GET", path="/ok")
    run = make_run(spec)

    def case(name: str, **overrides):
        return make_case(run, op, name=name, **overrides)

    passed = case("通过")
    bad_request = case("400")
    unauthorized = case("401")
    not_found = case("404")
    conflict = case("409")
    rate_limited = case("429")
    server = case("500")
    schema = case("schema 断言失败")
    mismatch = case("内容断言失败", confidence="low")
    genuine_timeout = case("真超时")
    stalled = case("补记超时")
    destructive = case("破坏性用例", is_destructive=True)
    rejected = case("被拒绝", review_status="rejected")

    _result(session, run, passed, status="passed", http_status=200)
    _result(session, run, bad_request, http_status=400)
    _result(session, run, unauthorized, http_status=401)
    _result(session, run, not_found, http_status=404)
    _result(session, run, conflict, http_status=409)
    _result(session, run, rate_limited, http_status=429)
    _result(session, run, server, http_status=500)
    _result(
        session,
        run,
        schema,
        http_status=200,
        outcomes=[{"index": 0, "type": "schema_valid", "outcome": "failed"}],
    )
    _result(
        session,
        run,
        mismatch,
        http_status=200,
        outcomes=[
            {"index": 0, "type": "json_path", "outcome": "failed", "expected": "a", "actual": "b"}
        ],
    )
    _result(session, run, genuine_timeout, status="timeout", http_status=None, error_kind="timeout")
    _result(
        session,
        run,
        stalled,
        status="timeout",
        http_status=None,
        error_kind="executor_stall",
    )
    # 破坏性用例与被拒绝的用例**没有结果行**——它们从未被执行。
    return run, {
        "passed": passed,
        "schema": schema,
        "mismatch": mismatch,
        "stalled": stalled,
        "destructive": destructive,
        "rejected": rejected,
    }


async def test_m3_acceptance(session, make_spec, make_operation, make_run, make_case) -> None:
    run, cases = _seed(session, make_spec, make_operation, make_run, make_case)

    analyzer = LogAnalyzer(LlmAttributor(FakeLLMProvider()))
    await analyze_failures({"run_id": str(run.id)}, session=session, analyzer=analyzer)
    report = build_report(session, run.id)
    content = report.content

    rows = attributions_repo.list_by_run(session, run.id)

    # ① 每条失败有且只有一条归因；补记的超时没有。
    assert len(rows) == 9
    assert cases["stalled"].id not in {row.case_id for row in rows}
    assert cases["passed"].id not in {row.case_id for row in rows}

    # ② 规则覆盖率有数字：9 条失败里 8 条走规则。
    assert content["rule_coverage"] == pytest.approx(8 / 9)
    assert content["llm_calls"] == 1

    # ③ 未完成不进通过率分母：1 通过 + 9 失败 = 10 条已执行。
    summary = content["summary"]
    assert summary["unfinished"] == 1
    assert summary["executed"] == 10
    assert summary["timeout"] == 1, "补记的超时不应算进 timeout"

    # ④ 责任分组齐全，且总数等于失败数。
    groups = content["attribution_groups"]
    assert groups["请求侧"] == 1
    assert groups["凭证侧"] == 1
    assert groups["测试资产侧"] == 2
    assert groups["环境侧"] == 2
    assert groups["服务侧"] == 2
    assert groups["需人工判断"] == 1
    assert groups["平台策略侧"] == 0
    assert groups["未归类"] == 0
    assert sum(groups.values()) == 9

    # ⑤ 低置信度 assertion_mismatch 带显式提示（LA3）。
    low = next(item for item in content["failures"] if item["case_id"] == str(cases["mismatch"].id))
    assert low["category"] == "assertion_mismatch"
    assert low["method"] == "llm"
    assert low["confidence"] == "low"
    assert "可能是用例本身的问题" in low["hint"]

    # ⑥ 报告渲染出三个必须可见的章节。
    rendered = report.rendered
    assert "未执行的破坏性用例" in rendered
    assert cases["destructive"].name in rendered
    assert "未完成的用例" in rendered
    assert cases["stalled"].name in rendered
    assert "规则覆盖率" in rendered
    assert "10.0%" in rendered, "通过率分母是 10，且未完成没有稀释它"
