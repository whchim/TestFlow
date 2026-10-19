"""Golden Set 的第一层：**派生算法 vs 人工标注**。

对应决策：`09-测试策略.md` §5.2（TS5）、§5.3、`11-开发路线图.md` M5。

## 为什么这条测试值得进 CI

它检验的不是"代码是否按自己的规则运行"，而是"**规则本身漏没漏**"。
生产环境的维度覆盖率以阶段 A 的输出为分母，所以算法少派一个维度时，
覆盖率仍然是 100%（§5.2）。只有这份独立的人工标注能发现它。

零外部依赖：不连数据库、不调模型。所以它和普通单测一样便宜。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from testflow.agents.spec_parser import parse_spec
from testflow.eval.golden import evaluate_derivation, summarize_derivation

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _ROOT / "eval" / "golden" / "golden_set.yaml"
_ANNOTATIONS = _ROOT / "eval" / "golden" / "golden_set.annotations.yaml"


@pytest.fixture(scope="module")
def annotations() -> dict:
    with _ANNOTATIONS.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def parsed():
    return parse_spec(_SPEC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def diffs(parsed, annotations):
    return evaluate_derivation(parsed.operations, annotations)


def test_golden_set_is_exactly_ten_operations(parsed, annotations) -> None:
    """PRD §4.3 规定 10 个接口。数量变了要同步改文档。"""
    assert len(parsed.operations) == 10
    assert len(annotations["operations"]) == 10


def test_annotations_cover_every_operation_exactly_once(parsed, annotations) -> None:
    documented = {(op.method, op.path) for op in parsed.operations}
    annotated = {(row["method"], row["path"]) for row in annotations["operations"]}

    assert annotated == documented


def test_derivation_matches_the_manual_annotation(diffs) -> None:
    """**这条断言就是 M5 的核心验收。**

    失败时先看是"少"还是"多"：

    * 少 → 派生算法有盲区（M5 之前 `pattern`/`format`/`multipleOf` 就是这样漏的）
    * 多 → 算法凭空发明维度，用例数与成本会虚高
    """
    problems = []
    for diff in diffs:
        if diff.missing:
            problems.append(f"{diff.method} {diff.path} 缺少 {diff.missing}")
        if diff.extra:
            problems.append(f"{diff.method} {diff.path} 多出 {diff.extra}")
    assert not problems, "\n".join(problems)


def test_every_declared_4xx_is_covered_by_a_dimension(diffs) -> None:
    """L2 的独立基准：声明的 4xx 必须有维度断言它。

    分母来自人工标注里的 `expected_client_error_codes`，不是
    `declared_client_error_codes()` 自己算的结果。
    """
    missing = {
        f"{diff.method} {diff.path}": diff.status_missing for diff in diffs if diff.status_missing
    }
    assert not missing, f"以下声明过的 4xx 没有对应用例：{missing}"


def test_only_health_is_a_pure_rule_operation(diffs) -> None:
    """TG5：无参数、无请求体、无 4xx 声明——只有 `/health` 完全满足。

    如果别的接口被判成纯规则，它会**跳过 LLM**，而生成结果里只有一条
    `value_valid`，看起来却"成功"了。
    """
    pure = {f"{diff.method} {diff.path}" for diff in diffs if diff.pure_rule_actual}
    assert pure == {"GET /health"}


def test_no_operation_has_an_unresolved_ref(diffs) -> None:
    """$ref 展开必须完整——未展开会让生成方拿不到字段约束。"""
    assert [f"{diff.method} {diff.path}" for diff in diffs if diff.unresolved_ref] == []


def test_summary_reports_perfect_coverage(diffs) -> None:
    summary = summarize_derivation(diffs)

    assert summary["operations_ok"] == summary["operations"] == 10
    assert summary["coverage"] == 1.0
    assert summary["missing"] == {}
    assert summary["extra"] == {}
