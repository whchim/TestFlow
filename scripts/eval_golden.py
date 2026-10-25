#!/usr/bin/env python
"""L3 评测：在 Golden Set 上测**派生算法**与**生成质量**。

对应决策：`09-测试策略.md` §5（L3）、§5.2（覆盖率的两个层次）、
§5.3（Golden Set）、§5.5（档位对比多轮）、§5.7（结果留痕）。

## 它测的不是 LLM

这是本项目最容易误解的一点。L3 的**第一层**只做一件事：

    人工标注的维度清单  vs  阶段 A 实际派生的维度清单

两边都不一样，说明我们的派生算法有系统性盲区——而生产环境里的覆盖率
**永远发现不了**，因为分母就是阶段 A 自己算的（§5.2 的自我参照陷阱）。

第一层是纯算法、零成本，所以可以进 CI（`tests/unit/test_golden_set.py`
跑的是同一份标注）。第二层（生成完整性）才需要真实调用模型。

## 用法

```bash
# 第一层：只比维度派生，不花钱
uv run python scripts/eval_golden.py

# 第二层：真实生成。TS6 要求每档至少 3 轮，报告均值与极差。
uv run python scripts/eval_golden.py --generate --repeat 3 --thinking both
```

结果写到 `eval/results/`（随代码入库，使"质量随版本的变化"可见，§5.7）。

**不进 CI 的只有第二层**（EN8）：它花真钱，且 LLM 输出有方差。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from testflow.agents.dimensions import derive_dimensions, is_pure_rule_operation
from testflow.agents.llm_case_gen import LlmCaseGenerator
from testflow.agents.spec_context import build_spec_context
from testflow.agents.spec_parser import ParsedOperation, parse_spec
from testflow.agents.test_case_gen import RuleBasedGenerator, _classify_confidence
from testflow.agents.validation import validate_generation
from testflow.config import load_settings
from testflow.eval.golden import OperationDiff, evaluate_derivation, summarize_derivation
from testflow.guardrail.async_client import build_llm_http_client
from testflow.guardrail.errors import GuardrailTimeout, GuardrailTransportError
from testflow.llm.client import UsageRecord
from testflow.llm.pricing import estimate_cost
from testflow.llm.qwen import QwenProvider
from testflow.observability.stats import percentile

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "eval" / "golden"
DEFAULT_SPEC = GOLDEN_DIR / "golden_set.yaml"
DEFAULT_ANNOTATIONS = GOLDEN_DIR / "golden_set.annotations.yaml"
RESULTS_DIR = ROOT / "eval" / "results"

_PROMPT_FILE = ROOT / "src" / "testflow" / "agents" / "prompts.py"

#: 与方法无关的终端编码修正。中文版 Windows 的 locale 是 GBK，而这份脚本
#: 输出大量中文与全角符号；不修正的话，看到的是乱码——而乱码会让人以为
#: **评测本身失败了**，实际上只是输出流的问题。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


# ---------------------------------------------------------------- 数据结构


@dataclass
class GenerationRow:
    method: str
    path: str
    llm_called: bool
    first_pass: bool
    case_count: int
    covered: int
    requested: int
    missing_dimensions: list[str] = field(default_factory=list)
    low_confidence_ok: bool = True
    error: str | None = None
    #: `transport`（超时/连接失败）与 `content`（L1/L2 不合格）必须分开。
    #: 把超时算进"首次通过率"会让网络抖动看起来像生成质量下降——
    #: 而这两件事的处置完全不同：一个重跑，一个改提示词。
    error_kind: str | None = None
    #: 标注为纯规则路径的接口**必须产生 0 次 LLM 调用**（TG5）。
    #: 记录在这里，而不是只依赖 `is_pure_rule_operation()` 的返回值。
    pure_rule_expected: bool = False

    @property
    def coverage(self) -> float:
        return (self.covered / self.requested) if self.requested else 1.0


# ---------------------------------------------------------------- 输入加载


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} 不是一份 YAML 对象")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001 —— 没有 git 时不该让评测失败
        return "unknown"


def _git_dirty() -> bool:
    """工作区是否有未提交改动。

    `git_commit` 单独存在时会**高估可追溯性**：在一个尚未提交的改动上跑评测，
    结果文件会写着一个与这份结果无关的旧提交号。`dirty: true` 把这件事说破。
    """
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(output.strip())


# ---------------------------------------------------------------- 第二层


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


async def evaluate_generation(
    operations: list[ParsedOperation],
    annotations: dict[str, Any],
    *,
    settings: Any,
    repeat: int,
    retries: int,
    spec_context: Any,
) -> dict[str, Any]:
    """在每一轮里逐接口生成，记录首次通过率与维度覆盖。

    **纯规则接口走 `RuleBasedGenerator`**，与生产路径一致（TG5）。
    它必须产生 0 次 LLM 调用——这一点由 `llm_called` 字段显式记录，
    而不是靠"相信分类函数"。
    """
    annotation_by_key = {(row["method"], row["path"]): row for row in annotations["operations"]}
    rule_generator = RuleBasedGenerator()
    records: list[UsageRecord] = []
    provider = QwenProvider(
        settings,
        http_client=build_llm_http_client(settings),
        on_usage=records.append,
    )
    generator = LlmCaseGenerator(provider, max_content_retries=retries, backoff_s=0)

    rounds: list[dict[str, Any]] = []
    for round_index in range(repeat):
        rows: list[GenerationRow] = []
        for operation in operations:
            annotation = annotation_by_key[(operation.method, operation.path)]
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
            before_calls = len(records)

            try:
                result = await active.generate(operation, dimensions, spec_context=spec_context)
            except Exception as exc:  # noqa: BLE001 —— 评测要记录失败，不中断整轮
                rows.append(
                    GenerationRow(
                        method=operation.method,
                        path=operation.path,
                        llm_called=len(records) > before_calls,
                        first_pass=False,
                        case_count=0,
                        covered=0,
                        requested=len(dimensions),
                        missing_dimensions=[spec.dimension for spec in dimensions],
                        error=f"{type(exc).__name__}: {exc}"[:300],
                        error_kind=(
                            "transport"
                            if isinstance(exc, (GuardrailTimeout, GuardrailTransportError))
                            else "content"
                        ),
                        pure_rule_expected=bool(annotation.get("pure_rule", False)),
                    )
                )
                continue

            got = {case.dimension for case in result.cases}
            wanted = {spec.dimension for spec in dimensions}
            validation = validate_generation(
                responses_schema=operation.responses_schema,
                requested=dimensions,
                result=result,
                operation_path=operation.path,
                request_body_schema=operation.request_body_schema,
            )

            low_expected = set(annotation.get("low_confidence_dimensions") or [])
            low_ok = True
            for case in result.cases:
                if case.dimension in low_expected:
                    low_ok = low_ok and _classify_confidence(operation, case) == "low"

            rows.append(
                GenerationRow(
                    method=operation.method,
                    path=operation.path,
                    llm_called=len(records) > before_calls,
                    first_pass=validation.ok,
                    case_count=len(result.cases),
                    covered=len(got & wanted),
                    requested=len(wanted),
                    missing_dimensions=sorted(wanted - got),
                    low_confidence_ok=low_ok,
                    pure_rule_expected=bool(annotation.get("pure_rule", False)),
                )
            )

        # 传输失败**不进首次通过率的分母**：超时不是生成质量问题，
        # 把它算进去会让网络抖动看起来像提示词退化。但次数要单独报出来，
        # 不能悄悄丢掉——否则"27 次只跑了 25 次"就没人知道了。
        transport_failures = sum(1 for row in rows if row.error_kind == "transport")
        graded = [row for row in rows if row.error_kind != "transport"]
        rounds.append(
            {
                "round": round_index,
                "rows": [asdict(row) | {"coverage": row.coverage} for row in rows],
                "first_pass_rate": (
                    sum(1 for row in graded if row.first_pass) / len(graded) if graded else None
                ),
                "transport_failures": transport_failures,
                "generation_coverage": sum(row.covered for row in graded)
                / max(1, sum(row.requested for row in graded)),
                "low_confidence_ok": all(row.low_confidence_ok for row in graded),
                # TG5：纯规则接口出现调用 = 它根本没走规则路径。
                "tg5_violations": [
                    f"{row.method} {row.path}"
                    for row in rows
                    if row.pure_rule_expected and row.llm_called
                ],
            }
        )

    first_pass = [
        round_["first_pass_rate"] for round_ in rounds if round_["first_pass_rate"] is not None
    ]
    coverage = [round_["generation_coverage"] for round_ in rounds]
    prompt_tokens = sum(record.prompt_tokens for record in records)
    cached_tokens = sum(record.cached_tokens for record in records)
    cost = _cost(records)

    return {
        "rounds": rounds,
        "first_pass": _range(first_pass),
        "generation_coverage": _range(coverage),
        "llm_calls": len(records),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": sum(record.completion_tokens for record in records),
        "cached_tokens": cached_tokens,
        "cache_hit_rate": (cached_tokens / prompt_tokens) if prompt_tokens else None,
        "cost_cny": str(cost) if cost is not None else None,
        "p95_latency_ms": percentile([record.elapsed_ms for record in records], 0.95),
    }


def _range(values: list[float]) -> dict[str, float | None]:
    """均值 + 极差（TS6）。全是传输失败时返回 `None`，**不伪装成 0%**。"""
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}


# ---------------------------------------------------------------- 报告


def _print_derivation(summary: dict[str, Any], diffs: list[OperationDiff]) -> None:
    coverage = summary["coverage"]
    print("\n== 第一层：派生正确性（人工标注 vs 阶段 A）==")
    if coverage is not None:
        print(
            f"  覆盖 {summary['matched_dimensions']}/{summary['expected_dimensions']} "
            f"= {coverage:.0%}（{summary['operations_ok']}/{summary['operations']} 个接口完全一致）"
        )
    for diff in diffs:
        mark = "OK " if diff.ok else "!! "
        print(f"  {mark}{diff.method:<6} {diff.path}")
        if diff.missing:
            print(f"       缺：{', '.join(diff.missing)}   <-- 算法盲区")
        if diff.extra:
            print(f"       多：{', '.join(diff.extra)}   <-- 过度派生")
        if diff.status_missing:
            print(f"       未覆盖的声明 4xx：{', '.join(map(str, diff.status_missing))}")
        if diff.pure_rule_expected != diff.pure_rule_actual:
            print(
                f"       TG5 判定不符：期望 {diff.pure_rule_expected}，实际 {diff.pure_rule_actual}"
            )
        if diff.unresolved_ref:
            print("       存在未展开的 $ref（SP1）")


def _print_generation(results: list[dict[str, Any]]) -> None:
    print("\n== 第二层：生成完整性（真实调用）==")
    for item in results:
        label = f"thinking={'on' if item['thinking'] else 'off'}"
        first = item["result"]["first_pass"]
        cover = item["result"]["generation_coverage"]
        cost = item["result"]["cost_cny"]
        transport = sum(round_["transport_failures"] for round_ in item["result"]["rounds"])
        print(
            f"  {label:<16} 首次通过 {_pct_range(first)}  "
            f"维度覆盖 {_pct_range(cover)}  "
            f"调用 {item['result']['llm_calls']} 次  "
            f"传输失败 {transport} 次  "
            f"成本 {'未知' if cost is None else f'￥{Decimal(cost):.6f}'}"
        )
        for round_ in item["result"]["rounds"]:
            for row in round_["rows"]:
                if not row["first_pass"]:
                    reason = row.get("error") or "缺 " + ", ".join(row["missing_dimensions"])
                    print(
                        f"      ! 第 {round_['round'] + 1} 轮 "
                        f"{row['method']} {row['path']} 未通过：{reason}"
                    )
        if not all(round_["low_confidence_ok"] for round_ in item["result"]["rounds"]):
            print("      ! 低置信度标注与人工预期不符")
        violations = sorted(
            {
                endpoint
                for round_ in item["result"]["rounds"]
                for endpoint in round_["tg5_violations"]
            }
        )
        if violations:
            print(f"      ! TG5 违反：这些纯规则接口产生了 LLM 调用——{violations}")


def _pct_range(stats: dict[str, float | None]) -> str:
    if stats["mean"] is None:
        return "—（无可评分样本）"
    return f"{stats['mean']:.0%}（{stats['min']:.0%} ~ {stats['max']:.0%}）"


def _previous_result(results_dir: Path, model: str) -> Path | None:
    candidates = sorted(results_dir.glob(f"*-{model}.json"))
    return candidates[-1] if candidates else None


def _print_delta(previous: dict[str, Any], current: dict[str, Any]) -> None:
    old = (previous.get("layer1") or {}).get("coverage")
    new = current["layer1"]["coverage"]
    if old is not None and new is not None:
        print(f"  与上次相比，第一层覆盖率 {old:.0%} → {new:.0%}")

    old_layer2 = previous.get("layer2") or []
    if not old_layer2 or not current.get("layer2"):
        return
    old_first = old_layer2[0]["result"]["first_pass"]["mean"]
    new_first = current["layer2"][0]["result"]["first_pass"]["mean"]
    if old_first is not None and new_first is not None:
        print(f"  与上次相比，首次通过率 {old_first:.0%} → {new_first:.0%}")


# ---------------------------------------------------------------- 入口


async def _run(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec or DEFAULT_SPEC)
    annotations_path = Path(args.annotations or DEFAULT_ANNOTATIONS)
    raw_spec = spec_path.read_text(encoding="utf-8")
    annotations = _load_yaml(annotations_path)

    parsed = parse_spec(raw_spec)
    annotations_by_key = {(row["method"], row["path"]) for row in annotations["operations"]}
    parsed_by_key = {(op.method, op.path) for op in parsed.operations}
    if annotations_by_key != parsed_by_key:
        print(
            "标注与文档的接口集合不一致：\n"
            f"  只出现在标注：{sorted(annotations_by_key - parsed_by_key)}\n"
            f"  只出现在文档：{sorted(parsed_by_key - annotations_by_key)}",
            file=sys.stderr,
        )
        return 1

    diffs = evaluate_derivation(parsed.operations, annotations)
    layer1 = summarize_derivation(diffs)
    _print_derivation(layer1, diffs)

    layer1_ok = (
        not layer1["missing"]
        and not layer1["extra"]
        and not layer1["status_missing"]
        and not layer1["pure_rule_mismatch"]
        and not layer1["unresolved_refs"]
    )

    # ★ 只有第二层才读配置。第一层**不依赖任何环境变量**——这正是它能
    # 在 CI 里无条件运行的前提。在这里无条件 `load_settings()` 会让
    # "零成本的第一层"在一个没配 LLM Key 的环境里直接起不来。
    settings = load_settings() if args.generate else None
    layer2: list[dict[str, Any]] = []
    if args.generate:
        assert settings is not None
        if args.repeat < 3:
            print(
                f"\n⚠️  重复 {args.repeat} 轮不足以区分档位差异与随机波动（TS6 要求 ≥ 3）。",
                file=sys.stderr,
            )
        spec_context = build_spec_context(raw_spec)
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
            result = await evaluate_generation(
                parsed.operations,
                annotations,
                settings=variant,
                repeat=args.repeat,
                retries=args.retries,
                spec_context=spec_context,
            )
            layer2.append({"thinking": thinking, "result": result})

    model = args.model or (settings.llm_model if settings is not None else "layer1-only")
    now = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "model": model,
        "golden_set": str(spec_path.relative_to(ROOT)),
        "golden_set_sha256": _sha256(spec_path),
        "annotations_sha256": _sha256(annotations_path),
        "prompt_sha256": _sha256(_PROMPT_FILE),
        "repeat": args.repeat if args.generate else 0,
        "layer1": layer1,
        "operations": [asdict(diff) for diff in diffs],
        "layer2": layer2 or None,
    }

    if layer2:
        _print_generation(layer2)

    results_dir = Path(args.results_dir or RESULTS_DIR)
    results_dir.mkdir(parents=True, exist_ok=True)
    previous_path = _previous_result(results_dir, model)
    out_path = results_dir / f"{now:%Y%m%d-%H%M%S}-{model}.json"
    if not args.no_write:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n结果已写入 {out_path.relative_to(ROOT)}")
        if previous_path is not None:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            _print_delta(previous, payload)

    if not layer1_ok:
        print("\n第一层未通过：派生算法与人工标注不一致。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TestFlow Golden Set L3 评测")
    parser.add_argument("--spec", help=f"Golden Set 文档（默认 {DEFAULT_SPEC.relative_to(ROOT)}）")
    parser.add_argument("--annotations", help="人工标注文件")
    parser.add_argument("--generate", action="store_true", help="跑第二层：真实调用模型")
    parser.add_argument("--repeat", type=int, default=3, help="生成轮数（TS6：≥ 3）")
    parser.add_argument(
        "--retries", type=int, default=0, help="内容重试次数；默认 0（测首次通过率）"
    )
    parser.add_argument("--thinking", choices=["on", "off", "both"], default="off")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--results-dir")
    parser.add_argument("--no-write", action="store_true", help="只打印，不写结果文件")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
