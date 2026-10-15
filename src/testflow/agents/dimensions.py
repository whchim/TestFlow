"""阶段 A：从 schema **确定性地**派生测试维度。**纯算法，不调用 LLM。**

对应决策：`03-模块详细设计/TestCaseGen.md` §2.2（两阶段）、§2.3（维度表）、TG1。

## 核心主张：维度确定，取值生成

    schema ──→ 测试维度清单     ← 本模块（纯算法）
                  「required_missing: email」
                  「boundary: age」
        ↓
    单个维度 + 上下文 ──→ 具体取值 + 断言    ← 阶段 B（LLM）
                  age=151

三个收益：

1. **可验证** —— 覆盖率的**分母**是算出来的，不由模型决定
2. **抗幻觉** —— 模型只填值，约束来自 schema，它编不出一个不存在的字段
3. **可评估** —— Golden Set 比的是**维度覆盖**，不是"用例条数"

## ⚠️ 覆盖率有一个自我参照的陷阱

分母由本模块产出，所以**本模块漏掉的维度不会出现在分母里**——
一个漏了 `maxLength` 的实现在覆盖率上仍然是 100%。

这不是可以靠"写得更仔细"解决的问题，而是度量本身的缺陷。
对策是外部的 Golden Set（M5）：人工标注"这份 schema 应当派出哪些维度"，
再和本模块的产出去比。**在那之前，覆盖率只能说明"派出来的都测了"，
不能说明"该派的都派了"。**
"""

from __future__ import annotations

from typing import Any

from testflow.domain.cases import DimensionSpec

__all__ = ["derive_dimensions", "is_pure_rule_operation"]

#: `boundary` 维度展开成两条：边界值本身（应当被接受）与越界值（应当被拒绝）。
#:
#: 只测其中一条是常见的偷懒：只测"151 被拒绝"而没有"150 被接受"，
#: 那么一个"任何 age 都拒绝"的实现在这条测试上是绿的——
#: 而它显然是个 bug。
_BOUNDARY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("minimum", "min", "minimum"),
    ("maximum", "max", "maximum"),
    ("minLength", "len_min", "minLength"),
    ("maxLength", "len_max", "maxLength"),
    ("minItems", "items_min", "minItems"),
    ("maxItems", "items_max", "maxItems"),
)


def is_pure_rule_operation(
    parameters_schema: dict[str, Any] | None,
    request_body_schema: dict[str, Any] | None,
    responses_schema: dict[str, Any] | None,
) -> bool:
    """这个接口是否**不需要 LLM**（TG5）。

    "无信息接口"的定义：没有请求参数、没有请求体、`responses_schema` 里
    没有声明任何 4xx。这种接口阶段 A 只会派出 `value_valid` 一条维度，
    而它的取值是平凡的（什么都不传）——**让模型来做这件事，只是把
    一个确定的答案换成一次网络请求和一个不确定的答案**。

    成本上也很实际：demo 的 fixture 里有一半端点属于这类，
    省下的调用不是小数。
    """
    has_params = bool((parameters_schema or {}).get("parameters"))
    has_body = bool(request_body_schema)
    has_client_errors = any(str(code).startswith("4") for code in (responses_schema or {}))
    return not (has_params or has_body or has_client_errors)


