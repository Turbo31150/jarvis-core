#!/usr/bin/env python3
"""JARVIS Cost Reporter — Daily cost report: tokens, API calls, cloud vs local"""
import redis, json, requests
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)

def daily_report(date: str = None) -> dict:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    report = {"date": date, "by_backend": {}, "total_cost_usd": 0.0,
              "local_requests": 0, "cloud_requests": 0}
    
    for key in r.scan_iter(f"jarvis:tokens:{date}:*"):
        backend = key.split(":")[-1]
        data = r.hgetall(key)
        cost = float(data.get("cost_usd", 0))
        tokens_in = int(float(data.get("tokens_in", 0)))
        tokens_out = int(float(data.get("tokens_out", 0)))
        report["by_backend"][backend] = {
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost
        }
        report["total_cost_usd"] += cost
        if backend == "local":
            report["local_requests"] += tokens_in + tokens_out
        else:
            report["cloud_requests"] += tokens_in + tokens_out
    
    # LLM router call counts
    for key in r.scan_iter("jarvis:llm_router:*:count"):
        task_type = key.split(":")[2]
        count = int(r.get(key) or 0)
        report.setdefault("router_calls", {})[task_type] = count
    
    report["total_cost_usd"] = round(report["total_cost_usd"], 4)
    return report

def send_daily_telegram():
    report = daily_report()
    lines = [f"💰 JARVIS Cost Report {report['date']}",
             f"Total cost: ${report['total_cost_usd']:.4f}"]
    for backend, data in report["by_backend"].items():
        lines.append(f"  {backend}: {data['tokens_in']+data['tokens_out']} tokens ${data['cost_usd']:.4f}")
    if report.get("router_calls"):
        lines.append(f"Router calls: {sum(report['router_calls'].values())}")
    
    try:
        from jarvis_telegram_alert import send_telegram
        return send_telegram("\n".join(lines))
    except:
        return False

if __name__ == "__main__":
    report = daily_report()
    print(f"Date: {report['date']} | Total: ${report['total_cost_usd']:.4f}")
    for backend, data in report["by_backend"].items():
        print(f"  {backend}: {data['tokens_in']+data['tokens_out']} tokens")
    if report.get("router_calls"):
        print(f"Router calls: {report['router_calls']}")
