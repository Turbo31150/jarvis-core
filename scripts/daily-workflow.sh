#!/bin/bash
# JARVIS Daily Automated Workflow
# Runs: morning scan + engagement + reporting
# Cron: 0 9 * * * /home/turbo/IA/Core/jarvis/scripts/daily-workflow.sh

LOG=~/IA/Core/jarvis/logs/daily-workflow.log
CLI=~/.browseros/bin/browseros-cli
SCRIPTS=~/IA/Core/jarvis/scripts
DATA=~/IA/Core/jarvis/data
TS=$(date +%Y%m%d)

echo "=== JARVIS Daily Workflow — $(date) ===" >> "$LOG"

# 1. Health check
echo "[1] Health check" >> "$LOG"
$CLI health >> "$LOG" 2>&1

# 2. Ensure tabs + groups
echo "[2] Ensure tabs" >> "$LOG"
"$SCRIPTS/browseros-ensure-tabs.sh" 9108 >> "$LOG" 2>&1

# 3. Codeur veille
echo "[3] Codeur scan" >> "$LOG"
python3 "$SCRIPTS/codeur-veille.py" --once >> "$LOG" 2>&1

# 4. Distributed query — market intelligence
echo "[4] Market intelligence" >> "$LOG"
python3 -c "
import json,time,requests,re
Q='Quels nouveaux projets IA freelance sont apparus en France cette semaine? Focus: agents autonomes, automatisation, voice AI. Donne 5 opportunites concretes.'
def clean(c):
    c=re.sub(r'<think>.*?</think>','',c,flags=re.DOTALL).strip()
    c=re.sub(r'<think>.*','',c,flags=re.DOTALL).strip()
    return c
try:
    r=requests.post('http://192.168.1.113:1234/v1/chat/completions',json={'model':'mistral/mistral-7b-instruct-v0.3','messages':[{'role':'user','content':Q}],'temperature':0.3,'max_tokens':500},timeout=20)
    c=clean(r.json()['choices'][0]['message']['content'])
    if len(c)<20: print(f'Warning: short response ({len(c)} chars)')
    with open('$DATA/daily-intelligence-$TS.txt','w') as f: f.write(c)
    print('Intelligence saved')
except Exception as e: print(f'Error: {e}')
" >> "$LOG" 2>&1

# 5. /tmp monitor
echo "[5] /tmp check" >> "$LOG"
"$SCRIPTS/tmp-monitor.sh" >> "$LOG" 2>&1

echo "=== Done — $(date) ===" >> "$LOG"

# 6. DB Sync
echo "[6] DB sync" >> "$LOG"
python3 "$SCRIPTS/db_sync.py" >> "$LOG" 2>&1
