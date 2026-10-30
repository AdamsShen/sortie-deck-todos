#!/usr/bin/env bash
# 备份 data 目录里的 sqlite 数据库与任务产物。
#
#   scripts/backup-data.sh [数据目录] [备份目录] [保留份数]
#
# 直接 cp 正在写入的 sqlite 会拿到半截文件，所以走 sqlite3 的 .backup
# （在线备份，不需要停服务）；没有 sqlite3 CLI 时退回 python3。
set -euo pipefail

DATA_DIR="${1:-./data}"
BACKUP_DIR="${2:-./data/backups}"
KEEP="${3:-7}"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/${STAMP}"
mkdir -p "${TARGET}"

backup_sqlite() {
  local src="$1"
  local dst="$2"
  [ -f "${src}" ] || return 0
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${src}" ".backup '${dst}'"
  else
    python3 - "${src}" "${dst}" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PY
  fi
  echo "  sqlite → ${dst}"
}

echo "备份 ${DATA_DIR} → ${TARGET}"
for db in initiatives.sqlite events.sqlite runs.sqlite api_keys.sqlite checkpoints.sqlite; do
  backup_sqlite "${DATA_DIR}/${db}" "${TARGET}/${db}"
done

# 任务产物 / 会话 / 记忆都是普通文件，直接打包
if [ -d "${DATA_DIR}/missions" ]; then
  tar -czf "${TARGET}/missions.tar.gz" -C "${DATA_DIR}" missions
  echo "  missions → ${TARGET}/missions.tar.gz"
fi

# 配置类小文件（用户、工具、知识库目录）
for f in users.json toolkit.json knowledge.json knowledge_bases.json agents.json; do
  [ -f "${DATA_DIR}/${f}" ] && cp "${DATA_DIR}/${f}" "${TARGET}/"
done

# 只保留最近 N 份
ls -1dt "${BACKUP_DIR}"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -rf "${old}"
done

echo "完成：${TARGET}"
