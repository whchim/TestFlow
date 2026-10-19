"""小样本统计助手。

M4 的验收要看 P95 单次生成耗时。这里刻意**不引入 numpy/pandas**：
一个分位数不值得多一个几十 MB 的依赖，而"用哪个插值方法"本身也不是
这个项目要讨论的问题——验收关心的是量级（秒 vs 分钟）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = ["percentile"]


def percentile(values: Sequence[int], p: float) -> int | None:
    """最近秩（nearest-rank）分位数。空序列返回 `None`。

    `p` 取 0–1。例如 20 个数取 P95 得到第 19 个（升序），而不是插值出
    一个不存在的耗时。对"最慢的那几次有多慢"这个问题，最近秩更直白。
    """
    if not values:
        return None
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"分位数必须在 [0, 1] 内：{p}")

    ordered = sorted(values)
    # 最近秩：第 ceil(p * n) 个（1 起），夹在合法范围内。
    rank = max(1, min(len(ordered), math.ceil(p * len(ordered))))
    return ordered[rank - 1]
