#!/usr/bin/env python3
"""JARVIS Log Analyzer — Parse systemd logs for JARVIS services, detect error patterns"""
import subprocess, redis, json, re
from datetime import datetime, timedelta
from collections import Counter

r = redis.Redis(decode_responses=True)

ERROR_PATTERNS = [
    (r"error|Error|ERROR",       "error"),
    (r"failed|Failed|FAILED",    "failure"),
    (r"timeout|Timeout|TIMEOUT", "timeout"),
    (r"connection refused",      "conn_refused"),
    (r"OOM|out of memory",       "oom"),
    (r"GPU|nvidia",              "gpu"),
]

def parse_service_logs(service: str, since: str = "1h ago") -> dict:
    try:
        out = subprocess.run(
            ["journalctl", "-u", f"jarvis-{service}.service", f"--since={since}",
             "--no-pager", "-o", "short-monotonic"],
            capture_output=True, text=True, timeout=10
        )
        lines = out.stdout.split("\n")
    except:
        return {"service": service, "lines": 0, "patterns": {}}
    
    pattern_counts = Counter()
    for line in lines:
        for pattern, label in ERROR_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                pattern_counts[label] += 1
    
    return {
        "service": service,
        "lines": len(lines),
        "patterns": dict(pattern_counts),
        "errors": pattern_counts.get("error", 0) + pattern_counts.get("failure", 0)
    }

def analyze_all(since: str = "1h ago") -> dict:
    services = ["api", "hw-monitor", "llm-monitor", "score-updater", "telegram-alert",
                "dashboard", "prometheus", "webhook"]
    results = {}
    total_errors = 0
    
    for svc in services:
        r2 = parse_service_logs(svc, since)
        results[svc] = r2
        total_errors += r2.get("errors", 0)
    
    summary = {"ts": datetime.now().isoformat()[:19], "total_errors": total_errors,
               "services": results}
    r.setex("jarvis:log_analysis", 1800, json.dumps(summary))
    return summary

if __name__ == "__main__":
    print("Analyzing last 1h of JARVIS service logs...")
    result = analyze_all("1h ago")
    print(f"Total errors: {result['total_errors']}")
    for svc, data in result["services"].items():
        if data["errors"] > 0 or data["lines"] > 10:
            icon = "⚠️" if data["errors"] > 0 else "✅"
            print(f"  {icon} {svc}: {data['lines']} lines, errors={data['errors']}, patterns={data['patterns']}")
