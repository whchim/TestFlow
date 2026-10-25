"""修复模型在自由格式对象上的"键名前导冒号"退化。

对应实测：`scripts/label_cases.py generate` 第一次生成时，Qwen 3.7 Flash
在 `request_body` 上产生了 `': email'` 这样的键（原始响应本身就是不合法的
JSON 片段）。这是序列化层退化，不是测试意图错误——把它清洗掉，
而不是让整条用例变成"生成失败"。
"""

from __future__ import annotations

import pytest

from testflow.agents.llm_case_gen import materialize_request_fields, repair_free_form_keys
from testflow.domain.assertions import StatusCodeAssertion
from testflow.domain.cases import GeneratedCase, GenerationResult
from testflow.llm.client import LLMContentError


def _case(
    *,
    body=None,
    query=None,
    headers=None,
    body_json=None,
    query_json=None,
    headers_json=None,
    path_json=None,
) -> GeneratedCase:
    return GeneratedCase(
        dimension="value_valid",
        name="POST /users",
        category="normal",
        request_body=body,
        request_body_json=body_json,
        request_query=query,
        request_query_json=query_json,
        request_headers=headers,
        request_headers_json=headers_json,
        path_params_json=path_json,
        assertions=[
            StatusCodeAssertion(type="status_code", expected=201, derived_from="spec"),
        ],
        derived_from="spec",
    )


def _repair(case: GeneratedCase) -> GeneratedCase:
    return repair_free_form_keys(GenerationResult(cases=[case])).cases[0]


def test_leading_colon_is_stripped_from_body_keys() -> None:
    fixed = _repair(_case(body={"name": "Alice", ": email": "a@example.com"}))

    assert fixed.request_body == {"name": "Alice", "email": "a@example.com"}


def test_nested_objects_are_cleaned_recursively() -> None:
    fixed = _repair(
        _case(
            body={
                ": billing": {": street": "1 Main St", "city": "X"},
                ": shipping": {"street": "2 Main St", "city": "Y"},
            }
        )
    )

    assert fixed.request_body == {
        "billing": {"street": "1 Main St", "city": "X"},
        "shipping": {"street": "2 Main St", "city": "Y"},
    }


def test_empty_keys_are_dropped() -> None:
    fixed = _repair(_case(body={": ": ": ", "email": "a@example.com"}))

    assert fixed.request_body == {"email": "a@example.com"}


def test_query_and_header_keys_are_cleaned_too() -> None:
    fixed = _repair(
        _case(
            query={": page": "2"},
            headers={": Content-Type": "application/json"},
        )
    )

    assert fixed.request_query == {"page": "2"}
    assert fixed.request_headers == {"Content-Type": "application/json"}


def test_values_are_not_modified() -> None:
    """值里的冒号是正常内容（例如时间戳、URL），不能一起清洗。"""
    fixed = _repair(_case(body={"at": "2026-10-25T08:00:00Z", "url": "https://x/y"}))

    assert fixed.request_body == {"at": "2026-10-25T08:00:00Z", "url": "https://x/y"}


def test_clean_body_is_returned_unchanged() -> None:
    case = _case(body={"name": "Alice", "email": "a@example.com"})
    result = repair_free_form_keys(GenerationResult(cases=[case]))

    assert result.cases[0].request_body == {"name": "Alice", "email": "a@example.com"}


# ------------------------------------------------ JSON 字符串通道（主路径）


def _materialize(case: GeneratedCase) -> GeneratedCase:
    return materialize_request_fields(GenerationResult(cases=[case])).cases[0]


def test_json_string_body_is_parsed_and_cleared() -> None:
    fixed = _materialize(_case(body_json='{"name": "Alice", "email": "a@example.com"}'))

    assert fixed.request_body == {"name": "Alice", "email": "a@example.com"}
    assert fixed.request_body_json is None


def test_json_string_wins_over_a_stale_dict() -> None:
    fixed = _materialize(_case(body={"stale": True}, body_json='{"name": "Alice"}'))

    assert fixed.request_body == {"name": "Alice"}


def test_invalid_json_string_is_a_content_error() -> None:
    with pytest.raises(LLMContentError, match="不是合法 JSON"):
        _materialize(_case(body_json='{"name": "Alice"'))


def test_non_object_json_is_a_content_error() -> None:
    with pytest.raises(LLMContentError, match="必须是 JSON 对象"):
        _materialize(_case(body_json='["not", "an", "object"]'))


def test_empty_json_string_leaves_the_dict_path_alone() -> None:
    fixed = _materialize(_case(body={"name": "Alice"}, body_json="   "))

    assert fixed.request_body == {"name": "Alice"}


def test_query_and_headers_use_the_json_string_channel() -> None:
    fixed = _materialize(
        _case(
            query_json='{"page": 2}',
            headers_json='{"Content-Type": "application/json"}',
        )
    )

    assert fixed.request_query == {"page": 2}
    assert fixed.request_headers == {"Content-Type": "application/json"}
    assert fixed.request_query_json is None
    assert fixed.request_headers_json is None


def test_path_params_use_the_json_string_channel() -> None:
    fixed = _materialize(_case(path_json='{"invoice_id": "inv-123"}'))

    assert fixed.path_params == {"invoice_id": "inv-123"}
    assert fixed.path_params_json is None


def test_header_values_must_be_strings() -> None:
    with pytest.raises(LLMContentError, match="键和值必须是字符串"):
        _materialize(_case(headers_json='{"Content-Type": 123}'))


def test_query_json_must_be_an_object() -> None:
    with pytest.raises(LLMContentError, match="必须是 JSON 对象"):
        _materialize(_case(query_json="[1, 2]"))
