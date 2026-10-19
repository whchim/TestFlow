"""归因规则库：每条规则、优先级短路、以及"不该命中"的边界。"""

from __future__ import annotations

from typing import Any

import pytest

from testflow.agents.rules.attribution import RULES, evaluate
from testflow.domain.attribution import (
    RESPONSIBILITY_GROUPS,
    AttributionCategory,
    FailureContext,
    responsibility_of,
)


def _ctx(**overrides: Any) -> FailureContext:
    base: dict[str, Any] = {
        "status": "failed",
        "http_status": 200,
        "error_kind": None,
        "response_body": None,
        "truncated": False,
        "assertion_outcomes": (),
        "case_confidence": "high",
        "responses_schema": None,
    }
    base.update(overrides)
    return FailureContext(**base)


# ─────────────────────────────────────────────── 每条规则至少一例


@pytest.mark.parametrize(
    ("ctx", "expected_ref", "expected_category", "expected_confidence"),
    [
        pytest.param(
            _ctx(status="error", http_status=None, error_kind="guardrail_denied"),
            "guardrail.denied",
            AttributionCategory.BLOCKED_BY_POLICY,
            "high",
            id="被平台策略拦截",
        ),
        pytest.param(
            _ctx(status="timeout", http_status=None, error_kind="timeout"),
            "transport.timeout",
            AttributionCategory.NETWORK_ERROR,
            "high",
            id="真超时",
        ),
        pytest.param(
            _ctx(status="error", http_status=None, error_kind="dns_failure"),
            "transport.other",
            AttributionCategory.NETWORK_ERROR,
            "high",
            id="DNS 失败",
        ),
        pytest.param(
            _ctx(http_status=409),
            "status.conflict",
            AttributionCategory.CONFLICT,
            "high",
            id="409",
        ),
        pytest.param(
            _ctx(http_status=429),
            "status.rate_limited",
            AttributionCategory.RATE_LIMITED,
            "high",
            id="429",
        ),
        pytest.param(
            _ctx(http_status=401),
            "status.unauthorized",
            AttributionCategory.AUTH_ERROR,
            "high",
            id="401",
        ),
        pytest.param(
            _ctx(http_status=403),
            "status.forbidden",
            AttributionCategory.AUTH_ERROR,
            "high",
            id="403",
        ),
        pytest.param(
            _ctx(http_status=400),
            "status.bad_request",
            AttributionCategory.REQUEST_INVALID,
            "high",
            id="400",
        ),
        pytest.param(
            _ctx(http_status=422),
            "status.unprocessable",
            AttributionCategory.REQUEST_INVALID,
            "high",
            id="422",
        ),
        pytest.param(
            _ctx(http_status=410, response_body='{"detail": "gone"}'),
            "body.validation_error",
            AttributionCategory.REQUEST_INVALID,
            "medium",
            id="其余 4xx + 校验错误体",
        ),
        pytest.param(
            _ctx(http_status=405),
            "status.method_not_allowed",
            AttributionCategory.REQUEST_INVALID,
            "high",
            id="405",
        ),
        pytest.param(
            _ctx(http_status=403, response_body='{"message": "Too Many Requests"}'),
            "status.quota_body",
            AttributionCategory.RATE_LIMITED,
            "medium",
            id="403 + 配额文案",
        ),
        pytest.param(
            _ctx(http_status=404),
            "status.not_found",
            AttributionCategory.RESOURCE_NOT_FOUND,
            "high",
            id="404",
        ),
        pytest.param(
            _ctx(http_status=503),
            "status.server_error",
            AttributionCategory.SERVER_ERROR,
            "high",
            id="5xx",
        ),
        pytest.param(
            _ctx(
                http_status=200,
                assertion_outcomes=({"index": 0, "type": "schema_valid", "outcome": "failed"},),
            ),
            "assertion.schema_valid",
            AttributionCategory.RESPONSE_SCHEMA_MISMATCH,
            "high",
            id="2xx + schema 断言失败",
        ),
    ],
)
def test_each_rule_matches(
    ctx: FailureContext,
    expected_ref: str,
    expected_category: AttributionCategory,
    expected_confidence: str,
) -> None:
    result = evaluate(ctx)

    assert result is not None
    assert result.rule_ref == expected_ref
    assert result.category is expected_category
    assert result.confidence == expected_confidence


