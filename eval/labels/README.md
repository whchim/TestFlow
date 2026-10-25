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

## 结果文件

`score` 默认写入 `eval/results/<时间>-labels.json`，包含：

- 用例准确率、断言准确率
- 按接口、按维度的拆解
- 本次标注对应的 `golden_set_sha256` 与模型名
