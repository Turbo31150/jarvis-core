#!/usr/bin/env python3
"""JARVIS SLA Tracker — Track uptime and response time SLAs per service"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

SLAS = {
    "api_gateway":  {"uptime_target": 99.0, "latency_p99_ms": 500},
    "m2_llm":       {"uptime_target": 95.0, "latency_p99_ms": 10000},
    "ol1_llm":      {"uptime_target": 99.0, "latency_p99_ms": 3000},
    "redis":        {"uptime_target": 99.9, "latency_p99_ms": 10},
    "dashboard":    {"uptime_target": 98.0, "latency_p99_ms": 200},
}

def record_check(service: str, ok: bool, latency_ms: float = 0):
    ts = datetime.now().isoformat()[:19]
    r.lpush(f"jarvis:sla:{service}:checks",
            json.dumps({"ok": ok, "lat": latency_ms, "ts": ts}))
    r.ltrim(f"jarvis:sla:{service}:checks", 0, 999)  # keep last 1000

def compute_sla(service: str, last_n: int = 100) -> dict:
    raw = r.lrange(f"jarvis:sla:{service}:checks", 0, last_n-1)
    if not raw:
        return {"uptime": None, "p99_ms": None, "samples": 0}
    checks = [json.loads(c) for c in raw]
    uptime = sum(1 for c in checks if c["ok"]) / len(checks) * 100
    lats = sorted(c["lat"] for c in checks if c["lat"] > 0)
    p99 = lats[int(len(lats)*0.99)] if len(lats) > 10 else (lats[-1] if lats else 0)
    target = SLAS.get(service, {})
    return {
        "service": service,
        "uptime_pct": round(uptime, 2),
        "uptime_target": target.get("uptime_target"),
        "p99_ms": round(p99),
        "p99_target": target.get("latency_p99_ms"),
        "samples": len(checks),
        "sla_ok": uptime >= target.get("uptime_target", 95)
    }

def full_report() -> dict:
    return {svc: compute_sla(svc) for svc in SLAS}

if __name__ == "__main__":
    # Simulate some checks
    import requests
    for svc, endpoint in [("api_gateway", "http://localhost:8767/health"),
                           ("dashboard", "http://localhost:8765/")]:
        t0 = time.perf_counter()
        try:
            ok = requests.get(endpoint, timeout=2).ok
            lat = round((time.perf_counter()-t0)*1000)
        except:
            ok, lat = False, 9999
        for _ in range(5):  # add 5 samples
            record_check(svc, ok, lat)
        print(f"  {svc}: {'OK' if ok else 'FAIL'} {lat}ms")
    
    report = full_report()
    for svc, data in report.items():
        if data["samples"] > 0:
            status = "✅" if data.get("sla_ok") else "⚠️"
            print(f"  {status} {svc}: {data['uptime_pct']}% uptime, p99={data['p99_ms']}ms")
