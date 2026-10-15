"""把一条用例组装成一个真实的 HTTP 请求。

对应决策：`03-模块详细设计/TestExecutor.md` §2 步骤 ③、TE1、`04-数据模型.md` §2.5。

## 边界：这里决定"发什么"，不决定"允不允许发"

```
TestExecutor（本模块）           Guardrail（独立模块）
──────────────────────           ──────────────────────
决定"发什么请求"                  决定"这个请求允不允许发出"
组装 URL / headers / body         白名单、内网阻断、破坏性拦截…
注入凭证（在 credentials.py）      写审计
```

所以本模块**不 import httpx**——它产出一个 `BuiltRequest` 数据对象，
由 `execution/runner.py` 交给 Guardrail。这条边界由架构测试强制
（`tests/architecture/`），`execution/` 里出现 `import httpx` 会让 CI 失败。

## ⚠️ 本模块不碰凭证

`request_headers` 里**没有**凭证（`04` §2.5），凭证由 `credentials.py`
在执行时按 host 注入。这不是靠"记得不要放"保证的，是靠数据模型：
凭证根本不在会被序列化给 LLM 的对象里。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

__all__ = ["BuiltRequest", "build_request", "join_url", "pick_base_url"]


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    """一个组装好的请求。**还没经过 Guardrail 校验。**

    `host` 单独拎出来是为了让凭证注入能按 host 查找——
    那一步发生在 Guardrail 之前，所以它需要知道目标是谁。
    """

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


def pick_base_url(declared_targets: list[str] | None) -> str | None:
    """从 Spec 声明的出站目标里选一个作为请求基地址。

    ⚠️ **M1 的已知限制：取第一个。**

    `cases` 表上**没有**"这条用例打哪个 server"这一列。而 OpenAPI 允许在
    根级/路径级/操作级三层声明 `servers`，一份文档可以合法地指向多台主机。

    为什么现在不修：修它要给 `cases` 加一列、加一次迁移，而 M1 的被测对象
    （fixture）只有一个 server。而"多 server 的 Spec"目前根本没有测试能覆盖，
    加了列也只是把不确定性从"取第一个"挪到"这一列填什么"。

    记在这里，是为了让 M2 引入多目标支持时**能找到这个位置**——
    而不是让下一个人对着"为什么所有请求都打第一台机器"发懵。
    """
    if not declared_targets:
        return None
    return declared_targets[0]


def join_url(base_url: str, path: str, query: Mapping[str, Any] | None = None) -> str:
    """把基地址、路径与查询串拼成一个完整 URL。

    两个容易出错的点：

    * **基地址可能自带路径前缀**（`https://api.example.com/v1`），
      而用例里存的是 Spec 的原始 `path`（`/users/{id}` 替换后的 `/users/42`）。
      直接字符串相加会得到 `/v1/users/42`（对），
      而丢掉基地址的路径会得到 `/users/42`（错，404）。
    * **查询参数的值可能是列表**。API 里两种编码都有（重复参数 vs 逗号连接），
      这里用**重复参数**（`?tag=a&tag=b`）——那是 `urlencode(doseq=True)` 的
      默认行为，也是绝大多数框架解析成数组的方式。
    """
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    joined = f"{base_path}/{path.lstrip('/')}" if path else (base_path or "/")

    encoded = urlencode(
        [(k, v) for k, value in (query or {}).items() for v in _as_list(value)],
        doseq=True,
    )
    return urlunsplit((base.scheme, base.netloc, joined, encoded, ""))


def _as_list(value: Any) -> list[Any]:
    """把查询参数值统一成列表。

    `None` 转成空列表：一个值为 `None` 的参数意味着"不传它"，
    而把它编码成字符串 `"None"` 会让服务端收到一个字面量为 None 的值——
    那是一个几乎必然导致失败的请求，而原因看起来完全不像"用例生成有问题"。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, bool):
        # `True` 在 Python 里是 `int`，不动它的话会被 `urlencode` 编成 `?flag=1`。
        # 而多数框架期望的是 `true`（小写）——JSON 的写法。
        return ["true" if value else "false"]
    return [value]


def encode_body(body: Any | None) -> bytes | None:
    """把请求体序列化成字节。

    只支持 JSON 体——TestFlow 处理的是 JSON API。其他类型（表单、二进制）
    在 M1 里会以 `TypeError` 暴露出来，而不是被静默地编成一个错误的形状。
    """
    if body is None:
        return None
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        # 原文照发：生成阶段可能产出了一个**故意非法的 JSON 字符串**
        # （用于测试服务端的解析健壮性），把它重新序列化会毁掉那个意图。
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def build_request(
    *,
    method: str,
    path: str,
    base_url: str,
    query: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    body: Any | None = None,
) -> BuiltRequest:
    """组装一个请求。

    Raises:
        ValueError: 基地址无法解析，或路径为空。

    Note:
        默认会带上 `Accept: application/json`——不写的话，很多框架会返回
        HTML，而断言是按 JSON 写的。**这个头不是"优化"，是让测试意图
        能被正确表达的前提**：用例的断言是"返回 JSON 且其中 id 存在"，
        而不带 Accept 的请求根本没有表达"我要 JSON"这个意思。

        但**不**自动带 `Content-Type`：只有在有 body 时才需要，而盲目加上
        会让一个无 body 的 POST 变成一个语义不同的请求。
    """
    if not path:
        raise ValueError("请求路径不能为空")

    base = urlsplit(base_url)
    if not base.scheme or not base.netloc:
        raise ValueError(f"基地址无法解析：{base_url!r}")

    merged_headers = {
        "accept": "application/json",
        # header 名统一小写：HTTP 头名大小写不敏感，而混用会让去重与覆盖
        # 出现"两个只是大小写不同的同名头"。
        **{k.lower(): v for k, v in (headers or {}).items()},
    }

    payload = encode_body(body)
    if payload is not None and "content-type" not in merged_headers:
        merged_headers["content-type"] = "application/json"

    return BuiltRequest(
        method=method.upper(),
        url=join_url(base_url, path, query),
        headers=merged_headers,
        body=payload,
    )
