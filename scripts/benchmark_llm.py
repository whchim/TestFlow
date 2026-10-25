#!/usr/bin/env python
"""M4 基准：LLM 参数对比、成本、缓存命中与 P95。

对应决策：`11-开发路线图.md` M4 验收、`02-架构设计.md` §5.3 / §5.4。

## 它回答三个问题

1. **参数怎么选**：思考模式开/关、采样参数，在**同一批接口**上谁更快、
   首次通过率更高、更便宜。
2. **一次 Run 花多少钱**：用真实的 `usage` 乘单价，而不是估算。
3. **缓存有没有生效**：`cached_tokens > 0` 才算真的命中。长期为 0 是
   缺陷（`07` §5.3），不是波动。

## 用法

```bash
# 参数对比（会真实调用，产生费用）
uv run python scripts/benchmark_llm.py --repeat 2

# 读取某次真实 Run 的用量，不调用模型
uv run python scripts/benchmark_llm.py --run-id <uuid>
```

**不进 CI**：假 Provider 不产生真实 usage，也无法证明缓存命中。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation
from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.spec_parser import ParsedOperation, parse_spec
from testflow.agents.validation import validate_generation
from testflow.config import load_settings
from testflow.guardrail.async_client import build_llm_http_client
from testflow.llm.client import UsageRecord
from testflow.llm.pricing import estimate_cost
from testflow.llm.qwen import QwenProvider
from testflow.observability.stats import percentile

_DEFAULT_MAX_OPERATIONS = 6


def _load_spec(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    # 默认用 fixture 自己的接口文档——它同时覆盖"简单接口"与"递归/大响应"。
    from testflow.testing.fixture_server import app

    document = app.openapi()
    document["servers"] = [{"url": "http://fixture:9000"}]
    document["paths"] = {
        path: value for path, value in document["paths"].items() if not path.startswith("/__")
    }
    return json.dumps(document)


def _select_operations(
    operations: list[ParsedOperation], *, wanted: list[str], limit: int
) -> list[ParsedOperation]:
    if wanted:
        selected = [
            op for op in operations if f"{op.method} {op.path}" in wanted or op.path in wanted
        ]
    else:
        # 只挑需要 LLM 的接口：纯规则接口没有调用，放进基准会稀释样本。
        selected = [
            op
            for op in operations
            if not is_pure_rule_operation(
                op.parameters_schema, op.request_body_schema, op.responses_schema
            )
        ]
    return selected[:limit]


def _dimensions_for(op: ParsedOperation):
    return derive_dimensions(
        operation_path=op.path,
        parameters_schema=op.parameters_schema,
        request_body_schema=op.request_body_schema,
        responses_schema=op.responses_schema,
    )


def _cost(records: list[UsageRecord]) -> Decimal | None:
    total = Decimal(0)
    for record in records:
        row_cost = estimate_cost(
            record.model,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            cached_tokens=record.cached_tokens,
            cache_write_tokens=record.cache_write_tokens,
        )
        if row_cost is None:
            return None
        total += row_cost
    return total


def _summarize(
    records: list[UsageRecord], *, operation_latencies: list[int], passes: int, total: int
):
    prompt = sum(r.prompt_tokens for r in records)
    cached = sum(r.cached_tokens for r in records)
    cost = _cost(records)
    return {
        "first_pass": (passes / total) if total else None,
        "calls": len(records),
        "prompt": prompt,
        "completion": sum(r.completion_tokens for r in records),
        "cached": cached,
        "cache_rate": (cached / prompt) if prompt else None,
        "cost": cost,
        "call_p50": percentile([r.elapsed_ms for r in records], 0.50),
        "call_p95": percentile([r.elapsed_ms for r in records], 0.95),
        "op_p95": percentile(operation_latencies, 0.95),
    }


def _print_row(label: str, stats: dict) -> None:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.0%}"

    cost = stats["cost"]
    print(
        f"{label:<22} 首次通过 {pct(stats['first_pass']):>5}  "
        f"调用 {stats['calls']:>2}  "
        f"输入 {stats['prompt']:>7}  输出 {stats['completion']:>6}  "
        f"缓存 {stats['cached']:>7}（{pct(stats['cache_rate'])}）  "
        f"成本 {'未知' if cost is None else f'￥{cost:.6f}'}  "
        f"单次 P50/P95 {stats['call_p50']}/{stats['call_p95']}ms  "
        f"操作 P95 {stats['op_p95']}ms"
    )


async def _run_grid(args: argparse.Namespace) -> int:
    settings = load_settings()
    parsed = parse_spec(_load_spec(args.spec))
    operations = _select_operations(
        parsed.operations, wanted=args.operations, limit=args.max_operations
    )
    if not operations:
        print("没有可基准的接口。", file=sys.stderr)
        return 1

    print(
        f"模型 {args.model or settings.llm_model} · "
        f"接口 {len(operations)} 个 · 重复 {args.repeat} 次"
    )
    for op in operations:
        print(f"  - {op.method} {op.path}")

    variants = [True, False] if args.thinking == "both" else [args.thinking == "on"]
    for thinking in variants:
        variant = settings.model_copy(
            update={
                "llm_enable_thinking_generation": thinking,
                "llm_temperature": args.temperature,
                "llm_top_p": args.top_p,
                "llm_seed": args.seed,
            }
        )
        records: list[UsageRecord] = []
        provider = QwenProvider(
            variant,
            http_client=build_llm_http_client(variant),
            model=args.model,
            on_usage=records.append,
        )
        generator = LlmCaseGenerator(provider, max_content_retries=args.retries, backoff_s=0)

        operation_latencies: list[int] = []
        passes = 0
        total = 0
        for _ in range(args.repeat):
            for op in operations:
                dimensions = _dimensions_for(op)
                started = time.perf_counter()
                try:
                    result = await generator.generate(op, dimensions)
                except Exception as exc:  # noqa: BLE001 —— 基准要记录失败而不是中断
                    print(f"  ! {op.method} {op.path} 失败：{type(exc).__name__}", file=sys.stderr)
                    total += 1
                    continue
                operation_latencies.append(int((time.perf_counter() - started) * 1000))
                total += 1
                if validate_generation(
                    responses_schema=op.responses_schema,
                    requested=dimensions,
                    result=result,
                    operation_path=op.path,
                    request_body_schema=op.request_body_schema,
                ).ok:
                    passes += 1

        _print_row(
            f"thinking={'on' if thinking else 'off'}",
            _summarize(
                records, operation_latencies=operation_latencies, passes=passes, total=total
            ),
        )
    return 0


def _run_from_db(run_id: str) -> int:
    from testflow.storage.db import create_db_engine, create_session_factory
    from testflow.storage.repositories import llm_usage as llm_usage_repo

    settings = load_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            rows = llm_usage_repo.list_by_run(session, uuid.UUID(run_id))
    finally:
        engine.dispose()

    if not rows:
        print(f"Run {run_id} 没有任何 llm_usage 记录。", file=sys.stderr)
        return 1

    for purpose in sorted({row.purpose for row in rows}):
        subset = [row for row in rows if row.purpose == purpose]
        print(f"== {purpose} ==")
        total = Decimal(0)
        unpriced = set()
        for row in subset:
            cost = estimate_cost(
                row.model,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cached_tokens=row.cached_tokens,
                cache_write_tokens=row.cache_write_tokens,
            )
            if cost is None:
                unpriced.add(row.model)
            else:
                total += cost
        prompt = sum(row.prompt_tokens for row in subset)
        cached = sum(row.cached_tokens for row in subset)
        print(f"  调用 {len(subset)} 次")
        print(f"  输入 {prompt} / 输出 {sum(r.completion_tokens for r in subset)} / 缓存 {cached}")
        print(f"  缓存命中率 {'—' if prompt == 0 else f'{cached / prompt:.0%}'}")
        print(
            f"  P50/P95 {percentile([r.elapsed_ms for r in subset], 0.5)}/"
            f"{percentile([r.elapsed_ms for r in subset], 0.95)} ms"
        )
        cost_text = f"未知（{'、'.join(sorted(unpriced))}）" if unpriced else f"￥{total:.6f}"
        print(f"  成本 {cost_text}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 LLM 基准")
    parser.add_argument("--spec", help="OpenAPI 文档路径；默认用 fixture")
    parser.add_argument("--operations", nargs="*", default=[], help="只测这些接口（如 /echo）")
    parser.add_argument("--max-operations", type=int, default=_DEFAULT_MAX_OPERATIONS)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--retries", type=int, default=0, help="内容重试次数；默认 0（测首次通过率）"
    )
    parser.add_argument("--model")
    parser.add_argument("--thinking", choices=["on", "off", "both"], default="both")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id", help="读取已有 Run 的用量，不调用模型")
    args = parser.parse_args()

    if args.run_id:
        return _run_from_db(args.run_id)
    return asyncio.run(_run_grid(args))


if __name__ == "__main__":
    raise SystemExit(main())
