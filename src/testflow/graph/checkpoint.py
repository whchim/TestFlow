"""基于纯 Redis 的 Checkpointer。**自写，因为官方那个用不了。**

对应决策：`02-架构设计.md` §6 / ADR-0009（Checkpoint 存 Redis 的独立 DB）、
`06-Agent编排.md` §5（中断与恢复）、G6。

## 为什么不用现成的

| 方案 | 结论 |
| :--- | :--- |
| `langgraph-checkpoint-redis` | ❌ 环境不支持 |
| `langgraph-checkpoint-postgres` | ❌ 违反 EN7 |
| `InMemorySaver` | ❌ 跨进程不通 |
| **自写（本文件）** | ✅ |

三条否决的理由：

* **官方 Redis saver** 需要 `redisvl` + RediSearch 模块，而 compose 里跑的是
  `redis:7-alpine`（没有这些模块）。它 pin 的 `langgraph-checkpoint` 版本
  也与已装的 4.3.0 冲突。
* **官方 Postgres saver** 的 `setup()` 会在 **Alembic 之外**建表，
  违反 EN7（migration 是 schema 的唯一事实来源）。它也与 `02` §6
  的 Redis 决策冲突。
* **`InMemorySaver`** 跨进程不通——而 IP-2 的本质就是"A 挂起、B 唤醒"。
  用它在本地测试全绿，一上多进程就全坏。

## 存储布局

    ckpt:{thread}:{ns}:{id}          一次 checkpoint（不含 channel_values）
    ckptb:{thread}:{ns}:{ch}:{ver}   一个 channel 的值（版本化的 blob）
    ckptw:{thread}:{ns}:{id}:{task}  某次 checkpoint 下的待处理写入
    ckptl:{thread}:{ns}              checkpoint id 列表（LPUSH，新→旧）

`channel_values` 单独存、按 `(channel, version)` 做键，是**照抄 `InMemorySaver`
的布局**——不这么做的话，同一个 channel 在多次 checkpoint 之间会被重复存储，
而 graph state 里最占地方的正是它。

## 序列化

用 `JsonPlusSerializer`（langgraph 自带的那个），它能处理 UUID、datetime、
dataclass 这些 JSON 原生不支持的类型。**不要用 `pickle`**：
checkpoint 的读取方是另一个进程，而 pickle 反序列化会执行任意代码——
一份被篡改的 checkpoint 等于一次远程代码执行。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, NoReturn, Protocol, cast

import redis.asyncio as aioredis
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from testflow.observability.logging import get_logger

__all__ = ["RedisCheckpointSaver", "make_checkpointer"]


def _as_bytes(value: bytes | str | None) -> bytes:
    """统一成 bytes。

    redis-py 的返回类型是 `bytes | str`，取决于客户端构造时 `decode_responses`
    的取值。本模块**显式要求 `decode_responses=False`**（二进制数据不能被
    按 UTF-8 解码——序列化结果里什么字节都可能有），但类型标注上仍要处理
    两种可能，否则一个误配的客户端会在运行期而不是在类型检查期暴露。
    """
    if value is None:
        return b""
    return value.encode() if isinstance(value, str) else value


def _as_text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


logger = get_logger(__name__)


class SettingsProtocol(Protocol):
    """`make_checkpointer` 需要的那两个配置项。

    用 `Protocol` 而不是直接标 `Settings`：`graph/` 没必要依赖整个配置模块，
    而依赖面越小，"换个配置来源"要改的地方就越少。运行期传一个真的
    `Settings` 进来即可——结构化类型会自动匹配。
    """

    @property
    def redis_url(self) -> str: ...
    @property
    def redis_checkpoint_db(self) -> int: ...


#: 保留 7 天（`02` §6）。Checkpoint 不是事实来源——权威状态在 PG 里
#: （`04` DM5）。过期的 checkpoint 意味着"这个 Run 太久没动静了"，
#: 而从最近状态重跑是不行的（N2 的 LLM 生成非确定），所以旧的要清掉，
#: 免得 Redis 无限增长。
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

#: 序列化数据的类型标记与内容之间的分隔。
#: 用长度前缀而不是"找一个不会出现的分隔符"——后者在面对任意二进制数据时
#: 总会有一个反例，而长度前缀不会。
_TYPE_LEN_BYTES = 2


def _pack(typed: tuple[str, bytes]) -> bytes:
    """`(type_str, payload)` → 单块字节。"""
    kind, payload = typed
    encoded = kind.encode("ascii")
    return len(encoded).to_bytes(_TYPE_LEN_BYTES, "big") + encoded + payload


def _unpack(raw: bytes) -> tuple[str, bytes]:
    size = int.from_bytes(raw[:_TYPE_LEN_BYTES], "big")
    return raw[_TYPE_LEN_BYTES : _TYPE_LEN_BYTES + size].decode("ascii"), raw[
        _TYPE_LEN_BYTES + size :
    ]


class RedisCheckpointSaver(BaseCheckpointSaver):
    """异步 Checkpointer，落在 Redis 的**独立 DB** 上。

    ## 为什么只有异步实现

    编排层是异步的（`02` D1），图推进走 `ainvoke`。同步方法在这里**直接报错**
    而不是"顺手也实现一个"——同步版本要么再开一个连接、要么阻塞事件循环，
    两种都会在某个时刻成为一个很难查的性能问题。

    报错比"能用但慢"好：后者会被用起来，然后没人知道为什么平台偶尔卡住。

    ## 为什么用独立的 DB（ADR-0009）

    Broker 与 Checkpoint 混用同一个 DB 时，一次 `FLUSHDB` 类的运维操作会
    同时删掉正在投递的任务和正在挂起的图状态。两者的淘汰策略需求也不同：
    任务队列要"尽快消费掉"，而 checkpoint 要"保留到 Run 结束"。
    """

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
        serde: JsonPlusSerializer | None = None,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self._redis = client
        self._ttl = ttl_seconds

    # ------------------------------------------------------------ 键

    @staticmethod
    def _ckpt_key(thread: str, ns: str, checkpoint_id: str) -> str:
        return f"ckpt:{thread}:{ns}:{checkpoint_id}"

    @staticmethod
    def _blob_key(thread: str, ns: str, channel: str, version: str) -> str:
        return f"ckptb:{thread}:{ns}:{channel}:{version}"

    @staticmethod
    def _writes_key(thread: str, ns: str, checkpoint_id: str, task_id: str, task_path: str) -> str:
        # `task_path` 进键名是必须的：同一张图在同一次 checkpoint 里可能有
        # 多个并行分支，它们的 `task_id` 相同而 `task_path` 不同。
        # 只用 `task_id` 会让后写的覆盖先写的——而症状是"某个分支的写入丢了"，
        # 极难定位。
        return f"ckptw:{thread}:{ns}:{checkpoint_id}:{task_id}:{task_path}"

    @staticmethod
    def _list_key(thread: str, ns: str) -> str:
        return f"ckptl:{thread}:{ns}"

    # ------------------------------------------------------- 写入

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]

        body = dict(checkpoint)
        # `channel_values` 单独存——它们是 state 里最占地方的部分，
        # 而且同一个 channel 在多次 checkpoint 间往往不变。
        values = cast("dict[str, Any]", body.pop("channel_values", None) or {})

        pipe = self._redis.pipeline()
        for channel, version in new_versions.items():
            key = self._blob_key(thread, ns, channel, str(version))
            if channel in values:
                pipe.set(key, _pack(self.serde.dumps_typed(values[channel])))
            else:
                # 显式记一个"空"标记，而不是不写：读取方据此区分
                # "这个 channel 这一版没有值"与"键丢了"。
                pipe.set(key, _pack(("empty", b"")))
            if self._ttl:
                pipe.expire(key, self._ttl)

        meta = get_checkpoint_metadata(config, metadata)
        key = self._ckpt_key(thread, ns, checkpoint_id)
        # 父 id 一并存：`alist` 要用它串出链，而 `CheckpointTuple.parent_config`
        # 是恢复时定位上一步的依据。
        pipe.set(key, _pack(self.serde.dumps_typed((body, meta, checkpoint.get("parent_id")))))
        if self._ttl:
            pipe.expire(key, self._ttl)

        # LPUSH：列表头是最新的。`alist` 默认从新到旧，正好对上。
        listing = self._list_key(thread, ns)
        pipe.lpush(listing, checkpoint_id)
        if self._ttl:
            pipe.expire(listing, self._ttl)

        await pipe.execute()

        return {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        key = self._writes_key(thread, ns, checkpoint_id, task_id, task_path)
        pipe = self._redis.pipeline()
        for channel, value in writes:
            # 一次写入一个 channel。用 RPUSH 而不是 SET：
            # **同一个 channel 可以被写多次**（一个任务产出多个写入，
            # 或恢复时重跑同一节点——中断的节点确实会从头重跑）。
            # 用 SET 会让后者覆盖前者，而顺序信息对图的状态合并是必要的。
            encoded = _pack(self.serde.dumps_typed(value))
            pipe.rpush(key, _pack(self.serde.dumps_typed((channel, encoded))))
        if self._ttl:
            pipe.expire(key, self._ttl)
        await pipe.execute()

    # ------------------------------------------------------- 读取

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")

        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            # 没指定 id → 取最新的那个。
            listed = await self._redis.lrange(self._list_key(thread, ns), 0, 0)
            if not listed:
                return None
            checkpoint_id = _as_text(listed[0])

        raw = _as_bytes(await self._redis.get(self._ckpt_key(thread, ns, checkpoint_id)))
        if raw is None:
            return None

        body, metadata, parent_id = self.serde.loads_typed(_unpack(raw))
        channel_values = await self._load_blobs(thread, ns, body.get("channel_versions", {}))

        final_config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread,
                    "checkpoint_ns": ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )

        return CheckpointTuple(
            config=cast("RunnableConfig", final_config),
            # `body` 是 `put` 时存进去的完整 checkpoint（少了 `channel_values`），
            # 所以这里的键是齐的。`cast` 是因为它在存储层是 `dict`，
            # 类型信息在序列化那一刻就丢了——而那个丢失是**不可避免的**：
            # 从 Redis 读回来的字节里没有任何类型标注。
            checkpoint=cast("Checkpoint", {**body, "channel_values": channel_values}),
            metadata=metadata,
            parent_config=cast("RunnableConfig | None", parent_config),
            pending_writes=await self._load_writes(thread, ns, checkpoint_id),
        )

    async def _load_blobs(self, thread: str, ns: str, versions: dict[str, Any]) -> dict[str, Any]:
        if not versions:
            return {}
        keys = [self._blob_key(thread, ns, channel, str(ver)) for channel, ver in versions.items()]
        raws = await self._redis.mget(keys)

        values: dict[str, Any] = {}
        for channel, raw in zip(versions, raws, strict=True):
            if raw is None:
                continue
            kind, payload = _unpack(_as_bytes(raw))
            if kind == "empty":
                continue
            values[channel] = self.serde.loads_typed((kind, payload))
        return values

    async def _load_writes(
        self, thread: str, ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        pattern = f"ckptw:{thread}:{ns}:{checkpoint_id}:*"
        writes: list[tuple[str, str, Any]] = []
        async for key in self._redis.scan_iter(match=pattern):
            # 键名里的 task_id 与 task_path 用 `:` 分隔，而 task_path 本身
            # 可能含 `:`（它是图的路径）。所以从**固定前缀之后**按
            # `split(":", 1)` 切开，而不是无脑 split。
            suffix = _as_text(key).split(f"ckptw:{thread}:{ns}:{checkpoint_id}:", 1)[1]
            task_id, _, _task_path = suffix.partition(":")
            for raw in await self._redis.lrange(key, 0, -1):
                channel, encoded = self.serde.loads_typed(_unpack(_as_bytes(raw)))
                writes.append((task_id, channel, self.serde.loads_typed(_unpack(encoded))))
        return writes

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")

        listed = await self._redis.lrange(self._list_key(thread, ns), 0, -1)
        # 去重并保序：`aput` 用 LPUSH，而同一个 checkpoint id 可能被写多次
        # （恢复时会重跑被打断的节点）。不去重的话列表会越长越多，
        # 而"最新的是哪个"仍然正确——但内存会无谓地涨。
        seen: set[str] = set()
        ids: list[str] = []
        for raw in listed:
            cid = _as_text(raw)
            if cid in seen:
                continue
            seen.add(cid)
            ids.append(cid)

        before_id = get_checkpoint_id(cast("RunnableConfig", before)) if before else None
        emitted = 0
        for checkpoint_id in ids:
            if before_id is not None and checkpoint_id >= before_id:
                continue
            if limit is not None and emitted >= limit:
                return
            config_with_id = {
                "configurable": {
                    "thread_id": thread,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            }
            result = await self.aget_tuple(cast("RunnableConfig", config_with_id))
            if result is None:
                continue
            if filter and any(result.metadata.get(k) != v for k, v in filter.items()):
                continue
            emitted += 1
            yield result

    # ------------------------------------------------------- 同步桩

    def _sync_unsupported(self) -> NoReturn:
        raise NotImplementedError(
            "RedisCheckpointSaver 只支持异步接口。编排层是异步的（`02` D1），"
            "图推进走 `ainvoke`。同步版本要么再开一个连接、要么阻塞事件循环——"
            "两种都会在某个时刻变成一个很难查的性能问题。"
        )

    def put(self, *args: Any, **kwargs: Any) -> Any:
        self._sync_unsupported()

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        self._sync_unsupported()

    def get_tuple(self, *args: Any, **kwargs: Any) -> Any:
        self._sync_unsupported()

    def list(self, *args: Any, **kwargs: Any) -> Any:
        self._sync_unsupported()


def make_checkpointer(
    settings: SettingsProtocol, *, client: aioredis.Redis | None = None
) -> RedisCheckpointSaver:
    """工厂。

    放成一个函数而不是让各处在构造处直接拼 URL，是为了让"Checkpoint 用哪个
    DB"这件事**只有一个答案**（`redis_checkpoint_db`，与 broker 分开——
    ADR-0009）。

    Args:
        settings: `Settings` 实例。类型标成 `object` 是为了避免
            `graph/` 反向依赖配置模块的类型——运行时它必须是一个
            `Settings`，属性访问会立刻报错，不会静默出错。
        client: 注入的连接。测试用它接一个假的 Redis。
    """
    if client is not None:
        return RedisCheckpointSaver(client)

    url = settings.redis_url
    db = settings.redis_checkpoint_db
    return RedisCheckpointSaver(aioredis.from_url(f"{url.rstrip('/')}/{db}"))
