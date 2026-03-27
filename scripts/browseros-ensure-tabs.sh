#!/bin/bash
# Ensure BrowserOS has key tabs open after startup
# Run after BrowserOS starts (via systemd or cron)

CDP_PORT=${1:-9108}
CDP="http://127.0.0.1:$CDP_PORT"

# Wait for BrowserOS to be ready
for i in $(seq 1 30); do
    if curl -s "$CDP/json/version" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Required tabs - these should always be open
REQUIRED_TABS=(
    "https://www.linkedin.com/feed/"
    "https://www.codeur.com/-6666zlkh"
    "https://chatgpt.com/"
    "https://gemini.google.com/app"
    "https://claude.ai/new"
    "https://www.perplexity.ai/"
    "https://aistudio.google.com/prompts/new_chat"
)

# Get current tabs
CURRENT=$(curl -s "$CDP/json" 2>/dev/null)

for url in "${REQUIRED_TABS[@]}"; do
    domain=$(echo "$url" | awk -F/ '{print $3}')
    # Check if a tab with this domain exists
    if ! echo "$CURRENT" | grep -qi "$domain"; then
        # Open the tab
        curl -s -X PUT "$CDP/json/new?$url" > /dev/null 2>&1
        echo "[$(date +%H:%M:%S)] Opened: $domain"
        sleep 1
    fi
done

echo "[$(date +%H:%M:%S)] All required tabs verified."
