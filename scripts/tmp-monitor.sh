#!/bin/bash
# Monitor /tmp usage - alert if > 70%
USAGE=$(df /tmp --output=pcent | tail -1 | tr -d ' %')
if [ "$USAGE" -gt 70 ]; then
    echo "[WARN] /tmp at ${USAGE}% - cleaning browseros profiles..."
    # Clean BrowserOS temp profiles (not persistent ones)
    rm -rf /tmp/browseros-codex-profile 2>/dev/null
    rm -rf /tmp/browseros-profile-* 2>/dev/null
    NEW=$(df /tmp --output=pcent | tail -1 | tr -d ' %')
    echo "[OK] /tmp cleaned: ${USAGE}% -> ${NEW}%"
    logger "JARVIS tmp-monitor: cleaned /tmp from ${USAGE}% to ${NEW}%"
fi
