#!/usr/bin/env python3
"""
JARVIS Unified Launcher — App web autonome sur :8765
Consolide : Dashboard, GPU, Cluster, Agents, Lumen, Voice, Trading
"""

import subprocess
import threading
import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime

# Cache des métriques (évite 20s de curl séquentiels par requête)
_CACHE = {"data": None, "ts": 0}
_CACHE_TTL = 12  # secondes

PORT = 8765
LUMEN_DIR = "/home/turbo/IA/Research/lumen-transcription-multilangue"


def _get_gpu_info():
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        gpus = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append(
                    {
                        "id": parts[0],
                        "name": parts[1][:20],
                        "temp": parts[2],
                        "mem_used": parts[3],
                        "mem_total": parts[4],
                        "util": parts[5],
                    }
                )
        return gpus
    except Exception:
        return []


def _get_services():
    services = [
        ("Dashboard", "http://127.0.0.1:8765"),
        ("n8n", "http://127.0.0.1:5678"),
        ("Pipeline", "http://127.0.0.1:9742"),
        ("Lumen Token", "http://127.0.0.1:8788"),
        ("Lumen App", "http://127.0.0.1:4173"),
        ("River Hunter", "http://192.168.1.85:5555"),
        ("LM Studio M1", "http://127.0.0.1:1234"),
        ("LM Studio M2", "http://192.168.1.26:1234"),
        ("Ollama OL1", "http://127.0.0.1:11434"),
        ("OpenClaw GW", "http://127.0.0.1:18789"),
    ]
    results = [None] * len(services)

    def check(i, name, url):
        try:
            r = subprocess.run(
                [
                    "curl",
                    "-s",
                    "--max-time",
                    "1",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            code = r.stdout.strip()
            results[i] = {
                "name": name,
                "url": url,
                "up": bool(code and code != "000"),
                "code": code,
            }
        except Exception:
            results[i] = {"name": name, "url": url, "up": False, "code": "err"}

    threads = [
        threading.Thread(target=check, args=(i, n, u))
        for i, (n, u) in enumerate(services)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)
    return [r for r in results if r]


def _get_docker():
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        containers = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            containers.append(
                {
                    "name": parts[0] if parts else "?",
                    "status": parts[1] if len(parts) > 1 else "?",
                    "ports": parts[2] if len(parts) > 2 else "",
                }
            )
        return containers
    except Exception:
        return []


def _start_lumen_token():
    """Démarre le token server Lumen si absent."""
    try:
        r = subprocess.run(["pgrep", "-f", "token-server.mjs"], capture_output=True)
        if r.returncode != 0:
            log = open("/tmp/lumen-token.log", "a")
            subprocess.Popen(
                ["node", "server/token-server.mjs"],
                cwd=LUMEN_DIR,
                stdout=log,
                stderr=log,
            )
    except Exception:
        pass


def render_chat():
    return """<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"><title>JARVIS Chat</title>
<style>
body{margin:0;background:#0a0a0a;color:#00ff88;font-family:monospace;display:flex;flex-direction:column;height:100vh}
#header{padding:10px 20px;background:#111;border-bottom:1px solid #00ff8844;display:flex;align-items:center;gap:15px}
#header h1{margin:0;font-size:1.1em;color:#00ff88}
#agent-select{background:#1a1a1a;color:#00ff88;border:1px solid #00ff8844;padding:5px 10px;border-radius:4px}
#main{display:flex;flex:1;overflow:hidden}
#chat-area{flex:1;display:flex;flex-direction:column;padding:10px}
#messages{flex:1;overflow-y:auto;padding:10px;background:#0d0d0d;border-radius:8px;margin-bottom:10px}
.msg{margin-bottom:10px;padding:8px 12px;border-radius:6px;white-space:pre-wrap;word-break:break-word}
.msg.user{background:#1a2a1a;border-left:3px solid #00ff88;text-align:right}
.msg.agent{background:#1a1a2a;border-left:3px solid #4488ff}
.msg .label{font-size:0.75em;opacity:0.6;margin-bottom:4px}
#input-row{display:flex;gap:8px}
#msg-input{flex:1;background:#1a1a1a;color:#00ff88;border:1px solid #00ff8844;padding:10px;border-radius:6px;font-family:monospace;font-size:0.9em;resize:none}
#send-btn{background:#00ff8822;color:#00ff88;border:1px solid #00ff8844;padding:10px 20px;border-radius:6px;cursor:pointer;font-family:monospace}
#screenshot-panel{width:320px;background:#0d0d0d;border-left:1px solid #00ff8844;display:flex;flex-direction:column;padding:10px;gap:8px}
#screenshot-panel h3{margin:0 0 5px;font-size:0.9em;color:#888}
#screenshot-img{width:100%;border-radius:6px;border:1px solid #00ff8822;cursor:pointer}
.action-btn{background:#1a1a1a;color:#888;border:1px solid #333;padding:6px;border-radius:4px;cursor:pointer;font-family:monospace;font-size:0.8em;width:100%;margin-top:4px}
.action-btn:hover{color:#00ff88;border-color:#00ff8844}
</style>
</head>
<body>
<div id="header">
  <h1>JARVIS OpenClaw</h1>
  <select id="agent-select">
    <option value="main">main</option>
    <option value="openclaw-master">openclaw-master</option>
    <option value="omega-dev-agent">omega-dev-agent</option>
    <option value="omega-analysis-agent">omega-analysis-agent</option>
    <option value="omega-system-agent">omega-system-agent</option>
    <option value="omega-trading-agent">omega-trading-agent</option>
    <option value="jarvis-cluster-health">jarvis-cluster-health</option>
    <option value="jarvis-gpu-manager">jarvis-gpu-manager</option>
  </select>
  <span id="status" style="font-size:0.75em;color:#666">pret</span>
  <a href="/" style="color:#888;text-decoration:none;margin-left:auto;font-size:0.85em">dashboard</a>
</div>
<div id="main">
  <div id="chat-area">
    <div id="messages"></div>
    <div id="input-row">
      <textarea id="msg-input" rows="3" placeholder="Ctrl+Enter pour envoyer"></textarea>
      <button id="send-btn" onclick="sendMsg()">Envoyer</button>
    </div>
  </div>
  <div id="screenshot-panel">
    <h3>Ecran M1</h3>
    <img id="screenshot-img" src="/api/screenshot" alt="screenshot" onclick="refreshScreen()" style="width:100%;border-radius:6px;cursor:pointer">
    <button class="action-btn" onclick="refreshScreen()">Actualiser</button>
    <h3 style="margin-top:12px">Lanceur</h3>
    <button class="action-btn" onclick="launchDG()">DiskGenius</button>
  </div>
</div>
<script>
const msgs = document.getElementById('messages');
function addMsg(role, label, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  const lbl = document.createElement('div');
  lbl.className = 'label';
  lbl.textContent = label;
  const body = document.createElement('div');
  body.textContent = text;
  d.appendChild(lbl);
  d.appendChild(body);
  msgs.appendChild(d);
  msgs.scrollTop = msgs.scrollHeight;
}
addMsg('agent', 'JARVIS', 'Connecte a OpenClaw. Selectionnez un agent et envoyez un message.');
async function sendMsg() {
  const input = document.getElementById('msg-input');
  const agent = document.getElementById('agent-select').value;
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  addMsg('user', 'Vous', msg);
  document.getElementById('status').textContent = 'envoi...';
  try {
    const r = await fetch('/api/openclaw', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({agent: agent, message: msg})
    });
    const data = await r.json();
    addMsg('agent', agent, data.response || data.error || 'no response');
    document.getElementById('status').textContent = 'ok';
  } catch(e) {
    addMsg('agent', 'error', e.message);
    document.getElementById('status').textContent = 'erreur';
  }
}
function refreshScreen() {
  document.getElementById('screenshot-img').src = '/api/screenshot?t=' + Date.now();
}
async function launchDG() {
  await fetch('/start/diskgenius');
  document.getElementById('status').textContent = 'DiskGenius lance';
}
document.getElementById('msg-input').addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.key === 'Enter') sendMsg();
});
setInterval(refreshScreen, 30000);
</script>
</body>
</html>"""


def render_html(gpus, services, containers):
    now = datetime.now().strftime("%H:%M:%S")
    date = datetime.now().strftime("%d %b %Y")

    # GPU cards
    gpu_html = ""
    for g in gpus:
        temp = int(g["temp"]) if g["temp"].isdigit() else 0
        pct = (
            round(int(g["mem_used"]) / max(int(g["mem_total"]), 1) * 100)
            if g["mem_used"].isdigit() and g["mem_total"].isdigit()
            else 0
        )
        util = int(g["util"]) if g["util"].isdigit() else 0
        t_cls = "ok" if temp < 70 else ("warn" if temp < 85 else "danger")
        u_cls = "ok" if util < 60 else ("warn" if util < 90 else "danger")
        gpu_html += f"""
        <div class="gpu-tile">
          <div class="gpu-header">
            <span class="gpu-id">GPU {g["id"]}</span>
            <span class="badge {t_cls}">{g["temp"]}°C</span>
          </div>
          <div class="gpu-name">{g["name"]}</div>
          <div class="metric-row">
            <span class="metric-label">VRAM</span>
            <div class="bar-track"><div class="bar-fill {t_cls}" style="width:{pct}%"></div></div>
            <span class="metric-val">{pct}%</span>
          </div>
          <div class="metric-row">
            <span class="metric-label">GPU</span>
            <div class="bar-track"><div class="bar-fill {u_cls}" style="width:{util}%"></div></div>
            <span class="metric-val">{util}%</span>
          </div>
          <div class="gpu-mem">{g["mem_used"]} / {g["mem_total"]} MB</div>
        </div>"""

    if not gpu_html:
        gpu_html = '<div class="empty-state">nvidia-smi indisponible</div>'

    # Services
    svc_html = ""
    for s in services:
        cls = "up" if s["up"] else "down"
        label = "UP" if s["up"] else "DOWN"
        svc_html += f"""
        <a href="{s["url"]}" target="_blank" class="svc-row {cls}">
          <span class="svc-dot"></span>
          <span class="svc-name">{s["name"]}</span>
          <span class="svc-badge">{label}</span>
        </a>"""

    # Containers
    ctr_html = ""
    for c in containers:
        up = "Up" in c["status"]
        cls = "up" if up else "down"
        ports = c["ports"].split(",")[0].strip()[:30] if c["ports"] else ""
        ctr_html += f"""
        <div class="svc-row {cls}">
          <span class="svc-dot"></span>
          <span class="svc-name">{c["name"]}</span>
          <span class="svc-badge" style="opacity:.6;font-size:10px">{ports}</span>
        </div>"""
    if not ctr_html:
        ctr_html = '<div class="empty-state">Aucun conteneur actif</div>'

    # Stats rapides
    up_count = sum(1 for s in services if s["up"])
    total_count = len(services)
    gpu_avg_temp = round(
        sum(int(g["temp"]) for g in gpus if g["temp"].isdigit()) / max(len(gpus), 1)
    )

    # Apps
    apps = [
        ("󰊤", "Claude", "http://127.0.0.1:8765"),
        ("⚙", "n8n", "http://127.0.0.1:5678"),
        ("🎙", "Lumen", "http://127.0.0.1:4173"),
        ("📈", "Trading", "http://192.168.1.85:5555"),
        ("🧠", "LM Studio", "http://127.0.0.1:1234"),
        ("📋", "Pipeline", "http://127.0.0.1:9742"),
    ]
    apps_html = "".join(
        f'<a href="{url}" target="_blank" class="app-tile"><span class="app-icon">{ico}</span><span class="app-label">{name}</span></a>'
        for ico, name, url in apps
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS OS</title>
<meta http-equiv="refresh" content="15">
<style>
:root {{
  --bg:       #080b10;
  --surface:  #0e1420;
  --surface2: #131929;
  --border:   #1e2d45;
  --text:     #cdd6f4;
  --muted:    #6c7b96;
  --blue:     #89b4fa;
  --green:    #a6e3a1;
  --yellow:   #f9e2af;
  --red:      #f38ba8;
  --cyan:     #89dceb;
  --purple:   #cba6f7;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ height: 100%; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  font-size: 13px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}}

/* ── Header ── */
.header {{
  background: linear-gradient(135deg, #0f1e36 0%, #0a1628 100%);
  border-bottom: 1px solid var(--border);
  padding: 18px 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}}
.logo {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.logo-icon {{
  width: 38px; height: 38px;
  background: linear-gradient(135deg, #3b82f6, #8b5cf6);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px;
}}
.logo-text {{ font-size: 22px; font-weight: 700; color: var(--blue); letter-spacing: -0.5px; }}
.logo-sub {{ font-size: 11px; color: var(--muted); margin-top: 1px; }}
.header-stats {{
  display: flex; gap: 24px;
}}
.stat-pill {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 6px 14px;
  display: flex; align-items: center; gap: 6px;
  font-size: 12px;
}}
.stat-val {{ color: var(--blue); font-weight: 600; }}
.stat-lbl {{ color: var(--muted); }}

/* ── Main ── */
.main {{
  flex: 1;
  padding: 20px 28px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}}

/* ── App bar ── */
.app-bar {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}}
.app-tile {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 18px;
  display: flex; align-items: center; gap: 8px;
  text-decoration: none;
  color: var(--text);
  transition: all 0.15s;
  cursor: pointer;
}}
.app-tile:hover {{
  background: var(--surface2);
  border-color: var(--blue);
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(137,180,250,.12);
}}
.app-icon {{ font-size: 16px; }}
.app-label {{ font-size: 12px; font-weight: 500; }}

/* ── Grid ── */
.grid {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 16px;
}}
@media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
@media (max-width: 600px) {{ .grid {{ grid-template-columns: 1fr; }} }}

/* ── Panel ── */
.panel {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
}}
.panel-header {{
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--muted);
  background: var(--surface2);
}}
.panel-header .ph-icon {{ font-size: 14px; }}
.panel-body {{ padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }}

/* ── GPU tiles ── */
.gpu-tile {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}}
.gpu-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }}
.gpu-id {{ font-weight: 600; color: var(--blue); font-size: 12px; }}
.gpu-name {{ color: var(--muted); font-size: 10px; margin-bottom: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.gpu-mem {{ font-size: 10px; color: var(--muted); margin-top: 6px; text-align: right; }}
.metric-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
.metric-label {{ width: 34px; font-size: 10px; color: var(--muted); }}
.metric-val {{ width: 32px; font-size: 10px; color: var(--muted); text-align: right; }}
.bar-track {{ flex: 1; height: 5px; background: rgba(255,255,255,.06); border-radius: 3px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 3px; transition: width .4s ease; }}
.bar-fill.ok {{ background: linear-gradient(90deg, #a6e3a1, #94e2d5); }}
.bar-fill.warn {{ background: linear-gradient(90deg, #f9e2af, #fab387); }}
.bar-fill.danger {{ background: linear-gradient(90deg, #f38ba8, #eba0ac); }}

/* ── Badge ── */
.badge {{
  font-size: 10px; font-weight: 600;
  padding: 2px 7px;
  border-radius: 5px;
}}
.badge.ok {{ background: rgba(166,227,161,.15); color: var(--green); }}
.badge.warn {{ background: rgba(249,226,175,.15); color: var(--yellow); }}
.badge.danger {{ background: rgba(243,139,168,.15); color: var(--red); }}

/* ── Service rows ── */
.svc-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  text-decoration: none;
  color: var(--text);
  transition: background .12s;
}}
.svc-row:hover {{ background: var(--surface2); }}
.svc-dot {{
  width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
}}
.svc-row.up .svc-dot {{ background: var(--green); box-shadow: 0 0 6px rgba(166,227,161,.5); }}
.svc-row.down .svc-dot {{ background: var(--red); }}
.svc-name {{ flex: 1; font-size: 12px; }}
.svc-badge {{
  font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px;
}}
.svc-row.up .svc-badge {{ background: rgba(166,227,161,.12); color: var(--green); }}
.svc-row.down .svc-badge {{ background: rgba(243,139,168,.12); color: var(--red); }}

.empty-state {{ color: var(--muted); font-size: 12px; padding: 8px 0; text-align: center; }}

/* ── Footer ── */
.footer {{
  border-top: 1px solid var(--border);
  padding: 10px 28px;
  display: flex; justify-content: space-between;
  font-size: 10px;
  color: var(--muted);
}}
.footer-pulse {{
  display: flex; align-items: center; gap: 6px;
}}
.pulse-dot {{
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--green);
  animation: pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{
  0%,100% {{ opacity:1; transform:scale(1); }}
  50% {{ opacity:.4; transform:scale(.8); }}
}}
</style>
</head>
<body>

<header class="header">
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <div>
      <div class="logo-text">JARVIS OS</div>
      <div class="logo-sub">Cluster M1 · M2 · OL1</div>
    </div>
  </div>
  <div class="header-stats">
    <div class="stat-pill">
      <span class="stat-val">{up_count}/{total_count}</span>
      <span class="stat-lbl">services</span>
    </div>
    <div class="stat-pill">
      <span class="stat-val">{len(gpus)}</span>
      <span class="stat-lbl">GPUs</span>
    </div>
    <div class="stat-pill">
      <span class="stat-val">{gpu_avg_temp}°C</span>
      <span class="stat-lbl">avg temp</span>
    </div>
    <div class="stat-pill">
      <span class="stat-val">{now}</span>
      <span class="stat-lbl">{date}</span>
    </div>
  </div>
</header>

<main class="main">

  <div class="app-bar">
    {apps_html}
  </div>

  <div class="grid">

    <div class="panel">
      <div class="panel-header"><span class="ph-icon">🖥</span> GPUs</div>
      <div class="panel-body">
        {gpu_html}
      </div>
    </div>

    <div class="panel">
      <div class="panel-header"><span class="ph-icon">🔗</span> Services</div>
      <div class="panel-body" style="padding:8px">
        {svc_html}
      </div>
    </div>

    <div class="panel">
      <div class="panel-header"><span class="ph-icon">🐳</span> Conteneurs</div>
      <div class="panel-body" style="padding:8px">
        {ctr_html}
      </div>
    </div>

  </div>

</main>

<footer class="footer">
  <div class="footer-pulse">
    <span class="pulse-dot"></span>
    JARVIS Unified Launcher v3.0 · port {PORT} · refresh 15s
  </div>
  <div>JARVIS-TURBO © 2026</div>
</footer>

</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence logs

    def _get_cached(self):
        now = time.time()
        if _CACHE["data"] and (now - _CACHE["ts"]) < _CACHE_TTL:
            return _CACHE["data"]
        data = {
            "ts": datetime.now().isoformat(),
            "gpus": _get_gpu_info(),
            "services": _get_services(),
            "containers": _get_docker(),
        }
        _CACHE["data"] = data
        _CACHE["ts"] = now
        return data

    def do_GET(self):
        if self.path == "/api/status":
            data = self._get_cached()
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/screenshot":
            subprocess.run(
                ["scrot", "/tmp/jarvis_screen.png"],
                env={"DISPLAY": ":1"},
                capture_output=True,
                timeout=3,
            )
            try:
                with open("/tmp/jarvis_screen.png", "rb") as f:
                    img = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(img)
            except Exception:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/chat":
            html = render_chat().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
        elif self.path == "/start/lumen":
            _start_lumen_token()
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:4173")
            self.end_headers()
        elif self.path == "/start/diskgenius":
            subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "DISPLAY=:1 wine ~/.wine/drive_c/Program\\ Files/DiskGenius/DiskGenius.exe &",
                ]
            )
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            data = self._get_cached()
            html = render_html(
                data["gpus"], data["services"], data["containers"]
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)

    def do_POST(self):
        if self.path == "/api/openclaw":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            agent = body.get("agent", "omega-dev-agent")
            message = body.get("message", "")
            try:
                r = subprocess.run(
                    [
                        "openclaw",
                        "agent",
                        "--local",
                        "--agent",
                        agent,
                        "--message",
                        message,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                resp = {
                    "ok": True,
                    "agent": agent,
                    "response": r.stdout.strip() or r.stderr.strip(),
                }
            except Exception as e:
                resp = {"ok": False, "error": str(e)}
            out = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(out)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"[JARVIS] Unified Launcher démarré sur http://127.0.0.1:{PORT}")
    _start_lumen_token()
    # Warm up cache in background
    threading.Thread(target=Handler._get_cached, args=(object(),), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.serve_forever()
