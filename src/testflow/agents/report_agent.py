"""N7 `build_report` —— 报告构造。**不调用 LLM**（D5）。

对应决策：`03-模块详细设计/ReportAgent.md` RA1~RA5、`04-数据模型.md` §2.8。

## RA5：模板只负责呈现，不负责计算

所有聚合都在构造 `ReportContent` 时完成，模板（Jinja2）拿到的是一个**已经
算好的对象**。理由很实际：模板里的算术不可测试——它只在渲染成字符串之后
才存在，而那个字符串又长又难断言。算好了再给模板，每一步聚合都能被单测覆盖。

## RA3：通过率的分母只含**已执行**的用例

未执行、未确认、被拒绝的**不进分母**。否则"我们没跑的用例"会稀释通过率——
一个真实 100% 失败的运行可能显示成 60%，而报告看起来一切正常。

## RA4：`rendered` 是快照，不是视图

写入之后不再重渲染，模板改了也不回填。报告会被引用（评审材料、问题工单），
三个月前那份应当保持原样——**"当时看到的"和"现在查到的"必须一致**。

## 归因（M3）

失败清单带上了归因类别、方法（规则 / LLM）与置信度；另有一节按
**责任归属**分组——使用者要做的第一个决定不是"这是哪一类失败"，
而是"该去找谁"。零值的组也会列出，那本身就是信息。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from testflow.domain.attribution import (
    RESPONSIBILITY_GROUPS,
    is_synthesized_stall,
    responsibility_of,
)
from testflow.domain.report import (
    ExecutionSummary,
    FailureItem,
    LLMUsageSummary,
    ReportContent,
    SkippedItem,
    pass_rate,
)
from testflow.llm.pricing import PriceTier, estimate_cost
from testflow.observability.logging import get_logger
from testflow.observability.stats import percentile
from testflow.storage.models import Attribution, LLMUsage, Report
from testflow.storage.repositories import attributions as attributions_repo
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import llm_usage as llm_usage_repo
from testflow.storage.repositories import reports as reports_repo
from testflow.storage.repositories import results as results_repo
from testflow.storage.repositories import runs as runs_repo

__all__ = ["build_report", "render_markdown", "summarize"]

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def summarize(session: Session, run_id: uuid.UUID) -> ExecutionSummary:
    """统计各状态的用例数。

    ⚠️ 数字来自 `case_results` 的**聚合查询**，不是 `runs` 表上的派生缓存
    （DM5）。缓存会因为崩溃、重投、并发而落后——而报告是给人看的权威产物，
    它不该显示一个可能不准的数。
    """
    results = results_repo.list_by_run(session, run_id)
    by_status: dict[str, int] = {}
    unfinished = 0
    for row in results:
        # 补记的超时**不是测试结论**，单列成"未完成"。
        # 混进 `timeout` 会让通过率分母变大，把真实的失败率说小——
        # 而它本来就不该在分母里（RA3 的细化）。
        if is_synthesized_stall(row.status, row.error_kind):
            unfinished += 1
            continue
        by_status[row.status] = by_status.get(row.status, 0) + 1

    total = cases_repo.count_by_run(session, run_id)
    executed = len(results) - unfinished

    return ExecutionSummary(
        total_cases=total,
        executed=executed,
        passed=by_status.get("passed", 0),
        failed=by_status.get("failed", 0),
        timeout=by_status.get("timeout", 0),
        error=by_status.get("error", 0),
        # 跳过的 = 总数 − 有结果行的。主要是破坏性用例（TE3）与被拒绝的用例
        # （IP-1）。**这个数必须算出来并呈现**，因为"没测什么"和"测了都过了"
        # 是两份含义完全不同的报告（RA2）。
        skipped=max(total - len(results), 0),
        unfinished=unfinished,
    )


def build_report(
    session: Session,
    run_id: uuid.UUID,
    *,
    price_table: Mapping[str, Sequence[PriceTier]] | None = None,
) -> Report:
    """构造并落库报告。**幂等**——重复调用覆盖同一行（`run_id` 有 UNIQUE）。

    `price_table` 只在测试/基准里显式传；生产用 `llm/pricing.py` 的默认表。
    成本随报告一起**快照落库**，所以单价日后变化不会改写历史报告（RA4）。
    """
    run = runs_repo.get(session, run_id)
    if run is None:
        raise ValueError(f"Run 不存在：{run_id}")

    summary = summarize(session, run_id)
    attributions = attributions_repo.list_by_run(session, run_id)
    total_failures = summary.failed + summary.timeout + summary.error
    content = ReportContent(
        run_id=str(run_id),
        spec_fingerprint=run.spec_fingerprint,
        summary=summary,
        failures=_failures(session, run_id, attributions),
        skipped=_skipped(session, run_id),
        unfinished_cases=_unfinished_cases(session, run_id),
        attribution_groups=_attribution_groups(attributions),
        rule_coverage=_rule_coverage(attributions, total_failures),
        llm_calls=sum(1 for row in attributions if row.method == "llm"),
        llm_usage=_llm_usage_summary(
            session,
            run_id,
            generated_cases=summary.total_cases,
            approved_cases=cases_repo.count_by_run(session, run_id, review_status="approved"),
            price_table=price_table,
        ),
    )

    rendered = render_markdown(content)
    report = reports_repo.upsert(
        session,
        run_id=run_id,
        content=content.model_dump(mode="json"),
        rendered=rendered,
    )
    logger.info(
        "报告已生成 run=%s 通过率=%s",
        run_id,
        f"{pass_rate(summary):.1%}" if pass_rate(summary) is not None else "无已执行用例",
    )
    return report


def _failures(
    session: Session, run_id: uuid.UUID, attributions: list[Attribution]
) -> list[FailureItem]:
    """失败清单（含归因）。

    把超时与请求层错误也列进来：它们是**测试结论**（TE7），不是系统故障，
    而用户最想先看到的就是"哪些没通过"。

    **补记的超时不列**：它们不是测试结论，属于"未完成"章节。
    """
    by_case = {row.case_id: row for row in attributions}
    items: list[FailureItem] = []
    for row in results_repo.list_by_run(session, run_id):
        if row.status == "passed" or is_synthesized_stall(row.status, row.error_kind):
            continue
        case = cases_repo.get(session, row.case_id)
        attribution = by_case.get(row.case_id)
        items.append(
            FailureItem(
                case_id=str(row.case_id),
                case_name=case.name if case else "(用例已删除)",
                dimension=case.dimension if case else "",
                status=row.status,
                http_status=row.http_status,
                expected=_first_expectation(row.assertion_outcomes),
                actual=_first_mismatch(row.assertion_outcomes),
                category=attribution.category if attribution else None,
                method=attribution.method if attribution else None,
                rule_ref=attribution.rule_ref if attribution else None,
                llm_model=attribution.llm_model if attribution else None,
                confidence=attribution.confidence if attribution else None,
                evidence=attribution.evidence if attribution else None,
                human_verdict=attribution.human_verdict if attribution else None,
                human_category=attribution.human_category if attribution else None,
                hint=_attribution_hint(attribution, case),
            )
        )
    # 稳定排序：报告两次生成的内容应当逐字一致，否则 diff 全是噪音。
    items.sort(key=lambda item: (item.status, item.case_name))
    return items


def _attribution_hint(attribution: Attribution | None, case: Any) -> str | None:
    """低置信度用例的 `assertion_mismatch` 必须显式提示（LA3）。

    不提示的后果：用户看到"断言不符"，以为服务坏了，而实际上很可能是
    这条用例的断言本来就是猜的。
    """
    if attribution is None or attribution.category != "assertion_mismatch":
        return None
    if case is None or case.confidence != "low":
        return None
    return "该用例的断言为推断所得，失败可能是用例本身的问题"


def _attribution_groups(attributions: list[Attribution]) -> dict[str, int]:
    """按责任归属分组。**零值的组也返回**——"环境侧 0 条"本身是信息。"""
    groups = dict.fromkeys(RESPONSIBILITY_GROUPS, 0)
    for row in attributions:
        group = responsibility_of(row.category)
        groups[group] = groups.get(group, 0) + 1
    return groups


def _rule_coverage(attributions: list[Attribution], total_failures: int) -> float | None:
    """规则覆盖率 = 规则归因数 / 可归因失败数（LA §2.2）。

    分母用 `case_results` 统计出来的失败数，**不是归因行数**——
    用行数会让"分析器漏了一条"变成"覆盖率 100%"。
    """
    if total_failures == 0:
        return None
    rule_attributed = sum(1 for row in attributions if row.method == "rule")
    return rule_attributed / total_failures


def _llm_usage_summary(
    session: Session,
    run_id: uuid.UUID,
    *,
    generated_cases: int,
    approved_cases: int,
    price_table: Mapping[str, Sequence[PriceTier]] | None = None,
) -> LLMUsageSummary:
    """从库里取用量行，交给纯函数聚合。"""
    return summarize_usage(
        llm_usage_repo.list_by_run(session, run_id),
        generated_cases=generated_cases,
        approved_cases=approved_cases,
        price_table=price_table,
    )


def summarize_usage(
    rows: Sequence[LLMUsage],
    *,
    generated_cases: int,
    approved_cases: int,
    price_table: Mapping[str, Sequence[PriceTier]] | None = None,
) -> LLMUsageSummary:
    """把逐次用量聚合成报告里的"质量与成本"（纯函数，便于单测）。

    ## 三条"不知道就说不知道"的规则

    1. **没有调用 → 成本 0.0**（纯规则 Run 确实没花钱），不是 `None`。
    2. **有调用但某个模型没有单价 → 成本 `None`**，并列出模型名。
       给出一个"只算了部分模型"的金额，比不给更糟——它看起来是权威的。
    3. **命中率的分母是 0 → `None`**（没有输入就没有命中率），不是 0%。
    """
    acceptance = approved_cases / generated_cases if generated_cases > 0 else None

    if not rows:
        # 没有调用 = 没花钱（纯规则 Run 是正常情形）。注意这与
        # "某次调用没回 usage" 不同——那种由 `usage_missing_calls` 标记。
        return LLMUsageSummary(cost_cny=0.0, generation_acceptance_rate=acceptance)

    prompt_tokens = sum(row.prompt_tokens for row in rows)
    cached_tokens = sum(row.cached_tokens for row in rows)

    total_cost = Decimal(0)
    unpriced: set[str] = set()
    for row in rows:
        row_cost = estimate_cost(
            row.model,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            cached_tokens=row.cached_tokens,
            cache_write_tokens=row.cache_write_tokens,
            price_table=price_table,
        )
        if row_cost is None:
            unpriced.add(row.model)
        else:
            total_cost += row_cost

    return LLMUsageSummary(
        calls=len(rows),
        generation_calls=sum(1 for row in rows if row.purpose == "case_generation"),
        attribution_calls=sum(1 for row in rows if row.purpose == "attribution"),
        prompt_tokens=prompt_tokens,
        completion_tokens=sum(row.completion_tokens for row in rows),
        cached_tokens=cached_tokens,
        cache_hit_rate=(cached_tokens / prompt_tokens) if prompt_tokens > 0 else None,
        cost_cny=None if unpriced else float(total_cost),
        unpriced_models=sorted(unpriced),
        p95_latency_ms=percentile([row.elapsed_ms for row in rows], 0.95),
        generation_acceptance_rate=acceptance,
        usage_missing_calls=sum(1 for row in rows if row.total_tokens == 0),
    )


def _first_expectation(outcomes: list | None) -> str | None:
    for item in outcomes or []:
        if item.get("outcome") in ("failed", "error") and item.get("expected") is not None:
            return str(item["expected"])
    return None


def _first_mismatch(outcomes: list | None) -> str | None:
    for item in outcomes or []:
        if item.get("outcome") == "failed":
            return str(item.get("actual"))
        if item.get("outcome") == "error":
            # `error` 没有"实际值"可展示——它的含义正是"取不到值"。
            # 显示原因是这里唯一有意义的做法。
            return f"无法求值：{item.get('reason') or '未知原因'}"
    return None


def _unfinished_cases(session: Session, run_id: uuid.UUID) -> list[SkippedItem]:
    """列出补记超时的用例。**不能只给一个数字**——用户要知道是哪几条。"""
    items: list[SkippedItem] = []
    for row in results_repo.list_by_run(session, run_id):
        if not is_synthesized_stall(row.status, row.error_kind):
            continue
        case = cases_repo.get(session, row.case_id)
        items.append(
            SkippedItem(
                case_id=str(row.case_id),
                case_name=case.name if case else "(用例已删除)",
                reason="executor_stall",
            )
        )
    items.sort(key=lambda item: item.case_name)
    return items


def _skipped(session: Session, run_id: uuid.UUID) -> list[SkippedItem]:
    """未执行的用例。**RA2 要求报告显式列出"没测什么"。**

    不列出来的后果是报告看起来"全绿"，而实际上有一类用例从未运行——
    那比没有报告更危险。

    未执行分三种原因，报告里要能分清：被用户**拒绝**（IP-1）、
    破坏性用例未授权（TE3）、以及其余"尚未了结"（超时/崩溃兜底）。
    """
    executed = {row.case_id for row in results_repo.list_by_run(session, run_id)}
    skipped: list[SkippedItem] = []
    for case in cases_repo.list_by_run(session, run_id):
        if case.id in executed:
            continue
        if case.review_status == "rejected":
            reason = "rejected"
        elif case.is_destructive:
            reason = "destructive_not_authorized"
        else:
            reason = "not_settled"
        skipped.append(
            SkippedItem(
                case_id=str(case.id),
                case_name=case.name,
                reason=reason,
            )
        )
    return skipped


def render_markdown(content: ReportContent) -> str:
    """渲染成 Markdown。

    模板从文件加载而不是内联字符串：报告是给人看的产物，改排版是常事，
    而在一段 Python 字符串里改 Markdown 很难看清结果。
    """
    environment = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("report.md.j2")
    return template.render(content=content, rate=_format_rate(content.summary))


def _format_rate(summary: ExecutionSummary) -> str:
    """通过率的展示形式。

    **一条都没执行时显示"不适用"而不是 `0%`**——"没跑"和"全挂了"是两件事，
    用 0% 表示前者会让人以为后者发生了。
    """
    rate = pass_rate(summary)
    return "不适用（无已执行用例）" if rate is None else f"{rate:.1%}"
