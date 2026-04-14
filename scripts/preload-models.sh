#!/bin/bash
# Préchargement + watchdog modèles clés — M1/M2/OL1
# Cron: */5 * * * * bash ~/jarvis/scripts/preload-models.sh >> /tmp/preload-models.log 2>&1

LOGF="/tmp/preload-models.log"
TS=$(date '+%H:%M:%S')

preload_lmstudio() {
    local url=$1 model=$2 label=$3
    # Vérifier si chargé via API v0
    state=$(curl -s --connect-timeout 2 "$url/api/v0/models/$model" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('state','?'))" 2>/dev/null)
    if [ "$state" != "loaded" ]; then
        echo "$TS RELOAD $label (was: $state)"
        curl -s -X POST "$url/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":2,\"stream\":false}" \
            --connect-timeout 5 -m 60 > /dev/null 2>&1 && echo "$TS OK $label"
    fi
}

preload_ollama() {
    local model=$1
    state=$(curl -s http://127.0.0.1:11434/api/ps 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); names=[m['name'] for m in d.get('models',[])]; print('loaded' if any('$model' in n for n in names) else 'not-loaded')" 2>/dev/null)
    if [ "$state" != "loaded" ]; then
        curl -s -X POST http://127.0.0.1:11434/api/generate \
            -d "{\"model\":\"$model\",\"prompt\":\"ping\",\"stream\":false,\"keep_alive\":-1}" \
            -m 30 > /dev/null 2>&1 && echo "$TS OK OL1/$model"
    fi
}

# M1 — modèles primaires
preload_lmstudio "http://192.168.1.85:1234" "qwen%2Fqwen3.5-9b" "M1/qwen3.5-9b" &
preload_lmstudio "http://192.168.1.85:1234" "zai-org%2Fglm-4.7-flash" "M1/glm-flash" &

# M2 — workers
preload_lmstudio "http://192.168.1.26:1234" "qwen%2Fqwen3.5-9b" "M2/qwen3.5-9b" &

# OL1 — gemma3
preload_ollama "gemma3:4b" &

wait
