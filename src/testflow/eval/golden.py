"""Golden Set 的第一层比较：**人工标注的维度清单 vs 阶段 A 的派生结果**。

对应决策：`09-测试策略.md` §5.2（覆盖率的两个层次）、§5.3（Golden Set 设计）。

## 为什么这一段要单独成为模块

它没有任何外部依赖：不连数据库、不调模型、不读环境变量。所以
`tests/unit/test_golden_set.py` 可以在 CI 的普通单测步骤里跑它——
**每次改派生算法都会立刻和人工基准比对**，而不是等到有人想起来手动跑 L3。

第二层（真实生成）留在 `scripts/eval_golden.py` 里：它花钱、有方差，
按 EN8 手动触发。

## 比较是集合语义

`expected_dimensions` 的顺序不重要，**多一个和少一个都算不一致**：

* **少**（`missing`）是算法盲区——该派的没派，而生产覆盖率永远发现不了
* **多**（`extra`）是过度派生——用例数虚高、成本上涨，同样需要解释

只检查"少的"会让算法可以通过"什么都派"来作弊。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation
from testflow.agents.spec_parser import ParsedOperation

__all__ = ["OperationDiff", "evaluate_derivation", "summarize_derivation"]


@dataclass(frozen=True, slots=True)
class OperationDiff:
    """一个接口的比对结果。"""

    method: str
    path: str
    expected: list[str]
    derived: list[str]
    missing: list[str]
    extra: list[str]
    declared_4xx: list[int]
    status_missing: list[int]
    pure_rule_expected: bool
    pure_rule_actual: bool
    unresolved_ref: bool

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.status_missing) and (
            self.pure_rule_expected == self.pure_rule_actual
        )


def _specs(operation: ParsedOperation):
    return derive_dimensions(
        operation_path=operation.path,
        parameters_schema=operation.parameters_schema,
        request_body_schema=operation.request_body_schema,
        responses_schema=operation.responses_schema,
    )


def evaluate_derivation(
    operations: list[ParsedOperation], annotations: dict[str, Any]
) -> list[OperationDiff]:
    """按标注逐接口比较。标注里出现但文档里没有的接口算**全部缺失**。"""
    by_key = {(op.method, op.path): op for op in operations}
    diffs: list[OperationDiff] = []

    for row in annotations["operations"]:
        method, path = row["method"], row["path"]
        expected_names = list(row["expected_dimensions"])
        operation = by_key.get((method, path))
        if operation is None:
            diffs.append(
                OperationDiff(
                    method=method,
                    path=path,
                    expected=sorted(expected_names),
                    derived=[],
                    missing=sorted(expected_names),
                    extra=[],
                    declared_4xx=[],
                    status_missing=[],
                    pure_rule_expected=bool(row.get("pure_rule", False)),
                    pure_rule_actual=False,
                    unresolved_ref=False,
                )
            )
            continue

        specs = _specs(operation)
        derived = [spec.dimension for spec in specs]
        derived_set = set(derived)
        expected = set(expected_names)

        declared = [int(code) for code in row.get("expected_client_error_codes", [])]
        covered_codes = {code for spec in specs for code in spec.expected_status}

        diffs.append(
            OperationDiff(
                method=operation.method,
                path=operation.path,
                expected=sorted(expected),
                derived=derived,
                missing=sorted(expected - derived_set),
                extra=sorted(derived_set - expected),
                declared_4xx=declared,
                status_missing=[code for code in declared if code not in covered_codes],
                pure_rule_expected=bool(row.get("pure_rule", False)),
                pure_rule_actual=is_pure_rule_operation(
                    operation.parameters_schema,
                    operation.request_body_schema,
                    operation.responses_schema,
                ),
                unresolved_ref=operation.has_unresolved_ref,
            )
        )
    return diffs


def summarize_derivation(diffs: list[OperationDiff]) -> dict[str, Any]:
    """聚合指标。**覆盖率的分母是人工标注**，不是派生结果。"""
    expected_total = sum(len(diff.expected) for diff in diffs)
    matched = sum(len(set(diff.expected) & set(diff.derived)) for diff in diffs)
    return {
        "operations": len(diffs),
        "expected_dimensions": expected_total,
        "matched_dimensions": matched,
        "coverage": (matched / expected_total) if expected_total else None,
        "operations_ok": sum(1 for diff in diffs if diff.ok),
        "missing": {f"{d.method} {d.path}": d.missing for d in diffs if d.missing},
        "extra": {f"{d.method} {d.path}": d.extra for d in diffs if d.extra},
        "status_missing": {
            f"{d.method} {d.path}": d.status_missing for d in diffs if d.status_missing
        },
        "pure_rule_mismatch": [
            f"{d.method} {d.path}" for d in diffs if d.pure_rule_expected != d.pure_rule_actual
        ],
        "unresolved_refs": [f"{d.method} {d.path}" for d in diffs if d.unresolved_ref],
    }
