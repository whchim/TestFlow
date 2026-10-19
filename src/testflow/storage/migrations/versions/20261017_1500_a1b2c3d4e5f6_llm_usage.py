"""llm usage log

Revision ID: a1b2c3d4e5f6
Revises: 9f2c1a7b4d10
Create Date: 2026-10-17 15:00:00.000000+00:00

对应决策：`03/TestCaseGen.md` §8.3、`04-数据模型.md` §2.11、M4 验收。

## 为什么这里只有一张表，没有回填

`audit_logs` 历史上没有 token 字段，也没有办法补——那些调用已经发生了，
用量信息从来没有被采集过。M4 从这一刻开始记，**不伪造历史**。
报告里的"未配置单价/无用量"是诚实的空值，不是 0。

## 为什么 `run_id` 是 CASCADE 而不是 SET NULL

用量是**这个 Run 的成本**；Run 删了，成本记录没有独立保留价值。
审计相反（`SET NULL`）：它要回答"曾经发生过什么"，与业务实体是否还在无关。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9f2c1a7b4d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('case_generation','attribution')",
            name="ck_llm_usage_purpose",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 "
            "AND cached_tokens >= 0 AND cache_write_tokens >= 0",
            name="ck_llm_usage_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "cached_tokens <= prompt_tokens",
            name="ck_llm_usage_cached_le_prompt",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_run", "llm_usage", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_usage_run", table_name="llm_usage")
    op.drop_table("llm_usage")