# ─────────────────────────────────────────────── 优先级与短路


def test_server_error_wins_over_assertion_rule() -> None:
    """同时满足 5xx 与"schema 断言失败"时，必须命中前者。

    这就是"从特殊到一般"的意义：一个 500 响应当然也不符合响应 schema，
    但把它归成 `response_schema_mismatch` 会让用户去核对字段，而不是
    去查服务端为什么 500。
    """
    ctx = _ctx(
        http_status=500,
        assertion_outcomes=({"index": 0, "type": "schema_valid", "outcome": "failed"},),
    )
    result = evaluate(ctx)
    assert result is not None
    assert result.rule_ref == "status.server_error"


def test_guardrail_denied_beats_everything() -> None:
    """被策略拦下时没有状态码；即使其他字段看起来像别的失败，也必须归它。"""
    ctx = _ctx(
        status="error",
        http_status=500,  # 理论上不会出现，但规则不能依赖"理论上"
        error_kind="guardrail_denied",
    )
    result = evaluate(ctx)
    assert result is not None
    assert result.category is AttributionCategory.BLOCKED_BY_POLICY


# ─────────────────────────────────────────────── 不该命中的情形


def test_executor_stall_is_not_attributed() -> None:
    """补记的超时不是测试结论，规则不该认领它。

    调用方会先过滤掉这种结果；这里额外守住"即使漏过来也不会被误判成
    `network_error`"——那会让报告捏造一个不存在的网络故障。
    """
    ctx = _ctx(status="timeout", http_status=None, error_kind="executor_stall")
    assert evaluate(ctx) is None


def test_plain_assertion_mismatch_is_left_to_llm() -> None:
    """2xx 的普通断言失败**不得**由规则判定（D1）。

    规则没有能力区分"服务错了"和"用例断言写错了"，硬猜一个等于把
    不确定性伪装成确定性（LA §3.3）。
    """
    ctx = _ctx(
        http_status=200,
        assertion_outcomes=(
            {"index": 0, "type": "json_path", "outcome": "failed", "expected": "a", "actual": "b"},
        ),
    )
    assert evaluate(ctx) is None


def test_assertion_error_is_left_to_llm() -> None:
    """`error` 是"无法求值"，不是"不成立"。合并两者会归错原因（TE2）。"""
    ctx = _ctx(
        http_status=200,
        assertion_outcomes=(
            {"index": 0, "type": "schema_valid", "outcome": "error", "reason": "response_not_json"},
        ),
    )
    assert evaluate(ctx) is None


def test_redirect_is_left_to_llm() -> None:
    ctx = _ctx(http_status=302)
    assert evaluate(ctx) is None


def test_non_json_4xx_body_is_left_to_llm() -> None:
    ctx = _ctx(http_status=410, response_body="<html>Gone</html>")
    assert evaluate(ctx) is None


def test_truncated_body_is_never_used_for_structure_matching() -> None:
    """截断的响应体不能用来判结构——缺失的键可能正好在被截掉的部分（DM6）。"""
    ctx = _ctx(
        http_status=410,
        response_body='{"detail": "go',
        truncated=True,
    )
    assert evaluate(ctx) is None


# ─────────────────────────────────────────────── 规则表自身


def test_rule_table_is_ordered_and_unique() -> None:
    """规则表顺序错了不会报错，只会**静默**命中错误的那一条。"""
    refs = [rule.rule_ref for rule in RULES]
    assert len(refs) == len(set(refs))
    assert [rule.priority for rule in RULES] == sorted(rule.priority for rule in RULES)


def test_rule_engine_never_emits_assertion_mismatch() -> None:
    """D1 的结构性保证：规则表里根本不存在这个类别。"""
    assert all(rule.category is not AttributionCategory.ASSERTION_MISMATCH for rule in RULES)


# ─────────────────────────────────────────────── 责任归属


def test_every_category_has_a_responsibility_group() -> None:
    """11 类必须全部能落到"该去找谁"——漏一个，报告里就会出现无归属的失败。"""
    grouped = {category for members in RESPONSIBILITY_GROUPS.values() for category in members}
    assert grouped == set(AttributionCategory)


def test_unknown_category_falls_back_instead_of_raising() -> None:
    """历史脏数据不该让整份报告生成失败。"""
    assert responsibility_of("not_a_category") == "未归类"
