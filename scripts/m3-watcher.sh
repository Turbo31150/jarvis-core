#!/bin/bash
# M3 watcher — vérifie si M3 (192.168.1.133) est en ligne
# Si online → active les agents M3 dans OpenClaw

M3_IP="192.168.1.133"
M3_PORT="1234"
LOG="/tmp/m3-watcher.log"

check_m3() {
    curl -s --connect-timeout 3 "http://$M3_IP:$M3_PORT/v1/models" > /dev/null 2>&1
    return $?
}

if check_m3; then
    echo "$(date) M3 ONLINE" >> "$LOG"
    # Récupérer modèles M3
    MODELS=$(curl -s "http://$M3_IP:$M3_PORT/v1/models" | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join([m['id'] for m in d.get('data',[])]))" 2>/dev/null)
    echo "$(date) M3 models: $MODELS" >> "$LOG"
    # Notifier via TTS
    bash ~/jarvis/scripts/jarvis-tts.sh "M3 est en ligne avec les modèles: $MODELS" &
else
    echo "$(date) M3 offline" >> "$LOG"
fi
