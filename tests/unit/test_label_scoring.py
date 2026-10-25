"""人工标注的用例准确率：汇总与校验。

对应决策：`09-测试策略.md` §5.4。

这些测试守住两条容易出错的统计口径：

* **未标注与跳过都不进分母**——把未标注当成正确会虚高，当成错误会虚低
* **用例准确率与断言准确率分开算**——一条用例可能只有一个断言写错
"""

from __future__ import annotations

import pytest

from testflow.eval.labels import score_labels, validate_labels


def _case(
    case_id: str,
    *,
    dimension: str = "value_valid",
    verdict: str | None = None,
    assertions: int = 1,
    assertion_verdicts: list[str | None] | None = None,
) -> dict:
    payload = {
        "id": case_id,
        "dimension": dimension,
        "assertions": [{"type": "status_code", "expected": 200}] * assertions,
        "verdict": verdict,
    }
    if assertion_verdicts is not None:
        payload["assertion_verdicts"] = assertion_verdicts
    return payload


def _sheet(*operations: tuple[str, str, list[dict]]) -> dict:
    return {
        "operations": [
            {"method": method, "path": path, "cases": cases} for method, path, cases in operations
        ]
    }


def test_all_correct_is_one_hundred_percent() -> None:
    sheet = _sheet(
        ("GET", "/a", [_case("a1", verdict="correct"), _case("a2", verdict="correct")]),
    )

    score = score_labels(sheet)

    assert score.case_accuracy == 1.0
    assert score.overall.correct == 2
    assert score.overall.labeled == 2


def test_unlabeled_and_skipped_are_not_in_the_denominator() -> None:
    sheet = _sheet(
        (
            "GET",
            "/a",
            [
                _case("a1", verdict="correct"),
                _case("a2", verdict="incorrect"),
                _case("a3", verdict="skip"),
                _case("a4", verdict=None),
            ],
        )
    )

    score = score_labels(sheet)

    assert score.total == 4
    assert score.overall.labeled == 2
    assert score.overall.skipped == 1
    assert score.overall.unlabeled == 1
    assert score.case_accuracy == 0.5


def test_breakdown_by_operation_and_dimension() -> None:
    sheet = _sheet(
        (
            "GET",
            "/a",
            [
                _case("a1", dimension="value_valid", verdict="correct"),
                _case("a2", dimension="not_found", verdict="incorrect"),
            ],
        ),
        (
            "POST",
            "/b",
            [_case("b1", dimension="value_valid", verdict="correct")],
        ),
    )

    score = score_labels(sheet)

    assert score.by_operation["GET /a"].accuracy == 0.5
    assert score.by_operation["POST /b"].accuracy == 1.0
    assert score.by_dimension["value_valid"].accuracy == 1.0
    assert score.by_dimension["not_found"].accuracy == 0.0


def test_assertion_accuracy_is_computed_only_over_labeled_assertions() -> None:
    sheet = _sheet(
        (
            "POST",
            "/a",
            [
                _case(
                    "a1",
                    assertions=3,
                    verdict="incorrect",
                    assertion_verdicts=["correct", "incorrect", None],
                )
            ],
        )
    )

    score = score_labels(sheet)

    assert score.assertion_labeled == 2
    assert score.assertion_correct == 1
    assert score.assertion_accuracy == 0.5


def test_no_assertion_labels_means_unknown_not_zero() -> None:
    sheet = _sheet(("GET", "/a", [_case("a1", verdict="correct")]))

    score = score_labels(sheet)

    assert score.assertion_accuracy is None


def test_invalid_verdict_is_reported() -> None:
    sheet = _sheet(("GET", "/a", [_case("a1", verdict="maybe")]))

    problems = validate_labels(sheet)

    assert any("verdict 只能是" in problem for problem in problems)
    with pytest.raises(ValueError, match="标注表不合法"):
        score_labels(sheet)


def test_duplicate_case_id_is_reported() -> None:
    sheet = _sheet(
        ("GET", "/a", [_case("same", verdict="correct")]),
        ("POST", "/b", [_case("same", verdict="correct")]),
    )

    assert any("id 重复" in problem for problem in validate_labels(sheet))


def test_assertion_verdict_length_must_match_assertions() -> None:
    sheet = _sheet(
        (
            "GET",
            "/a",
            [_case("a1", assertions=2, verdict="correct", assertion_verdicts=["correct"])],
        )
    )

    assert any("长度与 assertions 不一致" in problem for problem in validate_labels(sheet))
