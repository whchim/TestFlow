"""attribution integrity constraints

Revision ID: 9f2c1a7b4d10
Revises: 722829373fa2
Create Date: 2026-10-17 12:00:00.000000+00:00

对应决策：`03-模块详细设计/LogAnalyzer.md` LA3 / LA6、`04-数据模型.md` §2.7。

## 为什么这三条要放到数据库里

`human_verdict = 'disagree'` 却**没有**记录正确类别，是 LA6 明令禁止的状态：
它让"人工纠错"退化成"点了不同意"，而规则库改进恰恰需要知道**正确类别**
才能回答"该补哪一条规则"。

这类约束写在应用层也能拦住 API 调用，但拦不住：
迁移脚本、手工 SQL、其他写入方、以及将来某个忘了校验的新入口。
写在数据库里，是**所有写入路径的唯一共同出口**（DM7 的同一条原则）。

## 为什么必须可回滚

`10` OP5 / §8：回滚流程依赖 `downgrade()`。空实现会让升级变成单行道。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9f2c1a7b4d10"
down_revision: str | None = "722829373fa2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = (
    "'request_invalid','auth_error','resource_not_found','conflict',"
    "'rate_limited','server_error','response_schema_mismatch',"
    "'assertion_mismatch','network_error','blocked_by_policy','unknown'"
)


def upgrade() -> None:
    op.create_check_constraint(
        "ck_attributions_confidence",
        "attributions",
        "confidence IS NULL OR confidence IN ('high','medium','low')",
    )
    op.create_check_constraint(
        "ck_attributions_human_category",
        "attributions",
        f"human_category IS NULL OR human_category IN ({_CATEGORIES})",
    )
    # LA6：表态为 disagree 时**必须**同时记录正确类别。
    # 同时禁止 agree 带类别——那会让"人同意了这个类别"与"人给了另一个类别"
    # 两种含义混在一行里。
    op.create_check_constraint(
        "ck_attributions_human_verdict_category",
        "attributions",
        "(human_verdict IS NULL AND human_category IS NULL) "
        "OR (human_verdict = 'agree' AND human_category IS NULL) "
        "OR (human_verdict = 'disagree' AND human_category IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_attributions_human_verdict_category", "attributions", type_="check")
    op.drop_constraint("ck_attributions_human_category", "attributions", type_="check")
    op.drop_constraint("ck_attributions_confidence", "attributions", type_="check")
