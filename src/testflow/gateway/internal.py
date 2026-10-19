"""内部端点。**三重隔离。**

对应决策：`05-API契约.md` §4.7 与 §5.3、`06-Agent编排.md` §6.1/§6.2、G8。

## 三重隔离，缺一不可

| 手段 | 防的是什么 |
| :--- | :--- |
| `include_in_schema=False` | 不出现在公开的接口契约里——**外部消费者看不到它存在** |
| 独立的 `internal_token` | 用户凭证拿到也无法触发图推进（`05` §5.3） |
| 网络层不可达 | 只在容器网络内暴露（`10` §4） |

单独用任何一条都不够：

* 只靠"不在文档里"是**隐晦式安全**——扫一遍路径就能发现
* 只靠令牌的话，它就成了一条公开的、可被暴力尝试的端点
* 只靠网络隔离的话，一个 SSRF（本项目专门在防的东西）就能打进来

## G8：完成判定**查数据库，不靠"最后一个任务自我识别"**

天真做法是让"最后一个完成的用例"自己判断"我是最后一个吗"。**它有竞态**：
两个任务可能各自以为自己不是最后一个，于是没人唤醒；也可能都以为自己是，
于是推进两次。

所以：每条用例完成都回调一次（幂等，重复无害），**由这个端点去查库**判定。
代价是 N 次多余的 HTTP 调用——而那个代价换来的是一条不会因并发而漏掉唤醒的路径。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from testflow.gateway.auth import require_internal_token
from testflow.gateway.deps import GatewayContext
from testflow.gateway.schemas import WakeResponse
from testflow.observability.logging import get_logger
from testflow.storage.repositories import cases as cases_repo
from testflow.storage.repositories import results as results_repo
from testflow.storage.repositories import runs as runs_repo

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    # ★ 不出现在接口契约里。**这条是隔离的第一层**，不是唯一的一层。
    include_in_schema=False,
    dependencies=[Depends(require_internal_token)],
)


@router.post("/runs/{run_id}/wake", response_model=WakeResponse, summary="[内部] 唤醒运行")
async def wake_run(run_id: str, request: Request) -> WakeResponse:
    """通知"这个 Run 又有用例完成了"。

    ## 幂等

    重复调用无害——本端点自己查库决定要不要推进。**调用方不需要为失败做补偿**：
    兜底轮询会收尾（`06` §6.4）。

    ## 两个返回值

    * `pending` —— 还有用例没跑完，**什么都没做**
    * `resumed` —— 全部到齐，已投递推进指令

    用响应体而不是状态码区分（`05` §4.7）：`202`/`200` 的区别在客户端看起来
    是"请求本身成功了没有"，而我们真正想让它知道的是"唤醒**生效了没有**"——
    那是排查回调链路时第一个要问的问题。

    ## 已经完成的 Run 返回 `pending`

    这是一个**正常的竞态**：兜底轮询是周期性的，而回调是即时的。
    轮询可能在 Run 已经跑完之后才扫到它——那时再推进一次会重跑整张图
    （N2 会重新生成用例、N4 会重新投递）。所以这里直接返回 `pending`。
    """
    from testflow.domain.graph import RunPhase

    context: GatewayContext = request.app.state.ctx
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        return WakeResponse(status="pending")

    session = context.session()
    try:
        run = runs_repo.get(session, parsed)
        if run is None or RunPhase(run.phase).is_terminal:
            return WakeResponse(status="pending")

        # ★★ **完成判定必须查权威表，不能读 `runs.expected_case_count`**（DM5）。
        #
        # 第一版读的是那个派生缓存，而**没有任何节点维护它**——它恒为 0。
        # 于是这里的条件是 `expected == 0` → 永远返回 `pending`，
        # **回调唤醒整条路径是死的**：每条用例完成时的唤醒全部空转，
        # Run 只能靠兜底轮询往前挪。
        #
        # 这个 bug 的表现是**慢**而不是错（Run 最终会完成），
        # 所以它不会被任何测试或告警抓住——只有把服务跑起来、
        # 看着 `awaiting_results` 干等十几秒才看得出来。
        #
        # `04` DM5 把这条规则写得很直白："任何正确性判断一律以
        # `SELECT count(*)` 查询 `case_results` 为准"。这里原先违反了它，
        # 而违反的代价恰好落在"该推进时不推进"上——那是 DM5 举的反例本身。
        settled = results_repo.count_results(session, parsed)
        expected = cases_repo.count_dispatchable(session, parsed)
    finally:
        session.close()

    if expected == 0 or settled < expected:
        # 还没到齐。**不做任何事**——这正是"每条用例都回调一次"这个设计的代价：
        # N-1 次调用会走到这个分支，而它们都是白跑的。
        # 收益是不需要"最后一个任务自我识别"（那有竞态，见模块文档字符串）。
        return WakeResponse(status="pending")

    _enqueue(context, parsed)
    return WakeResponse(status="resumed")


def _enqueue(context: GatewayContext, run_id: uuid.UUID) -> None:
    """投递推进指令。

    **失败只记日志，不改变响应。** 这个端点是"通知者"，不是"执行者"——
    它的职责到"判定该推进了"为止。投递失败时兜底轮询会补上，
    而把它变成 5xx 会让 Worker 侧以为"回调失败"并重试——
    重试是对的，但我们并不需要它重试（幂等 + 兜底已经覆盖）。
    """
    try:
        context.require_producer().orchestrate(run_id, "results_ready")
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        logger.error("唤醒时投递推进指令失败 run=%s：%s", run_id, exc)
