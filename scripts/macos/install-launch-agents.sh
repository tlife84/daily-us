#!/bin/bash
# config.yaml 의 active_hours / interval_minutes 와 일치하는 고정 시각
# (StartCalendarInterval) LaunchAgent 를 생성/등록한다.
# (Windows scripts/update-scheduled-tasks.ps1 의 macOS 대응)
#
# launchd 는 잠자기 중 놓친 시각들을 깨어날 때 1회로 합쳐 즉시 실행하므로
# (Windows StartWhenAvailable 대응), 고정 시각 트리거라도 격자가 밀리지 않는다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT/scripts/macos/poll-watcher.sh"
AGENTS_DIR="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

mkdir -p "$AGENTS_DIR" "$ROOT/logs"
chmod +x "$RUNNER"

calendar_entries() { # <시작 HH:MM> <끝 HH:MM> <간격(분)> [요일 0=일요일]
    local start=$1 end=$2 interval=$3 weekday=${4:-}
    local t=$(( 10#${start%%:*} * 60 + 10#${start##*:} ))
    local end_t=$(( 10#${end%%:*} * 60 + 10#${end##*:} ))
    while (( t <= end_t )); do
        printf '      <dict>'
        if [[ -n "$weekday" ]]; then
            printf '<key>Weekday</key><integer>%d</integer>' "$weekday"
        fi
        printf '<key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>\n' \
            $(( t / 60 )) $(( t % 60 ))
        t=$(( t + interval ))
    done
}

install_agent() { # <label> <watcher> <로그이름> <제한시간(초)> <calendar xml>
    local label=$1 watcher=$2 logname=$3 limit=$4 calendar=$5
    local plist="$AGENTS_DIR/$label.plist"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER</string>
    <string>$watcher</string>
    <string>$logname</string>
    <string>$limit</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
$calendar
  </array>
  <key>StandardOutPath</key>
  <string>$ROOT/logs/launchd-$logname.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT/logs/launchd-$logname.log</string>
</dict>
</plist>
EOF

    launchctl bootout "$GUI/$label" 2>/dev/null || true
    launchctl bootstrap "$GUI" "$plist"
    launchctl enable "$GUI/$label"
    echo "Installed $label: $(grep -c '<key>Hour</key>' "$plist") fixed trigger(s)"
}

# 시각/간격/제한시간은 config.yaml 및 Windows 작업과 일치시킨다.
install_agent 'com.daily-us.good-morning'           'good_morning_damsaem'   'good-morning'           540  "$(calendar_entries 07:00 09:10 10)"
install_agent 'com.daily-us.always-date'            'always_date'            'always-date'            3000 "$(calendar_entries 07:00 22:00 60)"
install_agent 'com.daily-us.company-analysis-guide' 'company_analysis_guide' 'company-analysis-guide' 3000 "$(calendar_entries 12:00 20:00 60 0)"

echo 'daily-us launch agents installed.'
