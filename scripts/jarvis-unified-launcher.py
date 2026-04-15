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


def render_html(gpus, services, containers):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # GPU blocks
    gpu_html = ""
    for g in gpus:
        temp = int(g["temp"]) if g["temp"].isdigit() else 0
        temp_color = "#26a69a" if temp < 70 else ("#f6a623" if temp < 85 else "#ef5350")
        pct = (
            round(int(g["mem_used"]) / max(int(g["mem_total"]), 1) * 100)
            if g["mem_used"].isdigit() and g["mem_total"].isdigit()
            else 0
        )
        gpu_html += f"""
        <div class="card gpu-card">
            <div class="card-title">GPU {g["id"]} — {g["name"]}</div>
            <div style="color:{temp_color}">🌡 {g["temp"]}°C | ⚡ {g["util"]}%</div>
            <div class="bar-outer"><div class="bar-inner" style="width:{pct}%;background:{temp_color}"></div></div>
            <div style="font-size:11px;color:#666">{g["mem_used"]} / {g["mem_total"]} MB</div>
        </div>"""

    # Services
    svc_html = ""
    for s in services:
        color = "#26a69a" if s["up"] else "#ef5350"
        dot = "●" if s["up"] else "○"
        link = f'<a href="{s["url"]}" target="_blank" style="color:{color}">{s["name"]}</a>'
        svc_html += f'<div class="svc-item"><span style="color:{color}">{dot}</span> {link}</div>'

    # Containers
    ctr_html = ""
    for c in containers:
        up = "Up" in c["status"]
        color = "#26a69a" if up else "#ef5350"
        ports = c["ports"][:40] if c["ports"] else ""
        ctr_html += f'<div class="svc-item"><span style="color:{color}">■</span> <b>{c["name"]}</b> <span style="color:#555">{ports}</span></div>'
    if not ctr_html:
        ctr_html = (
            '<div class="svc-item" style="color:#555">Aucun conteneur actif</div>'
        )

    # Apps rapides
    apps = [
        ("🤖 Claude Code", "terminal", "gnome-terminal -- claude"),
        ("📊 Dashboard", "link", "http://127.0.0.1:8765"),
        ("🔄 n8n Workflows", "link", "http://127.0.0.1:5678"),
        ("🎙 Lumen Transcription", "link", "http://127.0.0.1:4173"),
        ("📈 River Hunter", "link", "http://192.168.1.85:5555"),
        ("🔧 LM Studio", "link", "http://127.0.0.1:1234"),
    ]
    apps_html = ""
    for label, kind, target in apps:
        if kind == "link":
            apps_html += (
                f'<a href="{target}" target="_blank" class="app-btn">{label}</a>'
            )
        else:
            apps_html += f'<div class="app-btn" onclick="alert(\'Terminal: {target}\')">{label}</div>'

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS OS</title>
<meta http-equiv="refresh" content="15">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace; padding: 16px; min-height: 100vh; }}
  h1 {{ color: #58a6ff; font-size: 28px; margin-bottom: 4px; }}
  .subtitle {{ color: #8b949e; font-size: 12px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
  .section h2 {{ color: #58a6ff; font-size: 13px; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  .card {{ background: #1c2128; border-radius: 6px; padding: 10px; margin-bottom: 8px; }}
  .card-title {{ color: #8b949e; font-size: 11px; margin-bottom: 4px; }}
  .gpu-card {{ display: inline-block; min-width: 140px; margin: 4px; }}
  .bar-outer {{ background: #21262d; height: 6px; border-radius: 3px; margin: 4px 0; }}
  .bar-inner {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}
  .svc-item {{ padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 12px; }}
  .svc-item:last-child {{ border-bottom: none; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .apps {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }}
  .app-btn {{ background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px 16px;
              cursor: pointer; font-size: 13px; transition: background 0.2s; text-decoration: none; color: #c9d1d9; }}
  .app-btn:hover {{ background: #30363d; }}
  .ts {{ color: #484f58; font-size: 11px; margin-top: 16px; }}
</style>
</head>
<body>
<h1>⚡ JARVIS OS</h1>
<div class="subtitle">Cluster M1/M2/OL1 · {now} · Auto-refresh 15s</div>

<div class="apps">
{apps_html}
</div>

<div class="grid">
  <div class="section">
    <h2>🖥 GPUs</h2>
    {gpu_html if gpu_html else '<div class="card" style="color:#555">nvidia-smi non disponible</div>'}
  </div>

  <div class="section">
    <h2>🔗 Services</h2>
    {svc_html}
  </div>

  <div class="section">
    <h2>🐳 Conteneurs Docker</h2>
    {ctr_html}
  </div>
</div>

<div class="ts">JARVIS Unified Launcher v2.0 · Port {PORT}</div>
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
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/start/lumen":
            _start_lumen_token()
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:4173")
            self.end_headers()
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


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"[JARVIS] Unified Launcher démarré sur http://127.0.0.1:{PORT}")
    _start_lumen_token()
    # Warm up cache in background
    threading.Thread(target=Handler._get_cached, args=(object(),), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.serve_forever()
