#!/usr/bin/env python3
"""monitoring_cloud.py — Circuit breaker status for LM cluster nodes."""
import json, time, datetime, os

CB_FILE = "/tmp/lm-circuit-breaker.json"
SESSION_FILE = "/tmp/lm-session-start.txt"

def load_cb():
    default = {"M1":{"failures":0,"until":0},"M2":{"failures":0,"until":0},"OL1":{"failures":0,"until":0},"CLOUD":{"failures":0,"until":0}}
    if not os.path.exists(CB_FILE):
        return default
    try:
        content = open(CB_FILE).read().strip()
        if not content:
            return default
        return json.loads(content)
    except Exception:
        return default

def get_session_start():
    if os.path.exists(SESSION_FILE):
        try:
            return float(open(SESSION_FILE).read().strip())
        except Exception:
            pass
    return time.time()

def main():
    now = time.time()
    session_start = get_session_start()
    session_duration = max(now - session_start, 1)
    cb = load_cb()
    nodes = sorted(cb.keys())
    print(f"\n{'='*62}")
    print(f"  LM Cluster Circuit Breaker — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*62}")
    print(f"  {'Node':<8} {'Status':<10} {'Failures':<10} {'Open Until':<20} {'Uptime%'}")
    print(f"  {'-'*7} {'-'*9} {'-'*9} {'-'*19} {'-'*7}")
    for node in nodes:
        data = cb.get(node, {"failures":0,"until":0})
        failures = data.get("failures", 0)
        until = float(data.get("until", 0) or 0)
        if until > now:
            status = "OPEN"
            open_until_str = datetime.datetime.fromtimestamp(until).strftime('%H:%M:%S')
            open_duration = min(300.0, until - now + 300)
        else:
            status = "CLOSED"
            open_until_str = "-"
            open_duration = 0.0
        uptime_pct = max(0.0, (session_duration - open_duration) / session_duration * 100)
        print(f"  {node:<8} {status:<10} {failures:<10} {open_until_str:<20} {uptime_pct:.1f}%")
    print(f"{'='*62}")
    print(f"  Session: {session_duration:.0f}s  |  CB: {CB_FILE}\n")

if __name__ == "__main__":
    main()
