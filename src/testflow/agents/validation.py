"""L1 / L2 校验（`03-模块详细设计/TestCaseGen.md` §5）。

## L1 与 L2 的分工

- **L1 结构层**：输出本身是否可用——非空、每条请求维度恰好一条、额外维度
  只能标 `inferred` 且不超过请求维度的一半、断言能被解析。
- **L2 覆盖率层**：该测的是不是都测了——每个请求维度都有用例、
  `responses_schema` 声明的每个 4xx 都有用例断言它。

## 为什么失败要带"哪一条不对"

重试时把原因告诉模型，比原样重发有效得多（§5.3）。"你的输出不合法"
对模型没有信息量；"缺少维度 `boundary:age`、声明的 409 没有用例"
能直接指导它修正。

## 为什么额外维度有 50% 上限

模型可以提出阶段 A 没想到的维度，但那是**补充**而不是替代。不设上限的话，
它可以给 3 个请求维度配 30 个自创维度——覆盖率的分母还是 3，而实际执行的
用例数翻了十倍，成本与执行时间都失控。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from testflow.agents.dimensions import (
    boundary_target,
    declared_client_error_codes,
    json_body_schema,
)
from testflow.domain.cases import DimensionSpec, GeneratedCase, GenerationResult

__all__ = [
    "IssueLayer",
    "ValidationIssue",
    "ValidationResult",
    "feedback_text",
    "validate_generation",
]

#: 反馈里最多列几条。太多会让提示词变长，而模型对一屏错误信息的吸收有限。
_MAX_FEEDBACK_ITEMS = 8

#: 维度名/字段名的展示上限。Spec 里的名字是用户输入，理论上是不可信文本；
#: 截断既是防止提示词注入，也是防止一条超长名字把反馈挤爆。
_NAME_LIMIT = 80


class IssueLayer(StrEnum):
    L1 = "L1"
    L2 = "L2"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    layer: IssueLayer
    code: str
    message: str
    dimension: str | None = None
    case_index: int | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_generation(
    *,
    responses_schema: dict[str, Any] | None,
    requested: Sequence[DimensionSpec],
    result: GenerationResult,
    operation_path: str = "",
    request_body_schema: dict[str, Any] | None = None,
) -> ValidationResult:
    """跑完 L1 + L2，返回全部问题（不是首个）。"""
    issues = [
        *_validate_l1(requested, result),
        *_validate_l2(responses_schema, requested, result),
        *_validate_path_params(operation_path, result),
        *_validate_schema_refs(responses_schema, result),
        *_validate_boundary_values(request_body_schema, result),
        *_validate_assertion_content(result),
    ]
    return ValidationResult(tuple(issues))


def _validate_l1(
    requested: Sequence[DimensionSpec], result: GenerationResult
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not result.cases:
        issues.append(
            ValidationIssue(
                layer=IssueLayer.L1,
                code="l1.cases_empty",
                message="返回的 cases 是空的——至少要为每个请求维度产出一条用例",
            )
        )
        return issues

    wanted = [spec.dimension for spec in requested]
    got = [case.dimension for case in result.cases]

    for name in wanted:
        count = got.count(name)
        if count == 0:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L1,
                    code="l1.dimension_missing",
                    message=f"缺少维度 `{_short(name)}`：请求了它，但返回里没有对应用例",
                    dimension=name,
                )
            )
        elif count > 1:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L1,
                    code="l1.dimension_duplicate",
                    message=f"维度 `{_short(name)}` 出现了 {count} 次；每个请求维度恰好一条",
                    dimension=name,
                )
            )

    extras = [case for case in result.cases if case.dimension not in wanted]
    for case in extras:
        if case.derived_from != "inferred":
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L1,
                    code="l1.dimension_not_requested",
                    message=(
                        f"多出未请求的维度 `{_short(case.dimension)}`："
                        "额外维度必须标 `derived_from: inferred`"
                    ),
                    dimension=case.dimension,
                )
            )

    inferred = [case for case in extras if case.derived_from == "inferred"]
    if len(inferred) * 2 > len(wanted):
        issues.append(
            ValidationIssue(
                layer=IssueLayer.L1,
                code="l1.too_many_extra_dimensions",
                message=(
                    f"额外维度 {len(inferred)} 条，超过请求维度 {len(wanted)} 条的一半；"
                    "补充维度不能替代给定维度"
                ),
            )
        )

    issues.extend(_assertion_issues(result.cases, layer=IssueLayer.L1))
    return issues


def _validate_l2(
    responses_schema: dict[str, Any] | None,
    requested: Sequence[DimensionSpec],
    result: GenerationResult,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    wanted = [spec.dimension for spec in requested]
    got = {case.dimension for case in result.cases}

    for name in wanted:
        if name not in got:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L2,
                    code="l2.dimension_uncovered",
                    message=f"请求的维度 `{_short(name)}` 没有被覆盖",
                    dimension=name,
                )
            )

    for code in declared_client_error_codes(responses_schema):
        if not any(_case_expects_status(case, code) for case in result.cases):
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L2,
                    code="l2.declared_4xx_uncovered",
                    message=(
                        f"接口声明了 {code}，但没有任何用例断言它；请补一条期望 {code} 的用例"
                    ),
                )
            )

    for index, case in enumerate(result.cases):
        if not case.assertions:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L2,
                    code="l2.case_without_assertion",
                    message=f"第 {index + 1} 条用例没有任何断言——它无法判定通过与否",
                    case_index=index,
                    dimension=case.dimension,
                )
            )
    return issues


def _assertion_issues(
    cases: Sequence[GeneratedCase], *, layer: IssueLayer
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, case in enumerate(cases):
        if not case.assertions:
            issues.append(
                ValidationIssue(
                    layer=layer,
                    code=f"{layer.value.lower()}.assertions_empty",
                    message=f"第 {index + 1} 条用例没有任何断言",
                    case_index=index,
                    dimension=case.dimension,
                )
            )
            continue
        for assertion in case.assertions:
            if assertion.type != "json_path":
                continue
            try:
                parse_jsonpath(assertion.path)
            except Exception:  # noqa: BLE001 —— 解析器的异常类型不是契约
                issues.append(
                    ValidationIssue(
                        layer=layer,
                        code=f"{layer.value.lower()}.invalid_jsonpath",
                        message=(
                            f"第 {index + 1} 条用例的 JSONPath `{_short(assertion.path)}` "
                            "不是合法表达式"
                        ),
                        case_index=index,
                        dimension=case.dimension,
                    )
                )
    return issues


def _validate_path_params(operation_path: str, result: GenerationResult) -> list[ValidationIssue]:
    """路径模板里的每个 `{变量}` 都必须有实际取值。

    只写 `/invoices/{invoice_id}` 的请求是**不可执行**的：它会被原样发出去，
    既验证不了有效资源，也验证不了 `not_found`。这是 M5 人工标注时发现的
    系统性问题——用例模型原本没有承载路径参数的字段。
    """
    placeholders = re.findall(r"\{([^{}]+)\}", operation_path or "")
    if not placeholders:
        return []

    issues: list[ValidationIssue] = []
    for index, case in enumerate(result.cases):
        params = case.path_params or {}
        missing = [
            name
            for name in placeholders
            if not isinstance(params.get(name), str) or not params.get(name, "").strip()
        ]
        if missing:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L1,
                    code="l1.path_param_missing",
                    message=(
                        f"第 {index + 1} 条用例缺少路径参数 "
                        f"{', '.join(f'{{{name}}}' for name in missing)}；"
                        "请求路径不能保留模板占位符"
                    ),
                    dimension=case.dimension,
                    case_index=index,
                )
            )
    return issues


def _validate_schema_refs(
    responses_schema: dict[str, Any] | None, result: GenerationResult
) -> list[ValidationIssue]:
    """`schema_valid` 只能指向**声明了 content schema** 的状态码。

    接口声明了 `201` 但只写了 `description`、没有任何响应体结构时，
    对它做整体结构校验没有依据——它一定无法求值。M5 人工标注里这类
    断言占了错误用例的很大一部分。
    """
    issues: list[ValidationIssue] = []
    for index, case in enumerate(result.cases):
        for assertion in case.assertions:
            if assertion.type != "schema_valid":
                continue
            ref = str(getattr(assertion, "schema_ref", ""))
            if not _response_has_content_schema(responses_schema, ref):
                issues.append(
                    ValidationIssue(
                        layer=IssueLayer.L1,
                        code="l1.schema_valid_without_schema",
                        message=(
                            f"第 {index + 1} 条用例对状态码 {ref} 用了 schema_valid，"
                            "但 Spec 没有为该响应声明 content schema"
                        ),
                        dimension=case.dimension,
                        case_index=index,
                    )
                )
    return issues


def _response_has_content_schema(responses_schema: dict[str, Any] | None, status: str) -> bool:
    if not isinstance(responses_schema, dict):
        return False
    response = responses_schema.get(status)
    if response is None and status.isdigit():
        # OpenAPI 的 responses 键通常是字符串，但调用方也可能传 int 键。
        response = next(
            (value for key, value in responses_schema.items() if str(key) == status),
            None,
        )
    if not isinstance(response, dict):
        return False
    content = response.get("content")
    if not isinstance(content, dict):
        return False
    return any(isinstance(media, dict) and "schema" in media for media in content.values())


def _validate_assertion_content(result: GenerationResult) -> list[ValidationIssue]:
    """断言本身必须有实际内容。

    `body_contains: ""`、`json_path: ""` 这类断言要么恒真、要么无法求值，
    会让一条用例看起来"有断言"，实际上什么都没验证。M5 第二轮标注里
    `/reports/summary` 就是被一个空的 `body_contains` 判成错误的。
    """
    issues: list[ValidationIssue] = []
    for index, case in enumerate(result.cases):
        for assertion in case.assertions:
            empty = False
            if assertion.type == "body_contains":
                empty = not str(getattr(assertion, "text", "") or "").strip()
            elif assertion.type == "json_path":
                empty = not str(getattr(assertion, "path", "") or "").strip()
            elif assertion.type == "header":
                empty = not str(getattr(assertion, "name", "") or "").strip()

            if empty:
                issues.append(
                    ValidationIssue(
                        layer=IssueLayer.L1,
                        code="l1.assertion_empty_value",
                        message=(
                            f"第 {index + 1} 条用例的 `{assertion.type}` 断言内容为空，"
                            "它无法验证任何东西"
                        ),
                        dimension=case.dimension,
                        case_index=index,
                    )
                )
    return issues


def _validate_boundary_values(
    request_body_schema: dict[str, Any] | None, result: GenerationResult
) -> list[ValidationIssue]:
    """`boundary:*` 的取值必须**恰好**落在 Spec 给出的边界上。

    M5 人工标注里有两类典型错误：`maxLength=280` 却给了 252 个字符；
    `maximum=100` 且 `multipleOf=5` 却给了 101（同时违反两个约束，
    无法单独验证最大值）。这两类都靠"模型自觉"不可靠，所以做成确定性校验。
    """
    body_schema = json_body_schema(request_body_schema) or {}
    properties = body_schema.get("properties")
    if not isinstance(properties, dict):
        return []

    issues: list[ValidationIssue] = []
    for index, case in enumerate(result.cases):
        parts = case.dimension.split(":")
        if len(parts) != 3 or parts[0] != "boundary":
            continue
        field, tag = parts[1], parts[2]
        field_schema = properties.get(field)
        if not isinstance(field_schema, dict):
            continue

        expected = boundary_target(field_schema, tag)
        if expected is None:
            continue
        kind, target = expected
        value = (case.request_body or {}).get(field)

        if kind == "length":
            ok = isinstance(value, str) and len(value) == target
            actual = f"长度 {len(value)}" if isinstance(value, str) else "不是字符串"
            wanted = f"长度 {target}"
        else:
            ok = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) == float(target)
            )
            actual = f"取值 {value!r}"
            wanted = f"取值 {target:g}"

        if not ok:
            issues.append(
                ValidationIssue(
                    layer=IssueLayer.L1,
                    code="l1.boundary_value_mismatch",
                    message=(
                        f"第 {index + 1} 条用例的 `{case.dimension}` 要求{wanted}，实际是{actual}"
                    ),
                    dimension=case.dimension,
                    case_index=index,
                )
            )
    return issues


def _case_expects_status(case: GeneratedCase, code: int) -> bool:
    for assertion in case.assertions:
        if assertion.type != "status_code":
            continue
        expected = assertion.expected
        if isinstance(expected, list):
            if code in expected:
                return True
        elif expected == code:
            return True
    return False


def feedback_text(result: ValidationResult, *, max_issues: int = _MAX_FEEDBACK_ITEMS) -> str:
    """把问题渲染成给模型的修正提示。**不是给人看的日志。**"""
    if result.ok:
        return ""
    shown = result.issues[:max_issues]
    lines = [f"- [{issue.layer.value}] {issue.message}" for issue in shown]
    remaining = len(result.issues) - len(shown)
    if remaining > 0:
        lines.append(f"- 还有 {remaining} 条问题未列出")
    return "\n".join(lines)


def _short(value: str) -> str:
    text = str(value)
    return text if len(text) <= _NAME_LIMIT else text[: _NAME_LIMIT - 1] + "…"
