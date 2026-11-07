#!/bin/sh
LABEL=com.elityre.claude-usage-dashboard
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed launchd agent $LABEL"
