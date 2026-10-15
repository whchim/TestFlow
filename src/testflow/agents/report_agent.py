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

## M1 的范围

路线图：**最小模板，只出通过率与失败清单**。归因分组、维度覆盖、
置信度分布属于 M3——而 `ReportContent` 的结构已经留好了位置。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from testflow.domain.report import (
    ExecutionSummary,
    FailureItem,
    ReportContent,
    SkippedItem,
    pass_rate,
)
from testflow.observability.logging import get_logger
from testflow.storage.models import Report
from testflow.storage.repositories import cases as cases_repo
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
    for row in results:
        by_status[row.status] = by_status.get(row.status, 0) + 1

    total = cases_repo.count_by_run(session, run_id)
    executed = len(results)

    return ExecutionSummary(
        total_cases=total,
        executed=executed,
        passed=by_status.get("passed", 0),
        failed=by_status.get("failed", 0),
        timeout=by_status.get("timeout", 0),
        error=by_status.get("error", 0),
        # 跳过的 = 总数 − 已执行。M1 里主要是破坏性用例（TE3）。
        # **这个数必须算出来并呈现**，因为"没测什么"和"测了都过了"
        # 是两份含义完全不同的报告（RA2）。
        skipped=max(total - executed, 0),
    )


def build_report(session: Session, run_id: uuid.UUID) -> Report:
    """构造并落库报告。**幂等**——同一个 Run 重复调用会覆盖同一行（`run_id` 有 UNIQUE）。"""
    run = runs_repo.get(session, run_id)
    if run is None:
        raise ValueError(f"Run 不存在：{run_id}")

    summary = summarize(session, run_id)
    content = ReportContent(
        run_id=str(run_id),
        spec_fingerprint=run.spec_fingerprint,
        summary=summary,
        failures=_failures(session, run_id),
        skipped=_skipped(session, run_id),
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


def _failures(session: Session, run_id: uuid.UUID) -> list[FailureItem]:
    """失败清单。M1 不含归因——`category` 由 M3 填。

    把超时与请求层错误也列进来：它们是**测试结论**（TE7），不是系统故障，
    而用户最想先看到的就是"哪些没通过"。
    """
    items: list[FailureItem] = []
    for row in results_repo.list_by_run(session, run_id):
        if row.status == "passed":
            continue
        case = cases_repo.get(session, row.case_id)
        items.append(
            FailureItem(
                case_id=str(row.case_id),
                case_name=case.name if case else "(用例已删除)",
                dimension=case.dimension if case else "",
                status=row.status,
                http_status=row.http_status,
                expected=_first_expectation(row.assertion_outcomes),
                actual=_first_mismatch(row.assertion_outcomes),
            )
        )
    # 稳定排序：报告两次生成的内容应当逐字一致，否则 diff 全是噪音。
    items.sort(key=lambda item: (item.status, item.case_name))
    return items


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


def _skipped(session: Session, run_id: uuid.UUID) -> list[SkippedItem]:
    """未执行的用例。**RA2 要求报告显式列出"没测什么"。**

    不列出来的后果是报告看起来"全绿"，而实际上有一类用例从未运行——
    那比没有报告更危险。
    """
    executed = {row.case_id for row in results_repo.list_by_run(session, run_id)}
    skipped: list[SkippedItem] = []
    for case in cases_repo.list_by_run(session, run_id):
        if case.id in executed:
            continue
        skipped.append(
            SkippedItem(
                case_id=str(case.id),
                case_name=case.name,
                reason="destructive_not_authorized" if case.is_destructive else "not_settled",
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
