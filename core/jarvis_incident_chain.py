#!/usr/bin/env python3
"""JARVIS Incident Chain — incident → diagnostic → remédiation → rapport Telegram"""

import re
import shlex
import subprocess
import json
import redis
from datetime import datetime
from jarvis_event_bus import subscribe, publish
from jarvis_telegram_alert import send_telegram as send_alert

r = redis.Redis(decode_responses=True)

CHAINS = {
    "gpu_critical": [
        (
            "diagnostic",
            "nvidia-smi --query-gpu=index,temperature.gpu,power.draw --format=csv,noheader",
        ),
        ("remediation", "python3 /home/turbo/jarvis/core/jarvis_score.py"),
        ("report", None),
    ],
    "node_down": [
        ("diagnostic", "ping -c 2 {node_ip}"),
        (
            "remediation",
            "ssh turbo@{node_ip} 'nohup lms server start --port 1234 &' 2>/dev/null || true",
        ),
        ("report", None),
    ],
    "ram_critical": [
        ("diagnostic", "ps aux --sort=-%mem | head -10"),
        ("remediation", "sync; echo 1 | sudo tee /proc/sys/vm/drop_caches"),
        ("report", None),
    ],
}


def run_chain(incident_type: str, context: dict = {}):
    chain = CHAINS.get(incident_type)
    if not chain:
        return {"error": f"no chain for {incident_type}"}

    results = []
    ts = datetime.now().isoformat()
    print(f"[{ts}] Chain: {incident_type}")

    for step_name, cmd in chain:
        if step_name == "report":
            summary = f"🔧 Incident {incident_type} traité\n"
            for r2 in results:
                summary += f"  {r2['step']}: {r2['status']}\n"
            send_alert(summary)
            results.append({"step": "report", "status": "sent", "output": summary})
            continue

        if cmd:
            # SEC-002: valider TOUTES les valeurs de context avant d'interpoler
            blocked = [
                k for k, v in context.items() if not re.fullmatch(r"[\w.\-:/]+", str(v))
            ]
            if blocked:
                results.append(
                    {
                        "step": step_name,
                        "status": "blocked",
                        "output": f"unsafe context keys: {blocked}",
                    }
                )
                continue  # saute ce step, pas juste la boucle inner
            cmd_fmt = cmd.format(**context)
            try:
                out = subprocess.run(
                    shlex.split(cmd_fmt), capture_output=True, text=True, timeout=30
                )
                results.append(
                    {
                        "step": step_name,
                        "status": "ok" if out.returncode == 0 else "err",
                        "output": out.stdout[:200] or out.stderr[:100],
                    }
                )
            except Exception as e:
                results.append(
                    {"step": step_name, "status": "exception", "output": str(e)}
                )

    publish(
        f"chain_{incident_type}_done", {"results": results}, source="incident_chain"
    )
    return results


def event_handler(event_data):
    evt = json.loads(event_data) if isinstance(event_data, str) else event_data
    etype = evt.get("type", "")
    if etype == "gpu_critical":
        run_chain("gpu_critical")
    elif etype == "node_down":
        run_chain("node_down", context={"node_ip": evt.get("data", {}).get("ip", "")})
    elif etype == "ram_critical":
        run_chain("ram_critical")


if __name__ == "__main__":
    print("Incident chains disponibles:", list(CHAINS.keys()))
    print("Mode listen: subscribe jarvis:events")
    subscribe(event_handler)
