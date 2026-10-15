"""每个 Run 的分布式锁（G9）。

对应决策：`06-Agent编排.md` §6.3。

## 防的是什么

同一个 Run 会被**多个来源**同时唤醒：每条用例完成时都回调一次（N 次），
兜底轮询也会调用同一个推进函数。若不串行，两个推进者会同时从同一个
Checkpoint 恢复、同时跑同一个节点——于是用例被投递两次（幂等能兜住，
但白做一遍工），而更糟的是 `phase` 会被两个writer 交替覆盖。

## 锁的三个设计选择

**① TTL 不续期。** 锁的持有时间必须覆盖最坏一跳（一次 LLM 调用可能几分钟），
所以 TTL 取得比较宽（400 秒）。但**不续期**是刻意的：

    偶尔重复推进（幂等兜得住）  ≫  永久死锁（需要人工介入）

一个续期机制在进程被强杀（`SIGKILL`，没有清理机会）时，会让锁一直挂到
TTL 到期为止；而如果 TTL 很长又续期失败，就变成了死锁。

**② 拿不到锁就**直接返回**，不等待。** 等一个正在跑的推进没有意义——
它跑完就会把状态推进到位，而我们的唤醒本来就是一封"去看看"的通知，不是指令。

**③ 幂等是前提，不是补充。** 锁只减少重复工作，**不保证**独占——
TTL 到期、Redis 抖动都可能让两个推进者并行。所以每个节点都必须自己幂等
（N2 跳过已成功的、N4 跳过已有结果的）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

__all__ = ["RunLock", "run_lock_key"]

#: 锁的存活时间。
#:
#: 必须大于最坏一跳：一次 LLM 调用（`llm_timeout_s` 默认 120 秒）×
#: 重试次数 + 余量。取小了会让锁在推进过程中过期，于是并行的推进者出现；
#: 取大了只会让"进程崩溃后重新推进"的等待变长，而那是罕见路径。
DEFAULT_TTL_SECONDS = 400


def run_lock_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:advance"


class RunLock:
    """基于 `SET NX EX` 的互斥锁。

    用 `SET NX EX` 而不是"先 GET 再 SET"：后者有两个来回，中间那段窗口
    正是竞态本身。`SET NX EX` 是 Redis 的**单个原子命令**。

    Redis 官方对这套用法的提醒是"不要用它做需要强一致的分布式锁"
    （Redlock 的争议）。**这里恰好不需要**——我们只需要"大概率串行"，
    正确性由各节点的幂等性保证，而不是由锁保证。
    """

    def __init__(
        self, redis: Redis, run_id: uuid.UUID | str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self._redis = redis
        self._key = run_lock_key(run_id)
        # 存 token 而不是布尔值：将来若要安全释放（比较并删除），
        # 必须有"这把锁是不是我拿的"的依据。现在不用，但先存着——
        # 改成不带 token 的版本容易，反过来要改成带的就得处理存量。
        self._token = uuid.uuid4().hex
        self._ttl = ttl_seconds

    async def acquire(self) -> bool:
        result = await self._redis.set(self._key, self._token, nx=True, ex=self._ttl)
        return bool(result)

    async def release(self) -> None:
        """释放锁。

        **用 Lua 脚本比较 token 再删**（`compare-and-delete`）。
        直接 `DEL` 的后果：如果本次推进超时、锁已过期、另一个推进者拿到了锁，
        这一句会把**别人**的锁删掉——于是第三个推进者又能进来。
        这类 bug 极难复现，而它造成的现象是"偶尔有两次重复投递"。

        用 Lua 而不是"GET 再 DEL"同理：中间那段窗口正是问题本身。
        """
        await self._redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
            "else return 0 end",
            1,
            self._key,
            self._token,
        )


@asynccontextmanager
async def acquire_run_lock(
    redis: Redis, run_id: uuid.UUID | str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> AsyncIterator[bool]:
    """拿到锁就 yield True 并在退出时释放；拿不到就 yield False。

    **拿不到时不抛异常**——"别人正在推进"是完全正常的情况
    （N 条用例完成时会有 N 次唤醒，其中 N-1 次会遇到这个分支）。
    抛异常会让调用方把它当成错误来处理，而它根本不是错误。
    """
    lock = RunLock(redis, run_id, ttl_seconds=ttl_seconds)
    if not await lock.acquire():
        yield False
        return
    try:
        yield True
    finally:
        await lock.release()
