#!/usr/bin/env bash
# T31-T32 — Devpost hackathon scanner + LM scoring + Telegram alert
set -euo pipefail

TELEGRAM_TOKEN="8369376863:AAF-7YGDbun8mXWwqYJFj-eX6P78DeIu9Aw"
CHAT_ID="2010747443"
LOG_PREFIX="[devpost-hackathon-scan]"

echo "$LOG_PREFIX $(date) — start"

RAW=$(curl -s "https://devpost.com/api/hackathons?challenge_type=online&status=open")

# Extract top 5 hackathons
HACKATHONS=$(echo "$RAW" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('hackathons', [])[:5]
for h in items:
    name = (h.get('title') or '').replace('|', ' ')
    theme = (h.get('tagline') or '')[:80].replace('|', ' ')
    prize = str(h.get('prize_amount') or h.get('total_prizes') or '?')
    url = h.get('url') or ''
    print(f'{name}|{theme}|{prize}|{url}')
")

if [[ -z "$HACKATHONS" ]]; then
  echo "$LOG_PREFIX no hackathons found, exiting"
  exit 0
fi

declare -A SCORES
declare -A META

while IFS='|' read -r name theme prize url; do
  [[ -z "$name" ]] && continue
  PROMPT="Score 0-10 cet hackathon pour dev IA solo freelance: $name $theme $prize. Réponds: score|raison courte"
  RESULT=$(bash ~/jarvis/scripts/lm-ask.sh "$PROMPT" 2>/dev/null | tr -d '\n' | head -c 200)
  SCORE=$(echo "$RESULT" | grep -oP '^\d+' | head -1)
  [[ -z "$SCORE" ]] && SCORE=0
  REASON=$(echo "$RESULT" | sed 's/^[0-9]*|//')
  SCORES["$name"]=$SCORE
  META["$name"]="$theme|$prize|$url|$REASON"
  echo "$LOG_PREFIX scored '$name' → $SCORE"
done <<< "$HACKATHONS"

# Sort by score descending, take top 3
TOP3_MSG=""
COUNT=0
for name in $(for k in "${!SCORES[@]}"; do echo "${SCORES[$k]} $k"; done | sort -rn | head -3 | awk '{$1=""; print $0}' | sed 's/^ //'); do
  IFS='|' read -r theme prize url reason <<< "${META[$name]}"
  COUNT=$((COUNT+1))
  TOP3_MSG="${TOP3_MSG}${COUNT}. *${name}* (score: ${SCORES[$name]}/10)
   ${theme}
   Prize: ${prize}
   ${reason}
   ${url}

"
done

MSG="*Devpost Hackathons IA — $(date +%Y-%m-%d)*

${TOP3_MSG}"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
  -d chat_id="${CHAT_ID}" \
  -d parse_mode="Markdown" \
  --data-urlencode "text=${MSG}" > /dev/null

echo "$LOG_PREFIX done — top 3 scored hackathons sent to Telegram"
