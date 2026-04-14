#!/bin/bash
# JARVIS Daily Check — Exécuté à 7h, rapport Telegram
echo "[$(date '+%H:%M')] JARVIS Daily Check"

# Score système
SCORE=$(python3 /home/turbo/jarvis/core/jarvis_score.py 2>/dev/null | grep "total" | grep -oP '\d+' | head -1)

# Services down
FAILED=$(systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l)

# Redis keys
REDIS_KEYS=$(redis-cli dbsize 2>/dev/null)

# Nodes
M2=$(curl -s -m 2 http://192.168.1.26:1234/v1/models >/dev/null 2>&1 && echo "✅" || echo "❌")
OL1=$(curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo "✅" || echo "❌")
M1=$(curl -s -m 2 http://192.168.1.85:1234/v1/models >/dev/null 2>&1 && echo "✅" || echo "❌")

MSG="🌅 JARVIS Daily Check $(date '+%d/%m %H:%M')
Score: ${SCORE}/100
Services failed: ${FAILED}
Redis keys: ${REDIS_KEYS}
M1: ${M1} M2: ${M2} OL1: ${OL1}"

# Send Telegram
source /home/turbo/IA/Core/jarvis/config/secrets.env 2>/dev/null
TOKEN=${TELEGRAM_TOKEN:-""}
CHAT=${TELEGRAM_CHAT:-""}
if [[ -n "$TOKEN" && -n "$CHAT" ]]; then
  curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}&text=${MSG}" >/dev/null
fi
echo "$MSG"
