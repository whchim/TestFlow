#!/usr/bin/env bash
#
# M1 的端到端验收：**一份 Spec 进去，一份报告出来**（11-开发路线图.md M1）。
#
#   docker compose up -d
#   ./scripts/demo.sh
#
# 全程走真实链路：Gateway 收文档 → 编排器跑图 → Worker 打 fixture → 出报告。
# 没有任何一步是打桩的。
#
# ## 为什么 demo 不需要 LLM API Key
#
# fixture 的端点里有一半满足 TG5 的"无信息接口"（没有请求参数、没有请求体、
# 没有声明任何 4xx），它们走**纯规则路径，零 LLM 调用**。
# 所以这条演示在没有任何外部依赖的情况下也能跑通——而那正是"链路优先于质量"
# 这条 M1 原则想要的：先证明管子通了，再谈里面流的是什么。
#
# 配了 `TESTFLOW_LLM_API_KEY` 时，`/echo` 这类需要生成的接口会走真实模型，
# 从而产出 RM4 要求的生成质量数字。

set -euo pipefail

# 统一 UTF-8。与 ci.sh 同一个理由：中文版 Windows 的 locale 是 GBK，
# 而内联的 Python 会往 stdout 打非 ASCII 字符。
export PYTHONUTF8=1

cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
step() { printf "\n%s==> %s%s\n" "$BOLD" "$1" "$OFF"; }
info() { printf "    %s%s%s\n" "$DIM" "$1" "$OFF"; }
ok()   { printf "    %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
fail() { printf "\n%s✗ %s%s\n" "$RED" "$1" "$OFF" >&2; exit 1; }

GATEWAY="${TESTFLOW_GATEWAY_URL:-http://127.0.0.1:3000}"
TOKEN="${TESTFLOW_API_TOKEN:-dev-api-token}"
FIXTURE_INTERNAL="http://fixture:9000"
TIMEOUT_SECONDS="${DEMO_TIMEOUT:-180}"

api() {
  local method="$1" path="$2"; shift 2
  curl -sS -X "$method" "$GATEWAY$path" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "$@"
}

# 用 Python 解析 JSON，不依赖 jq——Git Bash 上通常没有它，
# 而给一个演示脚本加一条"先装 jq"的前置条件会显著降低它被跑起来的概率。
json_get() { python -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}))" "$1"; }

# ───────────────────────────────────────────────── 0. 前置检查

