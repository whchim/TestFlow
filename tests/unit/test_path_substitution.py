"""路径参数替换。

M5 人工标注发现：`/invoices/{invoice_id}` 的请求路径原样带着模板占位符，
根本不可执行。用例模型补了 `path_params` 之后，这里保证替换发生且只发生一次。
"""

from __future__ import annotations

from testflow.agents.test_case_gen import _substitute_path


def test_single_placeholder_is_replaced() -> None:
    assert _substitute_path("/items/{item_id}", {"item_id": "item-42"}) == "/items/item-42"


def test_multiple_placeholders_are_replaced() -> None:
    assert (
        _substitute_path(
            "/orgs/{org_id}/items/{item_id}",
            {"org_id": "acme", "item_id": "7"},
        )
        == "/orgs/acme/items/7"
    )


def test_path_without_placeholders_is_unchanged() -> None:
    assert _substitute_path("/health", None) == "/health"


def test_unrelated_params_do_not_change_the_path() -> None:
    assert _substitute_path("/items/{id}", {"other": "x"}) == "/items/{id}"


def test_values_are_stringified() -> None:
    assert _substitute_path("/items/{id}", {"id": "42"}) == "/items/42"
