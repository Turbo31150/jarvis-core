#!/usr/bin/env python3
"""JARVIS Notification Router — Smart routing of alerts to Telegram/log/Redis"""

import redis
import json
import time
import requests
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:notif"

CHANNELS = {
    "telegram": {"min_severity": "warning", "enabled": True},
    "redis_log": {"min_severity": "info", "enabled": True},
    "console":   {"min_severity": "debug", "enabled": True},
}

SEVERITY_RANK = {"debug": 0, "info": 1, "warning": 2, "critical": 3}


def _load_telegram_cfg():
    try:
        for f in ["/home/turbo/IA/Core/jarvis/config/secrets.env"]:
            env = {}
            with open(f) as fp:
                for line in fp:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        env[k] = v
            return env.get("TELEGRAM_TOKEN", ""), env.get("TELEGRAM_CHAT", "")
    except Exception:
        return "", ""


def _send_telegram(msg: str) -> bool:
    token, chat = _load_telegram_cfg()
    if not token or not chat:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg},
            timeout=8,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send(
    message: str,
    severity: str = "info",
    source: str = "jarvis",
    event_type: str = "notification",
    dedup_key: str = None,
) -> dict:
    # Deduplication — skip if same key sent within 5 min
    if dedup_key:
        dedup_redis_key = f"{PREFIX}:dedup:{dedup_key}"
        if r.get(dedup_redis_key):
            return {"status": "deduped", "key": dedup_key}
        r.setex(dedup_redis_key, 300, "1")

    payload = {
        "ts": datetime.now().isoformat()[:19],
        "type": event_type,
        "severity": severity,
        "source": source,
        "message": message,
    }

    sent_to = []
    rank = SEVERITY_RANK.get(severity, 1)

    # Redis log — always
    if CHANNELS["redis_log"]["enabled"]:
        r.lpush("jarvis:event_log", json.dumps(payload))
        r.ltrim("jarvis:event_log", 0, 999)
        sent_to.append("redis_log")

    # Telegram — warning+
    if CHANNELS["telegram"]["enabled"] and rank >= SEVERITY_RANK["warning"]:
        icon = "🚨" if severity == "critical" else "⚠️"
        ok = _send_telegram(f"{icon} [{source}] {message}")
        if ok:
            sent_to.append("telegram")

    # Track stats
    r.hincrby(f"{PREFIX}:stats:{severity}", "count", 1)
    r.hset(f"{PREFIX}:stats:{severity}", "last_ts", payload["ts"])

    return {"status": "sent", "channels": sent_to, "severity": severity}


def stats() -> dict:
    result = {}
    for sev in SEVERITY_RANK:
        data = r.hgetall(f"{PREFIX}:stats:{sev}")
        if data:
            result[sev] = {"count": int(data.get("count", 0)), "last": data.get("last_ts", "")}
    return result


if __name__ == "__main__":
    # Test
    r1 = send("Test info notification", "info", "test")
    print(f"info: {r1}")
    r2 = send("Test dedup", "info", "test", dedup_key="test-dedup-123")
    r3 = send("Test dedup again", "info", "test", dedup_key="test-dedup-123")
    print(f"dedup1: sent={r2['status']}, dedup2: {r3['status']}")
    print(f"Stats: {stats()}")
