#!/bin/bash
# BrowserOS with persistent profile — keeps cookies between sessions
# Usage: ./browseros-persistent.sh [--cdp-port PORT]

CDP_PORT=${1:-9222}
MCP_PORT=${2:-8080}
PROFILE_DIR="$HOME/.config/browseros-jarvis"

mkdir -p "$PROFILE_DIR"

echo "BrowserOS Persistent Launcher"
echo "  Profile: $PROFILE_DIR"
echo "  CDP port: $CDP_PORT"
echo "  MCP port: $MCP_PORT"

# Kill any existing BrowserOS on same port
pkill -f "remote-debugging-port=$CDP_PORT" 2>/dev/null
sleep 1

# Launch with persistent profile
browseros \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-port=$CDP_PORT \
  --enable-mcp \
  --mcp-port=$MCP_PORT \
  --no-first-run \
  --remote-allow-origins=* \
  --disable-gpu-sandbox \
  --enable-logging \
  --window-size=1920,1080 \
  "$@"
