"""`specs` 表的数据访问。

对应决策：`04-数据模型.md` §2.1、DM2（指纹）、`SpecParser.md` SP5。

**每个函数显式接收 `Session`，模块内不持有任何全局状态。** 理由与
`config.py` 不做模块级单例相同：一旦有全局 session，测试就没法把两个用例
隔离到不同事务里，而"上一个测试留下的数据"是最难查的一类失败。

**Spec 是不可变的**：这里没有、也不该有 `update` 函数。内容变了就是另一行
（指纹不同）。这条性质让 `declared_targets` 这类派生字段可以安全地存在本表上。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from testflow.storage.models import Run, Spec

__all__ = ["delete_orphans_older_than", "get", "get_by_fingerprint", "insert"]


def get(session: Session, spec_id: uuid.UUID) -> Spec | None:
    return session.get(Spec, spec_id)


def get_by_fingerprint(session: Session, fingerprint: str) -> Spec | None:
    """按指纹查已有的 Spec。**这是 SP 幂等性的落点。**

    重复上传同一份文档时命中这里，从而跳过重新解析与重新落 `operations`。
    查询走 `fingerprint` 上的 UNIQUE 索引，代价恒定。
    """
    return session.scalars(select(Spec).where(Spec.fingerprint == fingerprint)).one_or_none()


def delete_orphans_older_than(
    session: Session,
    *,
    cutoff: datetime.datetime,
    batch_size: int = 500,
) -> int:
    """删除**没有任何 Run 引用**且早于 `cutoff` 的 Spec（`04` §5 / `10` §7）。

    ## 为什么必须单独判断"孤儿"

    Spec 不随 Run 级联删除——它可能被多个 Run 复用。反过来，一个 Spec
    只有在**引用它的最后一个 Run 也被清掉之后**才成为孤儿。所以清理顺序
    是先 Run、后 Spec；否则一个刚过期 Run 的 Spec 会多活一天。

    `NOT EXISTS` 而不是 `LEFT JOIN ... IS NULL`：前者语义更直白——
    "不存在任何引用"，并且能走 `ix_runs_spec_id`（PostgreSQL **不会**为
    外键自动建索引，那个索引是 M5 清理任务的一部分）。

    ## 并发新建 Run 的窗口

    查询与删除之间如果恰好有人为这个 Spec 创建了 Run，数据库会因外键
    （`runs.spec_id` 无级联、默认 RESTRICT）拒绝这次删除，而不是造成
    悬空引用。清理任务在下一个周期会重新判断。这比"先删再用"安全：
    **失败一次无副作用，错误地删掉则有。**

    按批删除的理由与 `runs.delete_older_than` 相同。
    """
    total = 0
    while True:
        referenced = exists(select(Run.id).where(Run.spec_id == Spec.id))
        ids = list(
            session.scalars(
                select(Spec.id)
                .where(Spec.created_at < cutoff, ~referenced)
                .order_by(Spec.created_at)
                .limit(batch_size)
            )
        )
        if not ids:
            break

        result = session.execute(
            sa_delete(Spec).where(Spec.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
        session.flush()
        deleted = int(getattr(result, "rowcount", 0) or 0)
        total += deleted
        if deleted == 0:
            break
    return total


def insert(session: Session, spec: Spec) -> Spec:
    """插入一行 Spec。**调用方负责先查指纹去重。**

    这里不做"存在则复用"——那会把一次查询的语义藏进一个名字叫 insert 的函数里，
    而调用方拿到返回值后无从判断到底是新插的还是复用的（`created_at` 会骗人）。
    去重逻辑放在 `SpecParser` 里显式表达。
    """
    session.add(spec)
    session.flush()
    return spec
