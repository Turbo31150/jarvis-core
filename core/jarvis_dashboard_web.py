#!/usr/bin/env python3
"""JARVIS Web Dashboard — Score /100 + métriques temps réel sur :8765"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import redis
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)

HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>JARVIS Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
body{{background:#131722;color:#d1d4dc;font-family:monospace;padding:20px}}
.score{{font-size:48px;color:{score_color};font-weight:bold}}
.bar{{background:#2a2e39;height:20px;border-radius:4px;margin:4px 0}}
.fill{{height:100%;border-radius:4px;background:{bar_color}}}
.gpu{{display:inline-block;margin:5px;padding:8px;background:#1e222d;border-radius:4px;min-width:100px}}
.ok{{color:#26a69a}}.warn{{color:#f6a623}}.err{{color:#ef5350}}
h2{{color:#9598a1;font-size:14px;margin-top:20px}}
</style></head><body>
<h1>JARVIS <span class="score">{total}/100</span></h1>
<p style="color:#9598a1">{ts}</p>
<h2>SCORE DÉTAIL</h2>
{score_bars}
<h2>GPUs</h2>
{gpu_blocks}
<h2>NODES</h2>
{node_blocks}
<h2>LLM BACKENDS</h2>
{llm_blocks}
<h2>CIRCUIT BREAKERS</h2>
{cb_status}
<h2>SERVICES</h2>
{service_blocks}
{cost_line}
<p style="color:#444;font-size:11px">Auto-refresh 10s | <a href="http://localhost:8767/workflow" style="color:#555">Workflows</a> | <a href="http://localhost:9090/metrics" style="color:#555">Prometheus</a></p>
</body></html>"""


def render():
    score = json.loads(r.get("jarvis:score") or "{}")
    total = score.get("total", 0)
    score_color = (
        "#26a69a" if total >= 90 else ("#f6a623" if total >= 70 else "#ef5350")
    )
    bar_color = score_color

    cats = [
        ("CPU Thermal", score.get("cpu_thermal", 0), f"{score.get('cpu_temp', '?')}°C"),
        ("RAM libre", score.get("ram", 0), f"{score.get('ram_free_gb', '?')} GB"),
        ("GPU temp", score.get("gpu", 0), f"max {score.get('gpu_max_temp', '?')}°C"),
        ("LLM", score.get("llm", 0), ""),
        ("Services", score.get("services", 0), ""),
    ]
    bars = ""
    for name, val, detail in cats:
        pct = val * 5
        c = "#26a69a" if val == 20 else ("#f6a623" if val >= 10 else "#ef5350")
        bars += f'<div>{name}: <b>{val}/20</b> {detail}<div class="bar"><div class="fill" style="width:{pct}%;background:{c}"></div></div></div>'

    gpus = ""
    for i in range(5):
        t = r.get(f"jarvis:gpu:{i}:temp") or "?"
        v = r.get(f"jarvis:gpu:{i}:vram_pct") or "?"
        tc = (
            "ok"
            if float(t) < 70
            else ("warn" if float(t) < 82 else "err")
            if t != "?"
            else "ok"
        )
        gpus += f'<div class="gpu"><b>GPU{i}</b><br><span class="{tc}">{t}°C</span><br>VRAM {v}%</div>'

    nodes = ""
    for n in ["M1", "M2", "M3"]:
        s = r.get(f"jarvis:node:{n}:status") or "?"
        c = "ok" if s == "up" else "err"
        nodes += f'<span class="{c}" style="margin-right:20px">{n}: {s}</span>'

    llms = ""
    for key in sorted(r.scan_iter("jarvis:llm:m*")):
        d = json.loads(r.get(key) or "{}")
        c = "ok" if d.get("ok") else "err"
        name = key.replace("jarvis:llm:", "")
        llms += f'<span class="{c}" style="margin-right:15px">{name}: {d.get("latency_ms", "?")}ms</span>'

    # Circuit breakers
    cb_status = ""
    for key in r.scan_iter("jarvis:cb:*"):
        svc = key.replace("jarvis:cb:", "")
        state = r.hget(key, "state") or "closed"
        c = "ok" if state == "closed" else ("warn" if state == "half_open" else "err")
        cb_status += (
            f'<span class="{c}" style="margin-right:12px">CB:{svc}={state}</span>'
        )

    # Token cost today
    today = datetime.now().strftime("%Y-%m-%d")
    cost_total = 0.0
    for key in r.scan_iter(f"jarvis:tokens:{today}:*"):
        cost_total += float(r.hget(key, "cost_usd") or 0)
    cost_line = f'<p style="color:#888;font-size:12px">💰 Tokens cost today: ${cost_total:.4f} | <a href="/api" style="color:#555">API</a></p>'

    # Service health
    import subprocess

    svc_out = (
        subprocess.run(
            [
                "systemctl",
                "is-active",
                "jarvis-api",
                "jarvis-dashboard",
                "jarvis-prometheus",
                "jarvis-hw-monitor",
                "jarvis-telegram-alert",
            ],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split("\n")
    )
    svc_names = ["api", "dashboard", "prometheus", "hw-monitor", "telegram"]
    service_blocks = ""
    for name, status in zip(svc_names, svc_out):
        c = "ok" if status == "active" else "err"
        service_blocks += (
            f'<span class="{c}" style="margin-right:12px">{name}:{status}</span>'
        )

    return HTML_TEMPLATE.format(
        total=total,
        score_color=score_color,
        bar_color=bar_color,
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        score_bars=bars,
        gpu_blocks=gpus,
        node_blocks=nodes,
        llm_blocks=llms,
        cb_status=cb_status,
        service_blocks=service_blocks,
        cost_line=cost_line,
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api":
            body = (r.get("jarvis:score") or "{}").encode()
            ct = "application/json"
        else:
            body = render().encode()
            ct = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    srv = HTTPServer(("0.0.0.0", 8765), Handler)
    print("JARVIS Dashboard → http://localhost:8765")
    srv.serve_forever()
