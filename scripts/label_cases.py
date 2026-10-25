#!/usr/bin/env python
"""人工标注 Golden Set 的用例准确率（路线 A）。

对应决策：`09-测试策略.md` §5.4、§5.3。

## 两步

```bash
# ① 生成待标注的用例表（会真实调用模型，约 ¥0.02）
uv run python scripts/label_cases.py generate

# ② 人工填写每条用例的 verdict 后，汇总
uv run python scripts/label_cases.py score
```

生成结果的默认位置：`eval/labels/golden-set-cases.yaml`。
**这个文件需要人工编辑**——脚本不会替你判断对错。

## 为什么不在生成时就打分

如果用模型给自己的输出打分，得到的仍然是一个自我参照的指标。
准确率的全部价值来自"另一个人拿着 Spec 逐条看"。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation
from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.spec_context import build_spec_context
from testflow.agents.spec_parser import ParsedOperation, parse_spec
from testflow.agents.test_case_gen import RuleBasedGenerator
from testflow.config import load_settings
from testflow.eval.labels import score_labels, validate_labels
from testflow.guardrail.async_client import build_llm_http_client
from testflow.llm.client import UsageRecord
from testflow.llm.pricing import estimate_cost
from testflow.llm.qwen import QwenProvider

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "eval" / "golden" / "golden_set.yaml"
ANNOTATIONS_PATH = ROOT / "eval" / "golden" / "golden_set.annotations.yaml"
DEFAULT_SHEET = ROOT / "eval" / "labels" / "golden-set-cases.yaml"
RESULTS_DIR = ROOT / "eval" / "results"

_INSTRUCTIONS = """\
对每一条用例填写 verdict：

  correct   请求构造合理，断言与 Spec 一致，且能验证该维度
  incorrect 请求取值、期望状态码或断言有错
  skip      无法判断（例如 Spec 信息不足），请在 note 里说明
  null      还没标

