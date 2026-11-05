#!/usr/bin/env bash
# Sortie Deck 自测 runner（阶段开始时注入 workspace/scripts/）。
# 职责：一次启动服务 → 轮询健康检查 → 跑全部用例 → 写结构化结果 → 清理进程。
# 模型只读 selftest_result.json 并修失败项，不再手工 pkill / sleep / ps。
set -uo pipefail

CONFIG="${1:-selftest.config.json}"
RESULT="selftest_result.json"
SERVER_LOG="selftest_server.log"
RUN_LOG="selftest_run.log"
TIMEOUT="${SELFTEST_TIMEOUT:-120}"

if [ ! -f "$CONFIG" ]; then
  printf '{"status":"missing_config","exit_code":2,"config":"%s"}\n' "$CONFIG" > "$RESULT"
  echo "selftest.config.json 不存在：请按项目已有 e2e 入口填写 start_command / health_url / test_command" >&2
  exit 2
fi

json_get() {
  python3 - "$CONFIG" "$1" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(sys.argv[2], "")
print(value if isinstance(value, str) else "")
PY
}

START_CMD="$(json_get start_command)"
HEALTH_URL="$(json_get health_url)"
TEST_CMD="$(json_get test_command)"

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    # 后台命令在 job control 下会自成一个进程组（PID = 进程组 id）：
    # 整组一起杀，避免 npm/node 之类 fork 出的孙子进程变孤儿继续占端口。
    kill -TERM -- "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

if [ -n "$START_CMD" ]; then
  set -m
  bash -c "$START_CMD" > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  set +m
  if [ -n "$HEALTH_URL" ]; then
    ready=0
    for _ in $(seq 1 "$TIMEOUT"); do
      if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
        ready=1
        break
      fi
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if [ "$ready" -ne 1 ]; then
      printf '{"status":"start_failed","exit_code":2,"server_log":"%s"}\n' "$SERVER_LOG" > "$RESULT"
      echo "服务未在 ${TIMEOUT}s 内就绪，见 $SERVER_LOG" >&2
      exit 2
    fi
  fi
fi

if [ -z "$TEST_CMD" ]; then
  printf '{"status":"missing_test_command","exit_code":2}\n' > "$RESULT"
  exit 2
fi

bash -c "$TEST_CMD" > "$RUN_LOG" 2>&1
CODE=$?

python3 - "$CODE" "$RESULT" "$RUN_LOG" <<'PY'
import json
import sys

code = int(sys.argv[1])
json.dump(
    {
        "status": "passed" if code == 0 else "failed",
        "exit_code": code,
        "log": sys.argv[3],
        "next": "" if code == 0 else "read the log, fix only the failing cases, then re-run this script",
    },
    open(sys.argv[2], "w", encoding="utf-8"),
)
PY
exit "$CODE"
