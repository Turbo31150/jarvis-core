#!/usr/bin/env python3
"""JARVIS Hardware Event Monitor — MCE + GPU thermal + RAM → Redis events + actions"""

import subprocess
import time
import redis
import json
import os
from datetime import datetime

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def publish_event(etype, data, severity="info"):
    payload = json.dumps(
        {
            "type": etype,
            "severity": severity,
            "data": data,
            "ts": datetime.now().isoformat()[:19],
        }
    )
    r.publish("jarvis:events", payload)
    r.lpush("jarvis:event_log", payload)
    r.ltrim("jarvis:event_log", 0, 999)


def check_mce():
    try:
        out = subprocess.check_output(
            ["mcelog", "--client"], text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        count = len([l for l in out.split("\n") if "Hardware Error" in l or "MCE" in l])
        if count > 0:
            publish_event("mce_error", {"count": count, "msg": out[:200]}, "warning")
            return count
    except:
        pass
    return 0


def check_gpu_thermal():
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,temperature.gpu,power.draw,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        alerts = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = list(map(str.strip, line.split(",")))
            idx, temp, power = int(parts[0]), int(parts[1]), float(parts[2])
            mem_used, mem_total = int(parts[3]), int(parts[4])
            r.setex(f"jarvis:gpu:{idx}:temp", 60, temp)
            r.setex(f"jarvis:gpu:{idx}:power", 60, power)
            r.setex(
                f"jarvis:gpu:{idx}:vram_pct", 60, round(mem_used / mem_total * 100, 1)
            )
            if temp > 82:
                publish_event(
                    "gpu_thermal_warning", {"gpu": idx, "temp": temp}, "critical"
                )
                alerts.append(f"GPU{idx}={temp}°C CRITICAL")
            elif temp > 75:
                publish_event("gpu_thermal_high", {"gpu": idx, "temp": temp}, "warning")
        return alerts
    except:
        return []


def check_ram():
    try:
        with open("/proc/meminfo") as f:
            m = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        avail_gb = m.get("MemAvailable", 0) / 1024 / 1024
        r.setex("jarvis:ram:free_gb", 60, round(avail_gb, 1))
        if avail_gb < 2:
            publish_event("ram_critical", {"free_gb": round(avail_gb, 1)}, "critical")
        elif avail_gb < 4:
            publish_event("ram_low", {"free_gb": round(avail_gb, 1)}, "warning")
        return avail_gb
    except:
        return 0


def check_anomalies():
    """Détection d'anomalies: tendance GPU temp + RAM dégradation"""
    # GPU: comparer temp actuelle vs moyenne 5 dernières
    for idx in range(5):
        temps_raw = r.lrange(f"jarvis:gpu:{idx}:temp_history", 0, 4)
        cur = r.get(f"jarvis:gpu:{idx}:temp")
        if cur and len(temps_raw) >= 3:
            avg = sum(int(t) for t in temps_raw) / len(temps_raw)
            if int(cur) > avg + 8:  # +8°C vs moyenne = anomalie
                publish_event(
                    "gpu_temp_anomaly",
                    {"gpu": idx, "temp": int(cur), "avg": round(avg, 1)},
                    "warning",
                )
        # Historique rolling
        if cur:
            r.lpush(f"jarvis:gpu:{idx}:temp_history", cur)
            r.ltrim(f"jarvis:gpu:{idx}:temp_history", 0, 9)
    # RAM: dérive sur 10 itérations
    ram_history = r.lrange("jarvis:ram:free_history", 0, 9)
    cur_ram = r.get("jarvis:ram:free_gb")
    if cur_ram and len(ram_history) >= 5:
        avg_ram = sum(float(x) for x in ram_history) / len(ram_history)
        if float(cur_ram) < avg_ram * 0.6:  # -40% vs moyenne
            publish_event(
                "ram_anomaly",
                {"free_gb": float(cur_ram), "avg_gb": round(avg_ram, 1)},
                "warning",
            )
    if cur_ram:
        r.lpush("jarvis:ram:free_history", cur_ram)
        r.ltrim("jarvis:ram:free_history", 0, 9)


if __name__ == "__main__":
    print(f"[HW Monitor] Starting — PID {os.getpid()}")
    iteration = 0
    while True:
        try:
            gpu_alerts = check_gpu_thermal()
            ram_free = check_ram()
            check_anomalies()
            if iteration % 10 == 0:  # Every 5min
                mce = check_mce()
            if gpu_alerts:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] GPU ALERTS: {gpu_alerts}"
                )
            iteration += 1
            time.sleep(30)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"ERR: {e}")
            time.sleep(10)
