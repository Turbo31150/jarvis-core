#!/bin/bash
# JARVIS Freelance Run — Execute after logging into Codeur + LinkedIn in BrowserOS
# Usage: ./jarvis-freelance-run.sh

CDP_PORT=${1:-9108}
SCRIPTS_DIR="$(dirname "$0")"
DATA_DIR="$SCRIPTS_DIR/../data"
LOG="$SCRIPTS_DIR/../logs/freelance-run.log"

echo "=== JARVIS Freelance Run — $(date) ===" | tee -a "$LOG"

# 1. Ensure tabs are open
echo "[1] Ensuring tabs..." | tee -a "$LOG"
"$SCRIPTS_DIR/browseros-ensure-tabs.sh" "$CDP_PORT" 2>&1 | tee -a "$LOG"

# 2. Run Codeur veille
echo "[2] Scanning Codeur projects..." | tee -a "$LOG"
cd "$SCRIPTS_DIR/.." && python3 scripts/codeur-veille.py --once --dry-run 2>&1 | tee -a "$LOG"

# 3. Run post-login actions (if logged in)
echo "[3] Post-login actions..." | tee -a "$LOG"
python3 "$SCRIPTS_DIR/post-login-actions.py" 2>&1 | tee -a "$LOG"

# 4. Check /tmp
echo "[4] Checking /tmp..." | tee -a "$LOG"
"$SCRIPTS_DIR/tmp-monitor.sh" 2>&1 | tee -a "$LOG"

echo "=== Done — $(date) ===" | tee -a "$LOG"
