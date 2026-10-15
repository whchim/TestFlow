"""数据库连接。

按 `08-工程规范.md` EN3：**配置由调用方注入，不在导入时读取环境变量**——
否则测试无法替换配置，且"导入即连库"会让 import 产生副作用。
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from testflow.config import Settings

__all__ = ["create_db_engine", "create_session_factory"]


def create_db_engine(settings: Settings) -> Engine:
    """构造引擎。

    `pool_pre_ping` 是必需的：单机部署下 PostgreSQL 可能因重启或空闲超时
    断开连接，而连接池持有的死连接会让下一个请求直接失败。它的代价是
    每次取连接多一次轻量往返。
    """
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """构造 Session 工厂。

    `expire_on_commit=False`：提交后仍可读取对象属性，避免在"提交 → 序列化
    响应"这类流程中触发意外的延迟加载（N+1 或已关闭事务的报错）。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
