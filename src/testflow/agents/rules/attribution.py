"""确定性归因规则库（`03-模块详细设计/LogAnalyzer.md` §4）。

## 规则优先，LLM 兜底

能用规则判定的绝不调用模型。四个理由（LA §2.1）：确定性、可解释、
零成本零延迟、可回归。LLM 只处理规则覆盖不到的模糊地带。

## 为什么按 `priority` 升序短路

规则集是**从特殊到一般**排列的：先判"请求根本没发出去"（传输层），
再判具体状态码，最后判宽泛的状态码段。首个命中即返回，所以一条失败
永远只有一个类别（LA §3.2 的互斥性靠这个保证，而不是靠"规则写的时候
小心一点"）。

优先级分段（LA §4.2）：

| 段 | 规则族 |
| :--- | :--- |
| 1–99 | 传输层（最高）——请求没发出去时，任何状态码判断都不适用 |
| 100–199 | 具体状态码 |
| 200–299 | 状态码段 + 响应体特征 |
| 300–399 | 宽泛状态码段 |
| 400–499 | 断言层 |

## 本模块**永不产出 `assertion_mismatch`**

那个类别天然二义：2xx 但内容不符，可能是服务与 spec 不一致，也可能是
用例的断言本身写错了。规则没有能力区分，只有 LLM 兜底带上"用例置信度"
才能给一个上限受限的判断（LA §3.3 / §6）。让规则硬猜一个，等于把
不确定性伪装成确定性。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from testflow.domain.attribution import AttributionCategory, Confidence, FailureContext

__all__ = ["RULES", "RuleResult", "evaluate"]


@dataclass(frozen=True, slots=True)
class RuleResult:
    """一条规则命中后的结论。"""

    category: AttributionCategory
    rule_ref: str
    confidence: Confidence
    """`high` / `medium` / `low`。

    规则路径的置信度按信号强度给（LA §7）：具体状态码与传输层错误是强信号
    → `high`；基于响应体特征匹配 → `medium`。
    """


@dataclass(frozen=True, slots=True)
class _Rule:
    rule_ref: str
    priority: int
    category: AttributionCategory
    confidence: Confidence
    match: Callable[[FailureContext], bool]

    def apply(self) -> RuleResult:
        return RuleResult(
            category=self.category,
            rule_ref=self.rule_ref,
            confidence=self.confidence,
        )


# ────────────────────────────────────────────────────── 判定片段

#: 传输层失败的 `error_kind`（`guardrail/errors.py` 的归类结果）。
#:
#: **不含 `timeout`**：超时由更高优先级的独立规则处理，且 `status` 是
#: `timeout` 而不是 `error`。混在一起会让两条规则同时命中，优先级的
#: 意义就只剩"谁写在前面"了。
_TRANSPORT_KINDS: Final = frozenset(
    {
        "dns_failure",
        "connection_refused",
        "connection_reset",
        "tls_error",
        "transport_error",
    }
)

#: 响应体里常见的"这是校验错误"的结构键。
_VALIDATION_BODY_KEYS: Final = frozenset({"errors", "error", "detail", "violations"})

#: "这其实是限流"的文本特征。
#:
#: 有些网关不给 429，而是用 403 + 一段配额说明。按状态码归成 `auth_error`
#: 会让人去查令牌权限，方向完全错了（LA §3.1 对 `rate_limited` 的排查方向）。
_QUOTA_PHRASES: Final = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "quota_exceeded",
    "throttl",
)


def _is_transport_failure(ctx: FailureContext) -> bool:
    kind = ctx.error_kind
    if not kind:
        return False
    # `classify_transport_error` 归不出来时用 `transport_<ExceptionName>`——
    # 那些也是传输层失败，不能因为名字没在表里就漏掉。
    return kind in _TRANSPORT_KINDS or kind.startswith("transport_")


def _looks_like_quota_exhausted(ctx: FailureContext) -> bool:
    if ctx.http_status != 403 or not ctx.response_body or ctx.truncated:
        return False
    body = ctx.response_body.lower()
    return any(phrase in body for phrase in _QUOTA_PHRASES)


def _is_other_4xx_with_validation_body(ctx: FailureContext) -> bool:
    if not ctx.is_4xx:
        return False
    # 截断的响应体不能用来判结构：缺失的键可能正好在被截掉的部分。
    # 这不是保守，是正确性——DM6 的同一条原则。
    if not ctx.response_body or ctx.truncated:
        return False
    try:
        body = json.loads(ctx.response_body)
    except (TypeError, ValueError):
        # 不是 JSON 就不用猜。交给 LLM 兜底，或最终落 `unknown`。
        return False
    return isinstance(body, dict) and bool(_VALIDATION_BODY_KEYS & body.keys())


def _has_failed_schema_assertion(ctx: FailureContext) -> bool:
    if not ctx.is_2xx:
        return False
    return any(
        item.get("type") == "schema_valid" and item.get("outcome") == "failed"
        for item in ctx.assertion_outcomes
    )


def _status_is(code: int) -> Callable[[FailureContext], bool]:
    return lambda ctx: ctx.http_status == code


# ────────────────────────────────────────────────────── 规则目录

#: **顺序即优先级**，首个命中即返回。
#:
#: 新增规则时：`rule_ref` 必须稳定（它会落库，用于追溯与规则覆盖率统计，
#: 改名等于把历史数据和新数据切成两半）。
RULES: Final[tuple[_Rule, ...]] = (
    # ── 1–99 传输层。请求没发出去 / 没收到响应，状态码判断一律不适用。
    _Rule(
        rule_ref="guardrail.denied",
        priority=10,
        category=AttributionCategory.BLOCKED_BY_POLICY,
        confidence="high",
        # 被策略拦下时 `http_status` 是 NULL（请求根本没发），
        # 所以它必须排在所有状态码规则之前。
        match=lambda ctx: ctx.error_kind == "guardrail_denied",
    ),
    _Rule(
        rule_ref="transport.timeout",
        priority=20,
        category=AttributionCategory.NETWORK_ERROR,
        confidence="high",
        match=lambda ctx: ctx.status == "timeout" and ctx.error_kind == "timeout",
    ),
    _Rule(
        rule_ref="transport.other",
        priority=30,
        category=AttributionCategory.NETWORK_ERROR,
        confidence="high",
        match=_is_transport_failure,
    ),
    # ── 100–199 具体状态码。
    _Rule(
        rule_ref="status.quota_body",
        priority=105,
        category=AttributionCategory.RATE_LIMITED,
        # 靠响应体文本判定，不是强信号 → medium。
        confidence="medium",
        match=_looks_like_quota_exhausted,
    ),
    _Rule(
        rule_ref="status.conflict",
        priority=110,
        category=AttributionCategory.CONFLICT,
        confidence="high",
        match=_status_is(409),
    ),
    _Rule(
        rule_ref="status.rate_limited",
        priority=120,
        category=AttributionCategory.RATE_LIMITED,
        confidence="high",
        match=_status_is(429),
    ),
    _Rule(
        rule_ref="status.unauthorized",
        priority=130,
        category=AttributionCategory.AUTH_ERROR,
        confidence="high",
        match=_status_is(401),
    ),
    _Rule(
        rule_ref="status.forbidden",
        priority=140,
        category=AttributionCategory.AUTH_ERROR,
        confidence="high",
        match=_status_is(403),
    ),
    _Rule(
        rule_ref="status.bad_request",
        priority=150,
        category=AttributionCategory.REQUEST_INVALID,
        confidence="high",
        match=_status_is(400),
    ),
    _Rule(
        rule_ref="status.unprocessable",
        priority=160,
        category=AttributionCategory.REQUEST_INVALID,
        confidence="high",
        match=_status_is(422),
    ),
    _Rule(
        rule_ref="status.method_not_allowed",
        priority=170,
        category=AttributionCategory.REQUEST_INVALID,
        confidence="high",
        # 405 / 406 / 415 都是"请求本身不合契约"，与 400/422 同一族。
        match=lambda ctx: ctx.http_status in (405, 406, 415),
    ),
    # ── 200–299 状态码段 + 响应体特征。
    _Rule(
        rule_ref="body.validation_error",
        priority=210,
        category=AttributionCategory.REQUEST_INVALID,
        confidence="medium",
        match=_is_other_4xx_with_validation_body,
    ),
    # ── 300–399 宽泛状态码段。
    _Rule(
        rule_ref="status.not_found",
        priority=310,
        category=AttributionCategory.RESOURCE_NOT_FOUND,
        confidence="high",
        match=_status_is(404),
    ),
    _Rule(
        rule_ref="status.server_error",
        priority=320,
        category=AttributionCategory.SERVER_ERROR,
        confidence="high",
        match=lambda ctx: ctx.is_5xx,
    ),
    # ── 400–499 断言层。
    _Rule(
        rule_ref="assertion.schema_valid",
        priority=410,
        category=AttributionCategory.RESPONSE_SCHEMA_MISMATCH,
        confidence="high",
        match=_has_failed_schema_assertion,
    ),
)


def evaluate(ctx: FailureContext) -> RuleResult | None:
    """按优先级求值，返回首个命中；全部不命中返回 `None`（交给 LLM）。

    `error_kind == "executor_stall"` 在调用方就被过滤掉了——它不是测试
    结论，是"结果没回来"（参见 `results.mark_unfinished_as_timeout`）。
    这里不再判一次：两处都判会让"到底谁负责过滤"变得模糊。
    """
    for rule in RULES:
        if rule.match(ctx):
            return rule.apply()
    return None


def _sanity_check() -> None:
    """导入时校验规则表本身，失败就立刻炸——规则顺序错了是**静默**错误。"""
    refs = [rule.rule_ref for rule in RULES]
    if len(refs) != len(set(refs)):
        raise RuntimeError(f"归因规则的 rule_ref 重复：{refs}")
    priorities = [rule.priority for rule in RULES]
    if priorities != sorted(priorities):
        raise RuntimeError(f"归因规则未按 priority 升序排列：{priorities}")


_sanity_check()
