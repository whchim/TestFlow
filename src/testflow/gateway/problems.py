"""RFC 9457 `application/problem+json` 错误响应。

对应决策：`05-API契约.md` API4。

## 为什么用标准格式而不是自定义的错误体

自定义错误体（`{"error": "...", "code": 1}`）的问题是**每个服务一套**，
而消费者（前端、CLI、将来的外部集成）要为每一套写一份解析代码。
RFC 9457 是一个既有的、有字段语义约定的格式，用它等于把这些约定白拿过来。

## `errors` 数组是本项目的扩展

`problem+json` 的四个标准字段（`type`/`title`/`status`/`detail`）描述的是
**一个**问题。而解析一份 OpenAPI 文档可能一次发现**几十个**问题
（每个不合规的字段一条），把它们压成一句话会丢掉大部分信息——
用户就得"改一处、上传一次、看下一个错误"，来回几十轮。

所以扩展了一个 `errors: [{pointer, message}]` 数组。`pointer` 用 JSON Pointer
指向文档里的具体位置，前端可以据此高亮。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

__all__ = ["ProblemDetail", "problem_response", "PROBLEM_CONTENT_TYPE"]

PROBLEM_CONTENT_TYPE = "application/problem+json"

#: 问题类型的 URI 前缀。用一个 `about:blank` 之外的命名空间，
#: 是为了让"这是什么问题"能被机器识别——而机器识别的入口就是一个稳定的 URI。
_TYPE_PREFIX = "https://testflow.local/problems/"


@dataclass(frozen=True, slots=True)
class ProblemDetail:
    """一个错误。字段名与 RFC 9457 对齐。"""

    type: str
    title: str
    status: int
    detail: str = ""
    #: 错误码，供机器分支判断。**不要把 `title` 拿去当错误码用**——
    #: 它是给人读的，改文案不该影响调用方的逻辑。
    code: str = ""
    #: 逐条的具体问题（指针 + 说明）。
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": f"{_TYPE_PREFIX}{self.type}",
            "title": self.title,
            "status": self.status,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.code:
            payload["code"] = self.code
        if self.errors:
            payload["errors"] = self.errors
        return payload


def problem_response(problem: ProblemDetail) -> JSONResponse:
    """构造 `problem+json` 响应。

    **`Content-Type` 必须是 `application/problem+json`**，不是 `application/json`。
    它是这个格式的约定，也是客户端能自动识别"这是个问题"的依据——
    写错了不会报错，只会让所有自动化的错误处理静默失效。
    """
    return JSONResponse(
        status_code=problem.status,
        content=problem.to_payload(),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def problem_from_request(request: Request, exc: Exception) -> JSONResponse:
    """兜底的异常处理器。

    **不把异常消息原样返回给客户端。** 异常消息里可能有 SQL 语句、
    文件路径、甚至（在最坏情况下）凭证——那是 SR-6 的范畴。
    内部细节进日志。
    """
    from testflow.observability.logging import get_logger

    get_logger(__name__).exception("未处理的异常 path=%s", request.url.path)
    return problem_response(
        ProblemDetail(
            type="internal_error",
            title="服务器内部错误",
            status=500,
            detail="请求未能完成。详细信息已记录在服务端日志中。",
            code="internal_error",
        )
    )