准确率只统计 correct + incorrect，skip 与未标注都不进分母。
如果还想单独统计断言准确率，可以填 assertion_verdicts：
与 assertions 等长的列表，元素是 correct / incorrect / null。
"""

# Windows 终端编码修正（与 eval_golden.py 同一理由）。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} 不是一份 YAML 对象")
    return payload


def _cost(records: list[UsageRecord]) -> Decimal | None:
    total = Decimal(0)
    for record in records:
        row = estimate_cost(
            record.model,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            cached_tokens=record.cached_tokens,
            cache_write_tokens=record.cache_write_tokens,
        )
        if row is None:
            return None
        total += row
    return total


def _case_id(operation: ParsedOperation, dimension: str, used: set[str]) -> str:
    base = f"{operation.method} {operation.path} :: {dimension}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base} #{index}"
        index += 1
    used.add(candidate)
    return candidate


async def _generate(args: argparse.Namespace) -> int:
    raw_spec = SPEC_PATH.read_text(encoding="utf-8")
    annotations = _load_yaml(ANNOTATIONS_PATH)
    parsed = parse_spec(raw_spec)
    expected = {
        (row["method"], row["path"]): row.get("expected_dimensions") or []
        for row in annotations["operations"]
    }

    settings = load_settings()
    if args.model:
        settings = settings.model_copy(update={"llm_model": args.model})
    records: list[UsageRecord] = []
    provider = QwenProvider(
        settings,
        http_client=build_llm_http_client(settings),
        on_usage=records.append,
    )
    generator = LlmCaseGenerator(provider, max_content_retries=args.retries)
    rule_generator = RuleBasedGenerator()
    spec_context = build_spec_context(raw_spec)

    operations_payload: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    total_cases = 0

    for operation in parsed.operations:
        dimensions = derive_dimensions(
            operation_path=operation.path,
            parameters_schema=operation.parameters_schema,
            request_body_schema=operation.request_body_schema,
            responses_schema=operation.responses_schema,
        )
        pure_rule = is_pure_rule_operation(
            operation.parameters_schema,
            operation.request_body_schema,
            operation.responses_schema,
        )
        active = rule_generator if pure_rule else generator

        try:
            result = await active.generate(operation, dimensions, spec_context=spec_context)
        except Exception as exc:  # noqa: BLE001 —— 一个接口失败不该中断整张表
            failures.append(
                {
                    "operation": f"{operation.method} {operation.path}",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            print(f"  ! {operation.method} {operation.path} 生成失败：{type(exc).__name__}")
            continue

        used_ids: set[str] = set()
        cases_payload: list[dict[str, Any]] = []
        for case in result.cases:
            cases_payload.append(
                {
                    "id": _case_id(operation, case.dimension, used_ids),
                    "dimension": case.dimension,
                    "name": case.name,
                    "category": case.category,
                    "request": {
                        "method": operation.method,
                        "path": operation.path,
                        "query": case.request_query,
                        "headers": case.request_headers,
                        "body": case.request_body,
                    },
                    "assertions": [a.model_dump(mode="json") for a in case.assertions],
                    "requires_auth": case.requires_auth,
                    "derived_from": case.derived_from,
                    "confidence": case.confidence,
                    "notes": case.notes,
                    # ↓ 人工填写
                    "verdict": None,
                    "assertion_verdicts": [],
                    "note": "",
                }
            )
        total_cases += len(cases_payload)
        operations_payload.append(
            {
                "method": operation.method,
                "path": operation.path,
                "expected_dimensions": expected.get((operation.method, operation.path), []),
                "spec": {
                    "parameters": operation.parameters_schema,
                    "request_body": operation.request_body_schema,
                    "responses": operation.responses_schema,
                },
                "cases": cases_payload,
            }
        )
        print(f"  {operation.method:<6} {operation.path:<28} {len(cases_payload)} 条")

    generated_at = datetime.now(UTC)
    cost = _cost(records)
    sheet = {
        "version": 1,
        "generated_at": generated_at.isoformat(),
        "model": provider.model_name,
        "golden_set_sha256": _sha256(SPEC_PATH),
        "instructions": _INSTRUCTIONS,
        "usage": {
            "calls": len(records),
            "prompt_tokens": sum(r.prompt_tokens for r in records),
            "cached_tokens": sum(r.cached_tokens for r in records),
            "cost_cny": str(cost) if cost is not None else None,
        },
        "operations": operations_payload,
        "generation_failures": failures,
    }

    out_path = Path(args.out or DEFAULT_SHEET)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(sheet, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )

    print(
        f"\n共 {total_cases} 条用例，写入 {out_path.relative_to(ROOT)}\n"
        f"调用 {len(records)} 次，成本 {'未知' if cost is None else f'￥{cost:.6f}'}"
    )
    if failures:
        print(f"⚠️  {len(failures)} 个接口生成失败，见文件末尾的 generation_failures。")
    print(f"\n下一步：编辑 {out_path.relative_to(ROOT)}，填写 verdict，然后运行")
    print("  uv run python scripts/label_cases.py score")
    return 0 if not failures else 1


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _score(args: argparse.Namespace) -> int:
    sheet_path = Path(args.sheet or DEFAULT_SHEET)
    sheet = _load_yaml(sheet_path)

    problems = validate_labels(sheet)
    if problems:
        print("标注表有问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    score = score_labels(sheet)
    print("\n== 用例准确率（人工标注）==")
    print(
        f"  可评分 {score.overall.labeled}/{score.total} 条"
        f"（跳过 {score.overall.skipped}，未标注 {score.overall.unlabeled}）"
    )
    print(
        f"  用例准确率 {_pct(score.case_accuracy)}"
        f"（正确 {score.overall.correct} / 错误 {score.overall.incorrect}）"
    )
    print(f"  断言准确率 {_pct(score.assertion_accuracy)}")

    print("\n  按接口：")
    for key, bucket in score.by_operation.items():
        print(f"    {key:<34} {_pct(bucket.accuracy):>6}  （{bucket.correct}/{bucket.labeled}）")

    bad = {
        key: bucket
        for key, bucket in score.by_dimension.items()
        if bucket.incorrect
        or (bucket.labeled and bucket.accuracy is not None and bucket.accuracy < 1)
    }
    if bad:
        print("\n  低于 100% 的维度：")
        for key, bucket in sorted(bad.items()):
            print(
                f"    {key:<40} {_pct(bucket.accuracy):>6}  （{bucket.correct}/{bucket.labeled}）"
            )

    if score.failures:
        print("\n  ⚠️ 生成失败、未参与评分的接口：")
        for failure in score.failures:
            print(f"    {failure['operation']}：{failure['error'][:120]}")

    if score.overall.unlabeled and not args.allow_partial:
        print(
            f"\n还有 {score.overall.unlabeled} 条未标注。"
            "全部标完再打分，或加 --allow-partial 生成一份部分结果。",
            file=sys.stderr,
        )
        return 2

    payload = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "sheet": str(sheet_path.relative_to(ROOT)),
        "model": sheet.get("model"),
        "golden_set_sha256": sheet.get("golden_set_sha256"),
        **score.as_dict(),
    }
    results_dir = Path(args.results_dir or RESULTS_DIR)
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-labels.json"
    if not args.no_write:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n结果已写入 {out_path.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden Set 用例准确率的人工标注")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="生成待标注的用例表（会调用模型）")
    generate.add_argument("--out", help=f"输出路径（默认 {DEFAULT_SHEET.relative_to(ROOT)}）")
    generate.add_argument("--model", help="覆盖 .env 里的模型名")
    generate.add_argument("--retries", type=int, default=2, help="内容校验失败的重试次数；默认 2")
    generate.set_defaults(func=lambda args: asyncio.run(_generate(args)))

    score = sub.add_parser("score", help="汇总人工标注")
    score.add_argument("--sheet", help=f"标注表路径（默认 {DEFAULT_SHEET.relative_to(ROOT)}）")
    score.add_argument("--results-dir")
    score.add_argument("--allow-partial", action="store_true", help="允许在未标完时输出部分结果")
    score.add_argument("--no-write", action="store_true", help="只打印，不写结果文件")
    score.set_defaults(func=_score)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
