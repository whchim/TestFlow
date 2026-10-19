"""把 LLM 用量落到 `llm_usage` 的**通用回调**。

与 `audit_sink.py` 同一个形状，理由也相同：

- **独立的短事务**：用量记录的是"这次调用已经计费"，不该随业务事务回滚
  而消失——业务失败不代表模型免费。
- **失败吞掉并记日志**：指标写入失败不能影响一次已付费的调用，更不能让
  Run 失败。但必须留下痕迹，否则"成本一直是 0"会以最难注意的形式存在。
- **同步回调**：编排层 `--pool=solo`，同一时刻只有一个 Run 在推进，
  几毫秒的写入在几十秒的模型调用面前可以忽略（同 `audit_sink` 的取舍）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from testflow.llm.client import UsageRecord, UsageSink
from testflow.observability.logging import get_logger
from testflow.storage.repositories import llm_usage as llm_usage_repo

__all__ = ["make_usage_sink"]

logger = get_logger(__name__)


def make_usage_sink(sessions: sessionmaker[Session]) -> UsageSink:
    def _write(record: UsageRecord) -> None:
        try:
            with sessions() as session:
                llm_usage_repo.record(
                    session,
                    run_id=record.run_id,
                    purpose=record.purpose,
                    model=record.model,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    cached_tokens=record.cached_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    total_tokens=record.total_tokens,
                    attempt=record.attempt,
                    elapsed_ms=record.elapsed_ms,
                    trace_id=record.trace_id,
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "用量写入失败 model=%s purpose=%s tokens=%s",
                record.model,
                record.purpose,
                record.total_tokens,
            )

    return _write
