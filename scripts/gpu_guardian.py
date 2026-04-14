#!/usr/bin/env python3
"""JARVIS GPU Guardian — auto-heal VRAM, HTTP health endpoint."""

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

CONFIG_PATH = "/home/turbo/IA/Core/jarvis/config/gpu_guardian.json"
DEFAULT_CONFIG = {
    "display_gpus": [0],
    "vram_threshold": 90,
    "alert_threshold": 80,
    "poll_interval": 10,
    "http_port": 9090,
}

_state: dict = {"gpus": [], "timestamp": ""}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**DEFAULT_CONFIG, **cfg}
    except Exception as e:
        print(f"[GPU-GUARDIAN] Config load error ({e}), using defaults", flush=True)
        return DEFAULT_CONFIG.copy()


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=10
        ).decode()
    except Exception:
        return ""


def query_gpus() -> list[dict]:
    out = run(
        [
            "nvidia-smi",
            "--query-gpu=index,temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            temp = int(parts[1])
            used = int(parts[2])
            total = int(parts[3])
            pct = round(used / total * 100, 1) if total > 0 else 0
            gpus.append(
                {
                    "index": idx,
                    "temp": temp,
                    "vram_used": used,
                    "vram_total": total,
                    "vram_pct": pct,
                    "status": "ok",
                }
            )
        except ValueError:
            continue
    return gpus


def find_top_process(gpu_idx: int) -> tuple[int | None, str]:
    """Return (pid, cmdline) of the process using most memory on gpu_idx."""
    out = run(["nvidia-smi", "pmon", "-s", "m", "-c", "1"])
    best_pid, best_mem, best_cmd = None, 0, ""
    for line in out.strip().splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        tokens = line.split()
        if len(tokens) < 4:
            continue
        try:
            g = int(tokens[0])
            pid = int(tokens[1])
            mem_str = tokens[3]
            mem = int(mem_str) if mem_str.isdigit() else 0
        except ValueError:
            continue
        if g == gpu_idx and mem > best_mem:
            best_pid, best_mem, best_cmd = (
                pid,
                mem,
                " ".join(tokens[4:]) if len(tokens) > 4 else "",
            )
    return best_pid, best_cmd


def kill_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def guardian_loop(cfg: dict) -> None:
    display_gpus: list[int] = cfg["display_gpus"]
    vram_threshold: float = cfg["vram_threshold"]
    alert_threshold: float = cfg["alert_threshold"]
    poll_interval: int = cfg["poll_interval"]

    print(
        f"[GPU-GUARDIAN] Started — poll={poll_interval}s, display_gpus={display_gpus}, "
        f"vram_threshold={vram_threshold}%, alert_threshold={alert_threshold}%",
        flush=True,
    )

    while True:
        gpus = query_gpus()
        now = datetime.utcnow().isoformat() + "Z"

        for gpu in gpus:
            idx = gpu["index"]
            pct = gpu["vram_pct"]
            temp = gpu["temp"]

            if idx in display_gpus:
                if pct > alert_threshold:
                    gpu["status"] = "alert"
                    print(
                        f"[GPU-GUARDIAN] ALERT GPU#{idx} (display) VRAM={pct}% "
                        f"temp={temp}°C — no kill (display GPU)",
                        flush=True,
                    )
            else:
                if pct > vram_threshold:
                    gpu["status"] = "killing"
                    pid, cmd = find_top_process(idx)
                    if pid:
                        ok = kill_process(pid)
                        status = "SIGTERM sent" if ok else "kill failed (permission?)"
                        print(
                            f"[GPU-GUARDIAN] VRAM={pct}% GPU#{idx} — {status} "
                            f"pid={pid} cmd='{cmd}'",
                            flush=True,
                        )
                    else:
                        print(
                            f"[GPU-GUARDIAN] VRAM={pct}% GPU#{idx} — no process found via pmon",
                            flush=True,
                        )
                elif pct > alert_threshold:
                    gpu["status"] = "warn"
                    print(
                        f"[GPU-GUARDIAN] WARN GPU#{idx} VRAM={pct}% temp={temp}°C",
                        flush=True,
                    )

        _state["gpus"] = gpus
        _state["timestamp"] = now
        time.sleep(poll_interval)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(_state, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence access logs


def main():
    cfg = load_config()
    port = cfg["http_port"]

    t = threading.Thread(target=guardian_loop, args=(cfg,), daemon=True)
    t.start()

    # Wait until first poll completes
    time.sleep(1)

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[GPU-GUARDIAN] HTTP health on :{port}/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[GPU-GUARDIAN] Shutting down", flush=True)


if __name__ == "__main__":
    main()
