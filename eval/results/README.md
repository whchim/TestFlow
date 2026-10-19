# L3 评测结果

`09-测试策略.md` §5.7 要求 L3 的结果**留痕**：不记录的话，就无法回答
"这次改动让质量变差了吗"。

每个文件由 `scripts/eval_golden.py` 生成，名字是
`<UTC 时间>-<模型名>.json`，内容包含：

| 字段 | 含义 |
| :--- | :--- |
| `git_commit` | 产出这份结果的代码版本 |
| `git_dirty` | 运行时有未提交改动时为 `true`——此时 `git_commit` 只是一个旧基线 |
| `model` | 生成用的模型 |
| `thinking` | 思考模式开/关（档位对比的一条轴） |
| `golden_set_sha256` | 输入文档的版本 |
| `annotations_sha256` | 人工标注的版本 |
| `prompt_sha256` | 提示词文件的版本 |
| `layer1` | 第一层：派生正确性（人工标注 vs 阶段 A） |
| `layer2` | 第二层：生成完整性（真实调用；未跑时为 `null`） |
| `layer2[].result.transport_failures` | 超时/连接失败次数。**不计入首次通过率的分母**——网络抖动不是生成质量问题，但次数必须单独可见 |
| `layer2[].result.rounds[].rows[].error_kind` | `transport` 或 `content`：两类失败的处置完全不同（重跑 vs 改提示词） |
| `layer2[].result.rounds[].tg5_violations` | 标注为纯规则路径、却产生了 LLM 调用的接口。非空即 TG5 失效 |

**这些文件随代码入库。** 它们是"质量随版本的变化"唯一的记录——
只把结果打印到终端，等于没有记录。

`--generate` 的结果会花真钱，所以按 EN8 手动触发，不进 CI。
第一层是纯算法，由 `tests/unit/test_golden_set.py` 在每次 CI 里跑。
