"""出站目标的值对象。

对应决策：`03-模块详细设计/Guardrail.md` GR3、`04-数据模型.md` §2.1 `declared_targets`。

**为什么单独一个值对象而不是直接用字符串**：白名单的匹配单位是
`scheme + host + port` 的**三元组**，不是"一个地址字符串"。把它表示成三个字段，
`https://api.example.com`（隐含 443）与 `https://api.example.com:443` 就必然
在构造时被归一化成同一个值——而不是依赖某处记得补默认端口。

`guardrail.policy.Target` 是它的运行时对偶：那个用于判定，这个用于存储与解析产出。
二者刻意不合并——`guardrail` 是可独立测试的安全模块，不应反向依赖 `domain`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

__all__ = ["DEFAULT_PORTS", "DeclaredTarget"]

#: 未显式写端口时的默认值。缺了这一步，同一个目标会以两种写法进入白名单。
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True, order=True)
class DeclaredTarget:
    """一个被声明的出站目标。**白名单的匹配单位。**

    用 `slots=True` 与 `frozen=True` 的理由同 `04` 的不可变 Spec：一个目标一旦
    解析出来就不该再变，也就不该有"谁改了它"的问题。
    """

    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @classmethod
    def parse(cls, url: str) -> DeclaredTarget:
        """从 `servers[].url` 解析出目标。

        端口在此**必须显式化**：否则 `https://api.example.com` 与
        `https://api.example.com:443` 会被当成两个不同的白名单条目，
        而它们其实是同一个地方。

        Raises:
            ValueError: 无法解析、scheme 不受支持、或端口非法。
        """
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname  # urlsplit 已做：小写、去方括号、剥离 userinfo

        if not scheme or not host:
            raise ValueError(f"无法解析的目标 URL：{url!r}")
        if scheme not in DEFAULT_PORTS:
            raise ValueError(f"不支持的 scheme：{scheme!r}（仅允许 http / https）")

        try:
            port = parts.port
        except ValueError as exc:  # 端口非数字或超出 0..65535
            raise ValueError(f"目标 URL 的端口无效：{url!r}") from exc

        # 末尾的点是 DNS 的绝对写法（`api.example.com.`），去掉以免同一目标两种写法。
        return cls(scheme=scheme, host=host.rstrip("."), port=port or DEFAULT_PORTS[scheme])
