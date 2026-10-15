"""LLM Provider 的窄接口。

对应决策：ADR-0005、`02-架构设计.md` D7、`08-工程规范.md` §2.3。

## 接口只有一个方法，而且很窄

    generate(prompt, schema) -> BaseModel

**厂商特有的参数不出现在签名里**（`enable_thinking`、`result_format`、
`stream`……）。它们由适配器内部映射。

这不是"为了好看"：业务代码（`TestCaseGen`、`LogAnalyzer`）依赖的应该是
"给我一个符合这个 Schema 的对象"这件事，而不是"百炼的某个参数叫什么"。
后者一旦渗进业务逻辑，换模型时改动面就失控了——而那正是 D7 要防的。

## 为什么这个文件不 import 任何 SDK

`llm/` 是**唯一**允许 import LLM SDK 的包（架构测试强制）。但接口本身不该
依赖任何一个实现——它只是几个类型。把 `openai` 的导入留在 `qwen.py` 里，
将来加 Claude 适配器时，这个文件和业务代码都不用动。

## 为什么 `schema` 是 Pydantic 模型而不是 JSON Schema 字符串

**Pydantic 模型是单一事实来源。** 手写一份 JSON Schema 就产生了两份需要
同步的东西：模型改了忘了改 Schema，而 Schema 才是发给模型的那一份——
于是模型按旧结构输出，解析失败，而错误指向解析器而不是那个忘了改的 Schema。

从模型导出 Schema 是确定的，反过来不是。
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

__all__ = ["LLMClient", "LLMError", "ModelT"]

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMError(Exception):
    """LLM 调用失败：网络、限流、超时、输出无法解析。

    **与"模型输出不合业务语义"是两回事**：后者由 Pydantic 的结构校验
    和 A2~A4 那些约束拦（生成期），而这里是"根本没拿到可用的输出"。
    两者在上层都会导致 `run_operations.gen_status = 'failed'`，
    但归因与重试策略不同。
    """


class LLMClient(Protocol):
    """所有 Provider 实现这一组方法。

    用 `Protocol` 而不是 ABC：测试里的假 Provider 不需要继承任何东西——
    它只要"长得像"。而一个必须继承基类的接口，会让"写一个假实现"这件事
    多出一道仪式，于是人们更倾向于去 mock 真实实现（那更脆）。
    """

    async def generate(self, *, prompt: str, schema: type[ModelT]) -> ModelT:
        """按 Schema 生成一个结构化对象。

        Args:
            prompt: 完整的用户提示。**稳定前缀的构造由调用方负责**
                （`agents/prompts.py`）——Provider 不该知道什么是"稳定前缀"，
                那是缓存策略，不是模型接口。
            schema: 期望的输出结构。实现方负责把它转成厂商的 Schema 形式。

        Returns:
            一个通过 Pydantic 校验的实例。

        Raises:
            LLMError: 调用失败或输出无法解析。
        """
        ...
