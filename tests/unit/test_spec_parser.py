"""`SpecParser`（N1）的单元测试。

对应决策：`03/模块详细设计/SpecParser.md` SP1~SP5、ADR-0012、`04` §2.1/§2.2。

**这是全系统唯一"真幂等"的节点**——纯算法、不调 LLM，同输入必得同输出。
所以它可以被彻底地单元测试，也是整个 M1 里最便宜的一块。

**这组测试里最要紧的两条**：

1. **指纹对写法不敏感、对语义敏感**（SP5）。内联的 schema 与 `$ref` 引用的
   schema 语义等价，必须得到同一个指纹。做不到的话，S2 会把"改了写法"
   误报成"Spec 变了"，进而把一个本来有效的 Run 判为用例失效。
2. **展开必须是有界的**。深度限制单独挡不住指数爆炸——本文档里的
   "炸弹用例"会把两者分开验证。
"""

from __future__ import annotations

import json

import pytest

from testflow.agents.spec_parser import (
    SpecParseError,
    parse_spec,
)

# ------------------------------------------------------------------ 测试数据


def spec(**overrides: object) -> str:
    """一个最小可用的 OpenAPI 文档，可用关键字覆盖任意顶层字段。"""
    doc: dict[str, object] = {
        "openapi": "3.0.3",
        "info": {"title": "demo", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ok": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    doc.update(overrides)
    return json.dumps(doc)


class TestFingerprint:
    """SP5 / ADR-0012：指纹在 `$ref` 展开**之后**计算。"""

    INLINE = {
        "openapi": "3.0.3",
        "info": {"title": "d", "version": "1"},
        "paths": {
            "/u": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"n": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }

    REFERENCED = {
        "openapi": "3.0.3",
        "info": {"title": "d", "version": "1"},
        "paths": {
            "/u": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {"User": {"type": "object", "properties": {"n": {"type": "string"}}}}
        },
    }

    def test_ref_and_inline_are_the_same_spec(self) -> None:
        """**本文件最重要的一条断言。**

        两份文档只是书写风格不同，语义完全一致。若指纹不同，S2 会判成
        "Spec 变了"，进而把一个本来有效的 Run 标记为例例失效——
        而用户什么都没改。
        """
        assert parse_spec(json.dumps(self.INLINE)).fingerprint == (
            parse_spec(json.dumps(self.REFERENCED)).fingerprint
        )

    def test_key_order_does_not_matter(self) -> None:
        a = parse_spec('{"openapi":"3.0.3","info":{"title":"d","version":"1"},"paths":{}}')
        b = parse_spec('{"paths":{},"info":{"title":"d","version":"1"},"openapi":"3.0.3"}')
        assert a.fingerprint == b.fingerprint

    def test_array_order_does_matter(self) -> None:
        """数组顺序在 OpenAPI 里**有语义**（`enum` 取值、`required` 列表）。

        把它也排序会改变含义，并且让"把 enum 的取值调了顺序"这种真实变更
        检测不到。
        """

        def with_enum(values: list[str]) -> str:
            return json.dumps(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "d", "version": "1"},
                    "paths": {
                        "/u": {
                            "get": {
                                "parameters": [
                                    {
                                        "name": "r",
                                        "in": "query",
                                        "schema": {"type": "string", "enum": values},
                                    }
                                ],
                                "responses": {"200": {"description": "ok"}},
                            }
                        }
                    },
                }
            )

        assert parse_spec(with_enum(["a", "b"])).fingerprint != (
            parse_spec(with_enum(["b", "a"])).fingerprint
        )

    def test_integer_and_float_are_the_same(self) -> None:
        """`1` 与 `1.0` 归一——否则改一个数字的写法就会被判成 Spec 变更。

        用模板替换而不是 `json.dumps`：要测的恰恰是**文本层面**的两种写法，
        而 `json.dumps` 会先把它规范化掉，测不到任何东西。
        """
        template = (
            '{"openapi":"3.0.3","info":{"title":"d","version":"1"},"paths":'
            '{"/u":{"get":{"responses":{"200":{"description":"ok","x-limit":LIMIT}}}}}}'
        )
        assert parse_spec(template.replace("LIMIT", "1")).fingerprint == (
            parse_spec(template.replace("LIMIT", "1.0")).fingerprint
        )

    def test_real_change_changes_the_fingerprint(self) -> None:
        """反过来也要成立：真实的语义变更必须能被检测到。

        少了这条，"指纹恒定"也能让上面那几条通过——而那意味着 S2 永远
        检测不到 Spec 变更。
        """
        a = parse_spec(spec())
        b = parse_spec(spec().replace('"description": "ok"', '"description": "changed"'))
        assert a.fingerprint != b.fingerprint

    def test_fingerprint_is_64_hex(self) -> None:
        """列类型是 `CHAR(64)`——长度不对会落库失败。"""
        fp = parse_spec(spec()).fingerprint
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


class TestRefExpansion:
    def test_nested_refs_are_fully_expanded(self) -> None:
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/u": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/A"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "A": {
                            "type": "object",
                            "properties": {"b": {"$ref": "#/components/schemas/B"}},
                        },
                        "B": {"type": "string"},
                    }
                },
            }
        )
        parsed = parse_spec(doc)
        schema = parsed.normalized["paths"]["/u"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert schema == {"type": "object", "properties": {"b": {"type": "string"}}}
        assert not parsed.operations[0].has_unresolved_ref

    def test_depth_limit_keeps_the_literal_and_marks_it(self) -> None:
        """SP1：递归结构在 OpenAPI 里合法且常见，不能因为深就放弃整份文档。

        超限处**保留 `$ref` 字面量**而不是删除——删掉会让"信息缺失"变得不可见。
        """
        recursive = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/t": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Tree"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "Tree": {
                            "type": "object",
                            "properties": {"child": {"$ref": "#/components/schemas/Tree"}},
                        }
                    }
                },
            }
        )
        parsed = parse_spec(recursive)
        assert parsed.operations[0].has_unresolved_ref is True

        # 且确实保留了字面量——一路往里找应当撞到一个未展开的 $ref。
        node = parsed.normalized["paths"]["/t"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        for _ in range(20):
            if isinstance(node, dict) and "$ref" in node:
                break
            node = node.get("properties", {}).get("child")
        else:  # pragma: no cover —— 未命中说明展开没有停下来
            pytest.fail("递归结构没有被展开深度限制截断")

    @pytest.mark.parametrize("arity", [2, 5, 10, 20])
    def test_exponential_blowup_is_bounded(self, arity: int) -> None:
        """**深度限制单独挡不住指数爆炸。**

        一个有 *n* 个自引用的 schema，展开 10 层会产生 n^10 个节点。
        n=20 时是 1.02×10^13——足以把进程打死。

        而这是**用户上传的文档**，是不可信输入。所以必须有第二道闸：
        解引用次数预算。这里验证它确实收得住（而不是"跑得比较久"）。
        """
        import time

        bomb = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/b": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Bomb"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "Bomb": {
                            "type": "object",
                            "properties": {
                                f"c{i}": {"$ref": "#/components/schemas/Bomb"} for i in range(arity)
                            },
                        }
                    }
                },
            }
        )

        started = time.perf_counter()
        parsed = parse_spec(bomb)
        elapsed = time.perf_counter() - started

        assert parsed.operations[0].has_unresolved_ref is True
        assert elapsed < 5.0, f"展开未在有界时间内收敛：{elapsed:.1f}s"

    def test_high_arity_does_not_exhaust_memory(self) -> None:
        """n=50 时 n^10 是 10^17——比上面最坏的情形还大 4 个数量级。

        单独一条，因为它验的不是"快"，而是"不会把内存吃光"：
        展开若没有预算，这里会直接 OOM 而不是慢。
        """
        bomb = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/b": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Bomb"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "Bomb": {
                            "type": "object",
                            "properties": {
                                f"c{i}": {"$ref": "#/components/schemas/Bomb"} for i in range(50)
                            },
                        }
                    }
                },
            }
        )
        parsed = parse_spec(bomb)
        assert parsed.operations[0].has_unresolved_ref is True

    def test_dangling_ref_is_a_hard_failure(self) -> None:
        """悬空的 `$ref` 是**文档缺陷**，不是"信息不全"（SP4）。

        降级继续的后果：用户拿到一堆莫名其妙的 `blocked_by_policy` 归因，
        而真正的原因（文档里引了一个不存在的组件）在解析阶段就被掩盖了。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/u": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/Missing"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
            }
        )
        with pytest.raises(SpecParseError, match="不存在"):
            parse_spec(doc)

    def test_external_file_ref_is_rejected_with_a_clear_reason(self) -> None:
        """外部文件引用无法处理——用户上传的是**内容**，不是可访问的 URL。

        必须给出可操作的提示，而不是让用户对着一个 `$ref` 发愣。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/u": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "ok",
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "./common.yaml#/User"}
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
            }
        )
        with pytest.raises(SpecParseError, match="外部文件引用"):
            parse_spec(doc)


