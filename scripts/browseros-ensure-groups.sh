#!/bin/bash
# Ensure BrowserOS has tab groups set up after startup
CLI=~/.browseros/bin/browseros-cli

# Wait for BrowserOS
for i in $(seq 1 30); do
    $CLI health > /dev/null 2>&1 && break
    sleep 1
done

# Open all required tabs
URLS=(
    "https://www.linkedin.com/feed/"
    "https://www.codeur.com/-6666zlkh"
    "https://chatgpt.com/"
    "https://gemini.google.com/app"
    "https://claude.ai/new"
    "https://www.perplexity.ai/"
    "https://aistudio.google.com/prompts/new_chat"
    "https://github.com/Turbo31150"
)

PAGES=$($CLI pages 2>/dev/null)
for url in "${URLS[@]}"; do
    domain=$(echo "$url" | awk -F/ '{print $3}')
    if ! echo "$PAGES" | grep -qi "$domain"; then
        $CLI open "$url" > /dev/null 2>&1
        sleep 1
    fi
done

sleep 3

# Get page IDs by position
PAGES=$($CLI pages 2>/dev/null)

# Create groups (idempotent — fails silently if already exists)
FREELANCE_IDS=""
IA_IDS=""
PORTFOLIO_IDS=""

n=1
while IFS= read -r line; do
    if echo "$line" | grep -qi "linkedin\|codeur"; then
        FREELANCE_IDS="$FREELANCE_IDS $n"
    elif echo "$line" | grep -qi "chatgpt\|gemini\|claude\|perplexity\|aistudio"; then
        IA_IDS="$IA_IDS $n"
    elif echo "$line" | grep -qi "github"; then
        PORTFOLIO_IDS="$PORTFOLIO_IDS $n"
    fi
    n=$((n+1))
done < <(echo "$PAGES" | grep "^\s*[0-9]")

[ -n "$FREELANCE_IDS" ] && $CLI group create --title "FREELANCE" $FREELANCE_IDS 2>/dev/null
[ -n "$IA_IDS" ] && $CLI group create --title "IA WEB" $IA_IDS 2>/dev/null
[ -n "$PORTFOLIO_IDS" ] && $CLI group create --title "PORTFOLIO" $PORTFOLIO_IDS 2>/dev/null

echo "[$(date +%H:%M:%S)] Tab groups ensured"
$CLI group list 2>/dev/null
