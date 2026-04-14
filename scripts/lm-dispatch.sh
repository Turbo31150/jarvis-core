#!/bin/bash
# lm-dispatch.sh — Chaque tâche assignée à un backend différent (M1/M2/M3/OL1) en parallèle
# Usage pipe JSON : echo '[{"task":"t1","prompt":"p1"},{"task":"t2","prompt":"p2"}]' | lm-dispatch.sh
# Sortie : une ligne JSON par tâche {"task":"t1","result":"...","backend":"M2"}

MODEL="${LM_MODEL:-qwen/qwen3.5-9b}"
MAX="${LM_MAX:-3000}"
SYS="Tu es un assistant concis. Réponds directement en français, sans préambule."

# Découverte backends
declare -a BACKEND_HOSTS=()
declare -a BACKEND_LABELS=()
for h in "192.168.1.26:1234" "192.168.1.85:1234" "192.168.1.133:1234"; do
  if curl -s -m 1 "http://$h/v1/models" >/dev/null 2>&1; then
    BACKEND_HOSTS+=("http://$h")
    [[ "$h" == *26* ]] && BACKEND_LABELS+=("M2")
    [[ "$h" == *85* ]] && BACKEND_LABELS+=("M1")
    [[ "$h" == *133* ]] && BACKEND_LABELS+=("M3")
  fi
done
if curl -s -m 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  BACKEND_HOSTS+=("ollama")
  BACKEND_LABELS+=("OL1")
fi

NB="${#BACKEND_HOSTS[@]}"
[[ "$NB" -eq 0 ]] && { echo '{"error":"aucun backend"}' >&2; exit 2; }

call_backend() {
  local host="$1" label="$2" task="$3" prompt="$4"
  local R
  if [[ "$host" == "ollama" ]]; then
    R=$(curl -s -m 90 http://127.0.0.1:11434/api/generate \
      -d "$(jq -nc --arg p "$SYS\n\n$prompt" '{model:"gemma3:4b",prompt:$p,stream:false}')" \
      | jq -r '.response // "ERR"' 2>/dev/null)
  else
    R=$(curl -s -m 90 "$host/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg m "$MODEL" --arg s "$SYS" --arg p "$prompt" --argjson n "$MAX" \
        '{model:$m,messages:[{role:"system",content:$s},{role:"user",content:$p}],max_tokens:$n,temperature:0.2}')" \
      | jq -r '.choices[0].message.content // "ERR"' 2>/dev/null)
  fi
  jq -nc --arg t "$task" --arg r "${R:-ERR}" --arg b "$label" '{task:$t,result:$r,backend:$b}'
}

# Lire tâches JSON depuis stdin
TASKS="$(cat)"
NTASKS=$(echo "$TASKS" | jq 'length' 2>/dev/null || echo 0)
[[ "$NTASKS" -eq 0 ]] && { echo '{"error":"aucune tâche"}' >&2; exit 1; }

PIDS=()
for i in $(seq 0 $((NTASKS-1))); do
  task=$(echo "$TASKS" | jq -r ".[$i].task")
  prompt=$(echo "$TASKS" | jq -r ".[$i].prompt")
  idx=$((i % NB))
  host="${BACKEND_HOSTS[$idx]}"
  label="${BACKEND_LABELS[$idx]}"
  call_backend "$host" "$label" "$task" "$prompt" &
  PIDS+=($!)
done
wait "${PIDS[@]}"
