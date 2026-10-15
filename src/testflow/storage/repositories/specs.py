"""`specs` 表的数据访问。

对应决策：`04-数据模型.md` §2.1、DM2（指纹）、`SpecParser.md` SP5。

**每个函数显式接收 `Session`，模块内不持有任何全局状态。** 理由与
`config.py` 不做模块级单例相同：一旦有全局 session，测试就没法把两个用例
隔离到不同事务里，而"上一个测试留下的数据"是最难查的一类失败。

**Spec 是不可变的**：这里没有、也不该有 `update` 函数。内容变了就是另一行
（指纹不同）。这条性质让 `declared_targets` 这类派生字段可以安全地存在本表上。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import Spec

__all__ = ["get", "get_by_fingerprint", "insert"]


def get(session: Session, spec_id: uuid.UUID) -> Spec | None:
    return session.get(Spec, spec_id)


def get_by_fingerprint(session: Session, fingerprint: str) -> Spec | None:
    """按指纹查已有的 Spec。**这是 SP 幂等性的落点。**

    重复上传同一份文档时命中这里，从而跳过重新解析与重新落 `operations`。
    查询走 `fingerprint` 上的 UNIQUE 索引，代价恒定。
    """
    return session.scalars(select(Spec).where(Spec.fingerprint == fingerprint)).one_or_none()


def insert(session: Session, spec: Spec) -> Spec:
    """插入一行 Spec。**调用方负责先查指纹去重。**

    这里不做"存在则复用"——那会把一次查询的语义藏进一个名字叫 insert 的函数里，
    而调用方拿到返回值后无从判断到底是新插的还是复用的（`created_at` 会骗人）。
    去重逻辑放在 `SpecParser` 里显式表达。
    """
    session.add(spec)
    session.flush()
    return spec
