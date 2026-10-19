"""模型调用的成本估算。**单价是数据，不是代码里的魔法数字。**

对应决策：`02-架构设计.md` §5.4、`11-开发路线图.md` M4（单 Run ≤ ¥2）。

## 为什么按"输入长度分档"

百炼不是一条线性单价：单次请求的输入跨过某个长度，**整单**跳到更高的
一档（`02` §5.1 记录过 Plus 档 >128K 时 ×5）。用一条固定单价算，
短请求会被高估、长请求会被严重低估——而低估的那部分恰好是"大文档"，
也正是成本最容易失控的场景。

## 为什么用 `Decimal`

成本会累加几百次调用再乘上百万分之一。用 `float` 累加会在末尾漂出
`0.010000000000000002` 这种数——它不影响量级，但会让"两个月的账单
对不对得上"这种核对变得没有意义。

## 未知模型返回 `None`，不猜价格

猜一个价格比不显示更糟：报告会给出一个**看起来权威但错误**的金额。
`None` 会渲染成"未配置单价"，并列出模型名——那是一个可行动的信息
（"去配置表里加一行"），而错误的数字不是。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "CACHE_WRITE_MULTIPLIER",
    "DEFAULT_PRICE_TABLE",
    "PriceTier",
    "estimate_cost",
    "price_table_from_overrides",
]

_MTOK: Final = Decimal(1_000_000)

#: 缓存**创建**的单价倍率（`02` §5.4：创建 125%，命中 10%）。
#:
#: 创建比普通输入更贵——这是合理的：提供商要把它写进缓存。
#: 隐式缓存若不计创建费，`cache_write_tokens` 会是 0，这一项自然为 0。
CACHE_WRITE_MULTIPLIER: Final = Decimal("1.25")


@dataclass(frozen=True, slots=True)
class PriceTier:
    """一档单价。单位：元 / 百万 token。"""

    max_input_tokens: int
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal


#: 默认单价表。数字来源见 `02` §5.1 / 百炼计费页（2026-10）。
#:
#: **这是一份代码里的表，不是配置项**：它需要被 review、被测试，
#: 而且改错一行会让所有报告的成本静默偏移。需要临时纠偏时用
#: `TESTFLOW_LLM_PRICING_OVERRIDES`（配置层），不改这里。
DEFAULT_PRICE_TABLE: Final[dict[str, tuple[PriceTier, ...]]] = {
    "qwen3.7-flash": (
        PriceTier(32_000, Decimal("0.2"), Decimal("0.8"), Decimal("0.04")),
        PriceTier(256_000, Decimal("0.6"), Decimal("2.4"), Decimal("0.12")),
        PriceTier(1_000_000, Decimal("1.2"), Decimal("4.8"), Decimal("0.24")),
    ),
    "qwen3.5-plus": (
        PriceTier(128_000, Decimal("0.8"), Decimal("4.8"), Decimal("0.08")),
        # 最后一档用一个大数兜住所有更长的输入。
        PriceTier(10**9, Decimal("4.0"), Decimal("24.0"), Decimal("0.4")),
    ),
}


def price_table_from_overrides(
    overrides: Mapping[str, Sequence[Mapping[str, float]]] | None,
    *,
    base: Mapping[str, Sequence[PriceTier]] | None = None,
) -> dict[str, tuple[PriceTier, ...]]:
    """把配置层的覆盖合并进默认表。

    覆盖是**整模型替换**，不是逐字段合并：单价三件套必须自洽
    （缓存价高于输入价是明显的笔误），逐字段合并会让这种笔误更难看出来。
    """
    table: dict[str, tuple[PriceTier, ...]] = {
        model: tuple(tiers) for model, tiers in (base or DEFAULT_PRICE_TABLE).items()
    }
    for model, tiers in (overrides or {}).items():
        table[model] = tuple(
            PriceTier(
                max_input_tokens=int(tier["max_input_tokens"]),
                input_per_mtok=Decimal(str(tier["input_per_mtok"])),
                output_per_mtok=Decimal(str(tier["output_per_mtok"])),
                cached_input_per_mtok=Decimal(str(tier["cached_input_per_mtok"])),
            )
            for tier in tiers
        )
    return table


def _select_tier(tiers: Sequence[PriceTier], prompt_tokens: int) -> PriceTier:
    for tier in tiers:
        if prompt_tokens <= tier.max_input_tokens:
            return tier
    return tiers[-1]


def estimate_cost(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    price_table: Mapping[str, Sequence[PriceTier]] | None = None,
) -> Decimal | None:
    """估算一次调用的成本（元）。未知模型返回 `None`。

    `cached_tokens` 是 `prompt_tokens` 的一部分（命中缓存的输入），
    所以要先从输入里减掉，否则同一批 token 会被算两次。
    """
    table = price_table or DEFAULT_PRICE_TABLE
    tiers = table.get(model)
    if not tiers:
        return None

    tier = _select_tier(tiers, max(prompt_tokens, 0))
    cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
    uncached = max(prompt_tokens, 0) - cached
    write = max(cache_write_tokens, 0)

    cost = (
        Decimal(uncached) * tier.input_per_mtok
        + Decimal(cached) * tier.cached_input_per_mtok
        + Decimal(write) * tier.input_per_mtok * CACHE_WRITE_MULTIPLIER
        + Decimal(max(completion_tokens, 0)) * tier.output_per_mtok
    ) / _MTOK

    return cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
