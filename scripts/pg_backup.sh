#!/usr/bin/env bash
#
# PostgreSQL 备份、校验与恢复演练（`10-部署运维.md` OP3 / OP4）。
#
#   ./scripts/pg_backup.sh backup          # 备份 + 立即校验
#   ./scripts/pg_backup.sh verify FILE     # 只校验一个备份文件
#   ./scripts/pg_backup.sh drill [FILE]    # 备份 → 恢复到临时库 → 对比行数 → 清理
#   ./scripts/pg_backup.sh prune           # 日备 7 份 + 周备 4 份
#
# ## 为什么恢复演练要恢复到**另一个库**
#
# OP4 的要求是"备份必须验证可恢复"。只做 `pg_restore --list` 只能证明
# 归档的目录完整，不能证明数据能落进一个真实的 schema。而直接恢复到
# 生产库会把现有数据覆盖掉——那是把"验证"做成了"事故"。
#
# 所以 drill 建一个临时库 `testflow_restore_check`，恢复、查询、对比行数，
# 然后删掉它。**生产库 `testflow` 从头到尾不被触碰。**
#
# ## 为什么用 `docker compose exec` 而不是宿主机上的 pg_dump
#
# 宿主机不需要安装 PostgreSQL 客户端，也不会出现"宿主机 psql 18 连
# 容器里的 pg_dump 17"这种版本错配。代价是二进制数据要经过 docker exec
# 的 stdin/stdout——`-T` 关掉 TTY 是必需的，否则输出会被终端控制字符污染。
#
# ## 备份位置
#
# 默认 `./backups/`（已加入 .gitignore）。OP3 建议放到宿主机之外的介质；
# 这里做了两件事：目录可配置（`TESTFLOW_BACKUP_DIR`），且**不自动上传**
# ——上传到哪、用什么凭证，是部署环境的问题，不该写死在一个脚本里。

set -euo pipefail

cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; OFF=$'\033[0m'
step() { printf "\n%s==> %s%s\n" "$BOLD" "$1" "$OFF"; }
info() { printf "    %s%s%s\n" "$DIM" "$1" "$OFF"; }
ok()   { printf "    %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
fail() { printf "\n%s✗ %s%s\n" "$RED" "$1" "$OFF" >&2; exit 1; }

SERVICE="postgres"
DB_USER="testflow"
DB_NAME="testflow"
DRILL_DB="testflow_restore_check"
BACKUP_DIR="${TESTFLOW_BACKUP_DIR:-backups}"
KEEP_DAILY=7
KEEP_WEEKLY=4

# 备份文件命名必须能被 prune 解析：`testflow-YYYYmmdd-HHMMSS.dump`。
# 用 UTC，避免两次运行跨越本地时区切换时出现"同一天两个时间戳"。
timestamp() { date -u +%Y%m%d-%H%M%S; }

require_postgres() {
  local state
  state=$(docker compose ps --format json "$SERVICE" 2>/dev/null | python -c "
import json, sys
raw = sys.stdin.read().strip()
print(json.loads(raw)['State'] if raw else 'missing')
" 2>/dev/null || echo "missing")
  [ "$state" = "running" ] || fail "$SERVICE 未运行（当前：$state）。先执行：docker compose up -d"
}

pg_exec() {
  docker compose exec -T "$SERVICE" "$@"
}

psql_value() {
  # 只取一个标量。`-A` 去掉对齐空格，`-t` 去掉表头与行数。
  pg_exec psql -U "$DB_USER" -d "$1" -At -c "$2"
}

# ------------------------------------------------------------------ 备份

cmd_backup() {
  local dir="${1:-$BACKUP_DIR}"
  mkdir -p "$dir"
  require_postgres

  step "备份 $DB_NAME"
  local file="$dir/testflow-$(timestamp).dump"
  local partial="$file.partial"

  # 先写 `.partial`，成功后再改名：pg_dump 失败时重定向已经创建了文件，
  # 如果不改名，prune 会把一个 0 字节的"备份"当成最新的那份保留下来。
  if pg_exec pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$partial"; then
    mv "$partial" "$file"
  else
    rm -f "$partial"
    fail "pg_dump 失败"
  fi

  local size
  size=$(du -h "$file" | cut -f1)
  ok "已写入 $file（$size）"

  # OP4：备份完**立刻**校验一次。等到需要恢复的那天才发现备份是坏的，
  # 就没有第二次机会了。
  cmd_verify "$file"
  echo "$file"
}

# ------------------------------------------------------------------ 校验

cmd_verify() {
  local file="${1:?用法：$0 verify <备份文件>}"
  [ -f "$file" ] || fail "文件不存在：$file"

  step "校验归档目录 $file"
  if pg_exec pg_restore --list < "$file" > /dev/null; then
    ok "归档可读（TOC 完整）"
  else
    fail "归档损坏或不是 -Fc 格式"
  fi
}

# ------------------------------------------------------------------ 恢复演练

cmd_drill() {
  local file="${1:-}"
  if [ -z "$file" ]; then
    file=$(cmd_backup | tail -n 1)
  else
    cmd_verify "$file"
  fi

  require_postgres
  step "恢复到临时库 $DRILL_DB（**不触碰 $DB_NAME**）"

  # 上一次演练中途被中断时，临时库可能还在。
  pg_exec psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DRILL_DB" > /dev/null
  pg_exec psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE $DRILL_DB" > /dev/null
  ok "临时库已创建"

  if ! pg_exec pg_restore -U "$DB_USER" -d "$DRILL_DB" --no-owner < "$file" > /dev/null; then
    pg_exec psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB" > /dev/null || true
    fail "恢复失败——备份文件不可用"
  fi
  ok "恢复完成"

  # 对比行数。只比"非零"不够：一个只恢复了 3 行的库也是非零的。
  local failed=0 table prod restored
  for table in runs specs cases case_results attributions reports; do
    prod=$(psql_value "$DB_NAME" "select count(*) from $table")
    restored=$(psql_value "$DRILL_DB" "select count(*) from $table")
    if [ "$prod" != "$restored" ]; then
      printf "    %s✗%s %-14s 生产 %s / 恢复 %s\n" "$RED" "$OFF" "$table" "$prod" "$restored"
      failed=1
    else
      printf "    %s✓%s %-14s %s\n" "$GREEN" "$OFF" "$table" "$prod"
    fi
  done

  pg_exec psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $DRILL_DB" > /dev/null
  ok "临时库已删除"

  [ "$failed" -eq 0 ] || fail "恢复后的行数与生产不一致"
  printf "\n%s✓ 恢复演练通过：备份可恢复到真实 schema，且数据完整%s\n\n" "$GREEN" "$OFF"
}

# ------------------------------------------------------------------ 保留策略

cmd_prune() {
  local dir="${1:-$BACKUP_DIR}"
  mkdir -p "$dir"

  step "清理旧备份（日备 $KEEP_DAILY 份 + 周备 $KEEP_WEEKLY 份）"
  python - "$dir" "$KEEP_DAILY" "$KEEP_WEEKLY" <<'PY'
import datetime
import re
import sys
from pathlib import Path

directory, keep_daily, keep_weekly = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
pattern = re.compile(r"^testflow-(\d{8})-(\d{6})\.dump$")

files = []
for path in directory.iterdir():
    match = pattern.match(path.name)
    if match:
        try:
            created = datetime.datetime.strptime(
                match.group(1) + match.group(2), "%Y%m%d%H%M%S"
            ).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
        files.append((created, path))

files.sort(reverse=True)
keep = {path for _, path in files[:keep_daily]}

# 周备：每个 ISO 周保留最新的一份，最多 4 个不同的周。
weeks: dict[tuple[int, int], Path] = {}
for created, path in files:
    iso = created.isocalendar()
    weeks.setdefault((iso.year, iso.week), path)
keep |= set(list(weeks.values())[:keep_weekly])

removed = 0
for _, path in files:
    if path not in keep:
        path.unlink()
        removed += 1
        print(f"    - 删除 {path.name}")

print(f"    保留 {len(keep)} 份，删除 {removed} 份")
PY
  ok "清理完成"
}

# ------------------------------------------------------------------ 入口

case "${1:-}" in
  backup) shift; cmd_backup "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  drill)  shift; cmd_drill "$@" ;;
  prune)  shift; cmd_prune "$@" ;;
  *)
    cat <<EOF
用法：$0 <命令>

  backup [目录]       备份 + 立即校验（默认目录：$BACKUP_DIR）
  verify <文件>       校验一个备份文件的归档目录
  drill  [文件]       备份/校验 → 恢复到临时库 → 对比行数 → 清理
  prune  [目录]       日备 $KEEP_DAILY 份 + 周备 $KEEP_WEEKLY 份

环境变量：
  TESTFLOW_BACKUP_DIR   备份目录（默认 ./backups）
EOF
    exit 2
    ;;
esac
