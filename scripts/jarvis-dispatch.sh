#!/bin/bash
# jarvis-dispatch.sh — Route tâche vers LLM pré-chargé (0 token Claude)
# Usage: jarvis-dispatch.sh "task" [--big] [--m2]
TASK="$*"
MODEL="qwen/qwen3.5-9b"
NODE="M1"
[[ "$*" == *"--big"* ]] && MODEL="qwen/qwen3.5-35b-a3b" && TASK="${TASK/--big/}"
[[ "$*" == *"--m2"* ]]  && NODE="M2" && TASK="${TASK/--m2/}"
python3 ~/IA/Core/jarvis/scripts/tool_preloader.py --dispatch "$TASK"
