#!/bin/bash
# lm-ask.sh — dual-model sync M1+M2, 4 slots parallèles par défaut
# Usage : lm-ask.sh "question" [--reason] [--big] [--model ID] [--max N] [--seq]
# Mode : 4 appels parallèles (M1×2 + M2×2), premier non-vide gagne
# --reason  → deepseek-r1 sur M1+M2
# --big     → qwen3.5-35b sur M2
# --seq     → séquentiel M1→M2 (fallback)

set -e

MODEL_FAST="qwen/qwen3.5-9b"
MODEL_R1="deepseek/deepseek-r1-0528-qwen3-8b"
MODEL_BIG="qwen/qwen3.5-35b-a3b"
MODEL_CLOUD="deepseek-v3.1:671b"
MODEL_CLOUD_FAST="qwen3-next:80b"
OLLAMA_CLOUD="https://api.ollama.com"
OLLAMA_CLOUD_KEY="${OLLAMA_API_KEY:-5ed25086692848798d4322d4c9861d57.-KFfHj-wHbNma_PyCrWdr9hQ}"
MAX=3000
MODE="dual"   # dual | seq | single
SYS="Tu es un assistant concis. Réponds directement en français, sans préambule."

MODELS_M1=("$MODEL_FAST" "$MODEL_R1")
MODELS_M2=("$MODEL_FAST" "$MODEL_R1")

while [[ "$1" == --* ]]; do
  case "$1" in
    --model)  MODELS_M1=("$2"); MODELS_M2=("$2"); shift 2 ;;
    --max)    MAX="$2"; shift 2 ;;
    --reason) MODELS_M1=("$MODEL_R1"); MODELS_M2=("$MODEL_R1"); shift ;;
    --big)    MODELS_M1=("$MODEL_FAST"); MODELS_M2=("$MODEL_BIG"); shift ;;
    --fast)   MODELS_M1=("$MODEL_FAST"); MODELS_M2=("$MODEL_FAST"); shift ;;
    --seq)    MODE="seq"; shift ;;
    --cloud)  MODELS_M1=("$MODEL_CLOUD_FAST"); MODELS_M2=("$MODEL_CLOUD_FAST"); MODE="cloud"; shift ;;
    --cloud-big) MODELS_M1=("$MODEL_CLOUD"); MODELS_M2=("$MODEL_CLOUD"); MODE="cloud"; shift ;;
    *) shift ;;
  esac
done

PROMPT="$*"
[[ -t 0 ]] || PROMPT="$(cat)
$PROMPT"
[[ -z "$PROMPT" ]] && { echo "Usage: lm-ask.sh \"question\"" >&2; exit 1; }

# Guard VRAM (enforce silencieux si critique >85%)
python3 ~/IA/Core/jarvis/scripts/lm_guard.py check >/dev/null 2>&1 || true
[[ $? -eq 2 ]] && python3 ~/IA/Core/jarvis/scripts/lm_guard.py enforce >/dev/null 2>&1 || true

M1="http://127.0.0.1:1234"
M2="http://192.168.1.26:1234"
CB_FILE="/tmp/lm-circuit-breaker.json"
CB_THRESHOLD=3
CB_TIMEOUT=300

# Init circuit breaker state file if missing
cb_init() {
  [[ -f "$CB_FILE" ]] && return
  echo '{"M1":{"failures":0,"until":0},"M2":{"failures":0,"until":0},"OL1":{"failures":0,"until":0}}' > "$CB_FILE"
}

# Returns 0 if node is OPEN (should skip), 1 if CLOSED (ok to use)
cb_check() {
  local node="$1"
  cb_init
  local until
  until=$(jq -r --arg n "$node" '.[$n].until // 0' "$CB_FILE" 2>/dev/null || echo 0)
  local now; now=$(date +%s)
  [[ "$until" -gt "$now" ]] && return 0  # circuit open → skip
  return 1  # circuit closed → ok
}

cb_record_failure() {
  local node="$1"
  cb_init
  local failures now until
  failures=$(jq -r --arg n "$node" '.[$n].failures // 0' "$CB_FILE" 2>/dev/null || echo 0)
  failures=$((failures + 1))
  now=$(date +%s)
  if [[ "$failures" -ge "$CB_THRESHOLD" ]]; then
    until=$((now + CB_TIMEOUT))
    jq --arg n "$node" --argjson f "$failures" --argjson u "$until" \
      '.[$n].failures = $f | .[$n].until = $u' "$CB_FILE" > "${CB_FILE}.tmp" && mv "${CB_FILE}.tmp" "$CB_FILE"
  else
    jq --arg n "$node" --argjson f "$failures" \
      '.[$n].failures = $f' "$CB_FILE" > "${CB_FILE}.tmp" && mv "${CB_FILE}.tmp" "$CB_FILE"
  fi
}

cb_record_success() {
  local node="$1"
  cb_init
  jq --arg n "$node" '.[$n].failures = 0 | .[$n].until = 0' "$CB_FILE" > "${CB_FILE}.tmp" && mv "${CB_FILE}.tmp" "$CB_FILE"
}