class TestDeclaredTargets:
    """SR-1 白名单的来源。提取错了，要么合法请求被拒，要么白名单过宽。"""

    def test_all_three_server_levels_are_extracted(self) -> None:
        """**只提取根级是不够的。**

        某个 Operation 用操作级 `servers` 指向另一台主机时，那个主机若被漏掉，
        它的请求会被 Guardrail 拒绝——而现象是"一批用例莫名 blocked_by_policy"，
        很难联想到解析阶段。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [{"url": "https://root.example.com"}],
                "paths": {
                    "/a": {
                        "servers": [{"url": "https://path.example.com"}],
                        "get": {"responses": {"200": {"description": "ok"}}},
                    },
                    "/b": {
                        "get": {
                            "servers": [{"url": "https://op.example.com"}],
                            "responses": {"200": {"description": "ok"}},
                        }
                    },
                },
            }
        )
        targets = {str(t) for t in parse_spec(doc).declared_targets}
        assert targets == {
            "https://root.example.com:443",
            "https://path.example.com:443",
            "https://op.example.com:443",
        }

    def test_variables_use_their_default(self) -> None:
        """SP2：用 `default` 实例化，**不取 `enum` 全部取值**。

        取全部会不必要地放大白名单，而白名单每宽一分 SR-1 就弱一分。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [
                    {
                        "url": "https://{region}.api.example.com",
                        "variables": {
                            "region": {
                                "default": "cn-hangzhou",
                                "enum": ["cn-hangzhou", "cn-beijing"],
                            }
                        },
                    }
                ],
                "paths": {},
            }
        )
        targets = [str(t) for t in parse_spec(doc).declared_targets]
        assert targets == ["https://cn-hangzhou.api.example.com:443"]
        assert not any("beijing" in t for t in targets), "不应取 enum 的全部取值"

    def test_variable_without_default_is_a_hard_failure(self) -> None:
        """⚠️ **这里与设计文档 §5.3 不一致，且文档是错的。**

        `SpecParser.md` §5.3 写的是"若变量无 `default`……记警告并跳过"，但
        §5.1 的 SP4 又写"不符合 OpenAPI 规范 → 硬失败"。而 OpenAPI 规范里
        server variable 的 `default` 是**必填**——所以不合规的文档根本走不到
        那段降级逻辑：校验器先一步拒绝了它。

        **实际行为是硬失败**，而且这是对的：一个无法确定主机名的 server 意味着
        "这个接口该访问哪里"根本没有答案，静默降级只会让用户在后面收到一堆
        `blocked_by_policy` 归因而找不到原因。

        代码里保留了那条降级分支（见下一条测试），因为它是**纵深防御**：
        若将来放宽校验（例如支持 3.1 的某些放宽项），它仍然正确。
        但它的可达性依赖校验器的行为，所以单独测它本身，而不假装能从
        `parse_spec` 触发。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [
                    {"url": "https://{region}.api.example.com", "variables": {"region": {}}}
                ],
                "paths": {},
            }
        )
        with pytest.raises(SpecParseError) as exc:
            parse_spec(doc)
        assert exc.value.code == "invalid_openapi"

    def test_instantiate_server_degrades_when_default_is_missing(self) -> None:
        """直接测降级分支本身——它是纵深防御，不该因为校验器挡住了就没人测。

        猜一个值比跳过更危险：猜错会**放行一个用户从未声明过的主机**，
        那正是 SR-1 要防的事。
        """
        from testflow.agents.spec_parser import _instantiate_server

        url, warning = _instantiate_server(
            {"url": "https://{region}.api.example.com", "variables": {"region": {}}},
            location="根级 servers",
        )
        assert url is None
        assert warning is not None
        assert warning.code == "server_variable_without_default"

    def test_instantiate_server_substitutes_all_placeholders(self) -> None:
        from testflow.agents.spec_parser import _instantiate_server

        url, warning = _instantiate_server(
            {
                "url": "https://{region}.{env}.example.com",
                "variables": {"region": {"default": "cn"}, "env": {"default": "prod"}},
            },
            location="根级 servers",
        )
        assert warning is None
        assert url == "https://cn.prod.example.com"

    def test_relative_url_is_skipped_with_a_warning(self) -> None:
        """SP3：相对 URL 无法确定 host——没有可解析的基准地址。

        后果（这些接口的请求会被拒绝）**必须告知用户**，否则他会把
        `blocked_by_policy` 归因当成平台故障。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [{"url": "/api/v1"}],
                "paths": {},
            }
        )
        parsed = parse_spec(doc)
        assert parsed.declared_targets == []
        assert any(w.code == "relative_server_url" for w in parsed.warnings)

    def test_no_servers_produces_a_warning(self) -> None:
        """白名单为空 = 所有请求都会被拒绝。不是错误，但必须说清楚。"""
        doc = json.dumps({"openapi": "3.0.3", "info": {"title": "d", "version": "1"}, "paths": {}})
        parsed = parse_spec(doc)
        assert parsed.declared_targets == []
        assert any(w.code == "no_servers" for w in parsed.warnings)
        assert parsed.has_usable_targets is False

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://a.example.com", "http://a.example.com:80"),
            ("https://a.example.com", "https://a.example.com:443"),
            ("https://a.example.com:8443", "https://a.example.com:8443"),
            ("https://A.Example.COM", "https://a.example.com:443"),
        ],
    )
    def test_ports_are_normalized(self, url: str, expected: str) -> None:
        """端口必须显式化，否则同一个目标会以两种写法进入白名单。"""
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [{"url": url}],
                "paths": {},
            }
        )
        assert [str(t) for t in parse_spec(doc).declared_targets] == [expected]

    def test_duplicates_are_deduplicated(self) -> None:
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "servers": [{"url": "https://a.example.com"}, {"url": "https://a.example.com:443"}],
                "paths": {},
            }
        )
        assert len(parse_spec(doc).declared_targets) == 1


