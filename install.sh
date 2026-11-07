#!/bin/sh
# Installs (or reinstalls) the dashboard as a macOS launchd agent so it starts at login and stays
# running. Idempotent: rerun after editing server.py to restart on the new code.
# Logs: ~/Library/Logs/claude-usage-dashboard.log   Config: see README "Runtime locations".
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LABEL=com.elityre.claude-usage-dashboard
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-usage-dashboard.log"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" "$HOME/Library/Application Support/claude-usage-dashboard"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" -e "s|__LOG__|$LOG|g" "$ROOT/$LABEL.plist" > "$DEST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed. Dashboard: http://localhost:$(python3 -c 'import json,os; p=os.path.join("'"$ROOT"'","config.json"); print(json.load(open(p)).get("port",8787) if os.path.exists(p) else 8787)')"
echo "Logs: $LOG   Uninstall: ./uninstall.sh"
