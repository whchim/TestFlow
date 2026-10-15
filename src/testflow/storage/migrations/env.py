"""Alembic 运行环境。

数据库地址**从应用配置读取**，不写进 `alembic.ini`——理由同 `08` EN3：
配置只有一处来源。这样迁移与运行时永远连的是同一个库，不会出现
"迁移改了 A 库、应用连的是 B 库"。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from testflow.config import load_settings
from testflow.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return load_settings().database_url


def run_migrations_offline() -> None:
    """生成 SQL 而不连库（`alembic upgrade head --sql`）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 让 autogenerate 能识别类型变化，而不只是增删列
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # 迁移是一次性任务，不需要连接池
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # 命名约定由 models 里的显式 name= 提供；此处不额外注入，
            # 避免"模型里手写的名字"与"自动生成的名字"两套并存。
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
