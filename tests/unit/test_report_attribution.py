"""报告的归因聚合：责任分组、规则覆盖率、低置信度提示、渲染。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from testflow.agents.report_agent import (
    _attribution_groups,
    _attribution_hint,
    _rule_coverage,
    render_markdown,
)
from testflow.domain.attribution import RESPONSIBILITY_GROUPS
from testflow.domain.report import (
    ExecutionSummary,
    FailureItem,
    ReportContent,
    SkippedItem,
)
from testflow.storage.models import Attribution


def _attribution(
    *,
    category: str = "server_error",
    method: str = "rule",
    confidence: str | None = "high",
    human_verdict: str | None = None,
) -> Attribution:
    """内存里的归因行——不落库，够 `_attribution_groups` 用。"""
    return Attribution(
        run_id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        category=category,
        method=method,
        confidence=confidence,
        human_verdict=human_verdict,
    )


def _content(**overrides) -> ReportContent:
    base = {
        "run_id": str(uuid.uuid4()),
        "spec_fingerprint": "f" * 64,
        "summary": ExecutionSummary(
            total_cases=4,
            executed=3,
            passed=1,
            failed=1,
            timeout=0,
            error=1,
            skipped=1,
            unfinished=1,
        ),
    }
    base.update(overrides)
    return ReportContent(**base)


# ─────────────────────────────────────────────── 责任分组


def test_all_groups_are_present_even_at_zero() -> None:
    """ "环境侧 0 条"是信息：它告诉用户不用去查网络（RA1）。"""
    groups = _attribution_groups([_attribution(category="server_error")])

    assert set(groups) == set(RESPONSIBILITY_GROUPS)
    assert groups["服务侧"] == 1
    assert groups["环境侧"] == 0
    assert groups["未归类"] == 0


def test_group_order_follows_the_canonical_mapping() -> None:
    groups = _attribution_groups([])
    assert list(groups) == list(RESPONSIBILITY_GROUPS)


def test_assertion_mismatch_lands_in_the_manual_group() -> None:
    """它天然二义，假装能自动归到某一侧等于把不确定性藏起来。"""
    groups = _attribution_groups([_attribution(category="assertion_mismatch")])
    assert groups["需人工判断"] == 1


# ─────────────────────────────────────────────── 规则覆盖率


def test_rule_coverage_uses_the_failure_count_not_the_row_count() -> None:
    """分母必须是 `case_results` 统计出的失败数。

    用归因行数当分母，"分析器漏了一条"会变成"覆盖率 100%"——
    而那恰好是这套指标要发现的问题。
    """
    rows = [_attribution(method="rule"), _attribution(method="llm")]
    # 2 条归因行，但实际有 4 条失败：覆盖率是 1/4，不是 1/2。
    assert _rule_coverage(rows, total_failures=4) == 0.25


def test_rule_coverage_is_none_when_nothing_failed() -> None:
    """ "没失败"与"规则一条都没覆盖"是两件事。"""
    assert _rule_coverage([], total_failures=0) is None


def test_rule_coverage_is_zero_when_everything_went_to_the_model() -> None:
    rows = [_attribution(method="llm"), _attribution(method="llm")]
    assert _rule_coverage(rows, total_failures=2) == 0.0


# ─────────────────────────────────────────────── 低置信度提示


def test_low_confidence_assertion_mismatch_gets_the_hint() -> None:
    row = _attribution(category="assertion_mismatch")
    case = SimpleNamespace(confidence="low")

    assert _attribution_hint(row, case) == "该用例的断言为推断所得，失败可能是用例本身的问题"


def test_high_confidence_assertion_mismatch_has_no_hint() -> None:
    row = _attribution(category="assertion_mismatch")
    assert _attribution_hint(row, SimpleNamespace(confidence="high")) is None


def test_other_categories_never_get_the_hint() -> None:
    row = _attribution(category="server_error")
    assert _attribution_hint(row, SimpleNamespace(confidence="low")) is None


def test_missing_attribution_has_no_hint() -> None:
    assert _attribution_hint(None, SimpleNamespace(confidence="low")) is None


# ─────────────────────────────────────────────── 渲染


def test_rendered_report_contains_every_responsibility_group() -> None:
    content = _content(
        attribution_groups=_attribution_groups([]),
        rule_coverage=0.5,
        llm_calls=1,
    )

    rendered = render_markdown(content)

    for group in RESPONSIBILITY_GROUPS:
        assert group in rendered
    assert "未完成" in rendered
    assert "未执行的破坏性用例" in rendered
    assert "50%" in rendered


def test_rendered_failure_row_carries_the_attribution() -> None:
    content = _content(
        failures=[
            FailureItem(
                case_id=str(uuid.uuid4()),
                case_name="GET /ok",
                dimension="value_valid",
                status="failed",
                http_status=500,
                category="server_error",
                confidence="high",
            )
        ]
    )

    rendered = render_markdown(content)

    assert "server_error" in rendered
    assert "high" in rendered


def test_destructive_cases_get_their_own_section() -> None:
    content = _content(
        skipped=[
            SkippedItem(
                case_id="1", case_name="DELETE /destructive", reason="destructive_not_authorized"
            ),
            SkippedItem(case_id="2", case_name="POST /echo", reason="rejected"),
        ]
    )

    rendered = render_markdown(content)

    destructive_section = rendered.split("## 未执行的破坏性用例")[1].split("##")[0]
    assert "DELETE /destructive" in destructive_section
    assert "POST /echo" not in destructive_section
