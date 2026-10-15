"""共享测试夹具。

`settings_factory` 存在的理由：`Settings` 有五个必填项，每个用到它的测试
文件都重复一遍那五行的字典，改一个字段名就要改多处。集中在这里。

**`_env_file=None` 是刻意的**：让测试不受本机 `.env` 影响。否则开发机上的
配置会悄悄改变测试行为——"在我机器上通过"的经典来源。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from testflow.config import Settings

#: 构造 Settings 所需的必填项，值本身无语义，只求能通过校验。
_REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/db",
    "internal_token": "internal-token",
    "credential_encryption_key": "fernet-key-placeholder",
    "llm_base_url": "https://example.invalid/v1",
    "llm_api_key": "sk-placeholder",
}


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """返回一个构造 `Settings` 的工厂，可按需覆盖任意字段。"""

    def _make(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **{**_REQUIRED, **overrides})

    return _make