call_model() {
  local host="$1" model="$2"
  local payload result
  payload="$(jq -nc --arg m "$model" --arg s "$SYS" --arg p "$PROMPT" --argjson n "$MAX" \
    '{model:$m,messages:[{role:"system",content:$s},{role:"user",content:$p}],max_tokens:$n,temperature:0.2,chat_template_kwargs:{enable_thinking:false}}')"
  result=$(curl -s -m 120 "$host/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    | jq -r '(.choices[0].message.content // .choices[0].message.reasoning_content // empty)' 2>/dev/null)
  echo "$result"
}

# call_model with circuit-breaker tracking
call_model_cb() {
  local node="$1" host="$2" model="$3"
  if [[ "$MODE" != "cloud" ]] && cb_check "$node"; then
    return 1  # circuit open, skip
  fi
  local result
  result=$(call_model "$host" "$model")
  if [[ -n "$result" ]]; then
    [[ "$MODE" != "cloud" ]] && cb_record_success "$node"
    echo "$result"
    return 0
  else
    [[ "$MODE" != "cloud" ]] && cb_record_failure "$node"
    return 1
  fi
}

call_ollama_cloud() {
  local model="${1:-$MODEL_CLOUD_FAST}"
  curl -s -m 180 "https://ollama.com/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $OLLAMA_CLOUD_KEY" \
    -d "$(jq -nc --arg m "$model" --arg s "$SYS" --arg p "$PROMPT" --argjson n "$MAX" \
      '{model:$m,messages:[{role:"system",content:$s},{role:"user",content:$p}],max_tokens:$n,temperature:0.2}')" \
    | jq -r '(.choices[0].message.content // empty)' 2>/dev/null
}

call_ollama() {
  curl -s -m 60 http://127.0.0.1:11434/api/generate \
    -d "$(jq -nc --arg p "$SYS\n\n$PROMPT" '{model:"gemma3:4b",prompt:$p,stream:false}')" \
    | jq -r '.response // empty' 2>/dev/null
}

# Vérif connectivité (1s timeout)
M1_UP=0; M2_UP=0; OL1_UP=0
curl -s -m 1 "$M1/v1/models" >/dev/null 2>&1 && M1_UP=1
curl -s -m 1 "$M2/v1/models" >/dev/null 2>&1 && M2_UP=1
curl -s -m 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && OL1_UP=1

[[ "$MODE" != "cloud" && $M1_UP -eq 0 && $M2_UP -eq 0 && $OL1_UP -eq 0 ]] && { echo "✘ Aucun backend dispo" >&2; exit 2; }

if [[ "$MODE" == "cloud" ]]; then
  # === MODE CLOUD : Ollama Cloud direct ===
  for m in "${MODELS_M1[@]}"; do
    R="$(call_ollama_cloud "$m")"
    [[ -n "$R" ]] && echo "$R" && exit 0
  done
  echo "✘ Ollama Cloud failed" >&2; exit 2

elif [[ "$MODE" == "dual" ]]; then
  # === MODE DUAL : M1×2 + M2×2 en parallèle — premier gagne ===
  TMPF="$(mktemp)"
  PIDS=()

  [[ $M1_UP -eq 1 ]] && for m in "${MODELS_M1[@]}"; do
    ( R="$(call_model_cb "M1" "$M1" "$m")"; [[ -n "$R" ]] && echo "$R" > "$TMPF" ) &
    PIDS+=($!)
  done

  [[ $M2_UP -eq 1 ]] && for m in "${MODELS_M2[@]}"; do
    ( R="$(call_model_cb "M2" "$M2" "$m")"; [[ -n "$R" ]] && echo "$R" > "$TMPF" ) &
    PIDS+=($!)
  done

  [[ $OL1_UP -eq 1 && ${#PIDS[@]} -eq 0 ]] && ! cb_check "OL1" && {
    ( R="$(call_ollama)"; if [[ -n "$R" ]]; then cb_record_success "OL1"; echo "$R" > "$TMPF"; else cb_record_failure "OL1"; fi ) &
    PIDS+=($!)
  }

  # Poll max 120s — premier résultat non-vide gagne
  for i in $(seq 1 240); do
    sleep 0.5
    if [[ -s "$TMPF" ]]; then
      cat "$TMPF"; rm -f "$TMPF"
      kill "${PIDS[@]}" 2>/dev/null
      exit 0
    fi
  done
  rm -f "$TMPF"; kill "${PIDS[@]}" 2>/dev/null
  echo "✘ Timeout dual-mode" >&2; exit 2

else
  # === MODE SEQ : M1 → M2 → OL1 ===
  for m in "${MODELS_M1[@]}"; do
    [[ $M1_UP -eq 1 ]] && { R="$(call_model_cb "M1" "$M1" "$m")"; [[ -n "$R" ]] && echo "$R" && exit 0; }
  done
  for m in "${MODELS_M2[@]}"; do
    [[ $M2_UP -eq 1 ]] && { R="$(call_model_cb "M2" "$M2" "$m")"; [[ -n "$R" ]] && echo "$R" && exit 0; }
  done
  [[ $OL1_UP -eq 1 ]] && ! cb_check "OL1" && { R="$(call_ollama)"; if [[ -n "$R" ]]; then cb_record_success "OL1"; echo "$R"; exit 0; else cb_record_failure "OL1"; fi; }
  echo "✘ Tous backends ont échoué" >&2; exit 2
fi

# === OPENROUTER CLOUD FALLBACK ===
call_openrouter() {
  local key="${OPENROUTER_API_KEY}"
  [[ -z "$key" ]] && source /home/turbo/IA/Core/jarvis/config/secrets.env 2>/dev/null
  key="${OPENROUTER_API_KEY}"
  [[ -z "$key" ]] && return 1
  curl -s -m 60 "https://openrouter.ai/api/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $key" \
    -H "HTTP-Referer: https://jarvis.local" \
    -d "$(jq -nc --arg s "$SYS" --arg p "$PROMPT" --argjson n "$MAX" \
      '{model:"openrouter/free",messages:[{role:"system",content:$s},{role:"user",content:$p}],max_tokens:$n}')" \
    | jq -r '(.choices[0].message.content // empty)' 2>/dev/null
}
