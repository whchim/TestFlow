"""cleanup indexes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-17 18:00:00.000000+00:00

对应决策：`04-数据模型.md` §5、`10-部署运维.md` §7（M5 数据清理任务）。

## 为什么清理任务需要自己的索引

三个查询都是"全表扫"的天然候选：

* `DELETE FROM runs WHERE created_at < cutoff` —— 表里最会变大的就是 runs
* `DELETE FROM specs WHERE created_at < ... AND NOT EXISTS (...)` —— 同上
* `NOT EXISTS (SELECT 1 FROM runs WHERE spec_id = ...)` —— PostgreSQL
  **不会**为外键自动建索引，没有它时每个 Spec 都扫一遍 runs

每日任务在小数据量下看不出差别；差别出现在运行一年之后——
而那时"清理任务变慢"不会报警，只会越来越久。

## 为什么不建 `runs(retention)` 之类的部分索引

90 天的删除线是**配置**（`run_retention_days`），可能被改。把配置值写进
索引定义会让改配置时悄悄用不上索引——那正是"改了配置却没有效果"的一类
问题。`created_at` 上的普通 B-tree 对任意 cutoff 都有效。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_runs_created_at", "runs", ["created_at"], unique=False)
    op.create_index("ix_runs_spec_id", "runs", ["spec_id"], unique=False)
    op.create_index("ix_specs_created_at", "specs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_specs_created_at", table_name="specs")
    op.drop_index("ix_runs_spec_id", table_name="runs")
    op.drop_index("ix_runs_created_at", table_name="runs")
