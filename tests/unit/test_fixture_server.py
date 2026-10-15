"""fixture 服务自身的契约测试。

对应决策：`09-测试策略.md` §3.2。

**为什么 fixture 本身也要测**

它是所有端到端测试的**地基**。如果 `/bad-json` 有一天真的返回了合法 JSON
（比如被"顺手修正"了），那么依赖它的那批测试会**静默地失去检验能力**——
它们仍然全绿，只是不再验证任何东西。这类"测试基建悄悄失效"是最难发现的。
"""

from __future__ import annotations

import inspect
import json
import time

import pytest
from fastapi.testclient import TestClient

from testflow.testing.fixture_server import app, slow

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_hits() -> None:
    client.post("/__reset")


class TestOkEndpoint:
    def test_returns_200_with_json(self) -> None:
        r = client.get("/ok")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestEchoEndpoint:
    def test_echoes_method_and_query(self) -> None:
        r = client.get("/echo", params={"a": "1"})
        body = r.json()
        assert body["method"] == "GET"
        assert body["query"] == {"a": "1"}

    def test_echoes_json_body(self) -> None:
        r = client.post("/echo", json={"x": [1, 2]})
        assert r.json()["body"] == {"x": [1, 2]}

    def test_echoes_non_json_body_as_text(self) -> None:
        r = client.post("/echo", content=b"raw text", headers={"content-type": "text/plain"})
        assert r.json()["body"] == "raw text"


class TestNoContentEndpoint:
    def test_returns_204_with_empty_body(self) -> None:
        r = client.get("/no-content")
        assert r.status_code == 204
        assert r.content == b""


class TestBadJsonEndpoint:
    """**`03/TestExecutor.md` TE2 的唯一验证手段。**"""

    def test_claims_json_but_is_not(self) -> None:
        r = client.get("/bad-json")
        assert r.status_code == 200, "必须是 2xx，否则走不到「求值响应体」那一步"
        assert r.headers["content-type"].startswith("application/json"), (
            "必须声称是 JSON，否则测的不是 TE2 的场景"
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(r.text)

    def test_body_looks_like_an_error_page(self) -> None:
        """模拟真实场景：服务返回了错误页，却声称 Content-Type 是 JSON。"""
        assert b"<html>" in client.get("/bad-json").content


class TestErrorEndpoints:
    def test_error_500(self) -> None:
        r = client.get("/error-500")
        assert r.status_code == 500
        assert "error" in r.json()

    def test_invalid_echo_has_validation_shape(self) -> None:
        r = client.post("/invalid-echo")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert isinstance(detail, list)
        assert {"loc", "msg", "type"} <= set(detail[0])


class TestSlowEndpoint:
    def test_actually_delays(self) -> None:
        start = time.monotonic()
        r = client.get("/slow", params={"seconds": 0.05})
        assert r.status_code == 200
        assert time.monotonic() - start >= 0.05

    def test_default_exceeds_configured_http_timeout(self) -> None:
        """默认延迟必须大于 `http_timeout_s`（10s），否则它测不出超时。"""
        assert inspect.signature(slow).parameters["seconds"].default > 10

    def test_rejects_non_positive_delay(self) -> None:
        assert client.get("/slow", params={"seconds": 0}).status_code == 422


class TestRedirectEndpoint:
    def test_returns_302_with_location(self) -> None:
        r = client.get("/redirect", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/ok"

    def test_can_target_metadata_address(self) -> None:
        """用于验证"即使跳转目标是元数据地址，也不跟随"（SR-4）。"""
        r = client.get(
            "/redirect", params={"to": "http://169.254.169.254/"}, follow_redirects=False
        )
        assert r.headers["location"] == "http://169.254.169.254/"


class TestLargeEndpoint:
    def test_exceeds_truncation_limit(self) -> None:
        """默认响应体必须超过 `04` DM6 的截断上限（256 KB）。"""
        r = client.get("/large")
        assert len(r.content) > 256 * 1024

    def test_size_is_configurable(self) -> None:
        assert len(client.get("/large", params={"kb": 1}).content) == 1024


class TestRecursiveEndpoint:
    def test_returns_nested_structure(self) -> None:
        body = client.get("/recursive", params={"depth": 2}).json()
        assert body["name"] == "level-0"
        assert body["child"]["name"] == "level-1"
        assert body["child"]["child"]["name"] == "leaf-2"

    def test_spec_contains_self_referencing_schema(self) -> None:
        """`/openapi.json` 中的自引用 `$ref` 是 `SpecParser` SP1 的验证素材。"""
        spec = client.get("/openapi.json").json()
        tree_node = spec["components"]["schemas"]["TreeNode"]
        assert tree_node["properties"]["child"]["anyOf"][0] == {
            "$ref": "#/components/schemas/TreeNode"
        }


class TestDestructiveEndpoint:
    def test_accepts_delete(self) -> None:
        assert client.delete("/destructive").status_code == 200

    def test_hit_is_recorded(self) -> None:
        """**这是验证第二层防护的关键机制。**

        Guardrail 拦截后，测试要断言的不是"收到了拒绝"，而是
        "本端点的命中数仍为 0"——那才能证明请求确实没到达。
        """
        assert client.get("/__hits").json().get("/destructive", 0) == 0
        client.delete("/destructive")
        assert client.get("/__hits").json()["/destructive"] == 1


class TestHitTracking:
    def test_counts_each_endpoint_separately(self) -> None:
        client.get("/ok")
        client.get("/ok")
        client.get("/no-content")
        hits = client.get("/__hits").json()
        assert hits["/ok"] == 2
        assert hits["/no-content"] == 1

    def test_reset_clears_counts(self) -> None:
        client.get("/ok")
        client.post("/__reset")
        assert client.get("/__hits").json() == {}


class TestSpecIsUsable:
    """fixture 同时是被测接口文档的来源——省掉手工维护一份测试用 Spec。"""

    def test_openapi_json_is_available(self) -> None:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["openapi"].startswith("3.")
        assert "/bad-json" in spec["paths"]
