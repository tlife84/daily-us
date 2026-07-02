#!/bin/bash
# launchd 에서 watcher 1회 폴링을 실행하는 래퍼 (Windows scripts/poll-*.ps1 대응).
# 사용법: poll-watcher.sh <watcher> <로그이름> [제한시간(초)]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WATCHER=$1
LOG="$ROOT/logs/$2.log"
LIMIT=${3:-3000}
PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$ROOT/logs"
cd "$ROOT"
ts() { date '+%Y-%m-%d %H:%M:%S %z'; }

# 집에서 푸시한 최신 코드를 받아온다. 실패해도 기존 코드로 폴링은 진행한다.
self_update() {
    local lock="$ROOT/.git-pull.lock"
    if ! mkdir "$lock" 2>/dev/null; then
        # 10분 넘은 잠금은 비정상 종료 잔재로 보고 제거 후 재시도
        local age=$(( $(date +%s) - $(stat -f %m "$lock" 2>/dev/null || echo 0) ))
        (( age > 600 )) && rmdir "$lock" 2>/dev/null
        if ! mkdir "$lock" 2>/dev/null; then
            echo "[$(ts)] git pull skipped (locked by another job)" >> "$LOG"
            return 0
        fi
    fi
    local before after changed
    before=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo none)
    if git -C "$ROOT" pull --ff-only >> "$LOG" 2>&1; then
        after=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo none)
        if [[ "$before" != "$after" ]]; then
            echo "[$(ts)] git updated ${before:0:7} -> ${after:0:7}" >> "$LOG"
            changed=$(git -C "$ROOT" diff --name-only "$before" "$after" 2>/dev/null)
            if echo "$changed" | grep -qx 'requirements.txt'; then
                echo "[$(ts)] requirements.txt changed; installing dependencies" >> "$LOG"
                "$PYTHON" -m pip install -r "$ROOT/requirements.txt" >> "$LOG" 2>&1
                "$PYTHON" -m playwright install chromium >> "$LOG" 2>&1
            fi
            if echo "$changed" | grep -qE '^(scripts/macos/install-launch-agents\.sh|config\.yaml)'; then
                echo "[$(ts)] NOTICE: schedule changed; re-run scripts/macos/install-launch-agents.sh" >> "$LOG"
            fi
        fi
    else
        echo "[$(ts)] git pull failed; running existing code" >> "$LOG"
    fi
    rmdir "$lock" 2>/dev/null
}

if [[ "${DAILY_US_SKIP_PULL:-}" != "1" ]]; then
    echo "[$(ts)] START watcher=$WATCHER" >> "$LOG"

    # 잠자기 복귀 직후 네트워크 미복구 시 최대 90초 대기
    # (Windows RunOnlyIfNetworkAvailable 대응)
    for _ in $(seq 1 18); do
        route -n get default >/dev/null 2>&1 && break
        sleep 5
    done

    self_update
    # pull 로 이 스크립트 자체가 바뀌었을 수 있으므로 최신 버전으로 재실행
    export DAILY_US_SKIP_PULL=1
    exec /bin/bash "${BASH_SOURCE[0]}" "$@"
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "[$(ts)] END watcher=$WATCHER exit=1 (python not found: $PYTHON)" >> "$LOG"
    exit 1
fi

# ExecutionTimeLimit 대응: LIMIT 초 초과 시 강제 종료
"$PYTHON" -m daily_us poll --watcher "$WATCHER" >> "$LOG" 2>&1 &
pid=$!
( sleep "$LIMIT" && kill -TERM "$pid" 2>/dev/null && sleep 10 && kill -KILL "$pid" 2>/dev/null ) &
watchdog=$!
wait "$pid"
code=$?
kill "$watchdog" 2>/dev/null
wait "$watchdog" 2>/dev/null

echo "[$(ts)] END watcher=$WATCHER exit=$code" >> "$LOG"
exit "$code"
