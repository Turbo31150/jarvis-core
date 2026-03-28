#!/bin/bash
# JARVIS PERPETUAL RUNNER — Never stops. Generates, improves, produces.
# Usage: ./scripts/perpetual.sh

BASE=~/IA/Core/jarvis
CLI=~/.browseros/bin/browseros-cli
LOG=$BASE/logs/perpetual.log

cycle=0
while true; do
    cycle=$((cycle + 1))
    echo "" >> $LOG
    echo "=== CYCLE $cycle — $(date) ===" >> $LOG
    
    # 1. Codeur scan
    python3 $BASE/scripts/codeur-veille.py --once >> $LOG 2>&1
    
    # 2. DB sync
    python3 $BASE/scripts/db_sync.py >> $LOG 2>&1
    
    # 3. /tmp monitor
    $BASE/scripts/tmp-monitor.sh >> $LOG 2>&1
    
    # 4. LinkedIn check (if BrowserOS available)
    if $CLI health > /dev/null 2>&1; then
        LI=$($CLI pages 2>&1 | grep -i linkedin | head -1 | grep -oP '^\s*\K\d+')
        if [ -n "$LI" ]; then
            UNREAD=$($CLI eval "Array.from(document.querySelectorAll('.nt-card')).filter(c=>c.textContent.includes('non lue')).length" -p $LI 2>/dev/null)
            echo "LinkedIn unread: $UNREAD" >> $LOG
        fi
    fi
    
    # 5. Generate content with M2 every 3rd cycle
    if [ $((cycle % 3)) -eq 0 ]; then
        python3 -c "
import requests, re, json, time
r=requests.post('http://192.168.1.26:1234/v1/chat/completions',json={
    'model':'qwen/qwen3-8b',
    'messages':[{'role':'user','content':'Reponds directement. Ecris un post LinkedIn court (300 chars) sur un sujet IA tendance. Pas de hashtag.'}],
    'temperature':0.5,'max_tokens':150},timeout=20)
c=re.sub(r'<think>.*?</think>','',r.json()['choices'][0]['message']['content'],flags=re.DOTALL).strip()
with open('$BASE/data/generated-post-'+time.strftime('%Y%m%d%H%M')+'.txt','w') as f: f.write(c)
print('Generated: '+c[:80])
" >> $LOG 2>&1
    fi
    
    # 6. Incident check
    python3 -c "
from core.workflows import incident_triage
inc = incident_triage()
if inc: print(f'INCIDENTS: {len(inc)}')
for i in inc: print(f'  [{i[\"severity\"]}] {i[\"type\"]}: {i.get(\"name\",\"\")}')
" >> $LOG 2>&1
    
    echo "Cycle $cycle complete at $(date +%H:%M:%S)" >> $LOG
    
    # Wait 15 minutes before next cycle
    sleep 900
done
