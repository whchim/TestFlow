"""半稳定层：同一份 Spec 内**逐字节不变**的上下文。

对应决策：`03/TestCaseGen.md` §6.1（三层结构）、TG6（稳定前缀不可变）、
`02-架构设计.md` §5.4（上下文缓存）。

## 为什么需要它

提示词分三层：稳定前缀 → 半稳定 → 易变。稳定前缀是系统规则（每条调用都一样），
易变层是当前接口。中间这层放**同一份 Spec 的全局信息**：

- 鉴权方式（`securitySchemes` 的名字、类型、header/query 位置）
- 通用的错误响应结构（`components.responses`）

放进去之后有三个收益：

1. **缓存前缀变长**——命中的部分只收 10% 的输入价
2. **模型能看到全局约定**——比如"这个 API 用 Bearer 认证"，生成鉴权相关用例时不再靠猜
3. **同 Spec 内逐字节一致**——它必须是确定的，否则缓存静默失效

## 三条必须遵守的确定性规则

- 只提取白名单字段（不把整份文档塞进来）
- 所有集合按名字排序、JSON `sort_keys=True`
- **不含** `spec_id`、Run ID、时间戳、描述文本

第三条尤其重要：把 `spec_id` 放进来会让**每一次 Run** 的前缀都不同，
缓存命中率直接归零——而它不会报错，只会让账单变贵（`07` §5.3）。
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import BaseModel, Field

from testflow.agents.spec_parser import SpecParseError, load_document
from testflow.observability.logging import get_logger

__all__ = [
    "CommonResponseContext",
    "SecuritySchemeContext",
    "SpecContext",
    "build_spec_context",
]

logger = get_logger(__name__)

#: `securitySchemes` 里保留的字段。`description` 刻意剔除：它是自由文本，
#: 长度不可控，且对"怎么发请求"没有结构化贡献。
_SCHEME_FIELDS: Final = ("type", "in", "scheme", "bearerFormat")


class SecuritySchemeContext(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    type: str | None = None
    location: str | None = None  # OpenAPI 的 `in`（header/query/cookie）
    scheme: str | None = None
    bearer_format: str | None = None


class CommonResponseContext(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    content_types: list[str] = Field(default_factory=list)


class SpecContext(BaseModel):
    """一份 Spec 的全局上下文。空对象渲染为空串。"""

    model_config = {"extra": "forbid"}

    security: list[dict[str, list[str]]] = Field(default_factory=list)
    security_schemes: list[SecuritySchemeContext] = Field(default_factory=list)
    common_responses: list[CommonResponseContext] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.security or self.security_schemes or self.common_responses)

    def prompt_block(self) -> str:
        """渲染成半稳定层。**必须逐字节确定**。

        用紧凑分隔符：这段在每次调用里都会重复，空白是纯成本。
        """
        if self.is_empty():
            return ""
        payload = self.model_dump(mode="json")
        return "## 该 API 的全局约定（同一份 Spec 内不变）\n" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def build_spec_context(raw_content: str) -> SpecContext:
    """从 Spec 原文提取半稳定层。解析失败返回空上下文，**不抛异常**。

    为什么容忍失败：这是提示词的**增强**，不是生成的前置条件。为了多一段
    上下文而让整个 Run 失败，代价与收益完全不成比例。
    """
    try:
        document = load_document(raw_content)
    except (SpecParseError, ValueError, TypeError):
        logger.warning("半稳定层提取失败（Spec 无法解析），按空上下文处理")
        return SpecContext()
    if not isinstance(document, dict):
        return SpecContext()

    return SpecContext(
        security=_security_requirements(document.get("security")),
        security_schemes=_security_schemes(document),
        common_responses=_common_responses(document),
    )


def _security_requirements(raw: Any) -> list[dict[str, list[str]]]:
    if not isinstance(raw, list):
        return []
    requirements: list[dict[str, list[str]]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized: dict[str, list[str]] = {}
        for name in sorted(item):
            scopes = item[name]
            normalized[str(name)] = sorted(str(scope) for scope in scopes or [])
        requirements.append(normalized)
    return requirements


def _security_schemes(document: dict[str, Any]) -> list[SecuritySchemeContext]:
    components = document.get("components")
    if not isinstance(components, dict):
        return []
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        return []

    result: list[SecuritySchemeContext] = []
    for name in sorted(schemes):
        scheme = schemes[name]
        if not isinstance(scheme, dict):
            continue
        values = {field: scheme.get(field) for field in _SCHEME_FIELDS}
        result.append(
            SecuritySchemeContext(
                name=str(name),
                type=_as_str(values["type"]),
                location=_as_str(values["in"]),
                scheme=_as_str(values["scheme"]),
                bearer_format=_as_str(values["bearerFormat"]),
            )
        )
    return result


def _common_responses(document: dict[str, Any]) -> list[CommonResponseContext]:
    components = document.get("components")
    if not isinstance(components, dict):
        return []
    responses = components.get("responses")
    if not isinstance(responses, dict):
        return []

    result: list[CommonResponseContext] = []
    for name in sorted(responses):
        response = responses[name]
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        content_types = sorted(str(key) for key in content) if isinstance(content, dict) else []
        result.append(CommonResponseContext(name=str(name), content_types=content_types))
    return result


def _as_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None
