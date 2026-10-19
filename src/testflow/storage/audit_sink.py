"""把 Guardrail 的审计记录落库的**通用回调**。

对应决策：`03-模块详细设计/Guardrail.md` GR6、`04-数据模型.md` §2.9。

## 为什么这是一个独立模块，而不是某个入口点的私有函数

因为**两条出站路径都要它**：Worker 打被测目标（`execution/tasks.py`），
编排层调模型提供商（`graph/tasks.py`）。第一版它只存在于前者。

两处各写一份的代价不是"多写 20 行"，而是**两条路径的审计会各自演化**：
一边加了 `trace_id`、另一边没加；一边吞异常、另一边不吞。
而审计表的用途正是"把两条路径放在一起看"——`direction` 那一列的存在
就说明了这个意图（`target` / `llm`）。分开写，那张表迟早没法横向比。

放在 `storage/` 而不是 `guardrail/`：它要开数据库会话，而 `guardrail/`
是纯策略与传输（不依赖存储）。放进去会把"安全判定"和"怎么存日志"绑死。

## ⚠️ 第一版真的漏过这一步

`execution/tasks.py` 最初构造 `GuardrailSyncClient` 时**没传 `audit_sink`**
——于是审计表一条记录都没有，而**没有任何东西会报错**：请求照常发出、
测试照常通过，只是"这个 Run 访问了哪些 host"这个问题永远答不上来。

发现它靠的是**跑完 demo 之后去查那张表**。写完一个"记录审计"的功能，
要去看记录有没有真的产生——这条注释留在这里，是因为同一个疏漏
在 LLM 那条路径上完全可以再发生一次（`graph/tasks.py` 的装配点）。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from testflow.guardrail.audit import AuditRecord
from testflow.observability.logging import get_logger
from testflow.storage.repositories import audit as audit_repo

__all__ = ["make_audit_sink"]

logger = get_logger(__name__)


def make_audit_sink(sessions: sessionmaker[Session]) -> Callable[[AuditRecord], None]:
    """构造一个"拿到记录就写库"的回调。**用独立的短事务。**

    不复用调用方的事务：审计要回答的是"我们放没放行这次请求"，
    而那件事在请求发出的那一刻就已经确定了。绑进业务事务的话，
    业务因为任何原因回滚，**连同那条日志一起消失**——
    而"某次出站曾经发生过"不依赖业务成功与否。

    这也解释了为什么这个回调是**同步**的：它要在业务事务还开着的时候被调用。
    开一个自己的短会话把两者的生命周期彻底分开，比想办法复用当前会话
    简单也正确。

    ⚠️ **同步回调意味着 LLM 那条路径上会有一次阻塞写落在事件循环里。**
    这是有意的：编排层是 `--pool=solo --concurrency=1`（`10` §2.2），
    同一时刻只有一个 Run 在推进，而它本来就在等一次几秒到几十秒的模型调用。
    几毫秒的数据库往返在它面前量级可以忽略。

    代价写在这里是为了让它**是个决定，而不是个疏忽**：若哪天编排层改成
    多 Run 并发，这个阻塞点就得连同 `--pool=solo` 一起重新评估——
    而那时读到这里的人会知道要重新评估。
    """

    def _write(record: AuditRecord) -> None:
        try:
            with sessions() as session:
                audit_repo.record(
                    session,
                    direction=record.direction,
                    target_host=record.target_host,
                    target_url=record.target_url,
                    decision=record.decision,
                    deny_reason=record.deny_reason,
                    run_id=record.run_id,
                    request_digest=record.request_digest,
                    trace_id=record.trace_id,
                )
                session.commit()
        except Exception:  # noqa: BLE001
            # **审计写失败不能带崩业务。** 一次业务请求的成功与否不该取决于
            # 日志存储的可用性——但必须留下痕迹，否则"审计静默失效"这件事
            # 会以"表一直是空的"这种最难注意到的形式存在。
            logger.exception(
                "审计写入失败 host=%s decision=%s", record.target_host, record.decision
            )

    return _write
