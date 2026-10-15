#!/usr/bin/env bash
#
# 导出并校验 OpenAPI 契约快照。对应 `05-API契约.md` §1.2 / `08` EN6。
#
# 契约的**唯一事实来源是代码**（FastAPI 路由 + Pydantic 模型），本脚本把它的
# 产物 `openapi.json` 落到仓库根目录并提交——这样契约的变更会出现在评审 diff 里。
#
# 退出码：
#   0  快照与代码一致
#   1  快照已更新（需要提交）—— 这是 CI 中的失败信号
#   2  无法导出（Gateway 尚未实现）

set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONUTF8=1

SNAPSHOT="openapi.json"

if ! uv run python -c "from testflow.entrypoints.gateway import create_app" 2>/dev/null; then
  echo "⊘ Gateway 尚未实现，无法导出契约快照（计划于 M1）" >&2
  exit 2
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# 导出并对结果排序，使 diff 稳定（否则 Pydantic 的字段顺序变化会产生噪声 diff）
uv run python - "$TMP" <<'PY'
import json
import sys

from testflow.entrypoints.gateway import create_app

spec = create_app().openapi()
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(spec, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY

if [ ! -f "$SNAPSHOT" ] || ! diff -q "$TMP" "$SNAPSHOT" > /dev/null; then
  cp "$TMP" "$SNAPSHOT"
  echo "✗ 契约快照已更新：$SNAPSHOT" >&2
  echo "  代码与已提交的契约不一致。请检查 diff 并提交——" >&2
  echo "  不要手动编辑本文件，它是从代码生成的（ADR-0023）。" >&2
  exit 1
fi

echo "✓ 契约快照与代码一致"
