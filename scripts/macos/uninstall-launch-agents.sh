#!/bin/bash
# install-launch-agents.sh 로 등록한 LaunchAgent 를 해제/삭제한다.
set -u

GUI="gui/$(id -u)"
for label in com.daily-us.good-morning com.daily-us.always-date com.daily-us.company-analysis-guide; do
    launchctl bootout "$GUI/$label" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
    echo "Removed $label"
done

echo 'daily-us launch agents removed.'
