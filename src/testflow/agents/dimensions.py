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

import math
from typing import Any

from testflow.domain.cases import DimensionSpec

__all__ = [
    "boundary_target",
    "derive_dimensions",
    "is_pure_rule_operation",
    "json_body_schema",
]

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


def json_body_schema(request_body_schema: dict[str, Any] | None) -> dict[str, Any] | None:
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


def declared_client_error_codes(responses_schema: dict[str, Any] | None) -> list[int]:
    """`responses` 里**声明过**的 4xx 状态码，升序。

    只认精确的三位数字键：`4XX` / `default` 是"任意 4xx/默认响应"，
    不是某个具体状态码——把它们当成具体码会让生成的用例去断言一个
    根本不存在的期望。
    """
    codes: list[int] = []
    for key in responses_schema or {}:
        text = str(key)
        if len(text) == 3 and text.startswith("4") and text.isdigit():
            codes.append(int(text))
    return sorted(set(codes))


def boundary_target(field_schema: dict[str, Any], tag: str) -> tuple[str, float] | None:
    """按 `boundary:<field>:<tag>` 算出**唯一正确**的长度或取值。

    返回 `(kind, target)`：`kind` 是 `length` 或 `value`。
    无法判断（字段缺对应约束、tag 不认识）时返回 `None`。

    这个函数同时被两处使用：

    * `validation.py` —— 校验模型给的取值是否恰好落在边界上
    * `llm_case_gen.repair_boundary_values` —— 把算错的边界值改成正确值

    放在 `dimensions.py` 而不是任一调用方：边界语义属于"阶段 A 的维度定义"，
    两边各写一份迟早会不一致。
    """
    if tag in {"len_min", "len_min_over", "len_max", "len_max_over"}:
        key = "minLength" if tag.startswith("len_min") else "maxLength"
        base = field_schema.get(key)
        if not isinstance(base, int) or isinstance(base, bool):
            return None
        if tag.endswith("_over"):
            base = base - 1 if tag.startswith("len_min") else base + 1
        return ("length", float(base))

    if tag in {"min", "min_over", "max", "max_over"}:
        key = "minimum" if tag.startswith("min") else "maximum"
        base = field_schema.get(key)
        if not isinstance(base, (int, float)) or isinstance(base, bool):
            return None

        multiple = field_schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and not isinstance(multiple, bool) and multiple > 0:
            step = float(multiple)
            # 有 multipleOf 时，边界值也必须是它的整数倍，否则会同时违反
            # 两个约束，无法单独验证当前维度。
            if tag == "min":
                value = math.ceil(base / step) * step
            elif tag == "min_over":
                value = math.floor((base - 1) / step) * step
            elif tag == "max":
                value = math.floor(base / step) * step
            else:
                value = math.ceil((base + 1) / step) * step
        else:
            value = base
            if tag == "min_over":
                value -= 1
            elif tag == "max_over":
                value += 1

        return ("value", float(value))

    return None


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

    body_schema = json_body_schema(request_body_schema)
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

    # ★ M4：Spec 声明的每个 4xx 都必须有对应用例。
    #
    # 不派生的话，L2 的"每个声明的 4xx 都被覆盖"只能靠模型自己发明维度——
    # 而模型发明维度正是 L1 要拦的事（额外维度必须标 inferred 且有 50% 上限）。
    # 让阶段 A 确定性地补上，L2 才是可达的，而不是一件碰运气的事。
    covered = {code for spec in specs for code in spec.expected_status}
    for code in declared_client_error_codes(responses_schema):
        if code in covered:
            continue
        specs.append(
            DimensionSpec(
                dimension=f"status:{code}",
                category="abnormal",
                expected_status=[code],
                rationale=f"Spec 声明了 {code}，必须有一条用例验证它",
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


#: 会触发 `constraint_violation` 维度的约束键。
#:
#: `03/TestCaseGen.md` §2.3 的维度表把 `enum` / `pattern` / `format` /
#: `multipleOf` 归在同一个维度名下：它们都是"值合法但违反某个约束"的形态。
#: 第一版只认 `enum`——于是**带 `pattern` 的字段一个维度都派生不出来**，
#: 而覆盖率仍然显示 100%，因为分母里根本没有它（§5.2 的自我参照陷阱）。
#: M5 的 Golden Set 第 3 号接口就是为了让这个盲区暴露出来。
_CONSTRAINT_KEYS: tuple[str, ...] = ("enum", "pattern", "format", "multipleOf")


def _constraint_dimensions(
    field: str, field_schema: dict[str, Any], responses_schema: dict[str, Any] | None
) -> list[DimensionSpec]:
    specs: list[DimensionSpec] = []

    present = [key for key in _CONSTRAINT_KEYS if key in field_schema]
    if present:
        specs.append(
            DimensionSpec(
                dimension=f"constraint_violation:{field}",
                category="abnormal",
                field=field,
                expected_status=_expected_status(responses_schema, "400", "422") or [400],
                rationale=f"{field} 违反 {' / '.join(present)} 约束应当被拒绝",
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
