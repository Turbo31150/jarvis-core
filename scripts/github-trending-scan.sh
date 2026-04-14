#!/usr/bin/env bash
# T26 — GitHub trending IA/LLM/agents scan + Telegram alert
set -euo pipefail

TELEGRAM_TOKEN="8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID="2010747443"
LOG_PREFIX="[github-trending-scan]"

echo "$LOG_PREFIX $(date) — start"

RAW=$(curl -s "https://api.github.com/search/repositories?q=llm+agents+local+2026&sort=stars&order=desc&per_page=5")

TOP3=$(echo "$RAW" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', [])[:3]
lines = []
for i, r in enumerate(items, 1):
    name = r.get('full_name', '?')
    stars = r.get('stargazers_count', 0)
    desc = (r.get('description') or '')[:120]
    url = r.get('html_url', '')
    lines.append(f'{i}. *{name}* — {stars}⭐\n   {desc}\n   {url}')
print('\n\n'.join(lines))
")

MSG="*GitHub Trending IA/LLM — $(date +%Y-%m-%d)*

${TOP3}"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
  -d chat_id="${CHAT_ID}" \
  -d parse_mode="Markdown" \
  --data-urlencode "text=${MSG}" > /dev/null

echo "$LOG_PREFIX done — top 3 sent to Telegram"
