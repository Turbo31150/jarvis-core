#!/usr/bin/env bash
# hf-model-watch.sh — JARVIS HuggingFace Model Watch (T35)
# Cron: 0 9 * * 0

set -euo pipefail

TELEGRAM_TOKEN="8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
TELEGRAM_CHAT="2010747443"
COMPATIBLE_ARCHS="mistral|llama|qwen|gemma"
DATE=$(date '+%Y-%m-%d')
ONE_WEEK_AGO=$(date -d '7 days ago' '+%Y-%m-%dT%H:%M:%SZ')

tg_send() {
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT}" \
    -d parse_mode="Markdown" \
    -d text="$1" > /dev/null
}

echo "[hf-model-watch] Démarrage $DATE"

# 1. Fetch modèles GGUF top downloads
RAW=$(curl -s "https://huggingface.co/api/models?sort=downloads&direction=-1&limit=50&filter=gguf&full=true" 2>&1)

if [[ -z "$RAW" || "$RAW" == "null" ]]; then
  echo "[hf-model-watch] Erreur API HuggingFace"
  exit 1
fi

# 2. Parse + filtre Python
CANDIDATES=$(python3 - << 'PYEOF'
import json, sys, os
from datetime import datetime, timezone

one_week_ago_str = os.environ.get('ONE_WEEK_AGO', '')

raw = sys.stdin.read()
try:
    models = json.loads(raw)
except:
    print("PARSE_ERROR")
    sys.exit(1)

compatible = ['mistral', 'llama', 'qwen', 'gemma']
results = []

for m in models:
    name = m.get('modelId', m.get('id', ''))
    downloads = m.get('downloads', 0)
    last_modified = m.get('lastModified', '')
    tags = [t.lower() for t in m.get('tags', [])]
    siblings = m.get('siblings', [])

    # Filtre architecture compatible
    name_lower = name.lower()
    if not any(a in name_lower for a in compatible):
        continue

    # Filtre taille < 15GB (cherche dans siblings les fichiers .gguf)
    total_size = 0
    for s in siblings:
        sz = s.get('size', 0) or 0
        if str(s.get('rfilename', '')).endswith('.gguf'):
            total_size += sz
    size_gb = total_size / (1024**3) if total_size > 0 else 0

    # Si pas de taille connue, on garde quand même (sera indiqué)
    if size_gb > 15:
        continue

    results.append({
        'name': name,
        'downloads': downloads,
        'size_gb': round(size_gb, 1),
        'last_modified': last_modified
    })

# Top 5 par downloads pour évaluation
for r in sorted(results, key=lambda x: x['downloads'], reverse=True)[:5]:
    print(f"{r['name']}|{r['size_gb']}|{r['downloads']}|{r['last_modified']}")
PYEOF
) <<< "$RAW"

if [[ "$CANDIDATES" == "PARSE_ERROR" || -z "$CANDIDATES" ]]; then
  echo "[hf-model-watch] Aucun candidat trouvé"
  tg_send "🤖 *HF Model Watch $DATE* — Aucun nouveau modèle GGUF compatible détecté."
  exit 0
fi

# 3+4. Évaluer chaque candidat via lm-ask
declare -A SCORES
REPORT=""

while IFS='|' read -r name size downloads last_mod; do
  [[ -z "$name" ]] && continue
  EVAL=$(bash ~/jarvis/scripts/lm-ask.sh "Évalue ce modèle GGUF pour un cluster LLM local: $name taille=${size}GB downloads=${downloads} dernière_maj=$last_mod. Architecture compatible LM Studio (mistral/llama/qwen/gemma). Pertinent pour agents IA autonomes? Score 0-10 avec justification courte." 2>&1)
  SCORE=$(echo "$EVAL" | grep -oP '\b([89]|10)\b' | head -1 || echo "?")
  SCORES["$name"]=$SCORE
  REPORT+="*$name* (${size}GB, score=$SCORE)\n${EVAL}\n\n"
  echo "[hf-model-watch] Évalué: $name → score=$SCORE"
done <<< "$CANDIDATES"

# 5. Top 3 sur Telegram
TOP3=$(echo "$REPORT" | head -80)
tg_send "🔭 *HF Model Watch — $DATE*

Top modèles GGUF évalués cette semaine:

${TOP3}"

echo "[hf-model-watch] Rapport Telegram envoyé. Terminé."
