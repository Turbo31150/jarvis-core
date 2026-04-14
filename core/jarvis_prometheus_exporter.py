#!/usr/bin/env python3
"""JARVIS Prometheus Exporter — Expose metrics in Prometheus format on :9090/metrics"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import redis, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

def generate_metrics() -> str:
    lines = [f"# HELP jarvis_score System health score", f"# TYPE jarvis_score gauge"]
    
    # Score
    import json
    score_raw = r.get("jarvis:score")
    if score_raw:
        score = json.loads(score_raw)
        lines.append(f'jarvis_score{{}} {score.get("total", 0)}')
    
    # GPU temps
    lines.append("# HELP jarvis_gpu_temp_celsius GPU temperature")
    lines.append("# TYPE jarvis_gpu_temp_celsius gauge")
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        if temp:
            lines.append(f'jarvis_gpu_temp_celsius{{gpu="{i}"}} {temp}')
    
    # GPU VRAM
    lines.append("# HELP jarvis_gpu_vram_pct GPU VRAM usage percent")
    lines.append("# TYPE jarvis_gpu_vram_pct gauge")
    for i in range(5):
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if vram:
            lines.append(f'jarvis_gpu_vram_pct{{gpu="{i}"}} {vram}')
    
    # RAM
    lines.append("# HELP jarvis_ram_free_gb Free RAM in GB")
    lines.append("# TYPE jarvis_ram_free_gb gauge")
    ram = r.get("jarvis:ram:free_gb")
    if ram:
        lines.append(f"jarvis_ram_free_gb{{}} {ram}")
    
    # LLM router stats
    lines.append("# HELP jarvis_llm_router_calls Total LLM router calls")
    lines.append("# TYPE jarvis_llm_router_calls counter")
    for key in r.scan_iter("jarvis:llm_router:*:count"):
        task_type = key.split(":")[2]
        count = r.get(key) or "0"
        lines.append(f'jarvis_llm_router_calls{{type="{task_type}"}} {count}')
    
    # Circuit breaker states
    lines.append("# HELP jarvis_circuit_open Circuit breaker open (1=open)")
    lines.append("# TYPE jarvis_circuit_open gauge")
    for key in r.scan_iter("jarvis:cb:*"):
        svc = key.replace("jarvis:cb:", "")
        state = r.hget(key, "state") or "closed"
        lines.append(f'jarvis_circuit_open{{service="{svc}"}} {1 if state=="open" else 0}')
    
    return "\n".join(lines) + "\n"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = generate_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(302)
            self.send_header("Location", "/metrics")
            self.end_headers()
    def log_message(self, *args): pass

if __name__ == "__main__":
    # Test output
    print(generate_metrics()[:500])
    print("---")
    srv = HTTPServer(("0.0.0.0", 9090), Handler)
    print("Prometheus exporter ready on :9090/metrics")
    srv.serve_forever()
