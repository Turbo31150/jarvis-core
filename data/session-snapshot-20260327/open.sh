#!/bin/bash
# JARVIS Quick Open — Accès rapide à tous les services
# Usage: open.sh <service>
# Example: open.sh codeur-messages, open.sh chatgpt, open.sh linkedin

declare -A URLS=(
  # Codeur.com
  [codeur]="https://www.codeur.com/users/733953/dashboard"
  [codeur-messages]="https://www.codeur.com/users/733953/messages"
  [codeur-projets]="https://www.codeur.com/projects"
  [codeur-ia]="https://www.codeur.com/projects?query=ia+python+automatisation"
  [codeur-profil]="https://www.codeur.com/-6666zlkh"
  [codeur-offres]="https://www.codeur.com/users/733953/offers"
  [codeur-login]="https://www.codeur.com/users/sign_in"
  [guillaume]="https://www.codeur.com/projects/480357/offers/6243666"
  [symfony]="https://www.codeur.com/projects/480338-developpeur-symfony-claude-code"

  # LinkedIn
  [linkedin]="https://www.linkedin.com/feed/"
  [linkedin-notif]="https://www.linkedin.com/notifications/"
  [linkedin-msg]="https://www.linkedin.com/messaging/"
  [linkedin-profil]="https://www.linkedin.com/in/franck-hlb-80bb231b1/"

  # IA Web
  [chatgpt]="https://chatgpt.com/"
  [claude]="https://claude.ai/new"
  [gemini]="https://gemini.google.com/app"
  [aistudio]="https://aistudio.google.com/prompts/new_chat"
  [perplexity]="https://www.perplexity.ai/"
  [mistral]="https://chat.mistral.ai/"
  [deepseek]="https://chat.deepseek.com/"

  # Cluster
  [m1]="http://127.0.0.1:1234/"
  [m3]="http://192.168.1.113:1234/"
  [m2]="http://192.168.1.26:1234/"
  [ollama]="http://127.0.0.1:11434/"
  [n8n]="http://127.0.0.1:5678/"
  [browseros]="http://127.0.0.1:9108/json"

  # Trading
  [mexc]="https://futures.mexc.com/exchange"
  [tradingview]="https://www.tradingview.com/chart/"

  # GitHub
  [github]="https://github.com/Turbo31150"
  [github-notif]="https://github.com/notifications"

  # API
  [qomon-api]="https://developers.qomon.app/api-docs/contacts/"
  [anthropic]="https://console.anthropic.com/"
  [openai]="https://platform.openai.com/usage"
)

if [ -z "$1" ] || [ "$1" = "help" ] || [ "$1" = "list" ]; then
  echo "JARVIS Quick Open — Services disponibles :"
  echo ""
  for key in $(echo "${!URLS[@]}" | tr ' ' '\n' | sort); do
    printf "  %-20s %s\n" "$key" "${URLS[$key]}"
  done
  exit 0
fi

URL="${URLS[$1]}"
if [ -z "$URL" ]; then
  echo "Service '$1' inconnu. Tape: open.sh list"
  exit 1
fi

# Try BrowserOS CDP first, then xdg-open
if curl -s --max-time 1 http://127.0.0.1:9108/json/version > /dev/null 2>&1; then
  python3 -c "
import asyncio, aiohttp, json
async def go():
    async with aiohttp.ClientSession() as s:
        async with s.get('http://127.0.0.1:9108/json/version') as r:
            info = await r.json()
        ws = await s.ws_connect(info['webSocketDebuggerUrl'])
        await ws.send_json({'id':1,'method':'Target.createTarget','params':{'url':'$URL'}})
        await asyncio.wait_for(ws.receive(), timeout=5)
        await ws.close()
asyncio.run(go())
  " 2>/dev/null && echo "✓ Ouvert dans BrowserOS: $1" && exit 0
fi

xdg-open "$URL" 2>/dev/null && echo "✓ Ouvert dans Chrome: $1"
