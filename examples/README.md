# 示例文档

## `fixture-openapi.json`

内置被测服务 `fixture` 的 OpenAPI 文档（11 个路径、12 个操作），
由 `src/testflow/testing/fixture_server.py` 自动生成。

可以直接拖进前端上传页，或用 CLI：

```bash
uv run testflow run examples/fixture-openapi.json --wait
```

### 它故意包含了一半会失败的接口

fixture 不是"一个正常的 API"，而是把各种边界行为做成**可执行的规格**
（`docs/09-测试策略.md` §3.2）：

| 接口 | 行为 | 用来验证 |
| :--- | :--- | :--- |
| `GET /ok` | 200 + 合法 JSON | 正常路径 |
| `GET/POST /echo` | 回显请求 | `json_path` / `header` 断言 |
| `GET /no-content` | 204 无响应体 | 空 body 上的断言 |
| `GET /bad-json` | 200，但响应体是 HTML | 三态断言（无法求值 vs 不成立） |
| `GET /error-500` | 500 | `server_error` 归因 |
| `POST /invalid-echo` | 400 + 校验错误结构 | `request_invalid` 归因 |
| `GET /slow` | 默认 30 秒才返回 | 三层超时 |
| `GET /redirect` | 302 | 不跟随重定向 |
| `GET /large` | 512 KB 响应体 | 响应截断 |
| `GET /recursive` | 递归 `$ref` | `$ref` 展开深度上限 |
| `DELETE /destructive` | 接受 DELETE | 破坏性方法拦截（不会被执行） |

因此一次完整运行的通过率大约在 50% 左右——**这是设计如此**，不是链路出了问题。

### 为什么 `servers` 写的是 `fixture:9000`

这是 Docker Compose 网络里的服务名。Gateway 与 Worker 都在同一个网络里，
所以能解析它；如果从宿主机直接访问，需要改成 `http://127.0.0.1:9000`，
并在配置里把该地址加入内网例外（`TESTFLOW_GUARDRAIL_ALLOW_PRIVATE_TARGETS`）。

Docker Compose 的默认配置已经包含 `fixture:9000`。

## Golden Set 不是运行样本

`eval/golden/golden_set.yaml` 也是一份 OpenAPI 文档，但它的 10 个接口是
**虚构的**（`http://golden.testflow.local` 并不存在），只用于检查
"维度派生算法有没有漏维度"。它不能真的执行，也不应该拿来当上传样本。
