"""Gateway 的进程级资源。**全部惰性构造。**

对应决策：`02` §2.2（网关是薄层）、`08` §1（入口点不被覆盖率要求）、
`scripts/export-openapi.sh`（CI 第 ④ 步会调用 `create_app()`）。

## 为什么必须惰性

`scripts/export-openapi.sh` 在 CI 里调用 `create_app()` 导出接口契约快照。
**如果 `create_app()` 在构造时就连数据库、连 Redis，那一步就会失败**——
而 CI 的 ④ 步本意是"比对契约与代码是否一致"，不是"检查数据库在不在"。

所以：`create_app()` 只装配路由与中间件，一个连接都不建。
真正的连接发生在**第一次请求**时。

这也是一个更普遍的原则：**能被静态导入的东西，不该在导入时产生副作用。**
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from testflow.config import Settings
from testflow.storage.db import create_db_engine, create_session_factory

__all__ = ["GatewayContext", "Producer"]


class Producer(Protocol):
    """把推进指令投递到编排队列。**由入口点注入。**

    Gateway 是**纯生产者**（`02` §2.2）：它不 import `graph/`，也不 import
    `execution/`——它只知道"有个 `orchestrate` 队列，往里丢一个 run_id"。
    这样网关保持薄层，而"图长什么样"的变化不会波及到它。
    """

    def orchestrate(self, run_id: uuid.UUID, reason: str) -> None: ...


@dataclass
class GatewayContext:
    """所有进程级资源。**字段默认全为 `None`，用的时候才建。**"""

    settings: Settings
    producer: Producer | None = None
    _engine: Engine | None = field(default=None, repr=False)
    _sessions: sessionmaker[Session] | None = field(default=None, repr=False)

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_db_engine(self.settings)
        return self._engine

    @property
    def sessions(self) -> sessionmaker[Session]:
        if self._sessions is None:
            self._sessions = create_session_factory(self.engine)
        return self._sessions

    def session(self) -> Session:
        return self.sessions()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._sessions = None

    def require_producer(self) -> Producer:
        """取生产者，缺失时**明确报错**。

        不在这里 fail-open（比如"投递不了就跳过"）：那会让 `POST /api/runs`
        返回 202（"已接受"）而实际上什么都没发生——用户等一个永远不会开始的
        Run，而没有任何错误。宁可返回 503。
        """
        if self.producer is None:
            raise RuntimeError(
                "Gateway 未接上编排队列的生产者。入口点必须在启动时注入它——"
                "否则创建 Run 会返回「已接受」而实际没有任何东西被执行。"
            )
        return self.producer


def _unused(_: Any) -> None:  # pragma: no cover
    """保留 `Any` 导入。"""
