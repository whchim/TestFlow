"""报告聚合与阶段判定的单元测试。

对应决策：`03/ReportAgent.md` RA3（通过率分母只含已执行用例）、
`06-Agent编排.md` G4（阶段用于展示，不用于路由）。

**为什么 `pass_rate` 值得单独测**

"通过率"看起来是一行除法，但它有两个很容易搞错、且**错了不会报错**的地方：

1. **分母**。若把未执行/被拒绝的用例也算进分母，通过率会被稀释——
   一个 100% 失败的运行可能显示成 60%，而报告看起来一切正常。
2. **零分母**。一条都没执行时返回 `0.0` 会被读成"全挂了"，
   而事实是"什么都没跑"。两者需要完全不同的处理，所以这里返回 `None`。
"""

from __future__ import annotations

import pytest

from testflow.domain.graph import NodeFailure, RunPhase
from testflow.domain.report import ExecutionSummary, pass_rate


def _summary(**kw: int) -> ExecutionSummary:
    base = {
        "total_cases": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "timeout": 0,
        "error": 0,
    }
    base.update(kw)
    return ExecutionSummary(**base)


class TestPassRateDenominator:
    """RA3：分母只含**已执行**的用例。"""

    def test_all_passed(self) -> None:
        assert pass_rate(_summary(passed=10, executed=10)) == 1.0

    def test_all_failed(self) -> None:
        assert pass_rate(_summary(failed=10, executed=10)) == 0.0

    def test_mixed(self) -> None:
        assert pass_rate(_summary(passed=3, failed=1, executed=4)) == 0.75

    @pytest.mark.parametrize("outcome", ["failed", "timeout", "error"])
    def test_timeout_and_error_count_as_not_passed(self, outcome: str) -> None:
        """超时与请求层错误也是"没通过"——它们是测试结论的一部分。

        `error` 在这里指**请求层面**失败（连接被拒、DNS、TLS），
        与断言级的 `error`（无法求值）是两回事。
        """
        s = _summary(passed=1, executed=2, **{outcome: 1})
        assert pass_rate(s) == 0.5

    def test_skipped_cases_do_not_dilute_the_rate(self) -> None:
        """**这条是 RA3 的核心。** 跳过的用例不进分母。

        若跳过的 90 条进了分母，一个真实 100% 失败的运行会显示成 10%——
        报告把"我们没跑"说成了"大部分通过了"。
        """
        s = _summary(passed=0, failed=10, skipped=90, total_cases=100, executed=10)
        assert pass_rate(s) == 0.0

    def test_unexecuted_cases_do_not_dilute_the_rate(self) -> None:
        s = _summary(passed=5, failed=5, executed=10, total_cases=1000)
        assert pass_rate(s) == 0.5


class TestPassRateZeroDenominator:
    def test_returns_none_not_zero(self) -> None:
        """ "没跑"与"全挂了"必须可区分——用 `0.0` 表示前者是错的。"""
        assert pass_rate(_summary()) is None

    def test_none_when_only_skipped(self) -> None:
        assert pass_rate(_summary(skipped=5, total_cases=5)) is None


class TestRunPhase:
    def test_values_match_the_runs_phase_check_constraint(self) -> None:
        """取值必须与 `04` §2.3 的 CHECK 约束逐字一致。

        不一致的后果不是报错，而是"某条路径上的 phase 写不进库"，
        且只在跑到那一步时才暴露。
        """
        assert {p.value for p in RunPhase} == {
            "init",
            "parsing",
            "generating",
            "awaiting_review",
            "dispatching",
            "awaiting_results",
            "analyzing",
            "reporting",
            "done",
            "failed",
            "abandoned",
        }

    @pytest.mark.parametrize("phase", [RunPhase.DONE, RunPhase.FAILED, RunPhase.ABANDONED])
    def test_terminal_phases(self, phase: RunPhase) -> None:
        assert phase.is_terminal

    @pytest.mark.parametrize(
        "phase",
        [
            RunPhase.INIT,
            RunPhase.PARSING,
            RunPhase.GENERATING,
            RunPhase.AWAITING_REVIEW,
            RunPhase.DISPATCHING,
            RunPhase.AWAITING_RESULTS,
            RunPhase.ANALYZING,
            RunPhase.REPORTING,
        ],
    )
    def test_non_terminal_phases(self, phase: RunPhase) -> None:
        """`awaiting_*` 是挂起，不是终态——兜底轮询正是靠这个区分"还活着"。"""
        assert not phase.is_terminal

    def test_str_serializes_to_the_bare_value(self) -> None:
        """`StrEnum` 而非 `(str, Enum)`：后者 `str()` 会带类名，落库即报错。"""
        assert str(RunPhase.AWAITING_RESULTS) == "awaiting_results"
        assert f"{RunPhase.DONE}" == "done"


class TestNodeFailure:
    def test_distinguishes_platform_failure_from_test_failure(self) -> None:
        """`NodeFailure` 记的是**平台**没做成某事，不是被测系统的表现。

        两者混在一起，报告就会把"我们没跑成"说成"你服务坏了"。
        """
        f = NodeFailure(node="parse_spec", kind="spec_parse_error", detail="缺少 info.title")
        assert f.node == "parse_spec"
        assert f.retryable is False
