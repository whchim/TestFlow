# 用例准确率的人工标注

`09-测试策略.md` §5.4 把指标分成两类：自动可算的（结构、覆盖）与必须人工标注的
（用例本身写得对不对）。这个目录是后者的落点。

## 流程

```bash
# ① 生成待标注的用例（会真实调用模型，约 ¥0.01）
uv run python scripts/label_cases.py generate

# ② 打开 eval/labels/golden-set-cases.yaml，逐条填写 verdict
#    correct / incorrect / skip；也可以写 note 说明原因

# ③ 汇总
uv run python scripts/label_cases.py score
```

`score` 在还有未标注的用例时会拒绝输出最终结果（退出码 2），
避免拿一个"只标了 5 条"的数字当结论。确实需要部分结果时加 `--allow-partial`。

## verdict 怎么填

| 值 | 含义 |
| :--- | :--- |
| `correct` | 请求构造合理，断言与 Spec 一致，且能验证该维度 |
| `incorrect` | 请求取值、期望状态码或断言有错 |
| `skip` | 无法判断（例如 Spec 信息不足），在 `note` 里写原因 |
| 空（`null`） | 还没标 |

准确率只统计 `correct + incorrect`。**未标注既不算对也不算错**——
两种默认都会让数字失真。

如果还想单独统计断言准确率，可以填 `assertion_verdicts`：与 `assertions`
等长的列表，元素是 `correct` / `incorrect` / `null`。不填则断言准确率为
`—`（未知），不是 0%。

## 为什么标注必须独立

这是一份**外部基准**。如果标注由生成方（模型）自己完成，或者直接复制代码的
输出，那么它测到的仍然是"代码和自己一致"，无法发现系统性错误——
这正是 `09` §5.2 说的自我参照陷阱。

## 一个实测发现

第一次生成标注表时，Qwen 3.7 Flash 在 `request_body`（自由格式对象）上
产生了不合法的 JSON 片段：键名前多出冒号（`': email'`），甚至出现空键。
SDK 的宽松解析把它变成 `{': email': ...}` 或 `{}`。

修法是让模型把请求体写成 **JSON 字符串**（`request_body_json`），平台再用
`json.loads` 解析并校验；解析失败会走既有的"带反馈重试"路径。
相关代码在 `agents/llm_case_gen.py::materialize_request_bodies`，
回归测试在 `tests/unit/test_free_form_repair.py`。

## 首轮基线（2026-10-25）

`golden-set-cases-labeled.yaml` 是第一轮的标注结果：

| 指标 | 值 |
| :--- | ---: |
| 用例准确率 | **47.2%**（17 正确 / 19 错误） |
| 断言准确率 | **78.3%** |
| 跳过 | 3 条（Spec 信息不足） |

> ⚠️ 这一轮由 **GPT 独立标注**，不是人工逐条复核。它比"生成方自己打分"可靠
> （不同模型、不同提示词），但严格来说仍是 **LLM-as-judge**。写进简历或文档时
> 应当说明来源；需要更强结论时，抽检一部分即可。

错误集中在三类**系统性缺陷**，而不是随机波动：

1. **路径参数没有取值**：`/invoices/{invoice_id}` 原样保留模板占位符（7 条）
2. **`schema_valid` 指向没有响应体 schema 的状态码**（8 条）
3. **请求结构损坏**：查询参数/请求头里混进了兄弟字段（`/reports/summary`、
   `/devices`）

这三类已修复：

- `GeneratedCase` 增加 `path_params_json`，执行前替换模板；L1 校验每个
  `{变量}` 都有值
- L1 新增 `schema_valid_without_schema`：Spec 没声明 content schema 时禁止该断言
- 查询参数与请求头也改走 JSON 字符串通道（与 `request_body_json` 同一机制）
- 边界值增加确定性修正：`maxLength=280` 就是 280 字符；`max=100` +
  `multipleOf=5` 的越界值是 105
- 修复后重新生成 `golden-set-cases.yaml`，需要**第二轮标注**才能得到新数字

## 第二轮结果（2026-10-26）

`golden-set-cases-round2-labeled.yaml` 是修复后的第二轮标注：

| 指标 | 第一轮（修复前） | 第二轮（修复后） |
| :--- | ---: | ---: |
| 用例准确率 | **47.2%**（17 / 36） | **85.2%**（23 / 27） |
| 断言准确率 | **78.3%** | **91.7%** |
| 跳过 | 3 | 12 |

### 为什么跳过从 3 涨到 12

不是生成变差了，而是**判据变严了**：第二轮用例开始对响应体做断言
（`$.id`、`$.error`），而 Golden Set 的这些接口大多**没有声明响应体 schema**。
独立标注只能判"Spec 支持"的事实，不能替 Spec 补答案，所以标 `skip`。

这 12 条 skip 指向的是 **Golden Set 本身的信息不足**，不是生成器的错误：

- `/orders`、`/users` 的 4xx 只写了 `description`，没有响应体结构
- `/items`、`/orders` 的成功响应没有声明 `id`
- `/health` 没有声明安全要求
- `/devices` 的自定义 `format: imei` 没有定义具体校验规则
- `/invoices` 的 409 没有说明如何构造冲突状态

如果要让更多用例可判定，下一步应当**给 Golden Set 补响应 schema 与失败触发条件**，
而不是放宽标注标准。

### 剩下 4 条错误

| 用例 | 原因 | 处理 |
| :--- | :--- | :--- |
| `/products` `boundary:quantity:min` | `quantity=1` 满足 `minimum=1`，但违反 `multipleOf=5` | 已修 `boundary_target`：最小合法值是 5 |
| `/products` `boundary:quantity:min_over` | `-4` 同时违反 `minimum` 与 `multipleOf` | 已修：应为 0（小于 1 且是 5 的倍数） |
| `/reports/summary` `value_valid` | `body_contains` 的 `text` 是空字符串，恒真 | 已加 L1 `assertion_empty_value` |
| `/invoices` `status:400` | `invoice_id` 只声明为 string，没有格式约束，无法构造 400 | 开放问题：声明的错误码缺少触发条件 |

前三条在第二轮之后已修复；第三条的同类问题还会被新的 L1 规则拦住。
最后一条是"声明了错误码但没有说明怎么触发"的普遍问题，需要 Golden Set
补充触发条件，或把这类维度标为低置信度。

## 结果文件

`score` 默认写入 `eval/results/<时间>-labels.json`，包含：

- 用例准确率、断言准确率
- 按接口、按维度的拆解
- 本次标注对应的 `golden_set_sha256` 与模型名

| 轮次 | 结果文件 |
| :--- | :--- |
| 第一轮（基线） | `eval/results/20261025-085145-labels.json` |
| 第二轮（修复后） | `eval/results/20261026-024359-labels.json` |
