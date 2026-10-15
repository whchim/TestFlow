"""`credentials` 表的数据访问。

对应决策：`04-数据模型.md` §2.10、PRD SR-6、`TestExecutor.md` §5 / TE4。

⚠️ **本模块只负责取行，不负责解密。** 解密在 `execution/credentials.py`——
把它分开是为了让"密文在哪里出现"这件事可查：`encrypted_value` 只在本模块
与执行层之间流动，报错、日志、序列化都不经过它。

**凭证永远不进入用例。** `cases` 表里没有凭证列，生成阶段也没有——
这不是靠"记得别放"保证的，是靠数据模型：凭证根本不在会被序列化给 LLM 的
对象里。执行时才按 host 查出来注入（TE4）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import Credential

__all__ = ["get_by_host", "get_by_name_and_host", "list_all"]


def get_by_host(session: Session, scope_host: str) -> Credential | None:
    """按目标 host 取凭证。执行时的唯一入口（TE4）。

    `scope_host` 的形态与 `declared_targets` 一致（含端口语义，见
    `TestExecutor.md` §5）——两边不一致会导致"白名单放行了、凭证找不到"，
    而现象是请求收到 401，看起来像目标服务的问题。

    Note:
        同一 host 可能配了多个名字的凭证（如"只读"与"读写"两个）。
        M1 取第一条即可——不存在"选哪个"的问题，因为 M1 还没有凭证管理界面。
        真要做选择时，应当在 `cases` 上指明用哪个，而不是让这里猜。
    """
    return session.scalars(
        select(Credential).where(Credential.scope_host == scope_host).limit(1)
    ).one_or_none()


def get_by_name_and_host(session: Session, name: str, scope_host: str) -> Credential | None:
    """按 (名字, host) 取——对应表上的 `uq_credentials_name_host`。"""
    return session.scalars(
        select(Credential).where(Credential.name == name, Credential.scope_host == scope_host)
    ).one_or_none()


def list_all(session: Session) -> list[Credential]:
    return list(
        session.scalars(select(Credential).order_by(Credential.scope_host, Credential.name))
    )
