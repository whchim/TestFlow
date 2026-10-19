"""半稳定层：确定性、白名单字段、空上下文。"""

from __future__ import annotations

import json

from testflow.agents.spec_context import SpecContext, build_spec_context


def _spec(**overrides) -> str:
    document = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "description": "别带我"},
                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Key"},
            },
            "responses": {
                "NotFound": {
                    "description": "not found",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        },
        "security": [{"apiKeyAuth": [], "bearerAuth": []}],
    }
    document.update(overrides)
    return json.dumps(document)


def test_security_schemes_are_sorted_and_whitelisted() -> None:
    ctx = build_spec_context(_spec())

    assert [s.name for s in ctx.security_schemes] == ["apiKeyAuth", "bearerAuth"]
    assert ctx.security_schemes[0].location == "header"
    assert ctx.security_schemes[1].scheme == "bearer"
    # 自由文本描述刻意不进来。
    assert "别带我" not in ctx.prompt_block()


def test_security_requirements_are_normalized() -> None:
    ctx = build_spec_context(_spec())
    assert ctx.security == [{"apiKeyAuth": [], "bearerAuth": []}]


def test_common_responses_include_sorted_content_types() -> None:
    ctx = build_spec_context(_spec())
    assert ctx.common_responses[0].name == "NotFound"
    assert ctx.common_responses[0].content_types == ["application/json"]


def test_output_is_byte_identical_when_dict_order_changes() -> None:
    """同一份 Spec 的两种写法必须得到同一段文本——否则缓存静默失效。"""
    first = build_spec_context(_spec()).prompt_block()

    document = json.loads(_spec())
    document["components"]["securitySchemes"] = dict(
        reversed(list(document["components"]["securitySchemes"].items()))
    )
    second = build_spec_context(json.dumps(document)).prompt_block()

    assert first == second


def test_context_excludes_volatile_identifiers() -> None:
    """`spec_id` / 时间戳进半稳定层会让**每一次 Run** 的前缀都不同。"""
    block = build_spec_context(_spec()).prompt_block()
    assert "spec_id" not in block
    assert "created_at" not in block
    assert "timestamp" not in block


def test_empty_context_renders_empty_string() -> None:
    ctx = build_spec_context(
        json.dumps({"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {}})
    )
    assert ctx.is_empty()
    assert ctx.prompt_block() == ""


def test_invalid_content_returns_empty_context_instead_of_raising() -> None:
    assert build_spec_context("not: [valid").is_empty()


def test_spec_context_model_is_closed() -> None:
    ctx = SpecContext()
    assert ctx.is_empty()
