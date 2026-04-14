#!/bin/bash
# ollama_ttl_fix.sh — Force TTL raisonnable sur tous les modèles Ollama chargés
# Évite le problème "expires: 2318" (TTL infini) qui sature le VRAM
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
models=$(curl -s "$OLLAMA_HOST/api/ps" | python3 -c "import json,sys; [print(m['name']) for m in json.load(sys.stdin).get('models',[])]" 2>/dev/null)
for model in $models; do
  expires=$(curl -s "$OLLAMA_HOST/api/ps" | python3 -c "import json,sys; [print(m.get('expires_at','?')) for m in json.load(sys.stdin).get('models',[]) if m['name']=='$model']" 2>/dev/null)
  echo "Model: $model | expires: $expires"
done
# Décharge modèles avec expire > 2100 (TTL infini)
echo "$models" | while read m; do
  [[ -z "$m" ]] && continue
  exp=$(curl -s "$OLLAMA_HOST/api/ps" | python3 -c "import json,sys,re; ms=json.load(sys.stdin).get('models',[]); m=next((x for x in ms if x['name']=='$m'),None); print(m.get('expires_at','') if m else '')" 2>/dev/null)
  year=$(echo "$exp" | grep -oE '^[0-9]{4}')
  if [[ "$year" -gt 2100 ]] 2>/dev/null; then
    echo "Unloading $m (TTL infini: $exp)"
    curl -s -X POST "$OLLAMA_HOST/api/generate" -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null
  fi
done
