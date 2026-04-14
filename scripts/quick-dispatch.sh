#!/bin/bash
# quick-dispatch.sh — Dispatch rapide d'une tâche au meilleur backend
# Usage: quick-dispatch.sh "task" [--trading|--code|--analysis|--big]
# Options:
#   --trading   → M2 qwen3.5-35b (analyse financière)
#   --code      → M1 qwen2.5-coder (code generation)
#   --analysis  → M2 deepseek-r1 (raisonnement)
#   --big       → M2 qwen3.5-35b-a3b (tâches lourdes)
#   --fast      → M1 qwen3.5-9b (réponse rapide)

M1="http://127.0.0.1:1234"
M2="http://192.168.1.26:1234"
MODEL="qwen/qwen3.5-9b"
HOST="$M1"
TIMEOUT=120

# Parse flags
MODE=""
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --trading)  MODEL="qwen/qwen3.5-35b-a3b"; HOST="$M2"; TIMEOUT=180 ;;
    --code)     MODEL="qwen/qwen3.5-9b"; HOST="$M1" ;;
    --analysis) MODEL="deepseek/deepseek-r1-0528-qwen3-8b"; HOST="$M2"; TIMEOUT=180 ;;
    --big)      MODEL="qwen/qwen3.5-35b-a3b"; HOST="$M2"; TIMEOUT=180 ;;
    --fast)     MODEL="qwen/qwen3.5-9b"; HOST="$M1" ;;
    *)          ARGS+=("$arg") ;;
  esac
done

TASK="${ARGS[*]}"
[[ -z "$TASK" ]] && { echo "Usage: quick-dispatch.sh 'task' [--trading|--code|--analysis|--big|--fast]" >&2; exit 1; }

echo "[$(date +%H:%M)] Backend: ${HOST##*/}  Model: ${MODEL##*/}" >&2
echo "[$(date +%H:%M)] Task: ${TASK:0:80}..." >&2

PAYLOAD=$(python3 -c "
import json, sys
task = sys.argv[1]
model = sys.argv[2]
data = {
    'model': model,
    'messages': [
        {'role': 'system', 'content': 'Réponds directement sans préambule. Si JSON demandé, JSON uniquement.'},
        {'role': 'user', 'content': '/no_think ' + task}
    ],
    'max_tokens': 2000,
    'stream': False,
    'temperature': 0.3
}
print(json.dumps(data))
" "$TASK" "$MODEL")

curl -s --max-time "$TIMEOUT" "$HOST/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" 2>/dev/null | python3 -c "
import json, sys, re
d = json.load(sys.stdin)
m = d['choices'][0]['message']
# content = final answer, reasoning_content = think chain
out = m.get('content', '').strip()
if not out:
    # Extract final answer from reasoning_content (after last double newline)
    rc = m.get('reasoning_content', '')
    # Try to find JSON block in reasoning
    j = re.search(r'(\{[^{}]*\"biais\"[^{}]*\})', rc, re.DOTALL)
    if j:
        out = j.group(1)
    else:
        # Last non-empty paragraph
        parts = [p.strip() for p in rc.split('\n\n') if p.strip()]
        out = parts[-1] if parts else rc
out = re.sub(r'<think>.*?</think>', '', out, flags=re.DOTALL).strip()
print(out)
" 2>&1
