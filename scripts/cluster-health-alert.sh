#!/usr/bin/env bash
# cluster-health-alert.sh — Health check cluster LLM + alertes Telegram
# Cron: */5 * * * * bash ~/jarvis/scripts/cluster-health-alert.sh

TELEGRAM_TOKEN="8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
TELEGRAM_CHAT="2010747443"
STATE_FILE="/tmp/cluster-health-state.json"

# Nœuds à vérifier: "label|host|port|type" (type: lmstudio|ollama)
NODES=(
  "M1|192.168.1.85|1234|lmstudio"
  "M2|192.168.1.26|1234|lmstudio"
  "M3|192.168.1.133|1234|lmstudio"
  "OL1|127.0.0.1|11434|ollama"
)

send_telegram() {
  local msg="$1"
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT}&text=${msg}&parse_mode=Markdown" > /dev/null 2>&1
}

check_node() {
  local label="$1" host="$2" port="$3" type="$4"
  # Ping d'abord
  if ! ping -c 1 -W 2 "$host" > /dev/null 2>&1; then
    echo "offline"
    return
  fi
  # Check API
  if [ "$type" = "ollama" ]; then
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${host}:${port}/api/tags")
    [ "$code" = "200" ] && echo "online" || echo "offline"
  else
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${host}:${port}/v1/models")
    [ "$code" = "200" ] && echo "online" || echo "offline"
  fi
}

# Charger état précédent
if [ -f "$STATE_FILE" ]; then
  prev_state=$(cat "$STATE_FILE")
else
  prev_state="{}"
fi

new_state="{}"
now=$(date +"%Y-%m-%d %H:%M:%S")

for node_def in "${NODES[@]}"; do
  IFS='|' read -r label host port type <<< "$node_def"
  status=$(check_node "$label" "$host" "$port" "$type")
  
  # Récupérer état précédent
  prev=$(echo "$prev_state" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('${label}','unknown'))" 2>/dev/null || echo "unknown")
  
  # Détecter transition OK → offline
  if [ "$prev" = "online" ] && [ "$status" = "offline" ]; then
    msg="🚨 *JARVIS CLUSTER ALERT*%0ANoeud *${label}* (${host}:${port}) est passé *OFFLINE*%0AHeure: ${now}"
    send_telegram "$msg"
    logger -t cluster-health "ALERT: ${label} went offline"
  fi
  
  # Détecter transition offline → OK (recovery)
  if [ "$prev" = "offline" ] && [ "$status" = "online" ]; then
    msg="✅ *JARVIS CLUSTER RECOVERY*%0ANoeud *${label}* (${host}:${port}) est de nouveau *ONLINE*%0AHeure: ${now}"
    send_telegram "$msg"
    logger -t cluster-health "RECOVERY: ${label} back online"
  fi
  
  # Mettre à jour le nouvel état
  new_state=$(echo "$new_state" | python3 -c "
import sys,json
d=json.load(sys.stdin)
d['${label}']='${status}'
print(json.dumps(d))
" 2>/dev/null || echo "$new_state")

done

# Sauvegarder le nouvel état
echo "$new_state" > "$STATE_FILE"
