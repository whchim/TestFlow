"""`operations` 表的数据访问。

对应决策：`04-数据模型.md` §2.2、DM3（`is_destructive`）、`SpecParser.md` SP1。

Operations 与 Spec 一样是**不可变**的——它们由 Spec 内容派生，Spec 不变则不变。
所以这里也只需要插入与查询。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import Operation

__all__ = ["insert_many", "list_by_spec", "list_by_ids"]


def insert_many(session: Session, operations: Sequence[Operation]) -> int:
    """批量插入。返回条数。

    用 `add_all` 而非 `bulk_save_objects`：ORM 的 unit-of-work 会把这些行
    攒到一次 flush，性能差距在 ≤200 条（`max_operations_per_spec`）的量级上
    可以忽略，而 `bulk_save_objects` 会绕过 ORM 事件与关系维护，
    让"为什么这个对象没进 session"变成一类难查的问题。
    """
    session.add_all(operations)
    session.flush()
    return len(operations)


def list_by_spec(session: Session, spec_id: uuid.UUID) -> list[Operation]:
    """列出某份 Spec 的全部接口。N2 遍历用。

    稳定排序（`method, path`）是刻意的：生成顺序会影响用例的展示顺序，
    让它随机变化会让"同一个 Run 跑两次"的对比变得困难。
    """
    return list(
        session.scalars(
            select(Operation)
            .where(Operation.spec_id == spec_id)
            .order_by(Operation.method, Operation.path)
        )
    )


def list_by_ids(session: Session, ids: Sequence[uuid.UUID]) -> list[Operation]:
    if not ids:
        return []
    return list(session.scalars(select(Operation).where(Operation.id.in_(ids))))
