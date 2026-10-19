"""`llm_usage` 的落库、聚合与报告接线。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from testflow.agents.report_agent import build_report
from testflow.llm.client import UsageRecord
from testflow.storage.repositories import llm_usage as llm_usage_repo
from testflow.storage.usage_sink import make_usage_sink

pytestmark = pytest.mark.integration


def _record(run_id: uuid.UUID | None, **overrides) -> UsageRecord:
    base = {
        "run_id": run_id,
        "trace_id": "trace",
        "purpose": "case_generation",
        "model": "qwen3.7-flash",
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "cached_tokens": 200,
        "cache_write_tokens": 0,
        "total_tokens": 1_100,
        "attempt": 1,
        "elapsed_ms": 120,
    }
    base.update(overrides)
    return UsageRecord(**base)


def test_sink_writes_a_row_with_the_run_id(session, make_spec, make_run) -> None:
    spec = make_spec()
    run = make_run(spec)
    factory = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    make_usage_sink(factory)(_record(run.id))

    rows = llm_usage_repo.list_by_run(session, run.id)
    assert len(rows) == 1
    assert rows[0].purpose == "case_generation"
    assert rows[0].cached_tokens == 200
    assert rows[0].trace_id == "trace"


def test_sink_swallows_write_failures(caplog) -> None:
    """指标写入失败不能影响一次已付费的调用。"""

    class _BrokenSessions:
        def __call__(self):
            raise RuntimeError("数据库挂了")

    make_usage_sink(_BrokenSessions())(_record(uuid.uuid4()))  # 不抛


def test_nullable_run_id_is_allowed(session, make_spec, make_run) -> None:
    """离线基准/无 Run 上下文的调用也要能记，只是不属于任何 Run。"""
    factory = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    make_usage_sink(factory)(_record(None))

    from sqlalchemy import select

    from testflow.storage.models import LLMUsage

    row = session.scalars(select(LLMUsage).where(LLMUsage.run_id.is_(None)).limit(1)).one_or_none()
    assert row is not None


def test_purpose_check_rejects_unknown_values(session) -> None:
    from testflow.storage.models import LLMUsage

    session.add(
        LLMUsage(
            run_id=None,
            purpose="something_else",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cached_tokens=0,
            cache_write_tokens=0,
            total_tokens=2,
            attempt=1,
            elapsed_ms=1,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_report_reads_usage_and_computes_cost(
    session, make_spec, make_operation, make_run, make_case
) -> None:
    spec = make_spec()
    op = make_operation(spec)
    run = make_run(spec)
    make_case(run, op, review_status="approved")
    factory = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    sink = make_usage_sink(factory)
    sink(_record(run.id))
    sink(_record(run.id, purpose="attribution", elapsed_ms=500))

    report = build_report(session, run.id)
    usage = report.content["llm_usage"]

    assert usage["calls"] == 2
    assert usage["generation_calls"] == 1
    assert usage["attribution_calls"] == 1
    assert usage["cached_tokens"] == 400
    assert usage["cost_cny"] > 0
    assert usage["p95_latency_ms"] == 500
    assert usage["generation_acceptance_rate"] == 1.0
