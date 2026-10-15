"""认证。**用户令牌与内部令牌是两套。**

对应决策：`05-API契约.md` §5.3、ADR-0022、`10-部署运维.md` §5。

## 两套令牌而不是一套

| 令牌 | 能做什么 | 谁能拿到 |
| :--- | :--- | :--- |
| `api_token` | 调用户端点（提交 Spec、发起 Run、看报告） | 使用者 |
| `internal_token` | 调内部端点（唤醒图推进） | 只在平台内部流转 |

**混用的后果很具体**：内部 Token 能触发图推进——那是一切执行的起点。
若它与用户 Token 可复用，一个泄露的用户凭证就变成了"能干扰任意 Run 的执行"。
`config.py` 里有一条启动期校验器直接拒绝二者相等。

## 环回部署下用户令牌可以为空

`bind_host` 是环回地址时不需要令牌（只有本机能连，而本机就是使用者）。
绑定到别的地址时**强制要求**——那条也是启动期校验（`config.py`），
不是运行期的判断。**"服务起来了但对局域网裸奔"比"启动失败"危险得多**：
后者你会去修，前者你不会。
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from testflow.gateway.deps import GatewayContext
from testflow.gateway.problems import PROBLEM_CONTENT_TYPE

__all__ = ["require_internal_token", "require_user_token"]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _matches(provided: str | None, expected: str | None) -> bool:
    """**常数时间比较。**

    用 `==` 比较令牌会在第一个不同的字符处提前返回，于是"逐字符试出正确令牌"
    在时间上可测。`hmac.compare_digest` 的时间与内容无关。

    这类攻击在网络上噪声很大、通常不可行——但**代价只是换一个函数名**，
    没有任何理由不换。
    """
    if expected is None or provided is None:
        return False
    return hmac.compare_digest(provided, expected)


def require_user_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """用户端点的鉴权。

    环回部署（`api_token` 为空）时**直接放行**——启动期校验已经保证了
    "绑定非环回地址时一定有令牌"，所以这里的判断不会成为漏洞。
    把这条约束放在两处（启动期 + 运行期）看起来冗余，但它们是不同性质的：
    启动期拦的是"配错了"，这里拦的是"请求没带凭证"。
    """
    context: GatewayContext = request.app.state.ctx
    expected = context.settings.api_token
    if expected is None:
        return

    provided = _bearer(authorization)
    if not _matches(provided, expected):
        # **不区分"没带"与"带错了"**：区分它们等于告诉攻击者
        # "这个令牌是对的，只是缺了个头"。
        raise _unauthorized("需要有效的用户令牌")


def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None),
) -> None:
    """内部端点的鉴权。

    用**独立的头**（`X-Internal-Token`）而不是复用 `Authorization`：
    这样一次误发的普通请求不会碰巧带上内部凭证，而审计日志里
    两种调用一眼可辨。

    ⚠️ 内部令牌**没有"环回时可以为空"的豁免**（`internal_token` 是必填项）
    ——它守护的是图推进，那是一条只会被平台自己调用的路径。
    """
    # ⚠️ **从注入的 context 取配置，不要 `load_settings()`。**
    #
    # 第一版调的是全局的 `load_settings()`——那让这个依赖**绕过了应用的
    # 配置注入**，后果有两个：它无法在测试里被替换（于是这条鉴权路径
    # 根本没法测），而且在任何"配置来源不是环境变量"的部署里，
    # 它会和应用的其余部分**用不同的令牌**去比对。
    #
    # 这类"某个地方偷偷读了全局"的 bug 不会报错，只会表现成
    # "明明配对了却说没权限"。
    context: GatewayContext = request.app.state.ctx
    if not _matches(x_internal_token, context.settings.internal_token):
        raise _unauthorized("需要有效的内部令牌")


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


PROBLEM = PROBLEM_CONTENT_TYPE
