"""`llm_usage` 表的数据访问。

对应决策：`03/TestCaseGen.md` §8.3、M4 验收。

**只追加，不更新。** 每次调用就是一行——包括重试的第二次、第三次。
把它当成"事件流"而不是"每个接口的最新状态"，成本才算得对：
一次不合格的生成 + 两次重试，是三笔真实支出，不是一笔。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from testflow.storage.models import LLMUsage

__all__ = ["list_by_run", "record"]


def record(
    session: Session,
    *,
    run_id: uuid.UUID | None,
    purpose: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    total_tokens: int,
    attempt: int,
    elapsed_ms: int,
    trace_id: str | None = None,
) -> LLMUsage:
    row = LLMUsage(
        run_id=run_id,
        purpose=purpose,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=total_tokens,
        attempt=attempt,
        elapsed_ms=elapsed_ms,
        trace_id=trace_id,
    )
    session.add(row)
    session.flush()
    return row


def list_by_run(session: Session, run_id: uuid.UUID) -> Sequence[LLMUsage]:
    return list(
        session.scalars(
            select(LLMUsage)
            .where(LLMUsage.run_id == run_id)
            # `id` 是自增序列，天然按写入顺序；`created_at` 在同一事务里
            # 可能完全相同，不能单独用来排序。
            .order_by(LLMUsage.id)
        )
    )
