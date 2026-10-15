"""结构化日志与**脱敏**。

对应决策：`08-工程规范.md` EN5 / `07-非功能需求.md` NF7 / PRD SR-6。

**脱敏放在这里，而不是"靠调用方记得不要写"**——理由与 `Guardrail.md` §10 相同：
调用方会忘记，而日志一旦落盘就难以彻底清除。日志会被采集、转发、存档，
一次误写就是一次长期的泄露。

⚠️ 这不是"防御性编程"，是**安全底线**：`config.py` 里刻意没有
`log_credentials` 这类开关（`tests/unit/test_config.py` 有断言保证），
所以这里的过滤是硬编码的、无条件的。

M4 会把它扩成完整的可观测性（结构化字段、采样、导出）。M1 只保证两件事：
**能配日志级别**、**敏感字段进不去**。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from testflow.observability.tracing import current_trace_id

__all__ = ["RedactionFilter", "configure_logging", "get_logger"]


def get_logger(name: str) -> logging.Logger:
    """取一个模块级 logger。

    存在的理由不是"包一层没有意义"，而是**给"日志怎么来的"一个统一入口**：
    将来要换成 `structlog`、要统一加 `trace_id`、要按模块设级别，
    改这一个函数就够了，不用去动几十个模块里的 `logging.getLogger(__name__)`。

    **不在这里附加 handler。** handler 由进程入口通过 `configure_logging`
    挂到根 logger 上——模块级 logger 自己挂 handler 会让同一条日志
    在多个进程/多次导入时重复输出。
    """
    return logging.getLogger(name)


#: 需要脱敏的 header / 字段名（大小写不敏感）。前四个是凭证载体，
#: 后两个是请求体与响应体的常见键名。
_SENSITIVE_KEYS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api_key",
        "apikey",
        "cookie",
        "set-cookie",
        "password",
        "secret",
        "token",
        "credential",
    }
)

#: 形如 `Authorization: Bearer xxx` 的替换。
#:
#: **只擦值、保留键名**——日志的作用是排障，"哪个字段被擦了"本身是信息。
#: 整段替换（含键名）会把 `{'Cookie': 'session=abc'}` 啃成 `{'[REDACTED] ...}`，
#: 安全但看不出发生了什么。分组 1 = 键名与分隔符，分组 2 = 值。
#:
#: `\b` 保证 `authorization_notes` 这类词不被误伤；`["']?` 容忍 repr 出来的
#: `{'Cookie': 'session=abc'}`——键名后面跟引号，是日志里最常见的形态。
#: 值的四个分支依次是：
#:   `"..."` / `'...'` —— repr 出来的带引号值
#:   `(bearer|basic)\s+\S+` —— **认证方案 + 令牌是两个 token**。
#:       少了这一支，`Authorization: Bearer sk-xxx` 只会被擦掉 `Bearer`，
#:       而**把令牌原样留在日志里**——这是实打实漏过一次的地方。
#:   `\S+` —— 兜底的单 token 值
_SENSITIVE_PAIR: Final = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|x-api-key|api[_-]?key|cookie|"
    r"password|secret|token|credential)\b[\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|(?:bearer|basic)\s+\S+|\S+)"
)

#: Bearer / Basic 后面跟的长串（独立出现、前面没有键名时）。
_BEARER: Final = re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9\-._~+/=]{8,}")

_MASK: Final = "[REDACTED]"


def _scrub_text(text: str) -> str:
    """保留键名、擦掉值。可反复施加（幂等）——`[REDACTED]` 不会被再擦一次。"""
    text = _SENSITIVE_PAIR.sub(rf"\g<1>{_MASK}", text)
    return _BEARER.sub(rf"\g<1>{_MASK}", text)


def _scrub(value: Any) -> Any:
    """递归脱敏任意结构。

    对 dict **按键名**判断、对 str **按模式**判断——两者都要，因为凭证可能
    作为字段值出现（`{"Authorization": "..."}`），也可能作为一整条消息出现。
    """
    if isinstance(value, dict):
        return {
            k: (_MASK if str(k).lower() in _SENSITIVE_KEYS else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    if isinstance(value, str):
        return _scrub_text(value)
    return value


class RedactionFilter(logging.Filter):
    """在日志**离开进程之前**把敏感内容擦掉。

    实现成 `Filter` 而非格式化器的一部分，是为了让它作用于**所有** handler——
    包括将来新增的、以及第三方库自己加的。挂在 logger 上会漏掉后者。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                # **顺序很重要**：先按结构擦 `args`，再拼成消息。
                # 反过来（先 getMessage 再擦）会把 dict 拍平成一句话，
                # 于是 `{"Cookie": "..."}` 里的键名就丢了，
                # 只能靠文本模式去猜——而它认不出引号包裹的键名。
                record.args = _scrub(record.args)
                # 拍平之后再过一遍文本模式，兜住"键名与值被拼进同一句话"的情况。
                record.msg = _scrub_text(record.getMessage())
            else:
                record.msg = _scrub_text(str(record.msg))
            record.args = ()
        except Exception:  # noqa: BLE001 —— 日志不能因为脱敏失败而让程序崩
            # 擦不干净时**不放行**：宁可丢一条日志，也不冒泄露的风险（fail-closed）。
            record.msg = "[REDACTION_FAILED]"
            record.args = ()

        # 额外字段（`logger.info(..., extra={...})`）同样要擦。
        for key, val in list(record.__dict__.items()):
            if key.startswith("_") or key in _STD_ATTRS:
                continue
            record.__dict__[key] = _scrub(val)
        return True


#: `LogRecord` 的内建属性名——不是 `extra` 传进来的，跳过它们以免误伤。
_STD_ATTRS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class _TraceIdFilter(logging.Filter):
    """把当前上下文的 `trace_id` 注入每条记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"
        return True


def configure_logging(level: str = "INFO") -> None:
    """配置根 logger。**幂等**——重复调用不会叠加 handler。

    由四个进程入口各自调用一次。放在入口而不是导入时执行：导入时配置会让
    "只是 import 了这个模块"的测试也被改掉日志配置。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 幂等：先摘掉本函数此前加过的 handler。
    for handler in list(root.handlers):
        if getattr(handler, "_testflow", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler._testflow = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    # 顺序有讲究：先擦敏感内容，再注入 trace_id——反过来会让刚注入的 trace_id
    # 也走一遍 scrub（虽然它不含敏感词，但没必要）。
    handler.addFilter(RedactionFilter())
    handler.addFilter(_TraceIdFilter())
    root.addHandler(handler)
