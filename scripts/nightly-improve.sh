#!/usr/bin/env bash
# nightly-improve.sh — JARVIS Nightly Deep Improvement (T34)
# Cron: 0 3 * * * via .openclaw/cron/jobs.json

set -euo pipefail

TELEGRAM_TOKEN="8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
TELEGRAM_CHAT="2010747443"
LOG_FILE="$HOME/.openclaw/workspace/IMPROVE_LOG.md"
DATE=$(date '+%Y-%m-%d %H:%M')

tg_send() {
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT}" \
    -d parse_mode="Markdown" \
    -d text="$1" > /dev/null
}

echo "[nightly-improve] Démarrage $DATE"

# 1. Diag cluster
DIAG=$(bash ~/jarvis/scripts/openclaw-diag.sh 2>&1 | tail -100) || DIAG="[diag indisponible]"

# 2. Erreurs OpenClaw du jour
LOG_PATH="/tmp/openclaw/openclaw-$(date +%Y-%m-%d).log"
if [[ -f "$LOG_PATH" ]]; then
  ERRORS=$(tail -50 "$LOG_PATH" | grep ERROR || echo "[aucune erreur ERROR]")
else
  ERRORS="[log introuvable: $LOG_PATH]"
fi

# 3. Analyse LLM
CONTEXT="Voici l'état JARVIS et les erreurs récentes.

=== DIAG CLUSTER ===
${DIAG}

=== ERREURS OPENCLAW ===
${ERRORS}

Donne 3 améliorations concrètes prioritaires avec commandes bash exactes."

SUGGESTIONS=$(bash ~/jarvis/scripts/lm-ask.sh --reason "$CONTEXT" 2>&1)

# 4. Sauvegarde
mkdir -p "$(dirname "$LOG_FILE")"
cat >> "$LOG_FILE" << MDEOF

---
## $DATE

### Erreurs détectées
\`\`\`
${ERRORS}
\`\`\`

### Suggestions LLM
${SUGGESTIONS}

MDEOF

echo "[nightly-improve] Suggestions sauvegardées dans $LOG_FILE"

# 5. Telegram
SUMMARY=$(echo "$SUGGESTIONS" | head -40)
tg_send "🔧 *JARVIS Nightly Improve — $DATE*

${SUMMARY}

_Log complet: \`$LOG_FILE\`_"

echo "[nightly-improve] Rapport Telegram envoyé. Terminé."
