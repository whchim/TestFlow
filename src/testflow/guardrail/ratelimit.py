"""限流与熔断（SR-5）。

对应决策：`03-模块详细设计/Guardrail.md` §7 / GR9、`10-部署运维.md` §3.4。

## QPS 上限是**异常兜底**，不是常规节流

GR9 精确化了这一点：**保护目标服务的常规手段是 `worker_concurrency` 本身**
——你配了 8 个并发，就不会产生超过 8 个在途请求。QPS 上限要挡的是
"配置写错、任务泄漏、某个 bug 让循环发请求"这类**异常**情况。

因此默认值（20）高于正常并发能产生的吞吐。若把它当作常规节流来调，
就会**持续压制自己**：限流器一直拒绝，表现成"平台很慢"，
而配置看起来完全正常。

## ⚠️ M1 的限流器是**每进程**的

Celery 用 prefork，每个子进程有自己的一份限流器状态。所以实际生效的 QPS
约为 `guardrail_qps_per_host × worker_concurrency`，而不是配置里那个数。

这是已知偏差，**M1 接受并记录**：做成 Redis 共享的限流器要引入一次网络往返
到每个请求上，而那会显著拉长每一次执行——用真实的性能损失换一个
"异常兜底"的精度，不划算。真要做，应当在 M2 连同完整审计一起考虑。

（这条偏差必须写出来。不写的话，运维时按配置值估算容量会严重偏离实际。）

## 熔断与限流的区别

* **限流**：正常但太快 → 等一下就好 → 拒绝原因是 `rate_limit`
* **熔断**：连续连接失败 → 目标大概是挂了 → 冷却期内直接拒绝，不再浪费超时

两者都记 `deny_reason = 'rate_limit'`（`DenyReason` 里没有单独的"熔断"取值），
但 `detail` 会区分——排查时能看出来是"太快"还是"对方挂了"。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

__all__ = ["HostLimiter", "RateLimiter", "RateLimitVerdict"]


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    detail: str = ""


@dataclass
class _HostState:
    """单个 `host:port` 的限流状态。"""

    #: 令牌桶：当前可用令牌数。
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    #: 在途请求数。
    in_flight: int = 0
    #: 连续失败计数（成功会清零）。
    consecutive_failures: int = 0
    #: 熔断打开的时刻；`None` 表示未熔断。
    opened_at: float | None = None


class RateLimiter:
    """按 `host:port` 的令牌桶 + 并发上限 + 连续失败熔断。

    所有时间用 `time.monotonic()`：墙上时钟会被 NTP 校正、会被手动改，
    而"这次请求距上次多久"这种问题只能由单调时钟回答。用 `time.time()`
    会在时钟回拨时产生负的时间差，进而让令牌桶一次补满。
    """

    def __init__(
        self,
        *,
        qps_per_host: float,
        max_concurrency_per_host: int,
        breaker_threshold: int,
        breaker_cooldown_s: int,
    ) -> None:
        self._qps = qps_per_host
        self._max_in_flight = max_concurrency_per_host
        self._breaker_threshold = breaker_threshold
        self._cooldown = breaker_cooldown_s
        self._hosts: dict[str, _HostState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 准入

    def acquire(self, host_port: str) -> RateLimitVerdict:
        """请求一个名额。**必须与 `release()` 配对**（成功拿到名额后）。

        Returns:
            `allowed=False` 时，调用方应直接拒绝请求并记审计。
        """
        now = time.monotonic()
        with self._lock:
            state = self._hosts.setdefault(host_port, _HostState(tokens=self._qps))
            self._refill(state, now)

            if self._is_open(state, now):
                remaining = int(self._cooldown - (now - (state.opened_at or now)))
                return RateLimitVerdict(
                    False, f"{host_port} 连续失败已熔断，冷却中（约剩 {max(remaining, 0)} 秒）"
                )

            if state.in_flight >= self._max_in_flight:
                return RateLimitVerdict(
                    False,
                    f"{host_port} 在途请求已达上限 {self._max_in_flight}",
                )

            if state.tokens < 1.0:
                return RateLimitVerdict(False, f"{host_port} 超出 QPS 上限 {self._qps}/秒")

            state.tokens -= 1.0
            state.in_flight += 1
            return RateLimitVerdict(True)

    def release(self, host_port: str, *, success: bool) -> None:
        """归还名额并记录结果。

        **连接层失败才计入熔断**——用例断言失败不算。判据是"能不能连上"，
        而不是"返回的内容对不对"：一个持续返回 500 的服务仍然是活的，
        把它熔断掉会让测试全部变成 `rate_limit` 而非真实的 `failed`。
        """
        with self._lock:
            state = self._hosts.get(host_port)
            if state is None:  # pragma: no cover —— acquire 必先于 release
                return
            state.in_flight = max(state.in_flight - 1, 0)

            if success:
                state.consecutive_failures = 0
                state.opened_at = None
            else:
                state.consecutive_failures += 1
                if state.consecutive_failures >= self._breaker_threshold:
                    state.opened_at = time.monotonic()

    # ------------------------------------------------------------ 内部

    def _refill(self, state: _HostState, now: float) -> None:
        """按流逝的时间补令牌，**上限是一个桶的容量**。

        容量等于 QPS 值本身：允许最多积攒 1 秒的额度。积攒更多会让
        "闲置很久之后一次性打出一大串请求"，而那正是限流要防的事。
        """
        elapsed = now - state.last_refill
        if elapsed <= 0:  # pragma: no cover —— 单调时钟不会倒退，防御性分支
            return
        state.tokens = min(self._qps, state.tokens + elapsed * self._qps)
        state.last_refill = now

    def _is_open(self, state: _HostState, now: float) -> bool:
        if state.opened_at is None:
            return False
        if now - state.opened_at < self._cooldown:
            return True
        # 冷却结束：半开。清零计数放一个请求过去探路；它若失败会立刻重新熔断。
        state.opened_at = None
        state.consecutive_failures = 0
        return False


class HostLimiter:
    """`RateLimiter` 的便捷外壳，把"准入→执行→归还"包成上下文管理器。

    存在理由是**让配对不会漏**：`acquire()` 拿了名额而不 `release()`，
    该 host 的在途计数会永久 +1，最终把并发上限占满——表现是"平台突然
    不再访问某个主机了"，而没有任何报错。
    """

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    def verdict(self, host_port: str) -> RateLimitVerdict:
        return self._limiter.acquire(host_port)

    def done(self, host_port: str, *, success: bool) -> None:
        self._limiter.release(host_port, success=success)
