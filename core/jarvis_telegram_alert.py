#!/usr/bin/env python3
"""JARVIS Telegram Alerts — Subscribe Redis events → push Telegram"""

import redis
import json
import requests
from datetime import datetime


# Load config
def load_secrets():
    env = {}
    for f in [
        "/home/turbo/IA/Core/jarvis/config/secrets.env",
        "/home/turbo/Workspaces/jarvis-linux/.env",
    ]:
        try:
            with open(f) as fp:
                for line in fp:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        env[k] = v
        except:
            pass
    return env


cfg = load_secrets()
TOKEN = cfg.get("TELEGRAM_TOKEN", "")
CHAT = cfg.get("TELEGRAM_CHAT", "")

ALERT_LEVELS = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}
IGNORED_TYPES = {"system_start", "system_ready"}


def send_telegram(msg: str) -> bool:
    if not TOKEN or not CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg},
            timeout=10,
        )
        return r.status_code == 200
    except:
        return False


def format_event(event: dict) -> str:
    etype = event.get("type", "unknown")
    severity = event.get("severity", "info")
    icon = ALERT_LEVELS.get(severity, "•")
    data = event.get("data", {})
    ts = event.get("ts", "")[:19]

    if etype == "gpu_thermal_warning":
        return f"{icon} *GPU CRITIQUE* GPU{data.get('gpu', '?')} = {data.get('temp', '?')}°C [{ts}]"
    elif etype == "node_up":
        return f"✅ *{data.get('node', '')} UP* — {data.get('models', [])[:2]} [{ts}]"
    elif etype == "node_down":
        return f"❌ *{data.get('node', '')} DOWN* [{ts}]"
    elif etype == "ram_critical":
        return f"{icon} *RAM CRITIQUE* {data.get('free_gb', '?')}GB libre [{ts}]"
    elif etype == "mce_error":
        return f"{icon} *MCE Error* count={data.get('count', '?')} [{ts}]"
    else:
        return f"{icon} *{etype}* {json.dumps(data)[:100]} [{ts}]"


def main():
    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    ps = r.pubsub()
    ps.subscribe("jarvis:events", "jarvis:alerts")

    print(f"[TelegramAlert] Listening — Token: {TOKEN[:20]}...")
    # Test message
    send_telegram(
        "🤖 *JARVIS Alert System* démarré\nCluster: 5 GPUs | M2+OL1+OR actifs | Score: 100/100"
    )

    for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            event = json.loads(msg["data"])
            etype = event.get("type", "")
            severity = event.get("severity", "info")

            # Only alert on warning/critical OR node status changes
            if severity in ("critical", "warning") or etype.startswith("node_"):
                if etype not in IGNORED_TYPES:
                    text = format_event(event)
                    ok = send_telegram(text)
                    print(
                        f"[{datetime.now().strftime('%H:%M')}] {'✅' if ok else '❌'} {etype} → Telegram"
                    )
        except Exception as e:
            print(f"ERR: {e}")


if __name__ == "__main__":
    main()
