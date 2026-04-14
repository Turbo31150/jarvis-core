#!/usr/bin/env python3
"""JARVIS Telemetry — Collect, aggregate and export system telemetry to Redis time series"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

TELEMETRY_PREFIX = "jarvis:telemetry:"
LATEST_KEY = "jarvis:telemetry:latest"
STATS_KEY = "jarvis:telemetry:stats"


def _read_cpu_pct() -> float:
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        vals = list(map(int, line.split()[1:]))
        idle = vals[3]
        total = sum(vals)
        key = "jarvis:telemetry:_prev_cpu"
        prev_raw = r.get(key)
        r.setex(key, 60, json.dumps({"total": total, "idle": idle}))
        if prev_raw:
            prev = json.loads(prev_raw)
            dtotal = total - prev["total"]
            didle = idle - prev["idle"]
            return round(100 * (1 - didle / max(dtotal, 1)), 1)
        return 0.0
    except Exception:
        return 0.0


def _read_ram() -> dict:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem = {}
        for line in lines[:5]:
            k, v = line.split(":")
            mem[k.strip()] = int(v.split()[0])
        total_mb = mem.get("MemTotal", 0) // 1024
        avail_mb = mem.get("MemAvailable", 0) // 1024
        used_mb = total_mb - avail_mb
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "free_mb": avail_mb,
            "used_pct": round(used_mb / max(total_mb, 1) * 100, 1),
        }
    except Exception:
        return {}


def _read_gpu() -> list:
    gpus = []
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if temp or vram:
            gpus.append(
                {"id": i, "temp_c": float(temp or 0), "vram_pct": float(vram or 0)}
            )
    return gpus


def _read_redis_info() -> dict:
    try:
        info = r.info("memory")
        return {
            "used_mb": round(info.get("used_memory", 0) / 1024 / 1024, 1),
            "peak_mb": round(info.get("used_memory_peak", 0) / 1024 / 1024, 1),
        }
    except Exception:
        return {}


def collect() -> dict:
    """Collect a full telemetry snapshot."""
    ts = time.time()
    snapshot = {
        "ts": ts,
        "cpu_pct": _read_cpu_pct(),
        "ram": _read_ram(),
        "gpus": _read_gpu(),
        "redis": _read_redis_info(),
        "llm_errors": {
            t: int(r.get(f"jarvis:llm_router:{t}:errors") or 0)
            for t in ["default", "code", "fast"]
        },
        "circuit_breakers": {
            key.replace("jarvis:cb:", ""): r.hget(key, "state") or "closed"
            for key in r.scan_iter("jarvis:cb:*")
        },
    }

    # Store latest
    r.setex(LATEST_KEY, 300, json.dumps(snapshot))

    # Time series: record key metrics
    try:
        from jarvis_time_series import record

        record("cpu_pct", snapshot["cpu_pct"])
        if snapshot["ram"]:
            record("ram_used_pct", snapshot["ram"].get("used_pct", 0))
        for gpu in snapshot["gpus"]:
            record(f"gpu{gpu['id']}_temp", gpu["temp_c"])
        record("redis_mb", snapshot["redis"].get("used_mb", 0))
    except Exception:
        pass

    r.hincrby(STATS_KEY, "collections", 1)
    return snapshot


def get_latest() -> dict:
    raw = r.get(LATEST_KEY)
    return json.loads(raw) if raw else {}


def export_prometheus() -> str:
    """Export telemetry in Prometheus text format."""
    snap = get_latest() or collect()
    lines = []

    def gauge(name, value, labels=""):
        lines.append(f"# TYPE jarvis_{name} gauge")
        lines.append(f"jarvis_{name}{{{labels}}} {value}")

    gauge("cpu_pct", snap.get("cpu_pct", 0))
    if snap.get("ram"):
        gauge("ram_used_pct", snap["ram"].get("used_pct", 0))
        gauge("ram_used_mb", snap["ram"].get("used_mb", 0))
    for gpu in snap.get("gpus", []):
        gauge("gpu_temp_c", gpu["temp_c"], f'gpu="{gpu["id"]}"')
        gauge("gpu_vram_pct", gpu["vram_pct"], f'gpu="{gpu["id"]}"')
    if snap.get("redis"):
        gauge("redis_mem_mb", snap["redis"].get("used_mb", 0))
    for name, state in snap.get("circuit_breakers", {}).items():
        val = 0 if state == "open" else (0.5 if state == "half_open" else 1)
        gauge("circuit_breaker", val, f'service="{name}"')

    return "\n".join(lines)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    snap = collect()
    print(f"Telemetry collected at {time.strftime('%H:%M:%S')}")
    print(f"  CPU: {snap['cpu_pct']}%")
    ram = snap.get("ram", {})
    print(
        f"  RAM: {ram.get('used_mb', '?')}/{ram.get('total_mb', '?')} MB ({ram.get('used_pct', '?')}%)"
    )
    for gpu in snap.get("gpus", []):
        print(f"  GPU{gpu['id']}: {gpu['temp_c']}°C  VRAM {gpu['vram_pct']}%")
    print(f"  Redis: {snap.get('redis', {}).get('used_mb', '?')} MB")
    print(f"  CBs: {snap.get('circuit_breakers', {})}")
    print("\nPrometheus (first 5 lines):")
    for line in export_prometheus().splitlines()[:5]:
        print(f"  {line}")
    print(f"\nStats: {stats()}")