step "0. 检查服务"
for service in gateway postgres redis fixture; do
  state=$(docker compose ps --format json "$service" 2>/dev/null | python -c "
import json,sys
raw = sys.stdin.read().strip()
print(json.loads(raw)['State'] if raw else 'missing')
" 2>/dev/null || echo "missing")
  if [ "$state" != "running" ]; then
    fail "$service 未运行（当前：$state）。先执行：docker compose up -d"
  fi
  ok "$service 运行中"
done

if ! curl -fsS "$GATEWAY/healthz" > /dev/null; then
  fail "Gateway 不可达：$GATEWAY"
fi
ok "Gateway 可达"

# ───────────────────────────────────────────────── 1. 迁移

step "1. 确保数据库结构已就绪"
if docker compose --profile tools run --rm migrate 2>&1 | tail -3; then
  ok "alembic upgrade head 完成"
else
  fail "迁移失败"
fi

# ───────────────────────────────────────────────── 2. 取被测文档

step "2. 取 fixture 的接口文档"

# ⚠️ **必须改写 `servers`。**
#
# fixture 自己生成的是 `http://127.0.0.1:9000`——那是它**在容器内**的地址。
# 而 Worker 在另一个容器里，它的 `127.0.0.1` 是它自己。
# 不改写的话白名单里会是一个连不上的地址，而现象是"所有请求都被拒绝"，
# 看起来像 Guardrail 配错了。
#
# 这一段同时演示了 `declared_targets` 的来源：白名单**从这份文档派生**，
# 所以改文档就等于改白名单——这正是 SR-1 的威胁模型。
SPEC=$(docker compose exec -T fixture python -c "
import json, urllib.request
doc = json.loads(urllib.request.urlopen('http://127.0.0.1:9000/openapi.json').read())
doc['servers'] = [{'url': '$FIXTURE_INTERNAL'}]
# 剔除以 \`__\` 开头的测试辅助端点（\`09\` §3.2 明确它们不属于那 11 个端点）。
# 留着它们会让演示自相矛盾：\`POST /__reset\` 会把命中计数清零，
# 而 \`GET /__hits\` 读的正是那个计数——两个测试辅助端点互相干扰，
# 而报告里会出现两条毫无意义的失败。
doc['paths'] = {p: v for p, v in doc['paths'].items() if not p.startswith('/__')}
print(json.dumps(doc))
")
info "已把 servers 改写为 $FIXTURE_INTERNAL（容器内互访的地址）"

# ───────────────────────────────────────────────── 3. 提交 Spec

step "3. 提交 Spec"
SPEC_RESPONSE=$(python -c "
import json,sys
print(json.dumps({'content': sys.stdin.read()}))
" <<< "$SPEC" | api POST /api/specs --data-binary @-)

SPEC_ID=$(json_get "d['spec_id']" <<< "$SPEC_RESPONSE")
OPERATION_COUNT=$(json_get "d['operation_count']" <<< "$SPEC_RESPONSE")
REUSED=$(json_get "d['reused']" <<< "$SPEC_RESPONSE")
TARGETS=$(json_get "d['declared_targets']" <<< "$SPEC_RESPONSE")

ok "spec_id=$SPEC_ID  接口数=$OPERATION_COUNT  复用=$REUSED"
info "白名单来源（declared_targets）：$TARGETS"

python -c "
import json,sys
warnings = json.load(sys.stdin).get('warnings') or []
for w in warnings:
    print(f'    ! {w[\"code\"]}: {w[\"detail\"]}')
" <<< "$SPEC_RESPONSE"

# ───────────────────────────────────────────────── 4. 发起运行

step "4. 发起运行"
RUN_RESPONSE=$(api POST /api/runs --data "{\"spec_id\": \"$SPEC_ID\"}")
RUN_ID=$(json_get "d['run_id']" <<< "$RUN_RESPONSE")
ok "run_id=$RUN_ID（已投递到 orchestrate 队列）"

# ───────────────────────────────────────────────── 5. 等待完成

step "5. 等待执行完成"
info "（M1 没有人工确认门：用例生成后自动全批准——见 11 §7 Q1）"

deadline=$(( SECONDS + TIMEOUT_SECONDS ))
last_phase=""
while [ $SECONDS -lt $deadline ]; do
  body=$(api GET "/api/runs/$RUN_ID")
  phase=$(json_get "d['phase']" <<< "$body")
  if [ "$phase" != "$last_phase" ]; then
    info "phase: $last_phase → $phase"
    last_phase="$phase"
  fi
  case "$phase" in
    done) break ;;
    failed|abandoned) fail "Run $phase：$(json_get "d.get('failure_reason')" <<< "$body")" ;;
  esac
  sleep 1
done

if [ "$last_phase" != "done" ]; then
  fail "Run 在 ${TIMEOUT_SECONDS} 秒内未完成（当前阶段：$last_phase）"
fi
ok "Run 完成"

# ───────────────────────────────────────────────── 6. 报告

step "6. 报告"
REPORT=$(api GET "/api/runs/$RUN_ID/report" -H "Accept: text/markdown")
printf "\n%s\n" "$REPORT"

# ───────────────────────────────────────────────── 7. 断言

step "7. 验收断言"

# 通过率取自报告的结构化内容——**分母只含已执行用例**（RA3）。
SUMMARY=$(api GET "/api/runs/$RUN_ID/report")
python - "$SUMMARY" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
s = payload["content"]["summary"]
denominator = s["passed"] + s["failed"] + s["timeout"] + s["error"]

if denominator == 0:
    print("    ✗ 一条用例都没执行——报告里的通过率会是「不适用」")
    raise SystemExit(1)

rate = s["passed"] / denominator
print(f"    通过率 = {rate:.1%}  （{s['passed']}/{denominator}，未执行 {s['skipped']} 条）")

# fixture 的实际行为是**确定的**：/ok 返回 200、/bad-json 返回 200 但正文不是 JSON……
# 所以通过率既不该是 0% 也不该是 100%。两个极端都说明链路上有问题，
# 而"非空且与 fixture 行为一致"正是路线图 M1 的验收措辞。
if not (0.0 < rate < 1.0):
    print(f"    ✗ 通过率 {rate:.1%} 落在两个极端上——链路上有问题")
    raise SystemExit(1)

print("    ✓ 通过率非空，且不是 100%（fixture 里本来就有一半端点会失败）")
PY

printf "\n%s✓ M1 端到端链路跑通%s\n\n" "$GREEN" "$OFF"
info "报告快照（Markdown）：GET $GATEWAY/api/runs/$RUN_ID/report"
info "运行状态：            GET $GATEWAY/api/runs/$RUN_ID"
info "事件流：              GET $GATEWAY/api/runs/$RUN_ID/events"