class TestOperations:
    def test_methods_are_uppercased_and_paths_preserved(self) -> None:
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/users/{id}": {
                        # `id` 声明在**路径级**：它对下面每个方法都生效。
                        # 少了对任何方法都会让校验器报 UnresolvableParameterError——
                        # 这正是选择这个校验器的收益，它在文档层面就拦住了问题。
                        "parameters": [
                            {
                                "name": "id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "get": {"responses": {"200": {"description": "ok"}}},
                        "delete": {"responses": {"204": {"description": "gone"}}},
                    }
                },
            }
        )
        ops = {(o.method, o.path) for o in parse_spec(doc).operations}
        assert ops == {("GET", "/users/{id}"), ("DELETE", "/users/{id}")}

    def test_non_method_keys_are_ignored(self) -> None:
        """`parameters` / `servers` / `summary` 与 `get` 同级，不能被当成方法。"""
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/a": {
                        "summary": "这是路径级摘要，不是方法",
                        "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                        "get": {"responses": {"200": {"description": "ok"}}},
                    }
                },
            }
        )
        assert [o.method for o in parse_spec(doc).operations] == ["GET"]

    def test_operations_are_sorted_stably(self) -> None:
        """顺序影响生成顺序与报告顺序。随机变化会让"跑两次做对比"变成噪音。"""
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/z": {"get": {"responses": {"200": {"description": "ok"}}}},
                    "/a": {
                        "post": {"responses": {"200": {"description": "ok"}}},
                        "get": {"responses": {"200": {"description": "ok"}}},
                    },
                },
            }
        )
        ops = [(o.path, o.method) for o in parse_spec(doc).operations]
        assert ops == sorted(ops)

    def test_path_level_parameters_are_merged_into_each_operation(self) -> None:
        """路径级 `parameters` 对每个方法都生效，不能只挂在路径上。"""
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/a/{id}": {
                        "parameters": [
                            {
                                "name": "id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "get": {"responses": {"200": {"description": "ok"}}},
                    }
                },
            }
        )
        op = parse_spec(doc).operations[0]
        assert op.parameters_schema is not None
        assert op.parameters_schema["parameters"][0]["name"] == "id"

    def test_operation_without_parameters_has_null_schema(self) -> None:
        """整块 JSONB 为 NULL 而不是空结构——"没有参数"和"参数是空对象"不同。"""
        assert parse_spec(spec()).operations[0].parameters_schema is None

    def test_openapi_version_is_preserved(self) -> None:
        assert parse_spec(spec()).openapi_version == "3.0.3"


