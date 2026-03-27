# JARVIS-COMET — Guide de Reproduction Complète

## Prérequis
- Ubuntu/Debian Linux avec GPU NVIDIA
- BrowserOS installé
- LM Studio sur M1/M2/M3
- Ollama sur OL1
- Node.js 22+, Python 3.12+, pip

## Architecture

```
~/IA/Core/jarvis/
├── CLAUDE.md                    — Config Claude Code
├── .mcp.json                    — MCP servers (BrowserOS, Playwright, Chrome DevTools)
├── config/
│   ├── comet-orchestrator.json  — Cluster routing config
│   ├── comet-v10.1-final.json   — Optimized routing (M2 fast, M3 deep)
│   ├── comet-system-prompt.md   — COMET system prompt
│   ├── browser-automation.env   — CDP env vars
│   ├── browseros-tab-groups.json — Tab group definitions
│   ├── mcp-browser.json         — MCP reference config
│   └── gemini-cli-browser.json  — Gemini CLI config
├── scripts/
│   ├── cdp.py                   — 33 functions CDP wrapper + auto-learning
│   ├── browser_agent.py         — Async/sync browser automation module
│   ├── comet_cluster.py         — Distributed AI query engine
│   ├── db_sync.py               — SQLite sync all data
│   ├── codeur-veille.py         — Codeur project scanner + Telegram alerts
│   ├── browseros-ensure-tabs.sh — Auto-open required tabs
│   ├── browseros-ensure-groups.sh — Auto-create tab groups
│   ├── browseros-persistent.sh  — Persistent BrowserOS launcher
│   ├── daily-workflow.sh        — Daily automated workflow
│   ├── post-login-actions.py    — Post-login automation
│   ├── tmp-monitor.sh           — /tmp space monitor
│   └── distributed-query.sh     — CLI distributed query
├── data/
│   ├── jarvis-master.db         — SQLite master database (9 tables)
│   ├── browser-nav-cache.json   — Learned navigation selectors
│   ├── browser-speed-routes.json — Speed routes for fast nav
│   ├── workflow-runs/           — All workflow run results
│   ├── screenshots/             — Browser screenshots
│   ├── blueprint/               — THIS reproduction guide
│   └── [content files]          — Proposals, posts, guides
└── logs/                        — All log files

~/.browseros/
├── SOUL.md                      — BrowserOS personality
├── memory/CORE.md               — BrowserOS memory
├── skills/                      — 17 skills (5 custom + 12 native)
│   ├── codeur-scanner/
│   ├── linkedin-engage/
│   ├── prospect-clients/
│   ├── ai-consensus/
│   └── distributed-workflow/
└── profile_permanent/           — Persistent browser profile

~/.gemini/settings.json          — Gemini CLI with BrowserOS MCP
~/.config/systemd/user/          — Systemd services
```

## Étapes de reproduction

### 1. Base system
```bash
sudo apt install python3 python3-pip nodejs npm
pip3 install websockets requests beautifulsoup4 aiohttp
```

### 2. Clone/create project
```bash
mkdir -p ~/IA/Core/jarvis/{config,scripts,data,logs}
```

### 3. Restore database
```bash
sqlite3 ~/IA/Core/jarvis/data/jarvis-master.db < blueprint/jarvis-master.sql
```

### 4. Copy all scripts
Copy everything from `scripts/` — each is self-contained.

### 5. Configure BrowserOS
```bash
cp -r skills/* ~/.browseros/skills/
cp SOUL.md ~/.browseros/
cp memory/CORE.md ~/.browseros/memory/
```

### 6. Configure MCP
Copy `.mcp.json` to project root for Claude Code.
Merge `~/.gemini/settings.json` for Gemini CLI.

### 7. Install crons
```bash
crontab -e
# Add:
*/15 * * * * ~/IA/Core/jarvis/scripts/tmp-monitor.sh
*/30 * * * * python3 scripts/codeur-veille.py --once
0 */2 * * * python3 scripts/db_sync.py
0 9 * * * ~/IA/Core/jarvis/scripts/daily-workflow.sh
@reboot sleep 20 && ~/IA/Core/jarvis/scripts/browseros-ensure-tabs.sh 9108
```

### 8. Launch BrowserOS
```bash
./scripts/browseros-persistent.sh 9108 8080
```

### 9. Verify
```bash
python3 scripts/db_sync.py           # DB sync
python3 scripts/comet_cluster.py     # Cluster health
browseros-cli health                 # BrowserOS
browseros-cli group list             # Tab groups
```