def _schema_of_request_body(request_body_schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 `requestBody` 里取出 JSON 媒体类型的 schema。

    只认 JSON——TestFlow 处理的是 JSON API。取不到时返回 `None`，
    调用方据此只产出 `value_valid`（信息不全时的降级，不是错误）。
    """
    if not isinstance(request_body_schema, dict):
        return None
    content = request_body_schema.get("content")
    if not isinstance(content, dict):
        return None
    for media_type, media in content.items():
        if "json" in str(media_type).lower() and isinstance(media, dict):
            schema = media.get("schema")
            if isinstance(schema, dict):
                return schema
    return None


def _expected_status(responses_schema: dict[str, Any] | None, *codes: str) -> list[int]:
    """从 Spec 里查出这些状态码**是否真的被声明过**。

    这个区分决定了用例的置信度：Spec 声明了 404 → 断言 404 是 `derived_from: "spec"`；
    没声明却断言 404 → 那是**推断**（`inferred`）。

    `TestCaseGen.md` §7 把"未声明该错误状态码"列为低置信度的首要触发条件——
    因为那时"期望 404"是猜的，而猜错的方向恰好是"用例本身写错了"。
    """
    if not responses_schema:
        return []
    return [int(code) for code in codes if str(code) in responses_schema]


def derive_dimensions(
    *,
    operation_path: str,
    parameters_schema: dict[str, Any] | None = None,
    request_body_schema: dict[str, Any] | None = None,
    responses_schema: dict[str, Any] | None = None,
) -> list[DimensionSpec]:
    """派生出该接口的全部测试维度。

    **顺序是稳定的**（先整体后逐字段，字段按名排序），因为产出顺序会影响
    用例的展示顺序与报告里的分组——让它在两次运行间一致，才能对比。

    Returns:
        至少一条（`value_valid`）。**永远不返回空**：没有任何维度的接口
        仍然值得发一次合法请求看看它到底返回什么。
    """
    specs: list[DimensionSpec] = [
        DimensionSpec(
            dimension="value_valid",
            category="normal",
            expected_status=_expected_status(responses_schema, "200", "201", "204") or [200],
            rationale="所有必填字段给合法值，验证正常路径",
        )
    ]

    body_schema = _schema_of_request_body(request_body_schema)
    if body_schema is not None:
        specs.extend(_dimensions_from_body_schema(body_schema, responses_schema))

    # 路径参数断言"资源不存在"。只在**资源型路径**上派生有意义的用例：
    # `/health` 没有路径参数，`/users/{id}` 才有。
    if "{id}" in operation_path or "{" in operation_path:
        specs.append(
            DimensionSpec(
                dimension="not_found",
                category="abnormal",
                expected_status=_expected_status(responses_schema, "404") or [404],
                rationale="不存在的资源应当返回 404",
            )
        )

    return specs


def _dimensions_from_body_schema(
    schema: dict[str, Any], responses_schema: dict[str, Any] | None
) -> list[DimensionSpec]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []

    required = schema.get("required")
    required_fields = sorted(required) if isinstance(required, list) else []

    specs: list[DimensionSpec] = []
    # 排序保证稳定——`properties` 的迭代顺序取决于文档里的书写顺序，
    # 而那是用户随时会改的东西，不该影响我们的产出顺序。
    for field in sorted(properties):
        field_schema = properties[field]
        if not isinstance(field_schema, dict):
            continue

        if field in required_fields:
            specs.append(
                DimensionSpec(
                    dimension=f"required_missing:{field}",
                    category="abnormal",
                    field=field,
                    expected_status=_expected_status(responses_schema, "400", "422") or [400],
                    rationale=f"缺少必填字段 {field} 应当被拒绝",
                )
            )
            specs.append(
                DimensionSpec(
                    dimension=f"type_mismatch:{field}",
                    category="abnormal",
                    field=field,
                    expected_status=_expected_status(responses_schema, "400", "422") or [400],
                    rationale=f"{field} 的类型不符应当被拒绝",
                )
            )

        specs.extend(_constraint_dimensions(field, field_schema, responses_schema))

    return specs


def _constraint_dimensions(
    field: str, field_schema: dict[str, Any], responses_schema: dict[str, Any] | None
) -> list[DimensionSpec]:
    specs: list[DimensionSpec] = []

    if "enum" in field_schema:
        specs.append(
            DimensionSpec(
                dimension=f"constraint_violation:{field}",
                category="abnormal",
                field=field,
                expected_status=_expected_status(responses_schema, "400", "422") or [400],
                rationale=f"{field} 取值不在 enum 内应当被拒绝",
            )
        )

    for key, tag, label in _BOUNDARY_KEYS:
        if key not in field_schema:
            continue
        # 两条：边界值本身（接受）+ 越界值（拒绝）。
        specs.append(
            DimensionSpec(
                dimension=f"boundary:{field}:{tag}",
                category="boundary",
                field=field,
                expected_status=_expected_status(responses_schema, "200", "201") or [200],
                rationale=f"{field} 取 {label} 的边界值应当被接受",
            )
        )
        specs.append(
            DimensionSpec(
                dimension=f"boundary:{field}:{tag}_over",
                category="boundary",
                field=field,
                expected_status=_expected_status(responses_schema, "400", "422") or [400],
                rationale=f"{field} 越出 {label} 应当被拒绝",
            )
        )

    return specs