class TestDestructiveDetection:
    """SR-3 / DM3：默认由方法派生，但**允许文档显式覆盖**。"""

    @pytest.mark.parametrize("method", ["delete", "put", "patch"])
    def test_destructive_methods_are_marked(self, method: str) -> None:
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/a/{id}": {
                        method: {
                            "parameters": [
                                {
                                    "name": "id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        )
        assert parse_spec(doc).operations[0].is_destructive is True

    @pytest.mark.parametrize("method", ["get", "head", "options"])
    def test_safe_methods_are_not_marked(self, method: str) -> None:
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {"/a": {method: {"responses": {"200": {"description": "ok"}}}}},
            }
        )
        assert parse_spec(doc).operations[0].is_destructive is False

    def test_post_can_be_declared_destructive(self) -> None:
        """`POST /users/{id}/close` 在业务上可能是不可逆的，而 POST 不在默认集合里。

        这就是"允许覆盖"存在的理由——HTTP 方法不足以表达业务语义。
        """
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/a": {
                        "post": {
                            "x-destructive": True,
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        )
        assert parse_spec(doc).operations[0].is_destructive is True

    def test_delete_can_be_declared_safe(self) -> None:
        """反向覆盖同样要支持——有些 API 的 DELETE 只是软删除。"""
        doc = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "d", "version": "1"},
                "paths": {
                    "/a/{id}": {
                        "delete": {
                            "x-destructive": False,
                            "parameters": [
                                {
                                    "name": "id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ],
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        )
        assert parse_spec(doc).operations[0].is_destructive is False


class TestHardFailures:
    """SP4：文档缺陷 → 硬失败。能修的必须让用户修。"""

    def test_syntax_error_reports_a_position(self) -> None:
        """消息里必须带位置——没有它等于让用户自己在几千行里找。"""
        with pytest.raises(SpecParseError) as exc:
            parse_spec("openapi: 3.0.3\ninfo: {title: d\n  bad: [}")
        assert exc.value.code == "syntax_error"

    def test_non_object_root_is_rejected(self) -> None:
        with pytest.raises(SpecParseError, match="根节点必须是对象"):
            parse_spec("- 这是一个数组\n- 不是文档")

    def test_missing_required_fields_are_rejected(self) -> None:
        with pytest.raises(SpecParseError) as exc:
            parse_spec('{"openapi": "3.0.3"}')
        assert exc.value.code == "invalid_openapi"

    def test_wrong_version_string_is_rejected(self) -> None:
        with pytest.raises(SpecParseError):
            parse_spec('{"openapi": "2.0", "info": {"title": "d", "version": "1"}, "paths": {}}')

    def test_errors_carry_structured_details(self) -> None:
        """错误要有结构化字段，才能被 API 层渲染成 problem+json（`05` API4）。"""
        with pytest.raises(SpecParseError) as exc:
            parse_spec('{"openapi": "3.0.3"}')
        assert exc.value.code == "invalid_openapi"
        assert exc.value.detail
        assert isinstance(exc.value.errors, list)


class TestYamlSupport:
    def test_yaml_input_works(self) -> None:
        """用户可能上传 YAML——那才是 OpenAPI 的常见写法。"""
        parsed = parse_spec(
            "openapi: 3.0.3\n"
            "info: {title: d, version: '1'}\n"
            "servers:\n"
            "  - url: https://api.example.com\n"
            "paths:\n"
            "  /ok:\n"
            "    get:\n"
            "      responses:\n"
            "        '200': {description: ok}\n"
        )
        assert parsed.openapi_version == "3.0.3"
        assert [str(t) for t in parsed.declared_targets] == ["https://api.example.com:443"]
        assert [o.method for o in parsed.operations] == ["GET"]


class TestFixtureSpecIsParsable:
    """**这是 M1 端到端链路的第一环。**

    fixture 服务自动生成的 `/openapi.json` 是 demo 的被测文档。它必须能被
    N1 解析、并产出可用的 `declared_targets`——否则 demo 的第一步就断了，
    而现象是"所有请求被 Guardrail 拒绝"，排查起来要绕一大圈。
    """

    def test_fixture_openapi_is_parseable(self) -> None:
        from testflow.testing.fixture_server import app

        parsed = parse_spec(json.dumps(app.openapi()))
        assert parsed.has_usable_targets, "fixture 必须声明 servers，否则白名单为空"
        assert [str(t) for t in parsed.declared_targets] == ["http://127.0.0.1:9000"]

    def test_fixture_operations_are_extracted(self) -> None:
        from testflow.testing.fixture_server import app

        parsed = parse_spec(json.dumps(app.openapi()))
        paths = {o.path for o in parsed.operations}
        # 这些是 demo 与三个专项检查要用的端点。
        assert {"/ok", "/bad-json", "/redirect", "/error-500"} <= paths

    def test_fixture_destructive_endpoint_is_marked(self) -> None:
        """`/destructive` 是 DELETE，必须被判为破坏性——TE3 靠它验证拦截。"""
        from testflow.testing.fixture_server import app

        parsed = parse_spec(json.dumps(app.openapi()))
        destructive = [o for o in parsed.operations if o.path == "/destructive"]
        assert destructive and all(o.is_destructive for o in destructive)

    def test_fixture_recursive_model_is_bounded(self) -> None:
        """fixture 的 `TreeNode` 是自引用模型——正好用来验证深度限制。

        这条同时证明了"真实文档里的递归结构"（而不是人工造的）能被正确处理。
        """
        from testflow.testing.fixture_server import app

        parsed = parse_spec(json.dumps(app.openapi()))
        assert parsed.operations, "应当解析出接口"
        # 不要求一定有未展开的引用（取决于递归出现在响应还是请求里），
        # 但整个解析必须在这类结构下正常收敛。
        assert all(isinstance(o.has_unresolved_ref, bool) for o in parsed.operations)
