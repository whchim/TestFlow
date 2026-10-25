"""人工标注的用例准确率：读取标注表、校验、汇总。

对应决策：`09-测试策略.md` §5.4（指标的来源不同）。

## 为什么这个指标要单独存在

另外几个数字都**不能**回答"用例写得对不对"：

| 指标 | 回答的问题 |
| :--- | :--- |
| L3 首次通过率 | 模型第一次输出是否通过结构 / 覆盖检查 |
| 生成采纳率 | 人愿不愿意用（demo 里是脚本自动批准，不算） |
| 规则覆盖率 | 失败归因里有多少由规则直接判定 |

"审核准确率"要回答的是：

> 生成出来的每一条用例，请求和断言**本身对不对**？

这需要一个人逐条看，并对照 OpenAPI 文档。所以它是**人工标注**指标，
不是运行时自动算出来的。

## 标注约定

每一条用例填一个 `verdict`：

* `correct` —— 请求构造合理，断言与 Spec 一致，能验证该维度
* `incorrect` —— 请求取值、期望状态码或断言有错
* `skip` —— 无法判断（例如 Spec 信息不足），请写 `note`
* 空（`null`）—— 还没标

准确率只统计 `correct + incorrect`，`skip` 与未标注都不进分母。
**不把未标注当成正确，也不把它当成错误**——两种都会让数字失真。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["LabelScore", "score_labels", "validate_labels", "VERDICTS"]

VERDICTS = frozenset({"correct", "incorrect", "skip"})


@dataclass(frozen=True, slots=True)
class Bucket:
    """按接口或维度聚合的一格。"""

    correct: int = 0
    incorrect: int = 0
    skipped: int = 0
    unlabeled: int = 0

    @property
    def labeled(self) -> int:
        return self.correct + self.incorrect

    @property
    def accuracy(self) -> float | None:
        return (self.correct / self.labeled) if self.labeled else None

    def add(self, verdict: str | None) -> Bucket:
        if verdict == "correct":
            return Bucket(self.correct + 1, self.incorrect, self.skipped, self.unlabeled)
        if verdict == "incorrect":
            return Bucket(self.correct, self.incorrect + 1, self.skipped, self.unlabeled)
        if verdict == "skip":
            return Bucket(self.correct, self.incorrect, self.skipped + 1, self.unlabeled)
        return Bucket(self.correct, self.incorrect, self.skipped, self.unlabeled + 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "incorrect": self.incorrect,
            "skipped": self.skipped,
            "unlabeled": self.unlabeled,
            "accuracy": self.accuracy,
        }


@dataclass(frozen=True, slots=True)
class LabelScore:
    total: int
    overall: Bucket
    assertion_labeled: int
    assertion_correct: int
    by_operation: dict[str, Bucket] = field(default_factory=dict)
    by_dimension: dict[str, Bucket] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    @property
    def case_accuracy(self) -> float | None:
        return self.overall.accuracy

    @property
    def assertion_accuracy(self) -> float | None:
        if not self.assertion_labeled:
            return None
        return self.assertion_correct / self.assertion_labeled

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total,
            "case_accuracy": self.case_accuracy,
            "cases": self.overall.as_dict(),
            "assertion_accuracy": self.assertion_accuracy,
            "assertions": {
                "labeled": self.assertion_labeled,
                "correct": self.assertion_correct,
            },
            "by_operation": {key: bucket.as_dict() for key, bucket in self.by_operation.items()},
            "by_dimension": {key: bucket.as_dict() for key, bucket in self.by_dimension.items()},
            "generation_failures": list(self.failures),
        }


def validate_labels(sheet: dict[str, Any]) -> list[str]:
    """校验标注表。返回问题清单，空表示合法。

    **不抛异常**：调用方通常想把所有问题一次列给用户，而不是修一个报一个。
    """
    problems: list[str] = []
    operations = sheet.get("operations")
    if not isinstance(operations, list):
        return ["标注表缺少 operations 数组"]

    seen: set[str] = set()
    for op_index, operation in enumerate(operations):
        cases = operation.get("cases") or []
        if not isinstance(cases, list):
            problems.append(f"operations[{op_index}].cases 不是数组")
            continue
        for case_index, case in enumerate(cases):
            where = f"operations[{op_index}].cases[{case_index}]"
            case_id = case.get("id")
            if not isinstance(case_id, str) or not case_id:
                problems.append(f"{where} 缺少 id")
            elif case_id in seen:
                problems.append(f"{where} 的 id 重复：{case_id}")
            else:
                seen.add(case_id)

            verdict = case.get("verdict")
            if verdict is not None and verdict not in VERDICTS:
                problems.append(
                    f"{where}.verdict 只能是 {sorted(VERDICTS)} 或 null，收到 {verdict!r}"
                )

            assertion_verdicts = case.get("assertion_verdicts") or []
            if assertion_verdicts:
                if not isinstance(assertion_verdicts, list):
                    problems.append(f"{where}.assertion_verdicts 不是数组")
                    continue
                if len(assertion_verdicts) != len(case.get("assertions") or []):
                    problems.append(f"{where}.assertion_verdicts 长度与 assertions 不一致")
                for item in assertion_verdicts:
                    if item is not None and item not in {"correct", "incorrect"}:
                        problems.append(
                            f"{where}.assertion_verdicts 只能填 correct / incorrect / null"
                        )
                        break
    return problems


def score_labels(sheet: dict[str, Any]) -> LabelScore:
    """汇总标注结果。标注非法时抛 `ValueError`（带着全部问题）。"""
    problems = validate_labels(sheet)
    if problems:
        raise ValueError("标注表不合法：\n" + "\n".join(f"- {p}" for p in problems))

    overall = Bucket()
    by_operation: dict[str, Bucket] = {}
    by_dimension: dict[str, Bucket] = {}
    assertion_labeled = 0
    assertion_correct = 0
    total = 0

    for operation in sheet.get("operations") or []:
        op_key = f"{operation.get('method')} {operation.get('path')}"
        for case in operation.get("cases") or []:
            total += 1
            verdict = case.get("verdict")
            overall = overall.add(verdict)
            by_operation[op_key] = by_operation.get(op_key, Bucket()).add(verdict)

            dimension = str(case.get("dimension") or "(unknown)")
            by_dimension[dimension] = by_dimension.get(dimension, Bucket()).add(verdict)

            for item in case.get("assertion_verdicts") or []:
                if item == "correct":
                    assertion_labeled += 1
                    assertion_correct += 1
                elif item == "incorrect":
                    assertion_labeled += 1

    return LabelScore(
        total=total,
        overall=overall,
        assertion_labeled=assertion_labeled,
        assertion_correct=assertion_correct,
        by_operation=by_operation,
        by_dimension=by_dimension,
        failures=tuple(sheet.get("generation_failures") or ()),
    )
