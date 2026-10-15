"""凭证解密与注入。**执行时才发生，且凭证不进入任何持久化。**

对应决策：PRD SR-6、`03-模块详细设计/TestExecutor.md` §5 / TE4、`04-数据模型.md` §2.10。

## 三条不可动摇的约束

1. **凭证只在执行时按 host 取出并解密**（TE4）。`cases` 表里没有凭证列，
   生成阶段也没有——靠数据模型而不是靠"记得别放"。
2. **解密后的明文只活在这一个函数的栈上**。它不写日志、不进响应体、
   不入任何 ORM 对象。用完即弃。
3. **日志永不写凭证**（SR-6）。`config.py` 里刻意没有 `log_credentials`
   这类开关（`tests/unit/test_config.py` 有断言保证），
   所以这条是不可关闭的。

## ⚠️ `api_key_header` / `api_key_query` 的字段名是**约定**，不是配置

`credentials` 表只有 `name`（凭证的名字，用于唯一约束）、`scope_host`、
`auth_type`、`encrypted_value`。**没有"这个 key 放在哪个 header"这一列。**

所以 M1 对这两种类型使用约定值：`X-API-Key` 与 `api_key`。
这是实际的缺口——用户没法指定一个非约定的字段名。

**为什么现在不修**：修它要给表加列 + 一次迁移，而 M1 的测试路径
（fixture）根本不需要凭证。更重要的原因：**在没有真实用户的前提下设计
这个字段，多半会设计错**（该是一个自由字符串？还是一个受控列表？
要不要支持多凭证？）。

记在这里，是为了让 M2 做凭证管理界面时能找到这个位置——
那时会有真实的使用场景来决定它的形状。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from testflow.execution.request_builder import BuiltRequest

__all__ = ["CredentialError", "CredentialMaterial", "apply_credential", "decrypt_value"]

#: `api_key_header` / `api_key_query` 的字段名约定——**见模块文档字符串的缺口说明**。
_DEFAULT_HEADER_NAME: Final = "X-API-Key"
_DEFAULT_QUERY_NAME: Final = "api_key"


class CredentialError(Exception):
    """凭证不可用：解密失败、类型未知、密钥格式非法。

    **与"认证失败"是两回事**：这里是我们这边的配置问题（密钥不对、
    密文损坏），不是被测系统拒绝了我们的凭证。执行器据此把用例记为
    `error` 而不是"断言失败"——把平台故障算成服务端问题会让报告失真。
    """


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    """解密后的一份凭证。**明文只应当活在这里和用完它的那一刻。**

    刻意 `frozen=True`：它会被往函数间传递，而一个可变对象可能在某个
    意料之外的地方被挂到别的结构上（比如某个 dataclass 的字段），
    进而被序列化进日志或响应。不可变让"它有没有被带出去"这件事
    在代码上是可追溯的。
    """

    auth_type: str
    secret: str
    key_version: int = 1


def _fernet(key: str) -> Fernet:
    """构造解密器。

    密钥是 44 字符的 base64（Fernet 格式），来自环境变量
    （`credential_encryption_key`）。**它不经由应用进程间传递**
    （`07` NF9）——防的是 `/proc` 与异常堆栈泄露。
    """
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CredentialError(
            "credential_encryption_key 不是合法的 Fernet 密钥"
            "（应为 44 字符的 base64）。可用 "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` 生成一个。'
        ) from exc


def decrypt_value(encrypted: bytes, key: str) -> str:
    """解密一份凭证。

    Raises:
        CredentialError: 密文损坏，或密钥不匹配。

    Note:
        **不把 `encrypted` 或异常详情放进错误消息**。`InvalidToken` 的
        原始消息不含明文，但一个"顺手把输入也打出来"的改动就会让它变成泄露点——
        而错误消息是最容易流进日志的东西。
    """
    try:
        return _fernet(key).decrypt(encrypted).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialError(
            "凭证解密失败：密钥与密文不匹配，或密文已损坏。（已刻意不输出任何密文或密钥内容）"
        ) from exc


def apply_credential(
    request: BuiltRequest,
    credential: CredentialMaterial,
) -> BuiltRequest:
    """把凭证注入请求，返回**新的** `BuiltRequest`。

    返回新对象而不是就地修改：`BuiltRequest` 是 `frozen` 的，
    而"原始请求"与"带凭证的请求"在排障时是两个有意义的状态——
    能对比它们才能回答"凭证到底注进去了没有"。

    Raises:
        CredentialError: 未知的 `auth_type`。
    """
    headers = dict(request.headers)
    url = request.url

    if credential.auth_type == "bearer":
        headers["authorization"] = f"Bearer {credential.secret}"

    elif credential.auth_type == "basic":
        # `basic` 的密文按 `用户名:口令` 存——那是一整个字符串，
        # 因为它本来就是"一对"信息，拆成两列没有收益。
        token = base64.b64encode(credential.secret.encode("utf-8")).decode("ascii")
        headers["authorization"] = f"Basic {token}"

    elif credential.auth_type == "api_key_header":
        headers[_DEFAULT_HEADER_NAME.lower()] = credential.secret

    elif credential.auth_type == "api_key_query":
        # 查询串里的凭证**会进审计的 `target_url`**。`guardrail.audit.redact_url`
        # 会把所有查询取值擦成 `[REDACTED]`，所以它不会以明文落库——
        # 但"凭证出现在 URL 里"本身仍是一条值得被记录下来的设计缺陷，
        # 这正是脱敏保留参数名的用意。
        url = _append_query(url, _DEFAULT_QUERY_NAME, credential.secret)

    else:
        raise CredentialError(
            f"未知的 auth_type：{credential.auth_type!r}。"
            "合法取值为 bearer / basic / api_key_header / api_key_query"
            "（`04` §2.10 的 CHECK 约束）。"
        )

    return BuiltRequest(method=request.method, url=url, headers=headers, body=request.body)


def _append_query(url: str, name: str, value: str) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    extra = urlencode({name: value})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))
